package org.xlerobot.arvision;

import android.app.Activity;
import android.os.Bundle;
import android.content.Intent;
import android.net.LocalServerSocket;
import android.net.LocalSocket;
import android.opengl.GLES20;
import android.opengl.GLES11Ext;
import android.opengl.GLSurfaceView;
import android.media.Image;
import android.graphics.ImageFormat;
import android.graphics.YuvImage;
import android.graphics.Rect;
import android.view.Gravity;
import android.view.WindowManager;
import android.widget.FrameLayout;
import android.widget.TextView;
import android.util.Log;
import com.google.ar.core.*;
import com.google.ar.core.exceptions.NotYetAvailableException;
import com.google.ar.core.exceptions.NotTrackingException;
import org.json.*;
import java.io.*;
import java.nio.*;
import javax.microedition.khronos.egl.EGLConfig;
import javax.microedition.khronos.opengles.GL10;

/** Local RGB-D and pose probe. No robot commands, cloud upload or account use. */
public final class MainActivity extends Activity implements GLSurfaceView.Renderer {
    private GLSurfaceView surface;
    private TextView label;
    private volatile Session session;
    private volatile boolean running;
    private File output;
    private int texture,program,width=1,height=1;
    private long frames=0,lastWrite=0,depthFrames=0;
    private boolean automaticDepth=false,rawDepth=false;
    private String captureSession=Long.toString(System.currentTimeMillis());
    private final Object packetLock=new Object();
    private volatile byte[] latestPacket;
    private volatile LocalServerSocket packetServer;
    private volatile LocalSocket packetClient;
    private String packetName;
    private byte[] snapshotRgb=new byte[0],snapshotDepth=new byte[0],snapshotRawDepth=new byte[0],snapshotConfidence=new byte[0];
    private FloatBuffer vertices=buffer(new float[]{-1,-1,1,-1,-1,1,1,1});
    private FloatBuffer texInput=buffer(new float[]{0,1,1,1,0,0,1,0});
    private FloatBuffer texOutput=buffer(new float[8]);

    private static FloatBuffer buffer(float[] values) {
        FloatBuffer b=ByteBuffer.allocateDirect(values.length*4).order(ByteOrder.nativeOrder()).asFloatBuffer();
        b.put(values).rewind();return b;
    }
    @Override public void onCreate(Bundle state) {
        super.onCreate(state);
        getWindow().addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
        output=getExternalFilesDir(null);output.mkdirs();
        surface=new GLSurfaceView(this);surface.setEGLContextClientVersion(2);
        surface.setPreserveEGLContextOnPause(true);surface.setRenderer(this);
        label=new TextView(this);label.setTextColor(0xffffffff);label.setBackgroundColor(0xb0000000);
        label.setPadding(18,18,18,18);label.setText("AR视觉初始化；只采集，不控制机器人");
        FrameLayout layout=new FrameLayout(this);layout.addView(surface);
        FrameLayout.LayoutParams params=new FrameLayout.LayoutParams(-1,-2,Gravity.TOP);layout.addView(label,params);
        setContentView(layout);
    }
    @Override public void onResume() {
        super.onResume();
        if(checkSelfPermission(android.Manifest.permission.CAMERA)!=android.content.pm.PackageManager.PERMISSION_GRANTED){requestPermissions(new String[]{android.Manifest.permission.CAMERA},1);return;}
        resumeSession();
    }
    @Override public void onRequestPermissionsResult(int request,String[] permissions,int[] grants){
        super.onRequestPermissionsResult(request,permissions,grants);
        if(request==1&&grants.length>0&&grants[0]==android.content.pm.PackageManager.PERMISSION_GRANTED)resumeSession();
    }
    private void resumeSession() {
        if(session!=null)return;
        try {
            JSONObject status=new JSONObject();status.put("capture_session",captureSession);status.put("availability",ArCoreApk.getInstance().checkAvailability(this).toString());
            Session s=new Session(this);
            automaticDepth=s.isDepthModeSupported(Config.DepthMode.AUTOMATIC);
            rawDepth=s.isDepthModeSupported(Config.DepthMode.RAW_DEPTH_ONLY);
            Config config=s.getConfig();
            config.setFocusMode(Config.FocusMode.AUTO);
            config.setUpdateMode(Config.UpdateMode.LATEST_CAMERA_IMAGE);
            if(automaticDepth)config.setDepthMode(Config.DepthMode.AUTOMATIC);
            else if(rawDepth)config.setDepthMode(Config.DepthMode.RAW_DEPTH_ONLY);
            s.configure(config);s.resume();captureSession=Long.toString(System.currentTimeMillis());frames=0;depthFrames=0;session=s;running=true;
            status.put("capture_session",captureSession);
            new File(output,"error.json").delete();
            startPacketServer();
            status.put("automatic_depth_supported",automaticDepth);status.put("raw_depth_supported",rawDepth);
            status.put("camera_id",s.getCameraConfig().getCameraId());status.put("phase","resumed");
            writeJson("startup.json",status);surface.onResume();
        } catch(Throwable e) {failure("startup",e);}
    }
    @Override public void onPause() {
        running=false;stopPacketServer();surface.onPause();
        Session s=session;if(s!=null){try{s.pause();s.close();}catch(Throwable ignored){}session=null;}
        super.onPause();
    }
    @Override protected void onNewIntent(Intent intent){super.onNewIntent(intent);setIntent(intent);try{startPacketServer();}catch(IOException e){failure("stream",e);}}
    private void stopPacketServer(){
        LocalServerSocket old=packetServer;packetServer=null;packetName=null;
        try{if(old!=null)old.close();}catch(IOException ignored){}
        LocalSocket client=packetClient;packetClient=null;
        try{if(client!=null)client.close();}catch(IOException ignored){}
        synchronized(packetLock){packetLock.notifyAll();}
    }
    private void startPacketServer()throws IOException {
        String receiver=getIntent().getStringExtra("receiver_session");
        if(receiver==null||!receiver.matches("[0-9a-f]{8}"))return;
        String name="xlerobot_ar_"+receiver;
        if(name.equals(packetName)&&packetServer!=null)return;
        stopPacketServer();latestPacket=null;
        final LocalServerSocket server=new LocalServerSocket(name);packetServer=server;packetName=name;
        Thread thread=new Thread(()->{
            while(packetServer==server){
                try(LocalSocket client=server.accept()){
                    packetClient=client;client.setSendBufferSize(256*1024);OutputStream wire=client.getOutputStream();byte[] previous=null;
                    while(packetServer==server){
                        byte[] packet;
                        synchronized(packetLock){
                            while(packetServer==server&&(latestPacket==null||latestPacket==previous))packetLock.wait(1000);
                            if(packetServer!=server)break;packet=latestPacket;
                        }
                        wire.write(packet);wire.flush();previous=packet;
                    }
                }catch(IOException|InterruptedException e){if(packetServer==server)Log.w("XLeRobotAR","stream reconnect",e);}
                finally{packetClient=null;}
            }
        },"ar-packet-stream");thread.setDaemon(true);thread.start();
    }
    private int shader(int type,String source) {
        int s=GLES20.glCreateShader(type);GLES20.glShaderSource(s,source);GLES20.glCompileShader(s);return s;
    }
    @Override public void onSurfaceCreated(GL10 gl,EGLConfig config) {
        int[] textures=new int[1];GLES20.glGenTextures(1,textures,0);texture=textures[0];
        GLES20.glBindTexture(GLES11Ext.GL_TEXTURE_EXTERNAL_OES,texture);
        GLES20.glTexParameteri(GLES11Ext.GL_TEXTURE_EXTERNAL_OES,GLES20.GL_TEXTURE_MIN_FILTER,GLES20.GL_LINEAR);
        GLES20.glTexParameteri(GLES11Ext.GL_TEXTURE_EXTERNAL_OES,GLES20.GL_TEXTURE_MAG_FILTER,GLES20.GL_LINEAR);
        program=GLES20.glCreateProgram();
        GLES20.glAttachShader(program,shader(GLES20.GL_VERTEX_SHADER,"attribute vec2 p;attribute vec2 uv;varying vec2 t;void main(){t=uv;gl_Position=vec4(p,0.,1.);}"));
        GLES20.glAttachShader(program,shader(GLES20.GL_FRAGMENT_SHADER,"#extension GL_OES_EGL_image_external : require\nprecision mediump float;varying vec2 t;uniform samplerExternalOES image;void main(){gl_FragColor=texture2D(image,t);}"));
        GLES20.glLinkProgram(program);
    }
    @Override public void onSurfaceChanged(GL10 gl,int w,int h){width=w;height=h;GLES20.glViewport(0,0,w,h);}
    @Override public void onDrawFrame(GL10 gl) {
        Session s=session;if(!running||s==null)return;
        try {
            s.setCameraTextureName(texture);s.setDisplayGeometry(getWindowManager().getDefaultDisplay().getRotation(),width,height);
            Frame frame=s.update();frames++;
            frame.transformCoordinates2d(Coordinates2d.OPENGL_NORMALIZED_DEVICE_COORDINATES,vertices,Coordinates2d.TEXTURE_NORMALIZED,texOutput);
            GLES20.glClear(GLES20.GL_COLOR_BUFFER_BIT);GLES20.glUseProgram(program);
            int p=GLES20.glGetAttribLocation(program,"p"),uv=GLES20.glGetAttribLocation(program,"uv");
            vertices.rewind();texOutput.rewind();GLES20.glEnableVertexAttribArray(p);GLES20.glEnableVertexAttribArray(uv);
            GLES20.glVertexAttribPointer(p,2,GLES20.GL_FLOAT,false,0,vertices);GLES20.glVertexAttribPointer(uv,2,GLES20.GL_FLOAT,false,0,texOutput);
            GLES20.glActiveTexture(GLES20.GL_TEXTURE0);GLES20.glBindTexture(GLES11Ext.GL_TEXTURE_EXTERNAL_OES,texture);
            GLES20.glUniform1i(GLES20.glGetUniformLocation(program,"image"),0);GLES20.glDrawArrays(GLES20.GL_TRIANGLE_STRIP,0,4);
            long now=android.os.SystemClock.elapsedRealtime();
            if(now-lastWrite<500)return;lastWrite=now;
            snapshotRgb=new byte[0];snapshotDepth=new byte[0];snapshotRawDepth=new byte[0];snapshotConfidence=new byte[0];
            JSONObject result=new JSONObject();Camera camera=frame.getCamera();
            result.put("schema",1);result.put("capture_session",captureSession);result.put("frames",frames);result.put("frame_timestamp_ns",frame.getTimestamp());
            result.put("host_device_monotonic_ms",now);result.put("tracking",camera.getTrackingState().toString());
            result.put("tracking_failure",camera.getTrackingFailureReason().toString());
            result.put("automatic_depth_supported",automaticDepth);result.put("raw_depth_supported",rawDepth);
            result.put("pose_translation",array(camera.getPose().getTranslation()));result.put("pose_quaternion",array(camera.getPose().getRotationQuaternion()));
            CameraIntrinsics intrinsics=camera.getImageIntrinsics();
            result.put("focal_length",array(intrinsics.getFocalLength()));result.put("principal_point",array(intrinsics.getPrincipalPoint()));
            result.put("image_dimensions",new JSONArray(intrinsics.getImageDimensions()));
            // Depth pixels share TEXTURE_NORMALIZED coordinates, not the full CPU image.
            // Export the SDK transform rather than assuming equal image aspect ratios.
            float[] corners=new float[8];
            frame.transformCoordinates2d(Coordinates2d.TEXTURE_NORMALIZED,new float[]{0,0,1,0,0,1,1,1},Coordinates2d.IMAGE_PIXELS,corners);
            result.put("depth_uv_to_image_pixels",array(corners));
            result.put("depth_registration","arcore_texture_normalized_to_cpu_image");
            boolean depth=false;
            if(camera.getTrackingState()==TrackingState.TRACKING){
            if(automaticDepth){try(Image image=frame.acquireDepthImage16Bits()){saveDepth(image,"depth.u16",result,"depth");depth=true;}catch(NotYetAvailableException|NotTrackingException e){result.put("depth_waiting",true);}}
            if(rawDepth){try(Image image=frame.acquireRawDepthImage16Bits()){saveDepth(image,"raw-depth.u16",result,"raw_depth");depth=true;}catch(NotYetAvailableException e){result.put("raw_depth_waiting",true);}
                try(Image confidence=frame.acquireRawDepthConfidenceImage()){snapshotConfidence=saveImage(confidence,"confidence.u8",1);result.put("confidence_available",true);result.put("confidence_timestamp_ns",confidence.getTimestamp());result.put("confidence_width",confidence.getWidth());result.put("confidence_height",confidence.getHeight());}catch(NotYetAvailableException e){result.put("confidence_available",false);}}
            }
            try(Image image=frame.acquireCameraImage()){
                saveRgb(image);result.put("rgb_timestamp_ns",image.getTimestamp());result.put("rgb_width",image.getWidth());result.put("rgb_height",image.getHeight());
            }catch(NotYetAvailableException e){result.put("rgb_waiting",true);}
            if(depth)depthFrames++;result.put("depth_frames",depthFrames);result.put("robot_commands",0);
            writePacked(result);
            writeJson("latest.json",result);
            try(FileOutputStream f=new FileOutputStream(new File(output,"events.jsonl"),true)){f.write((result.toString()+"\n").getBytes("UTF-8"));}
            final String text="AR视觉 · "+camera.getTrackingState()+"\n深度支持 "+automaticDepth+" / 原始深度 "+rawDepth+"\n帧 "+frames+" / 深度组 "+depthFrames+"\n"+camera.getTrackingFailureReason();
            runOnUiThread(()->label.setText(text));
        }catch(Throwable e){failure("frame",e);running=false;}
    }
    private JSONArray array(float[] values)throws JSONException{JSONArray a=new JSONArray();for(float v:values)a.put((double)v);return a;}
    private void saveDepth(Image image,String filename,JSONObject result,String prefix)throws Exception {
        byte[] values=saveImage(image,filename,2);int valid=0;long sum=0;int min=65536,max=0;
        if(prefix.equals("depth"))snapshotDepth=values;else snapshotRawDepth=values;
        for(int i=0;i<values.length;i+=2){int mm=(values[i]&255)|((values[i+1]&255)<<8);if(mm>0){valid++;sum+=mm;min=Math.min(min,mm);max=Math.max(max,mm);}}
        result.put(prefix+"_width",image.getWidth());result.put(prefix+"_height",image.getHeight());result.put(prefix+"_timestamp_ns",image.getTimestamp());
        result.put(prefix+"_positive_fraction",(double)valid/(image.getWidth()*image.getHeight()));
        if(valid>0){result.put(prefix+"_min_mm",min);result.put(prefix+"_max_mm",max);result.put(prefix+"_mean_mm",(double)sum/valid);}
    }
    private byte[] saveImage(Image image,String filename,int bytesPerPixel)throws Exception {
        Image.Plane plane=image.getPlanes()[0];ByteBuffer b=plane.getBuffer();int w=image.getWidth(),h=image.getHeight();byte[] out=new byte[w*h*bytesPerPixel];
        int index=0;for(int y=0;y<h;y++)for(int x=0;x<w;x++)for(int c=0;c<bytesPerPixel;c++)out[index++]=b.get(y*plane.getRowStride()+x*plane.getPixelStride()+c);
        writeBytes(filename,out);return out;
    }
    private void saveRgb(Image image)throws Exception {
        int w=image.getWidth(),h=image.getHeight();byte[] out=new byte[w*h*3/2];Image.Plane[] planes=image.getPlanes();int n=0;
        for(int y=0;y<h;y++)for(int x=0;x<w;x++)out[n++]=planes[0].getBuffer().get(y*planes[0].getRowStride()+x*planes[0].getPixelStride());
        for(int y=0;y<h/2;y++)for(int x=0;x<w/2;x++){
            out[n++]=planes[2].getBuffer().get(y*planes[2].getRowStride()+x*planes[2].getPixelStride());
            out[n++]=planes[1].getBuffer().get(y*planes[1].getRowStride()+x*planes[1].getPixelStride());
        }
        ByteArrayOutputStream bytes=new ByteArrayOutputStream();new YuvImage(out,ImageFormat.NV21,w,h,null).compressToJpeg(new Rect(0,0,w,h),78,bytes);snapshotRgb=bytes.toByteArray();writeBytes("rgb.jpg",snapshotRgb);
    }
    private void writePacked(JSONObject result)throws Exception {
        byte[][] parts={result.toString().getBytes("UTF-8"),snapshotRgb,snapshotDepth,snapshotRawDepth,snapshotConfidence};
        ByteArrayOutputStream bytes=new ByteArrayOutputStream();DataOutputStream stream=new DataOutputStream(bytes);
        stream.write(new byte[]{'X','L','A','R','1',0,0,0});for(byte[] part:parts)stream.writeInt(part.length);for(byte[] part:parts)stream.write(part);stream.flush();
        byte[] packet=bytes.toByteArray();writeBytes("snapshot.bin",packet);
        synchronized(packetLock){latestPacket=packet;packetLock.notifyAll();}
    }
    private void writeJson(String name,JSONObject value)throws Exception{writeBytes(name,value.toString().getBytes("UTF-8"));}
    private void writeBytes(String name,byte[] bytes)throws Exception {
        File pending=new File(output,name+".pending");try(FileOutputStream f=new FileOutputStream(pending)){f.write(bytes);}if(!pending.renameTo(new File(output,name)))throw new IOException("Atomic replace failed");
    }
    private void failure(String phase,Throwable e){
        Log.e("XLeRobotAR",phase,e);final String text=phase+": "+e.getClass().getSimpleName()+" "+e.getMessage();
        runOnUiThread(()->label.setText(text));try{JSONObject out=new JSONObject();out.put("capture_session",captureSession);out.put("phase",phase);out.put("error",text);out.put("robot_commands",0);writeJson("error.json",out);}catch(Throwable ignored){}
    }
}
