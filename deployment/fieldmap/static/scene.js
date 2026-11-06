import * as THREE from '/presence/assets/vendor/three.module.min.js';
import {OrbitControls} from '/presence/assets/vendor/addons/controls/OrbitControls.js';

// Perspective, depth-tested meshes. Dimensions of environment objects remain schematic.
export class SpaceScene {
  constructor(host, onSelect, onFailure) {
    this.host=host; this.onSelect=onSelect; this.onFailure=onFailure;
    this.scene=new THREE.Scene(); this.scene.background=new THREE.Color('#131a16');
    this.camera=new THREE.PerspectiveCamera(43,1,.02,500);
    this.renderer=new THREE.WebGLRenderer({antialias:true, powerPreference:'low-power'});
    this.renderer.setPixelRatio(Math.min(devicePixelRatio,1.5));
    this.renderer.domElement.setAttribute('aria-label','3D场景：左键旋转，右键平移，滚轮缩放');
    host.append(this.renderer.domElement);
    this.controls=new OrbitControls(this.camera,this.renderer.domElement);
    this.controls.minDistance=.3; this.controls.maxDistance=120; this.controls.maxPolarAngle=Math.PI*.49;
    this.controls.addEventListener('change',()=>this.invalidate());
    this.scene.add(new THREE.HemisphereLight(0xe4f1da,0x38402f,2.7));
    const light=new THREE.DirectionalLight(0xffefcf,2); light.position.set(4,8,3); this.scene.add(light);
    this.group=new THREE.Group(); this.scene.add(this.group);
    this.ray=new THREE.Raycaster(); this.ray.params.Line.threshold=.045;
    this.resize=new ResizeObserver(()=>this.size()); this.resize.observe(host);
    this.visibility=()=>this.invalidate(); document.addEventListener('visibilitychange',this.visibility);
    this.renderer.domElement.addEventListener('webglcontextlost',e=>{e.preventDefault();if(this.disposing)return;onFailure('WebGL上下文丢失，已回到平面；可重新选择3D');});
    let down;
    this.renderer.domElement.addEventListener('pointerdown',e=>{down=[e.clientX,e.clientY];});
    this.renderer.domElement.addEventListener('pointerup',e=>{
      if(!down||Math.hypot(e.clientX-down[0],e.clientY-down[1])>5)return;
      const r=host.getBoundingClientRect(); this.ray.setFromCamera(new THREE.Vector2((e.clientX-r.left)/r.width*2-1,-(e.clientY-r.top)/r.height*2+1),this.camera);
      const hit=this.ray.intersectObjects(this.group.children,true).find(h=>h.object.userData.feature||h.object.userData.samples);
      if(hit?.object.userData.feature)this.onSelect({feature:hit.object.userData.feature});
      else if(hit?.object.userData.samples){const a=hit.object.userData.samples;this.onSelect({ms:a[Math.min(hit.index||0,a.length-1)].ms});}
    });
    this.reset(); this.size();
  }
  size(){const r=this.host.getBoundingClientRect();if(r.width&&r.height&&(r.width!==this.width||r.height!==this.height)){this.width=r.width;this.height=r.height;this.renderer.setSize(r.width,r.height,false);this.camera.aspect=r.width/r.height;this.camera.updateProjectionMatrix();this.invalidate();}}
  invalidate(){if(this.pending||this.host.hidden||document.hidden)return;this.pending=requestAnimationFrame(()=>{this.pending=0;if(!this.host.hidden&&!document.hidden)this.renderer.render(this.scene,this.camera);});}
  clear(){this.group.traverse(o=>{o.geometry?.dispose();for(const m of(Array.isArray(o.material)?o.material:[o.material])){m?.map?.dispose();m?.dispose();}});this.group.clear();}
  label(value,x,z,color='#c8d6bd'){
    const canvas=document.createElement('canvas');canvas.width=512;canvas.height=72;
    const c=canvas.getContext('2d');c.fillStyle='#162019dc';c.fillRect(0,0,512,72);c.fillStyle=color;c.font='24px "Microsoft YaHei", sans-serif';c.textAlign='center';c.fillText(value,256,47,500);
    const sprite=new THREE.Sprite(new THREE.SpriteMaterial({map:new THREE.CanvasTexture(canvas),depthTest:false}));sprite.scale.set(1.9,.27,1);sprite.position.set(x,.09,z);this.group.add(sprite);
  }
  update(input){
    this.input=input;this.clear();
    const colors={candidate:0x8ed9af,blocked:0xf09d83,unknown:0x969a91};
    if(input.mode==='environment'){
      for(const f of input.data?.features||[]){
        const k=input.kind(f);if(input.hidden.has(k))continue;
        const ps=f.polygon||[[(f.x||0)-(f.w||50)/2,(f.y||0)-(f.h||40)/2],[(f.x||0)+(f.w||50)/2,(f.y||0)-(f.h||40)/2],[(f.x||0)+(f.w||50)/2,(f.y||0)+(f.h||40)/2],[(f.x||0)-(f.w||50)/2,(f.y||0)+(f.h||40)/2]];
        if(ps.length<3)continue;
        const shape=new THREE.Shape(ps.map(p=>new THREE.Vector2(p[0]/100,-p[1]/100)));
        const height=f.shape==='area'?.012:(f.height||35)/100;
        const geometry=new THREE.ExtrudeGeometry(shape,{depth:height,bevelEnabled:false});geometry.rotateX(-Math.PI/2);
        const mesh=new THREE.Mesh(geometry,new THREE.MeshStandardMaterial({color:colors[k],roughness:.9,transparent:f.shape==='area',opacity:f.shape==='area'?.28:1,emissive:f.id===input.selected?0x283914:0}));
        mesh.userData.feature=f.id;this.group.add(mesh);
        const p=f.label_position||[f.x||0,f.y||0];this.label(f.short_name||f.name,p[0]/100,p[1]/100);
      }
    }else{
      input.tracks.forEach((t,i)=>{
        if(!t.points.length)return;
        const color=new THREE.Color().setHSL((i*.19+.24)%1,.55,.65);
        // Each episode has its own origin. No connecting geometry between episodes.
        for(const failed of [false,true]){
          const vertices=[],samples=[];
          for(let n=1;n<t.points.length;n++){
            const a=t.points[n-1],b=t.points[n];
            if((b.status==='failed')!==failed||(a.event_id!==b.event_id&&b.ms-a.ms>600000))continue;
            vertices.push(new THREE.Vector3(-a.y_m,.035,-a.x_m),new THREE.Vector3(-b.y_m,.035,-b.x_m));samples.push(a,b);
          }
          if(!vertices.length)continue;
          const g=new THREE.BufferGeometry().setFromPoints(vertices);
          const m=failed?new THREE.LineDashedMaterial({color:0xf09d83,dashSize:.035,gapSize:.025}):new THREE.LineBasicMaterial({color});
          const line=new THREE.LineSegments(g,m);line.computeLineDistances();line.userData.samples=samples;this.group.add(line);
        }
        const first=t.points[0],last=t.points.at(-1);
        for(const [p,c]of [[first,0xd6f281],[last,0xf09d83]]){const dot=new THREE.Mesh(new THREE.SphereGeometry(.035,10,8),new THREE.MeshBasicMaterial({color:c}));dot.position.set(-p.y_m,.04,-p.x_m);this.group.add(dot);}
      });
    }
    const bounds=new THREE.Box3().setFromObject(this.group);
    this.extent=bounds.isEmpty()?3:Math.max(bounds.getSize(new THREE.Vector3()).length(),2);
    this.center=bounds.isEmpty()?new THREE.Vector3():bounds.getCenter(new THREE.Vector3());this.center.y=0;
    const gridSize=Math.max(4,Math.ceil(this.extent+2));const grid=new THREE.GridHelper(gridSize,Math.min(100,gridSize*5),0x45513c,0x28372a);this.group.add(grid);
    const robot=new THREE.Group();
    const body=new THREE.Mesh(new THREE.BoxGeometry(.6,.22,.8),new THREE.MeshStandardMaterial({color:0xa5bf86,roughness:.8}));body.position.y=.15;robot.add(body);
    const arrow=new THREE.ArrowHelper(new THREE.Vector3(0,0,-1),new THREE.Vector3(0,.3,0),.65,0xd6f281,.15,.12);robot.add(arrow);
    this.robot=robot;this.group.add(robot);this.pose(input.pose);this.size();
  }
  pose(p){if(!this.robot)return;this.robot.visible=this.input?.mode==='environment'||!!p;if(p){this.robot.position.set(-p.y_m,0,-p.x_m);this.robot.rotation.y=p.yaw_rad;}this.invalidate();}
  reset(){const c=this.center||new THREE.Vector3();const d=Math.max(3,(this.extent||4)*1.1);this.controls.target.copy(c);this.camera.position.set(c.x+d*.65,d*.8,c.z+d*.9);this.controls.update();this.invalidate();}
  scale(f){this.camera.position.sub(this.controls.target).multiplyScalar(1/f).add(this.controls.target);this.controls.update();this.invalidate();}
  dispose(){this.disposing=true;cancelAnimationFrame(this.pending);this.resize.disconnect();document.removeEventListener('visibilitychange',this.visibility);this.controls.dispose();this.clear();this.renderer.dispose();this.renderer.forceContextLoss();this.renderer.domElement.remove();}
}
