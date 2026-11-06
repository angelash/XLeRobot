'use strict';
export const extension={render:null,photo:null,strip:null,reset:null,scale:null};
const $ = id => document.getElementById(id);
const NS='http://www.w3.org/2000/svg';
const colors={candidate:'#8ed9af',blocked:'#f09d83',unknown:'#969a91'};
const labels={candidate:'候选可走',blocked:'不可走',unknown:'未知 / 遮挡'};
let data=null,selected=null,observation=null,zoom=1,depth=false,overlays=true,saving=false,reviewDirty=false;
const hidden=new Set();
const text=(id,value)=>{$(id).textContent=value;};
function el(tag,attrs={},content){const n=document.createElementNS(NS,tag);Object.entries(attrs).forEach(([k,v])=>n.setAttribute(k,String(v)));if(content!==undefined)n.textContent=content;return n;}
function node(tag,cls,content){const n=document.createElement(tag);if(cls)n.className=cls;if(content!==undefined)n.textContent=content;return n;}
function at(value){if(!value)return '时间未知';const d=new Date(value);return Number.isNaN(d.valueOf())?value:d.toLocaleString('zh-CN',{hour12:false});}
function kind(f){return data.review.items[f.id]?.kind||f.kind;}
function showNotice(message){text('notice',message);$('notice').hidden=!message;}
async function request(url,body){const controller=new AbortController();const timer=setTimeout(()=>controller.abort(),7000);try{const r=await fetch(url.startsWith("/api/")?"/api/platform/fieldmap/"+url.slice(5):url,{cache:'no-store',signal:controller.signal,...(body?{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}:{})});let value;try{value=await r.json();}catch{throw Error('服务返回了无法读取的数据');}if(!r.ok)throw Error(value.error||value.detail||`请求失败 ${r.status}`);return value;}finally{clearTimeout(timer);}}
function pt(p){return [360+p[0]*zoom,332+p[1]*zoom];}
function points(ps){return ps.map(p=>pt(p).join(',')).join(' ');}
function rectPoints(x,y,w,h){return [[x-w/2,y-h/2],[x+w/2,y-h/2],[x+w/2,y+h/2],[x-w/2,y+h/2]];}
function drawMap(){
  drawContext();
  if(extension.render?.({data,selected,depth,zoom,hidden,kind,selectFeature}))return;
  drawEnvironment($('map'));
  $('mapEmpty').hidden=Boolean(data.features?.length);
}
function drawEnvironment(svg){
  svg.replaceChildren();
  const defs=el('defs');const pattern=el('pattern',{id:svg.id+'-hatch',width:8,height:8,patternUnits:'userSpaceOnUse',patternTransform:'rotate(35)'});pattern.append(el('path',{d:'M0 0V8',stroke:'#889079','stroke-width':.8,opacity:.35}));defs.append(pattern);svg.append(defs);
  const grid=el('g',{stroke:'#2a382b','stroke-width':.65,fill:'none'});
  for(let n=-400;n<=400;n+=40){const a=pt([n,-410]),b=pt([n,270]),c=pt([-400,n]),d=pt([400,n]);grid.append(el('line',{x1:a[0],y1:a[1],x2:b[0],y2:b[1]}),el('line',{x1:c[0],y1:c[1],x2:d[0],y2:d[1]}));}
  for(const r of [90,180,270]){const ps=Array.from({length:121},(_,i)=>[r*Math.sin(i/120*Math.PI*2),r*Math.cos(i/120*Math.PI*2)]);grid.append(el('polygon',{points:points(ps),'stroke-dasharray':'3 6',opacity:.65}));}svg.append(grid);
  const outline=el('g',{'data-hatch':svg.id+'-hatch'});(data.features||[]).filter(f=>f.shape==='area').forEach(f=>drawFeature(outline,f));svg.append(outline);
  const objects=el('g',{'data-hatch':svg.id+'-hatch'});(data.features||[]).filter(f=>f.shape!=='area').sort((a,b)=>(a.y||0)-(b.y||0)).forEach(f=>drawFeature(objects,f));svg.append(objects);
  // Observation bearings are joint-derived; metric camera geometry is not implied.
  const rayGroup=el('g',{'pointer-events':'none'});for(const o of data.observations||[]){if(o.camera!==0||o.bearing_deg===null||o.bearing_deg===undefined)continue;const a=o.bearing_deg*Math.PI/180;const p=pt([Math.sin(a)*260,-Math.cos(a)*260]),r=pt([Math.sin(a)*43,-Math.cos(a)*43]);rayGroup.append(el('line',{x1:r[0],y1:r[1],x2:p[0],y2:p[1],stroke:o.id===observation?'#d6f281':'#6b7f58','stroke-width':o.id===observation?1.8:.7,'stroke-dasharray':'4 5',opacity:o.id===observation?.9:.25}));}svg.append(rayGroup);
  const center=pt([0,0]);const robot=el('g',{transform:`translate(${center})`});robot.append(el('circle',{r:35*zoom,fill:'#293926',stroke:'#d6f281','stroke-width':1.3}),el('path',{d:`M0 ${-25*zoom}L${10*zoom} ${-8*zoom}H${-10*zoom}Z`,fill:'#d6f281',transform:`rotate(${data.robot_heading_deg_estimate||0})`}),el('rect',{x:-17*zoom,y:-4*zoom,width:34*zoom,height:24*zoom,rx:5,fill:'#adbf8b'}));svg.append(robot);svg.append(el('text',{x:center[0],y:center[1]+53*zoom,'text-anchor':'middle',class:'robot-label'},data.robot_label||'小九 · 初始位置'));
  [['左',[-315,0]],['右',[315,0]],['前方',[0,-285]],['后方',[0,210]]].forEach(([label,p])=>{const q=pt(p);svg.append(el('text',{x:q[0],y:q[1],'text-anchor':'middle',class:'direction-label'},label));});
}
function drawFeature(parent,f){const k=kind(f);if(hidden.has(k))return;const g=el('g',{class:`map-feature ${selected===f.id?'selected':''}`,role:'button',tabindex:0,'aria-label':`${f.name}，${labels[k]}`,'data-feature':f.id});const c=colors[k];let ps=f.polygon||rectPoints(f.x||0,f.y||0,f.w||50,f.h||40);const footprint=el('polygon',{class:'footprint',points:points(ps),fill:k==='unknown'?`url(#${parent.getAttribute('data-hatch')})`:c,'fill-opacity':f.shape==='area'?.13:.25,stroke:c,'stroke-width':1.1,'stroke-dasharray':k==='unknown'?'4 5':'none'});g.append(footprint);
  const q=pt(f.label_position||[f.x||0,f.y||0]);g.append(el('text',{x:q[0],y:q[1],'text-anchor':'middle',class:'map-label'},f.short_name||f.name));
  const choose=()=>selectFeature(f.id);g.addEventListener('click',choose);g.addEventListener('keydown',e=>{if(e.key==='Enter'||e.key===' '){e.preventDefault();choose();}});parent.append(g);
}
function imageURL(name){return '/fieldmap/images/'+encodeURIComponent(name)+'?snapshot='+encodeURIComponent(data.snapshot_id||'current');}
function drawPhoto(){
  if(extension.photo?.())return;
  const o=data.observations.find(v=>v.id===observation);const svg=$('photo');svg.replaceChildren();$('photoEmpty').hidden=Boolean(o);$('photoLink').hidden=!o;if(!o)return;
  svg.setAttribute('viewBox',`0 0 ${o.width||640} ${o.height||480}`);text('photoName',o.label);text('photoBearing',o.bearing_deg===null||o.bearing_deg===undefined?'相机方位未标定':`${o.bearing_estimated?'约':'相对初始车头 '}${o.bearing_deg>0?'+':''}${Math.round(o.bearing_deg)}°`);text('photoTime',at(o.captured_at));$('photoLink').href=imageURL(o.image);text('photoNote',o.note||'实际抓拍；相对方位来自关节读数，距离未测量。');
  drawObservation(svg,o);
  document.querySelectorAll('.observation').forEach(b=>{b.classList.toggle('selected',b.dataset.id===o.id);b.setAttribute('aria-pressed',String(b.dataset.id===o.id));});
}
function drawObservation(svg,o){
  svg.replaceChildren();if(!o)return;
  svg.setAttribute('viewBox',`0 0 ${o.width||640} ${o.height||480}`);
  const im=el('image',{href:imageURL(o.image),width:o.width||640,height:o.height||480});im.addEventListener('error',()=>{svg.append(el('rect',{width:'100%',height:'100%',fill:'#19221b'}),el('text',{x:25,y:45,class:'image-fail'},'这张证据图片暂时不可读取，请刷新重试'));});svg.append(im);
  if(overlays){data.features.forEach(f=>{(f.evidence||[]).filter(e=>e.observation===o.id&&e.polygon).forEach(e=>{const k=kind(f);if(hidden.has(k))return;const ps=e.polygon.map(p=>[p[0]*(o.width||640),p[1]*(o.height||480)]);const group=el('g',{class:`photo-overlay ${selected===f.id?'selected':''}`,role:'button',tabindex:0,'aria-label':f.name});group.append(el('polygon',{points:ps.map(p=>p.join(',')).join(' '),fill:colors[k],stroke:colors[k]}));const p=ps[0];group.append(el('text',{x:Math.max(5,Math.min((o.width||640)-120,p[0]+5)),y:Math.max(20,p[1]-7)},f.short_name||f.name));group.addEventListener('click',()=>selectFeature(f.id));group.addEventListener('keydown',ev=>{if(ev.key==='Enter')selectFeature(f.id);});svg.append(group);});});}
}
let contextKey='';
function drawContext(){
  if(!data)return;
  const key=[data.id,data.snapshot_id,data.review.revision,selected,observation,overlays,zoom,...hidden].join('|');
  if(key===contextKey)return;contextKey=key;
  drawEnvironment($('contextMap'));
  $('contextMapEmpty').hidden=Boolean(data.features?.length);
  text('contextSummary',data.summary||'这份环境快照没有总体说明；可逐项检查识别信息。');
  text('contextMapLimit',data.map_limit||'位置、尺度与物品高度为示意；候选地面仍需验证。');
  text('contextSnapshotState',data.readonly?'历史快照 · 只读':'当前保存的环境快照');
  const times=(data.observations||[]).map(o=>new Date(o.captured_at).valueOf()).filter(Number.isFinite);
  text('contextSurveyTime',times.length?`照片观察时段 ${at(Math.min(...times))} — ${at(Math.max(...times))}`:'这份快照缺少可确认的照片时刻');
  const f=data.features.find(v=>v.id===selected);
  const reference=f?.evidence?.find(v=>v.observation===observation)||f?.evidence?.[0];
  const o=data.observations.find(v=>v.id===(f?reference?.observation:observation));
  drawObservation($('contextPhoto'),o);
  $('contextPhotoEmpty').hidden=Boolean(o);$('contextPhoto').hidden=!o;$('contextPhotoLink').hidden=!o;
  text('contextPhotoName',o?.label||'无照片');text('contextPhotoTime',o?at(o.captured_at):'—');
  text('contextPhotoNote',o?.note||'点击区域或物品查看原识别依据；它与时间轴相机照片分别保留。');
  if(o)$('contextPhotoLink').href=imageURL(o.image);
  const strip=$('contextObservationStrip');strip.replaceChildren();
  for(const item of data.observations){
    const b=node('button',`observation context-observation ${item.id===o?.id?'selected':''}`);
    b.dataset.id=item.id;b.title=item.label;b.setAttribute('aria-label',item.label);b.setAttribute('aria-pressed',String(item.id===o?.id));
    const image=node('img');image.src=imageURL(item.image);image.alt='';image.loading='lazy';
    b.append(image,node('span','',item.short_label||item.label));b.addEventListener('click',()=>selectObservation(item.id));strip.append(b);
  }
}
function photoFeature(){return data.features.find(f=>f.id===selected&&f.evidence?.some(e=>e.observation===observation))||data.features.find(f=>f.evidence?.some(e=>e.observation===observation));}
function selectObservation(id){observation=id;selected=photoFeature()?.id;reviewDirty=false;drawPhoto();drawMap();drawList();drawDetail();}
function drawStrip(){if(extension.strip?.())return;const strip=$('observationStrip');strip.replaceChildren();for(const o of data.observations){const b=node('button','observation');b.dataset.id=o.id;b.title=o.label;b.setAttribute('aria-label',o.label);const im=node('img');im.src=imageURL(o.image);im.alt='';im.loading='lazy';b.append(im,node('span','',o.short_label||o.label));b.addEventListener('click',()=>selectObservation(o.id));strip.append(b);}}
function drawList(){const list=$('featureList');list.replaceChildren();let checked=0;const counts={blocked:0,candidate:0,unknown:0};data.features.forEach((f,i)=>{const k=kind(f);counts[k]++;const review=data.review.items[f.id];if(review?.confirmed)checked++;const b=node('button',`feature-row ${selected===f.id?'selected':''}`);b.dataset.id=f.id;const title=node('span','feature-name',f.name);title.append(node('small','',f.brief||f.description));const cl=node('span',`classification ${k}`);cl.append(node('i',`swatch ${k}`),document.createTextNode(labels[k]));b.append(node('span','feature-number',String(i+1).padStart(2,'0')),title,cl,node('span','confidence',f.confidence==='high'?'识别较明确':f.confidence==='medium'?'判断待确认':'缺少证据'),node('span','review-check',review?.confirmed?'已核':'↗'));b.addEventListener('click',()=>selectFeature(f.id));list.append(b);});for(const k of Object.keys(counts))text(k+'Count',counts[k]);text('featureTotal',`${data.features.length} 项`);text('reviewCount',`${checked} / ${data.features.length} 已复核`);}
function selectFeature(id){selected=id;reviewDirty=false;const f=data.features.find(x=>x.id===id);const e=f?.evidence?.find(v=>v.observation===observation)||f?.evidence?.[0];if(e)observation=e.observation;drawMap();drawPhoto();drawList();drawDetail();}
function drawDetail(){const f=data.features.find(v=>v.id===selected);$('reviewForm').hidden=!f||data.readonly;const facts=$('detailFacts');facts.replaceChildren();$('reviewAssessment').hidden=true;if(!f){text('detailTitle','这张照片暂无物品标记');text('detailDescription','可继续查看其他视角，或从地图选择物品核对。');text('saveMessage','');return;}const review=data.review.items[f.id];if(review){$('reviewAssessment').hidden=false;text('reviewAssessment',`已保存复核：${labels[review.kind]||review.kind} · ${review.confirmed?'已现场核对':'待核对'}${review.note?' · '+review.note:''}`);}text('detailTitle',f.name);text('detailDescription',f.description);[labels[kind(f)],f.confidence==='high'?'物体识别较明确':f.confidence==='medium'?'需现场确认':'未知区域','位置仅为示意',`${(f.evidence||[]).length} 处照片依据`].forEach(t=>facts.append(node('span','fact',t)));$('reviewKind').value=review?.kind||f.kind;$('reviewNote').value=review?.note||'';$('reviewConfirmed').checked=review?.confirmed||false;text('saveMessage',data.readonly?'历史快照只读 · 复核仍按原地图保存':review?'最近复核：'+at(review.at):'');}
function render(){text('summary',data.summary||'观察结果等待复核。');text('surveyNumber',data.number||'01');text('mapLimit',data.map_limit||'示意地图尚未测量距离。可见地面不等于已经验证可通行。');text('dataSource',`最近补图 ${at(data.updated_at||data.created_at)} · ${data.method||'实际相机证据'}`);const details=$('scanDetails');details.replaceChildren();for(const t of data.scan?.notes||['扫描尚未开始。'])details.append(node('p','',t));if(!observation||!data.observations.some(o=>o.id===observation))observation=data.default_observation||data.observations[0]?.id;selected=photoFeature()?.id;drawStrip();drawMap();drawPhoto();drawList();drawDetail();}
let loadVersion=0;
async function load(snapshot='current'){const version=++loadVersion;try{const value=await request('/api/map?snapshot='+encodeURIComponent(snapshot));if(version!==loadVersion)return;data=value;observation=null;contextKey='';render();}catch(e){showNotice('地图读取失败：'+(e.name==='AbortError'?'连接超时':e.message));}}
async function poll(){try{const s=await request('/api/status');text('liveBadge',s.online?'车载服务在线':'车载服务未响应');$('liveBadge').classList.toggle('error',!s.online);text('motors',`${s.motor_count||0} / 16`);$('motorDot').classList.toggle('ok',s.motor_count===16);text('wheels',s.wheels_stopped===true?'已停稳':s.wheel_drive_disabled?'驱动力已关闭':s.wheels_stopped===false?'运动或状态待确认':'无法确认');text('pad',s.gamepad?'已连接':'未连接 · 非必需');text('voltage',s.voltage?`${s.voltage[0].toFixed(1)}–${s.voltage[1].toFixed(1)} V`:'—');text('temperature',s.temperature_max===null||s.temperature_max===undefined?'—':`${s.temperature_max} °C`);text('statusTime',`遥测 ${at(s.sample_at||s.at)}`);if(s.error)showNotice('控制服务状态：'+s.error);else if(!saving)showNotice('');}catch(e){text('liveBadge','连接超时');$('liveBadge').classList.add('error');text('wheels','无法确认');showNotice('实时状态暂时不可用，当前显示的是此前保存的地图。');}finally{setTimeout(poll,3500);}}
$('flatView').addEventListener('click',()=>{depth=false;$('flatView').setAttribute('aria-pressed','true');$('depthView').setAttribute('aria-pressed','false');if(data)drawMap();});$('depthView').addEventListener('click',()=>{depth=true;$('depthView').setAttribute('aria-pressed','true');$('flatView').setAttribute('aria-pressed','false');if(data)drawMap();});
$('resetView').addEventListener('click',()=>{zoom=1;extension.reset?.();if(data)drawMap();});$('zoomIn').addEventListener('click',()=>{if(extension.scale?.(1.2))return;zoom=Math.min(1.7,zoom+.15);if(data)drawMap();});$('zoomOut').addEventListener('click',()=>{if(extension.scale?.(1/1.2))return;zoom=Math.max(.6,zoom-.15);if(data)drawMap();});
document.querySelectorAll('[data-filter]').forEach(b=>b.addEventListener('click',()=>{const k=b.dataset.filter;if(hidden.has(k))hidden.delete(k);else hidden.add(k);b.setAttribute('aria-pressed',String(!hidden.has(k)));if(data){drawMap();drawPhoto();}}));
$('overlayToggle').addEventListener('click',()=>{overlays=!overlays;text('overlayToggle',overlays?'隐藏标注':'显示标注');$('overlayToggle').setAttribute('aria-pressed',String(overlays));if(data){drawPhoto();drawContext();}});
$('reviewForm').addEventListener('input',()=>{reviewDirty=true;});
$('reviewForm').addEventListener('submit',async e=>{e.preventDefault();if(!data||!selected||saving||data.readonly)return;saving=true;$('saveReview').disabled=true;text('saveMessage','正在保存……');try{const value=await request('/api/review',{survey_id:data.id,revision:data.review.revision,feature_id:selected,kind:$('reviewKind').value,note:$('reviewNote').value,confirmed:$('reviewConfirmed').checked});data.review=value;reviewDirty=false;drawMap();drawPhoto();drawList();text('saveMessage','已保存到笔记本。复核不自动执行行驶。');}catch(err){text('saveMessage','保存失败：'+(err.name==='AbortError'?'连接超时，请刷新确认是否已保存':err.message));}finally{saving=false;$('saveReview').disabled=false;}});
$('stopButton').addEventListener('click',async()=>{$('stopButton').disabled=true;showNotice('正在请求停止，并读取轮组状态……');try{const result=await request('/api/stop',{});showNotice(result.status.wheels_stopped?'已请求停止，双轮遥测已停稳。':'已请求停止；仍需确认轮组状态。');}catch(e){showNotice('停止请求未能确认：'+e.message+'。请以机器人实际状态为准。');}finally{$('stopButton').disabled=false;}});
function drawBody(profile, preview){const svg=$('bodyDiagram');svg.replaceChildren();const upper=Math.max(preview.combined_aabb_m.x_max,preview.forward_m+profile.full_turn_radius_m),lower=Math.min(preview.combined_aabb_m.x_min,preview.forward_m-profile.full_turn_radius_m);const scale=Math.min(250/(upper-lower),300/(2*profile.full_turn_radius_m)),cx=200,cy=35+upper*scale;const box=(b,color,fill,dash='')=>svg.append(el('rect',{x:cx-b.y_max*scale,y:cy-b.x_max*scale,width:(b.y_max-b.y_min)*scale,height:(b.x_max-b.x_min)*scale,stroke:color,fill,'fill-opacity':.14,'stroke-dasharray':dash}));svg.append(el('circle',{cx,cy:cy-preview.forward_m*scale,r:profile.full_turn_radius_m*scale,fill:'none',stroke:'#a6aaa0','stroke-dasharray':'4 5'}));box(preview.translation_aabb_m,'#6e9570','#8ed9af');box(preview.rotation_aabb_m,'#be7a54','#e6a57e','5 3');box(profile.box_m,'#273c2a','#718b6f');svg.append(el('circle',{cx,cy,r:3,fill:'#273c2a'}));svg.append(el('text',{x:cx+8,y:cy-6,'font-size':11,fill:'#344c38'},'当前轴心'));svg.append(el('text',{x:15,y:22,'font-size':12,fill:'#344c38'},'↑ 车头 / 向前'));svg.append(el('text',{x:15,y:320,'font-size':11,fill:'#63725d'},'绿：平移扫掠　橙：转身扫掠外框'));svg.append(el('text',{x:15,y:338,'font-size':10,fill:'#707567'},'虚线圆：完整转身充分空间 · 非现场比例图'));}
let bodyProfile=null;
async function previewBody(){try{if(!bodyProfile)bodyProfile=await request('/api/body');const preview=await request('/api/body/plan',{posture:'folded',forward_m:Number($('bodyForward').value)/100,turn_deg:Number($('bodyTurn').value)});text('bodySummary',`收臂暂定外框 80 × 60 cm · 完整转身约需轴心周围 ${Math.round(bodyProfile.full_turn_radius_m*100)} cm 半径`);text('bodyResult','已计算整车中途扫掠；现场间距仍待观察确认，不自动放行动作。');drawBody(bodyProfile,preview);}catch(e){text('bodyResult','包络预览不可用：'+(e.name==='AbortError'?'连接超时':e.message));}}
$('bodyForm').addEventListener('submit',e=>{e.preventDefault();previewBody();});
load();poll();previewBody();

export const environment={get data(){return data;},drawMap,drawPhoto,drawStrip,load,render,selectFeature};
