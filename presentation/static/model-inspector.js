import * as THREE from './vendor/three.module.min.js';
import {GLTFExporter} from './vendor/addons/exporters/GLTFExporter.js';
import {GLTFLoader} from './vendor/addons/loaders/GLTFLoader.js';
import {makeDetailedMoss} from './moss-detailed.js';
import {makeSimpleMoss} from './avatar.js';
import {MossRig} from './moss-rig.js';
import {MOSS_IDLE_CLIPS, MossIdlePlayer} from './moss-idle.js';

const $=id=>document.getElementById(id),host=$('inspection-canvas');
const scene=new THREE.Scene();
const renderer=new THREE.WebGLRenderer({alpha:true,antialias:true,powerPreference:'low-power'});
renderer.setPixelRatio(Math.min(devicePixelRatio,1.5));renderer.outputColorSpace=THREE.SRGBColorSpace;
renderer.toneMapping=THREE.ACESFilmicToneMapping;renderer.toneMappingExposure=1.0;
host.append(renderer.domElement);
const camera=new THREE.PerspectiveCamera(31,1,.1,50);camera.position.set(0,0,7.0);camera.lookAt(0,0,0);
scene.add(new THREE.HemisphereLight('#e2edf0','#354342',2.2));
for(const [color,power,position] of [['#edece1',3.5,[-3,4,5]],['#a6c5db',2.5,[4,1,-3]]]){
  const light=new THREE.DirectionalLight(color,power);light.position.set(...position);scene.add(light);
}
const pivot=new THREE.Group();scene.add(pivot);
let model,rig,variant='licensed',yaw=-25,pitch=4,distance=7,drag,lastX,lastY,exporting=false,generation=0;
const licensedUrl='/presence/assets/models/moss-550w-jom.glb';
const idle=new MossIdlePlayer(),motion=matchMedia('(prefers-reduced-motion: reduce)');
let idleMode=null,idleFrame=0,idleTime=0,idleLabel=null;

for(const clip of MOSS_IDLE_CLIPS){
  const option=document.createElement('option');option.value=clip.id;
  option.textContent=`${clip.label} · ${clip.seconds[0]}–${clip.seconds[1]} 秒`;
  $('idle-clip').append(option);
}
function describeIdle(){
  $('idle-description').textContent=MOSS_IDLE_CLIPS.find(clip=>clip.id===$('idle-clip').value).description;
}
function showJoints(){
  const yaw=Math.round((rig?.yaw||0)*180/Math.PI),pitch=Math.round((rig?.pitch||0)*180/Math.PI);
  $('joint-yaw').value=yaw;$('joint-pitch').value=pitch;
  $('joint-yaw-value').textContent=`${yaw}°`;$('joint-pitch-value').textContent=`${pitch}°`;
}
function stopIdle(note='已停止，关节已回正。',resetPose=true){
  cancelAnimationFrame(idleFrame);idle.reset();idleMode=null;idleTime=0;idleLabel=null;
  $('idle-progress').value=0;$('idle-stop').disabled=true;
  $('idle-random').setAttribute('aria-pressed','false');$('idle-status').textContent=note;
  if(resetPose&&rig?.ready){rig.lookAt(0,0,0);showJoints();render();}
}
function tickIdle(time){
  if(document.hidden||motion.matches||!rig?.ready){stopIdle('已暂停动作预览。');return;}
  if(idleTime&&time-idleTime<1000/30){idleFrame=requestAnimationFrame(tickIdle);return;}
  const seconds=idleTime?Math.max(0,(time-idleTime)/1000):0;idleTime=time;
  const pose=idle.advance(seconds,{loop:idleMode==='random'});
  rig.lookAt(pose.yaw,pose.pitch,pose.arm);showJoints();render();
  if(!idle.current){
    stopIdle('这套动作播放完毕。');$('idle-progress').value=1;return;
  }
  const current=idle.current;
  $('idle-progress').value=current.elapsed/current.duration;
  if(idleLabel!==current){
    idleLabel=current;
    $('idle-status').textContent=`${current.clip.label} · ${current.duration.toFixed(1)} 秒${idleMode==='random'?' · 随机轮播中':''}`;
  }
  idleFrame=requestAnimationFrame(tickIdle);
}
function playIdle(mode){
  stopIdle();if(!rig?.ready||motion.matches)return;
  idleMode=mode;idle.play(mode==='single'?$('idle-clip').value:undefined);
  $('idle-stop').disabled=false;$('idle-random').setAttribute('aria-pressed',String(mode==='random'));
  idleFrame=requestAnimationFrame(tickIdle);
}
function motionPreference(){
  $('idle-preview').disabled=motion.matches;$('idle-random').disabled=motion.matches;
  if(motion.matches)stopIdle('系统已启用减少动态效果，自动动作已暂停。');
}
describeIdle();motionPreference();
$('idle-clip').addEventListener('change',()=>{stopIdle();describeIdle();});
$('idle-preview').addEventListener('click',()=>playIdle('single'));
$('idle-random').addEventListener('click',()=>playIdle('random'));
$('idle-stop').addEventListener('click',()=>stopIdle());
motion.addEventListener('change',motionPreference);
document.addEventListener('visibilitychange',()=>{if(document.hidden)stopIdle('页面隐藏，预览已停止。');});

function dispose(object){
  const materials=new Set(),textures=new Set(),images=new Set();
  object?.traverse(node=>{node.geometry?.dispose();for(const material of (Array.isArray(node.material)?node.material:[node.material]))if(material){materials.add(material);for(const value of Object.values(material))if(value?.isTexture)textures.add(value);}});
  textures.forEach(texture=>{if(texture.image?.close)images.add(texture.image);texture.dispose();});
  images.forEach(image=>image.close());materials.forEach(material=>material.dispose());
}
function render(){
  pivot.rotation.set(pitch*Math.PI/180,yaw*Math.PI/180,0);
  camera.position.z=distance;renderer.render(scene,camera);
  $('yaw').value=Math.round(yaw);$('pitch').value=Math.round(pitch);
  $('yaw-value').textContent=`${Math.round(yaw)}°`;$('pitch-value').textContent=`${Math.round(pitch)}°`;
}
function resize(){
  const {width,height}=host.getBoundingClientRect();if(!width||!height)return;
  renderer.setSize(width,height,false);camera.aspect=width/height;camera.updateProjectionMatrix();render();
}
async function replace(){
  stopIdle();
  const request=++generation,selection=variant;
  $('export-model').disabled=true;$('export-status').textContent='正在载入形象…';
  let next;
  try{
    next=selection==='licensed'?(await new GLTFLoader().loadAsync(licensedUrl)).scene:selection==='detailed'?makeDetailedMoss():makeSimpleMoss('#ef493e');
    if(request!==generation){dispose(next);return;}
    if(selection!=='detailed'){
      const box=new THREE.Box3().setFromObject(next),center=box.getCenter(new THREE.Vector3()),size=box.getSize(new THREE.Vector3());
      const wrapper=new THREE.Group();next.position.sub(center);wrapper.add(next);wrapper.scale.setScalar(3.15/Math.max(size.x,size.y,size.z));next=wrapper;
    }
  }catch(error){
    if(request===generation){$('export-status').textContent='形象载入失败：'+error.message;$('model-label').textContent='载入失败';}
    return;
  }
  if(model){pivot.remove(model);dispose(model);}
  model=next;
  rig=selection==='licensed'?new MossRig(model):null;
  $('joint-controls').hidden=!rig?.ready;
  $('idle-controls').hidden=!rig?.ready;
  $('joint-yaw').value=0;$('joint-pitch').value=0;
  $('joint-yaw-value').textContent='0°';$('joint-pitch-value').textContent='0°';
  pivot.add(model);
  $('model-label').textContent=selection==='licensed'?'550W / JOM':selection==='detailed'?'550W / DETAILED':'550W / SIMPLE';
  $('stage-source').textContent=selection==='licensed'?'Jom / CC BY 4.0':'本地生成三维模型';
  const stats=model.userData.statistics||{};
  let triangles=0,meshes=0;model.traverse(node=>{if(node.isMesh){meshes++;triangles+=(node.geometry.index?.count||node.geometry.attributes.position.count)/3;}});
  $('part-count').textContent=stats.parts||'—';$('mesh-count').textContent=meshes;
  $('triangle-count').textContent=Math.round(triangles).toLocaleString('en-US');
  $('export-status').textContent='';$('export-model').disabled=false;render();
  $('idle-status').textContent=motion.matches?'系统已启用减少动态效果，自动动作已暂停。':'展示页待机时自动随机播放，每套 5–30 秒。';
}
function selectView(name){
  const views={front:[0,0,'正面'],quarter:[-25,4,'前侧视角'],side:[-90,0,'侧面'],rear:[180,0,'背面']};
  [yaw,pitch]=views[name];$('view-name').textContent=views[name][2];
  document.querySelectorAll('[data-view]').forEach(button=>button.setAttribute('aria-pressed',String(button.dataset.view===name)));
  render();
}
document.querySelectorAll('[data-view]').forEach(button=>button.addEventListener('click',()=>selectView(button.dataset.view)));
$('inspector-variant').addEventListener('change',event=>{if(exporting)return;variant=event.target.value;replace();});
for(const id of ['yaw','pitch'])$(id).addEventListener('input',()=>{yaw=Number($('yaw').value);pitch=Number($('pitch').value);$('view-name').textContent='自定义视角';render();});
for(const id of ['joint-yaw','joint-pitch'])$(id).addEventListener('input',()=>{
  stopIdle('手动调整关节。',false);
  const yaw=Number($('joint-yaw').value),pitch=Number($('joint-pitch').value);
  rig?.lookAt(yaw*Math.PI/180,pitch*Math.PI/180);
  $('joint-yaw-value').textContent=`${yaw}°`;$('joint-pitch-value').textContent=`${pitch}°`;render();
});
renderer.domElement.addEventListener('pointerdown',event=>{drag=true;lastX=event.clientX;lastY=event.clientY;renderer.domElement.setPointerCapture(event.pointerId);});
renderer.domElement.addEventListener('pointermove',event=>{
  if(!drag)return;yaw=Math.max(-180,Math.min(180,yaw+(event.clientX-lastX)*.45));pitch=Math.max(-40,Math.min(40,pitch+(event.clientY-lastY)*.30));
  lastX=event.clientX;lastY=event.clientY;$('view-name').textContent='自定义视角';render();
});
for(const type of ['pointerup','pointercancel','lostpointercapture'])renderer.domElement.addEventListener(type,()=>drag=false);
renderer.domElement.addEventListener('wheel',event=>{event.preventDefault();distance=Math.max(4.7,Math.min(10,distance+event.deltaY*.003));render();},{passive:false});
$('export-model').addEventListener('click',async()=>{
  if(exporting||!model)return;exporting=true;$('export-model').disabled=true;$('inspector-variant').disabled=true;$('export-status').textContent='正在准备自包含 GLB…';
  const saved=[];
  try{
    let data;
    if(variant==='licensed'){
      const response=await fetch(licensedUrl);if(!response.ok)throw new Error('模型资源无法读取');
      data=await response.arrayBuffer();
    }else{
      model.traverse(node=>{saved.push([node,node.userData]);node.userData={};});
      model.userData={title:'MOSS 550W '+variant,version:2,provenance:'Independently built geometry from public visual reference; MakerWorld 924464 not imported.',reference:'https://www.gcores.com/articles/162106'};
      data=await new GLTFExporter().parseAsync(model,{binary:true,maxTextureSize:1024});
    }
    const url=URL.createObjectURL(new Blob([data],{type:'model/gltf-binary'}));
    const link=document.createElement('a');link.href=url;link.download=variant==='licensed'?'moss-550w-jom.glb':`moss-550w-${variant}-v2.glb`;link.click();
    setTimeout(()=>URL.revokeObjectURL(url),1500);$('export-status').textContent=`已导出 ${(data.byteLength/1024**2).toFixed(2)} MiB · ${variant==='licensed'?'Jom / CC BY 4.0':'含本地生成材质'}`;
  }catch(error){$('export-status').textContent='导出失败：'+error.message;}
  finally{for(const [node,userData] of saved)node.userData=userData;exporting=false;$('export-model').disabled=false;$('inspector-variant').disabled=false;}
});
new ResizeObserver(resize).observe(host);
window.addEventListener('pagehide',()=>{cancelAnimationFrame(idleFrame);idle.reset();dispose(model);renderer.dispose();});
replace();resize();
