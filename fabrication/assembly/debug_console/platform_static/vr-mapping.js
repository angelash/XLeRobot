'use strict';
// Pure mapping for the PICO WebXR page: controller buttons, body frame and the
// same hand-follow kinematics as vr_kinematics.py (used for the offline preview).
// No DOM or WebXR objects are required, so Node tests can load this file.
(function(root){
  const RAD=Math.PI/180, DEG=180/Math.PI;
  const L1=.1159, L2=.1350, O1=Math.atan2(.028,.11257), O2=Math.atan2(.0052,.1349)+O1;
  const PAN_OFFSET=.0306, TOOL=.10, PSI_OFFSET=(O2-O1)*DEG, MIN_REACH=.08, MAX_DELTA=.6;

  // xr-standard: 0 trigger, 1 squeeze, 3 thumbstick press, 4 X/A, 5 Y/B.
  // "xlevr" is the index order used by the upstream XLeVR page.
  const SCHEMES={standard:{trigger:0,squeeze:1,stick:3,primary:4,secondary:5},
                 xlevr:{trigger:0,squeeze:1,stick:2,primary:3,secondary:4}};

  function forward(lift,elbow){
    const a=(90-lift)*RAD-O1, b=(elbow+90)*RAD-O2;
    return [L1*Math.cos(a)+L2*Math.cos(a-b), L1*Math.sin(a)+L2*Math.sin(a-b)];
  }
  function inverse(x,z){
    const r=Math.hypot(x,z);
    if(!(Math.abs(L1-L2)+1e-6<r&&r<L1+L2-1e-6))throw new Error('末端超出二连杆工作空间');
    const b=Math.acos(Math.max(-1,Math.min(1,(r*r-L1*L1-L2*L2)/(2*L1*L2))));
    const a=Math.atan2(z,x)+Math.atan2(L2*Math.sin(b),L1+L2*Math.cos(b));
    if(!(-.1<=a+O1&&a+O1<=3.45)||!(-.2<=b+O2&&b+O2<=Math.PI))throw new Error('末端逆解超过上游模型关节范围');
    return [90-(a+O1)*DEG,(b+O2)*DEG-90];
  }
  function wrap(deg){return ((deg+180)%360+360)%360-180;}
  function anchor(lift,elbow,wrist){
    const [x,z]=forward(lift,elbow), [l2,e2]=inverse(x,z);
    if(Math.abs(l2-lift)>1||Math.abs(e2-elbow)>1)throw new Error('当前折叠姿态不在跟随模型内');
    const sum=lift+elbow+wrist, psi=PSI_OFFSET-sum;
    const tip=[x+TOOL*Math.cos(psi*RAD), z+TOOL*Math.sin(psi*RAD)];
    return {x,z,sum,psi,tip,reach:PAN_OFFSET+tip[0]};
  }
  function follow(g,delta,pitch=0){
    if(delta.some(d=>Math.abs(d)>MAX_DELTA))throw new Error('手柄位移超过0.6米');
    const ahead=Math.max(g.reach+delta[0],MIN_REACH), pan=Math.atan2(delta[1],ahead)*DEG;
    const tipX=Math.hypot(ahead,delta[1])-PAN_OFFSET, tipZ=g.tip[1]+delta[2], psi=g.psi+pitch;
    let wx=tipX-TOOL*Math.cos(psi*RAD), wz=tipZ-TOOL*Math.sin(psi*RAD);
    const reach=Math.hypot(wx,wz), low=Math.abs(L1-L2)+1e-3, high=L1+L2-1e-3, edge=!(low<=reach&&reach<=high);
    if(edge){const s=Math.min(high,Math.max(low,reach))/reach;wx*=s;wz*=s;}
    const [lift,elbow]=inverse(wx,wz);
    return {pan,lift,elbow,wrist:PSI_OFFSET-psi-lift-elbow,edge};
  }
  function jawPoint(lift,elbow,wrist){
    const [x,z]=forward(lift,elbow), psi=(PSI_OFFSET-lift-elbow-wrist)*RAD;
    return [x+TOOL*Math.cos(psi), z+TOOL*Math.sin(psi)];
  }

  function deadzone(v,dz=.15){
    v=Math.max(-1,Math.min(1,Number(v)||0));
    return Math.abs(v)<=dz?0:Math.sign(v)*(Math.abs(v)-dz)/(1-dz);
  }
  function readPad(pad,scheme='standard'){
    const map=SCHEMES[scheme]||SCHEMES.standard, b=pad?.buttons||[], a=pad?.axes||[];
    const button=i=>b[i]||{pressed:false,value:0};
    // Some runtimes report the stick on axes 0/1, xr-standard on 2/3: take the active pair.
    const low=[a[0]||0,a[1]||0], high=[a[2]||0,a[3]||0];
    const stick=Math.hypot(...high)>=Math.hypot(...low)?high:low;
    return {trigger:Math.max(0,Math.min(1,Number(button(map.trigger).value)||(button(map.trigger).pressed?1:0))),
      squeeze:!!button(map.squeeze).pressed||Number(button(map.squeeze).value)>.6,
      stickX:stick[0], stickY:stick[1], stickPress:!!button(map.stick).pressed,
      primary:!!button(map.primary).pressed, secondary:!!button(map.secondary).pressed,
      pressed:b.map((x,i)=>x&&(x.pressed||x.value>.5)?i:-1).filter(i=>i>=0)};
  }
  // Left stick: forward/back drives, left/right turns. Turn is positive to the left.
  function drive(x,y,gear=1){return [-deadzone(y)*gear,-deadzone(x)*gear].map(v=>v===0?0:v);}
  // Right stick: right pans the head right; forward tilts it up.
  function head(x,y){return [deadzone(x),-deadzone(y)].map(v=>v===0?0:v);}

  function rotate(q,v){
    const [x,y,z,w]=q, [vx,vy,vz]=v;
    const ix=w*vx+y*vz-z*vy, iy=w*vy+z*vx-x*vz, iz=w*vz+x*vy-y*vx, iw=-x*vx-y*vy-z*vz;
    return [ix*w+iw*-x+iy*-z-iz*-y, iy*w+iw*-y+iz*-x-ix*-z, iz*w+iw*-z+ix*-y-iy*-x];
  }
  function quat(o){return Array.isArray(o)?o:[o.x,o.y,o.z,o.w];}
  // Heading of the -Z axis on the floor plane; 0 means facing -Z.
  function yaw(q){const f=rotate(quat(q),[0,0,-1]);return Math.atan2(-f[0],-f[2]);}
  // World displacement -> body (forward, left, up) for a calibrated heading.
  function toBody(d,heading){
    const f=[-Math.sin(heading),0,-Math.cos(heading)], r=[Math.cos(heading),0,-Math.sin(heading)];
    return [d[0]*f[0]+d[2]*f[2], -(d[0]*r[0]+d[2]*r[2]), d[1]];
  }
  // Pointing elevation and roll about the pointing axis, both in degrees.
  // Roll is positive when the hand turns clockwise as seen by its owner.
  function orientation(q){
    q=quat(q);
    const f=rotate(q,[0,0,-1]), x=rotate(q,[1,0,0]);
    const pitch=Math.asin(Math.max(-1,Math.min(1,f[1])))*DEG;
    const horizontal=Math.hypot(f[0],f[2]);
    if(horizontal<.26)return {pitch,roll:0,rollValid:false};
    const r0=[-f[2]/horizontal,0,f[0]/horizontal];
    const u0=[r0[1]*f[2]-r0[2]*f[1], r0[2]*f[0]-r0[0]*f[2], r0[0]*f[1]-r0[1]*f[0]];
    const dot=(a,b)=>a[0]*b[0]+a[1]*b[1]+a[2]*b[2];
    return {pitch,roll:-Math.atan2(dot(x,u0),dot(x,r0))*DEG,rollValid:true};
  }
  function clamp(v,limit){return Math.max(-limit,Math.min(limit,v));}

  const api={SCHEMES,PSI_OFFSET,TOOL,PAN_OFFSET,forward,inverse,anchor,follow,jawPoint,wrap,
             deadzone,readPad,drive,head,rotate,yaw,toBody,orientation,clamp};
  if(typeof module!=='undefined'&&module.exports)module.exports=api;
  else root.VRMapping=api;
})(typeof self!=='undefined'?self:this);
