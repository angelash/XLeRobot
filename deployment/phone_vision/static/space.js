import * as THREE from '/presence/assets/vendor/three.module.min.js';
import {OrbitControls} from '/presence/assets/vendor/addons/controls/OrbitControls.js';

export class PhoneSpace {
  constructor(host){
    this.host=host;this.scene=new THREE.Scene();this.scene.background=new THREE.Color('#13211b');
    this.camera=new THREE.PerspectiveCamera(48,1,.02,100);this.camera.position.set(2,1.5,3);
    this.renderer=new THREE.WebGLRenderer({antialias:true,powerPreference:'low-power'});this.renderer.setPixelRatio(Math.min(devicePixelRatio,1.5));
    this.renderer.domElement.setAttribute('aria-label','手机成像点云；左键旋转，右键平移，滚轮缩放');host.append(this.renderer.domElement);
    this.controls=new OrbitControls(this.camera,this.renderer.domElement);this.controls.minDistance=.1;this.controls.maxDistance=25;this.controls.addEventListener('change',()=>this.draw());
    this.group=new THREE.Group();this.scene.add(this.group);this.scene.add(new THREE.GridHelper(10,50,0x456653,0x233a2c));this.scene.add(new THREE.AxesHelper(.6));
    this.resize=new ResizeObserver(()=>{const r=host.getBoundingClientRect();if(!r.width||!r.height)return;this.renderer.setSize(r.width,r.height,false);this.camera.aspect=r.width/r.height;this.camera.updateProjectionMatrix();this.draw();});this.resize.observe(host);
    this.renderer.domElement.addEventListener('webglcontextlost',e=>{e.preventDefault();host.dispatchEvent(new CustomEvent('space-error',{detail:'三维画面暂不可用，保存的照片与数据仍可查看'}));});
    this.size=2;
  }
  async set(data,imageURL){
    if(!Array.isArray(data.positions)||data.positions.length>180000||data.positions.length%3)throw Error('空间数据超过展示预算');
    const version=(this.version||0)+1;this.version=version;let colors;
    if(data.colors?.length===data.positions.length)colors=data.colors.map(v=>Math.min(255,Math.max(0,v))/255);
    else if(imageURL&&data.pixels?.length===data.count*2){
      try{const im=new Image();im.src=imageURL;await im.decode();if(version!==this.version)return;
        const cv=document.createElement('canvas');cv.width=data.width;cv.height=data.height;const ctx=cv.getContext('2d',{willReadFrequently:true});ctx.drawImage(im,0,0,cv.width,cv.height);const rgba=ctx.getImageData(0,0,cv.width,cv.height).data;colors=[];
        for(let n=0;n<data.pixels.length;n+=2){const k=(data.pixels[n+1]*cv.width+data.pixels[n])*4;colors.push(rgba[k]/255,rgba[k+1]/255,rgba[k+2]/255);}
      }catch{colors=null;}
    }
    if(version!==this.version)return;
    if(!colors){colors=[];for(let n=0;n<data.positions.length;n+=3){const color=new THREE.Color().setHSL(.52+Math.min(.35,Math.abs(data.positions[n+2])*.08),.55,.63);colors.push(color.r,color.g,color.b);}}
    this.clear();const geo=new THREE.BufferGeometry();geo.setAttribute('position',new THREE.Float32BufferAttribute(data.positions,3));geo.setAttribute('color',new THREE.Float32BufferAttribute(colors,3));
    const material=new THREE.PointsMaterial({size:this.size,sizeAttenuation:false,vertexColors:true});this.points=new THREE.Points(geo,material);this.group.add(this.points);
    if(data.trajectory?.length){const line=new THREE.BufferGeometry().setFromPoints(data.trajectory.map(v=>new THREE.Vector3(...v)));this.group.add(new THREE.Line(line,new THREE.LineBasicMaterial({color:0xd7f47b})));}
    const bounds=new THREE.Box3().setFromBufferAttribute(geo.attributes.position);this.center=bounds.isEmpty()?new THREE.Vector3():bounds.getCenter(new THREE.Vector3());this.extent=bounds.isEmpty()?2:Math.max(.5,bounds.getSize(new THREE.Vector3()).length());
    if(!this.loaded)this.reset();this.loaded=true;this.draw();
  }
  clear(){this.group.traverse(v=>{v.geometry?.dispose();v.material?.dispose();});this.group.clear();}
  draw(){if(this.pending||document.hidden||!this.host.offsetParent)return;this.pending=requestAnimationFrame(()=>{this.pending=0;if(!document.hidden&&this.host.offsetParent)this.renderer.render(this.scene,this.camera);});}
  pointSize(n){this.size=n;if(this.points)this.points.material.size=n;this.draw();}
  reset(){const c=this.center||new THREE.Vector3(),d=Math.max(1,this.extent||3);this.controls.target.copy(c);this.camera.position.set(c.x+d*.45,c.y+d*.28,c.z+d*.65);this.controls.update();this.draw();}
  empty(){this.version=(this.version||0)+1;this.loaded=false;this.clear();this.draw();}
}
