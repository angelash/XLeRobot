import * as THREE from './vendor/three.module.min.js';
import {GLTFExporter} from './vendor/addons/exporters/GLTFExporter.js';
import {makeDetailedMoss} from './moss-detailed.js';
import {makeSimpleMoss} from './avatar.js';

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
let model,variant='detailed',yaw=-25,pitch=4,distance=7,drag,lastX,lastY,exporting=false;

function dispose(object){
  const materials=new Set(),textures=new Set();
  object?.traverse(node=>{node.geometry?.dispose();for(const material of (Array.isArray(node.material)?node.material:[node.material]))if(material){materials.add(material);for(const value of Object.values(material))if(value?.isTexture)textures.add(value);}});
  textures.forEach(texture=>texture.dispose());materials.forEach(material=>material.dispose());
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
function replace(){
  if(model){pivot.remove(model);dispose(model);}
  model=variant==='detailed'?makeDetailedMoss():makeSimpleMoss('#ef493e');
  if(variant==='simple'){
    const box=new THREE.Box3().setFromObject(model),center=box.getCenter(new THREE.Vector3()),size=box.getSize(new THREE.Vector3());
    const wrapper=new THREE.Group();model.position.sub(center);wrapper.add(model);wrapper.scale.setScalar(3.15/Math.max(size.x,size.y,size.z));model=wrapper;
  }
  pivot.add(model);
  $('model-label').textContent=variant==='detailed'?'550W / DETAILED':'550W / SIMPLE';
  const stats=model.userData.statistics||{};
  let triangles=0,meshes=0;model.traverse(node=>{if(node.isMesh){meshes++;triangles+=(node.geometry.index?.count||node.geometry.attributes.position.count)/3;}});
  $('part-count').textContent=stats.parts||'—';$('mesh-count').textContent=meshes;
  $('triangle-count').textContent=Math.round(triangles).toLocaleString('en-US');
  $('export-status').textContent='';render();
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
renderer.domElement.addEventListener('pointerdown',event=>{drag=true;lastX=event.clientX;lastY=event.clientY;renderer.domElement.setPointerCapture(event.pointerId);});
renderer.domElement.addEventListener('pointermove',event=>{
  if(!drag)return;yaw=Math.max(-180,Math.min(180,yaw+(event.clientX-lastX)*.45));pitch=Math.max(-40,Math.min(40,pitch+(event.clientY-lastY)*.30));
  lastX=event.clientX;lastY=event.clientY;$('view-name').textContent='自定义视角';render();
});
for(const type of ['pointerup','pointercancel','lostpointercapture'])renderer.domElement.addEventListener(type,()=>drag=false);
renderer.domElement.addEventListener('wheel',event=>{event.preventDefault();distance=Math.max(4.7,Math.min(10,distance+event.deltaY*.003));render();},{passive:false});
$('export-model').addEventListener('click',async()=>{
  if(exporting)return;exporting=true;$('export-model').disabled=true;$('inspector-variant').disabled=true;$('export-status').textContent='正在生成自包含 GLB…';
  const saved=[];model.traverse(node=>{saved.push([node,node.userData]);node.userData={};});
  model.userData={title:'MOSS 550W '+variant,version:2,provenance:'Independently built geometry from public visual reference; MakerWorld 924464 not imported.',reference:'https://www.gcores.com/articles/162106'};
  try{
    const data=await new GLTFExporter().parseAsync(model,{binary:true,maxTextureSize:1024});
    const url=URL.createObjectURL(new Blob([data],{type:'model/gltf-binary'}));
    const link=document.createElement('a');link.href=url;link.download=`moss-550w-${variant}-v2.glb`;link.click();
    setTimeout(()=>URL.revokeObjectURL(url),1500);$('export-status').textContent=`已导出 ${(data.byteLength/1024**2).toFixed(2)} MiB · 含本地生成材质`;
  }catch(error){$('export-status').textContent='导出失败：'+error.message;}
  finally{for(const [node,userData] of saved)node.userData=userData;exporting=false;$('export-model').disabled=false;$('inspector-variant').disabled=false;}
});
new ResizeObserver(resize).observe(host);
window.addEventListener('pagehide',()=>{dispose(model);renderer.dispose();});
replace();resize();
