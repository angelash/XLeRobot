/* A persistent surround atlas inside Studio. No actuator endpoints. */
const api='/api/platform/fieldmap',phone='/api/platform/phone-vision';
const $=id=>document.getElementById(id),put=(id,text)=>{$(id).textContent=text;};
const ns='http://www.w3.org/2000/svg';
function svg(tag,attrs={},text){const n=document.createElementNS(ns,tag);for(const[k,v]of Object.entries(attrs))n.setAttribute(k,String(v));if(text!==undefined)n.textContent=text;return n;}
async function get(url){const r=await fetch(url,{cache:'no-store',signal:AbortSignal.timeout(30000)});if(!r.ok){const e=await r.json().catch(()=>({}));throw Error(e.detail||'空间记录暂不可读');}return r.json();}
let scan=null,plan=null,version=0,mapVersion=0,zoom=1,pan=[0,0],modelKey='',refreshing=false;
const normalize=a=>(a+540)%360-180;
function bearing(){const angle=Number($('surroundBearing').value);put('surroundBearingLabel',Math.abs(angle)<8?'正前方':Math.abs(angle)>172?'正后方':(angle<0?'左侧 ':'右侧 ')+Math.abs(angle)+'°');
  if(!scan)return;
  const sector=Math.min(71,Math.floor((angle+180)/5)),known=scan.coverage.sectors[sector];
  const indices=scan.coverage.sector_sources[sector]?.source_indices||[];
  const candidates=scan.views.filter(v=>indices.includes(v.index));
  const view=candidates.sort((a,b)=>Math.abs(normalize(a.heading_deg-angle))-Math.abs(normalize(b.heading_deg-angle)))[0];
  $('surroundPhoto').hidden=!known||!view;
  if(view){$('surroundPhoto').src=view.url;$('surroundOriginal').href=view.url;put('surroundPhotoCaption',`源视向 ${view.heading_deg.toFixed(1)}° · ${new Date(view.captured_at*1000).toLocaleTimeString('zh-CN',{hour12:false})} · 原图`);}
  else put('surroundPhotoCaption','此方位尚未采到原图；保持未知。');
  ring(angle);
}
function ring(current=Number($('surroundBearing').value)){
  const host=$('surroundRing');host.replaceChildren();
  const point=(d,r)=>[160+Math.sin(d*Math.PI/180)*r,145-Math.cos(d*Math.PI/180)*r];
  host.append(svg('circle',{cx:160,cy:145,r:74,fill:'#121c16',stroke:'#3e5038','stroke-dasharray':'3 6'}));
  for(let i=0;i<72;i++){
    const start=i*5-180+.7,end=start+3.6,[x1,y1]=point(start,103),[x2,y2]=point(end,103),known=scan?.coverage.sectors[i];
    const path=svg('path',{d:`M ${x1} ${y1} A 103 103 0 0 1 ${x2} ${y2}`,fill:'none',stroke:known?'#d6f281':'#4b5843','stroke-width':12,role:'button',tabindex:0,'aria-label':`${i*5-177.5}度，${known?'已拍':'未拍'}`});
    const choose=()=>{$('surroundBearing').value=i*5-178;bearing();};path.addEventListener('click',choose);path.addEventListener('keydown',e=>{if(e.key==='Enter')choose();});host.append(path);
  }
  host.append(svg('text',{x:160,y:141,'text-anchor':'middle','font-size':28,fill:'#d6f281'},scan?scan.coverage.seen_degrees+'°':'—'));
  host.append(svg('text',{x:160,y:164,'text-anchor':'middle','font-size':10},'已拍方位'));
  for(const[d,name]of [[0,'前'],[90,'右'],[-90,'左'],[180,'后']]){const[x,y]=point(d,130);host.append(svg('text',{x,y:y+4,'text-anchor':'middle','font-size':12},name));}
  const[x,y]=point(current,84);host.append(svg('line',{x1:160,y1:145,x2:x,y2:y,stroke:'#78c6d1','stroke-width':2}));
}
async function loadScan(){const id=$('surroundScan').value;if(!id){ring();return;}const v=++version;
  try{const data=await get(api+'/surround/'+id);if(v!==version)return;scan=data;
    put('surroundDegrees',data.coverage.seen_degrees+'°');put('surroundStatus',`${data.view_count}个原始视角 · ${new Date(data.created_at).toLocaleString('zh-CN')} · ${data.coverage.complete_360?'全周影像方位已覆盖':'仍缺'+data.coverage.missing_degrees+'°，继续补采'}。驻点与AR图各自保留坐标。`);
    put('surroundGap',`尚缺 ${data.coverage.missing_degrees}°；最大连续缺口 ${data.coverage.largest_gap_degrees}°。${data.coverage.complete_360?'影像覆盖不代表全屋障碍已测清。':'缺失方向不画成已知空间。'}`);
    put('surroundOptics',(data.optics.method==='RGB_features_and_encoder_yaw_fit'?'重叠照片＋编码器拟合':'暂定光学投影')+' · 保留时间差与缺口');
    $('surroundPanorama').src=data.preview_url||data.panorama_url;$('surroundPanorama').hidden=false;$('surroundPanoEmpty').hidden=true;$('surroundPanoramaLink').hidden=false;$('surroundPanoramaLink').href=data.panorama_url;bearing();
  }catch(e){put('surroundStatus',e.message);}}
function drawMap(){
  const host=$('surroundMap');host.replaceChildren();
  const pattern=svg('pattern',{id:'surroundUnknownPattern',patternUnits:'userSpaceOnUse',width:12,height:12});pattern.append(svg('path',{d:'M 0 12 L 12 0',stroke:'#253329','stroke-width':1}));const defs=svg('defs');defs.append(pattern);host.append(defs,svg('rect',{width:760,height:560,fill:'url(#surroundUnknownPattern)'}));
  if(!plan||!plan.cells.length){host.append(svg('text',{x:380,y:275,fill:'#a7b99b','font-size':16,'text-anchor':'middle'},'此会话暂无可投影空间点'));return;}
  const b=plan.bounds_cells,cx=(b[0]+b[1])/2,cz=(b[2]+b[3])/2;
  const scale=Math.min(660/Math.max(35,b[1]-b[0]+8),460/Math.max(35,b[3]-b[2]+8))*zoom;
  const p=(x,z)=>[380+(x-cx)*scale+pan[0],280+(z-cz)*scale+pan[1]];
  const colors={obstacle_candidate:'#e6a17c',floor_candidate:'#7ebba0',surface:'#a8ccd0'};
  // A filled cell means a measured surface estimate, never an inferred ray.
  const paths={obstacle_candidate:[],floor_candidate:[],surface:[]},sizeCell=Math.max(.8,scale*.85).toFixed(2);
  for(const cell of plan.cells){const[x,y]=p(cell.x,cell.z);paths[cell.kind].push(`M${x.toFixed(2)} ${y.toFixed(2)}h${sizeCell}v${sizeCell}h-${sizeCell}z`);}
  for(const[kind,parts]of Object.entries(paths))if(parts.length)host.append(svg('path',{d:parts.join(''),fill:colors[kind],opacity:kind==='surface'?.6:.9,'data-cell-kind':kind,'data-cell-count':parts.length}));
  for(let x=Math.ceil(b[0]/20)*20;x<=b[1];x+=20){const[px]=p(x,0);host.append(svg('line',{x1:px,y1:24,x2:px,y2:530,stroke:'#6a7b61','stroke-opacity':.15}));}
  const path=plan.camera_trajectory_ar.map(v=>p((v[0]-plan.origin_ar[0])/.05,(v[2]-plan.origin_ar[2])/.05).join(',')).join(' ');
  if(path)host.append(svg('polyline',{points:path,fill:'none',stroke:'#d6f281','stroke-width':2}));
  const [ox,oy]=p(0,0);host.append(svg('circle',{cx:ox,cy:oy,r:5,fill:'#d6f281'}),svg('text',{x:ox+10,y:oy-10,fill:'#d6f281','font-size':11},'手机起点'));
  const size=20*scale;host.append(svg('line',{x1:25,y1:530,x2:25+size,y2:530,stroke:'#c5d0ba','stroke-width':2}),svg('text',{x:25,y:519,fill:'#aebda3','font-size':11},'AR估计 1m · 未标定'));
}
async function loadModel(){const id=$('surroundModel').value;if(!id)return;const v=++mapVersion;
  try{put('surroundMapStatus','正在从保留的AR空间点计算俯视投影…');const data=await get(api+'/surround/plan?model_id='+id);if(v!==mapVersion)return;
    const key=id+':'+data.model_sha256;if(key!==modelKey){plan=data;zoom=1;pan=[0,0];modelKey=key;drawMap();}
    put('surroundMapBasis','AR会话 '+String(data.capture_session||'未知').slice(-8)+' · 历史局部图');
    const floor=data.surface_candidate?'检出水平面候选，仍待地面标定':'尚无稳定水平面候选';
    put('surroundMapStatus',`${data.point_count.toLocaleString()}个空间点 → ${data.cells.length.toLocaleString()}个5厘米AR估计格 · ${floor}。已验证自由格：0。`);
  }catch(e){put('surroundMapStatus','俯视来源暂不可读：'+e.message);}}
async function refresh(){if(refreshing)return;refreshing=true;$('surroundRefresh').disabled=true;
  try{const[scans,models]=await Promise.all([get(api+'/surround'),get(phone+'/models')]);
    const a=$('surroundScan'),b=$('surroundModel'),oldA=a.value,oldB=b.value;
    a.replaceChildren(...scans.items.map(s=>new Option(`${s.label} / ${s.coverage.seen_degrees}° / ${new Date(s.created_at).toLocaleTimeString('zh-CN')}`,s.id)));
    b.replaceChildren(...models.items.map(m=>new Option(`${m.label} / ${m.point_count.toLocaleString()}点 / ${new Date(m.updated_at||m.saved_at).toLocaleString('zh-CN')}`,m.id)));
    if(scans.items.some(s=>s.id===oldA))a.value=oldA;
    if(models.items.some(m=>m.id===oldB))b.value=oldB;else b.value=[...models.items].sort((x,y)=>y.point_count-x.point_count)[0]?.id||'';
    if(!scans.items.length){a.add(new Option('尚无环视；保留现有局部空间资料',''));put('surroundStatus','环视采集尚未完成，后方仍待观察。');ring();}
    await Promise.all([loadScan(),loadModel()]);
  }catch(e){put('surroundStatus','空间来源读取失败：'+e.message);}finally{refreshing=false;$('surroundRefresh').disabled=false;}}
$('surroundScan').addEventListener('change',loadScan);$('surroundModel').addEventListener('change',loadModel);$('surroundRefresh').addEventListener('click',refresh);$('surroundBearing').addEventListener('input',bearing);
$('surroundFit').addEventListener('click',()=>{zoom=1;pan=[0,0];drawMap();});
for(const[id,factor]of [['surroundZoomIn',1.25],['surroundZoomOut',.8]])$(id).addEventListener('click',()=>{zoom=Math.max(.4,Math.min(7,zoom*factor));drawMap();});
let drag=null;$('surroundMap').addEventListener('pointerdown',e=>{drag=[e.clientX,e.clientY,...pan];e.currentTarget.setPointerCapture(e.pointerId);});$('surroundMap').addEventListener('pointermove',e=>{if(!drag)return;const scale=760/e.currentTarget.getBoundingClientRect().width;pan=[drag[2]+(e.clientX-drag[0])*scale,drag[3]+(e.clientY-drag[1])*scale];drawMap();});$('surroundMap').addEventListener('pointerup',()=>drag=null);$('surroundMap').addEventListener('pointercancel',()=>drag=null);
refresh();setInterval(()=>{if(!document.hidden)refresh();},20000);
