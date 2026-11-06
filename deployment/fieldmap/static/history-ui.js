import {environment,extension} from './app.js';

const $=id=>document.getElementById(id), NS='http://www.w3.org/2000/svg';
const html=(tag,cls,text)=>{const e=document.createElement(tag);if(cls)e.className=cls;if(text!==undefined)e.textContent=text;return e;};
const svg=(tag,attrs={},text)=>{const e=document.createElementNS(NS,tag);for(const[k,v]of Object.entries(attrs))e.setAttribute(k,v);if(text!==undefined)e.textContent=text;return e;};
const date=ms=>new Date(ms).toLocaleString('zh-CN',{hour12:false});
const clock=ms=>new Date(ms).toLocaleTimeString('zh-CN',{hour12:false});
const cameraName={'0':'头部','1':'右腕','2':'左腕'};
const opName={travel:'平移',rotate:'转向',advance:'前进脉冲',drive:'轮组动作',snapshot:'拍照',move:'关节动作',arm:'关节动作',wait:'等待'};
const S={mode:'history',range:null,window:null,cursor:0,result:null,episode:'',camera:'0',nearest:{},album:[],status:null,playing:false,queryVersion:0,photoVersion:0};
let scene=null,sceneLoading=false,sceneKey='',view=null,photoTimer,playFrame,photoAbort,albumAbort,queryAbort;
function message(value){$('historyMessage').textContent=value;}
function params(extra={}){return new URLSearchParams({start_ms:S.range[0],end_ms:S.range[1],...($('runFilter').value?{run_id:$('runFilter').value}:{}),...extra});}
async function get(path,signal){
  const own=new AbortController(),timer=setTimeout(()=>own.abort(),15000);
  const abort=()=>own.abort();signal?.addEventListener('abort',abort,{once:true});if(signal?.aborted)own.abort();
  try{const r=await fetch('/api/platform/fieldmap/'+path,{signal:own.signal,cache:'no-store'});if(!r.ok){let body;try{body=await r.json();}catch{}throw Error(body?.detail||`请求失败 ${r.status}`);}return await r.json();}
  finally{clearTimeout(timer);signal?.removeEventListener('abort',abort);}
}
function inputDate(ms){const d=new Date(ms);return new Date(ms-d.getTimezoneOffset()*60000).toISOString().slice(0,19);}
function writeRange(){if(!S.range)return;$('rangeStart').value=inputDate(S.range[0]);$('rangeEnd').value=inputDate(S.range[1]);}
function tracks(){return(S.result?.tracks||[]).filter(t=>!S.episode||t.id===S.episode);}
function pose(){
  const candidates=tracks().filter(t=>t.start_ms<=S.cursor&&t.end_ms>=S.cursor);
  const t=candidates.at(-1);if(!t)return null;
  const a=t.points;let i=a.findIndex(p=>p.ms>S.cursor);if(i<0)return a.at(-1);if(i===0)return null;
  const before=a[i-1],after=a[i];if(before.event_id!==after.event_id||after.ms===before.ms)return before;
  const f=(S.cursor-before.ms)/(after.ms-before.ms);return{...before,...Object.fromEntries(['x_m','y_m','yaw_rad'].map(k=>[k,before[k]+(after[k]-before[k])*f]))};
}
function selectedEvent(){const es=S.result?.events||[];return es.find(e=>e.moving&&e.start_ms<=S.cursor&&e.end_ms>=S.cursor)||[...es].reverse().find(e=>e.start_ms<=S.cursor);}
function detail(){
  const e=selectedEvent(),host=$('eventDetail');host.replaceChildren();
  if(!e){host.append(html('p','','这段时间没有动作记录；照片仍可按时间查看。'));return;}
  const title=html('h3','',`${opName[e.op]||e.op} · ${e.status==='failed'?'失败 / 部分执行':e.status==='running'?'未封口':'已记录'}`);
  if(e.status==='failed')title.className='failed';host.append(title,html('p','event-run',e.title||e.label),html('p','',`${date(e.start_ms)} → ${clock(e.end_ms)}`));
  const facts=html('div','event-facts');
  const entries=[['请求',e.requested_m==null?'—':`${(e.requested_m*100).toFixed(1)} cm`],['轮估位移',e.distance_m_estimate==null?'—':`${(e.distance_m_estimate*100).toFixed(1)} cm`],['轮计数',e.delta?`9: ${e.delta['9']} / 10: ${e.delta['10']}`:'缺失'],['时间来源',e.time_basis==='receipt_timestamp'?'回执时间':'完成时刻 + 时长回推'],['定位',e.pose_start?'双轮积分估计':e.pose_unavailable_reason||'无位姿数据']];
  for(const[k,v]of entries){const span=html('span');span.append(html('small','',k),html('strong','',v));facts.append(span);}host.append(facts);
  if(e.reason)host.append(html('p','failed',e.reason));
  const p=pose();if(p)host.append(html('p','pose-readout',`估计位置 x ${p.x_m.toFixed(3)} m · y ${p.y_m.toFixed(3)} m · 朝向 ${(p.yaw_rad*180/Math.PI).toFixed(1)}°`));
  document.querySelectorAll('.history-event').forEach(b=>{const yes=b.dataset.id===e.id;b.classList.toggle('selected',yes);b.setAttribute('aria-pressed',yes);});
}
function drawTimeline(){
  if(!S.window)return;
  const [lo,hi]=S.window,span=Math.max(1,hi-lo),host=$('timelineSvg');host.replaceChildren();
  for(let i=0;i<=6;i++){const x=i/6*1000;host.append(svg('line',{x1:x,x2:x,y1:22,y2:85,stroke:'#3a493a','stroke-width':1}),svg('text',{x:Math.min(998,Math.max(2,x)),y:14,fill:'#a2aea2','font-size':10,'text-anchor':i===0?'start':i===6?'end':'middle'},clock(lo+span*i/6)));}
  for(const e of S.result?.events||[]){
    if(e.end_ms<lo||e.start_ms>hi)continue;
    const x=Math.max(0,(e.start_ms-lo)/span*1000),right=Math.min(1000,(e.end_ms-lo)/span*1000);
    const y=e.moving?32:63,c=e.status==='failed'?'#f09d83':e.moving?'#a6ce80':'#6d9b93';
    host.append(svg('rect',{x,y,width:Math.max(2,right-x),height:e.moving?19:9,rx:1,fill:c,opacity:.85}));
  }
  const percent=(S.cursor-lo)/span*100;$('timeCursor').style.left=`${Math.max(0,Math.min(100,percent))}%`;$('timeCursor').hidden=percent<0||percent>100;
  $('timeline').setAttribute('aria-valuemin',Math.round(lo));$('timeline').setAttribute('aria-valuemax',Math.round(hi));$('timeline').setAttribute('aria-valuenow',Math.round(S.cursor));$('timeline').setAttribute('aria-valuetext',date(S.cursor));
  $('cursorTime').textContent=date(S.cursor);$('windowTimes').textContent=`${date(lo)} — ${date(hi)}`;
}
function seek(ms,{photos=true,follow=false}={}){
  if(!S.range)return;S.cursor=Math.max(S.range[0],Math.min(S.range[1],ms));
  if(follow&&S.window&&(S.cursor<S.window[0]||S.cursor>S.window[1])){const span=S.window[1]-S.window[0];S.window=clampWindow(S.cursor-span*.15,S.cursor+span*.85);}
  drawTimeline();detail();if(S.mode==='history')environment.drawMap();
  if(photos){photoAbort?.abort();++S.photoVersion;clearTimeout(photoTimer);photoTimer=setTimeout(loadNearest,130);}
}
function showPhoto(p){
  const image=$('historyPhoto');$('photo').hidden=true;image.hidden=!p;$('photoEmpty').hidden=!!p;$('photoLink').hidden=!p;
  $('photoEmpty').textContent='这个范围内没有此相机的照片';
  if(!p){image.removeAttribute('src');$('photoName').textContent=cameraName[S.camera]+' · 无照片';$('photoTime').textContent='—';$('photoBearing').textContent='—';$('photoNote').textContent='当前筛选范围内缺少这路相机的照片；可更换相机或时间范围。';return;}
  image.onerror=()=>{image.hidden=true;$('photoEmpty').hidden=false;$('photoEmpty').textContent='原始照片暂不可读；可选其他时间或相机';};
  if(image.getAttribute('src')!==p.url)image.src=p.url;
  image.dataset.photoId=p.id;$('photoLink').href=p.url;
  $('photoName').textContent=`${cameraName[p.camera]||p.camera} · ${p.phase}`;
  const delta=(p.at_ms-S.cursor)/1000;
  $('photoBearing').textContent=`距游标 ${delta>=0?'+':''}${delta.toFixed(2)} s`;
  $('photoTime').textContent=date(p.at_ms);
  $('photoNote').textContent=(p.time_basis==='frame_capture'?'实际帧捕获时间':'拍摄组起始时间估计 · 非逐帧精确时间')+'。照片是历史抓拍；相机之间可能存在时差。';
  document.querySelectorAll('.album-photo').forEach(b=>b.classList.toggle('selected',b.dataset.id===p.id));
}
async function loadNearest(){
  if(!S.range||S.mode!=='history')return;
  photoAbort?.abort();photoAbort=new AbortController();const version=++S.photoVersion;
  try{
    const result=await get('history/nearest?'+params({at_ms:Math.round(S.cursor)}),photoAbort.signal);if(version!==S.photoVersion||S.mode!=='history')return;
    S.nearest=result.cameras;showPhoto(S.nearest[S.camera]?.[0]);
    const host=$('nearestCameras');host.replaceChildren();
    for(const c of ['0','1','2']){const p=S.nearest[c]?.[0],b=html('button','nearest-camera');b.setAttribute('aria-label',`${cameraName[c]}最近照片`);b.disabled=!p;
      if(p){const im=html('img');im.src=p.thumbnail_url;im.alt='';im.loading='lazy';b.append(im);}
      b.append(html('span','',p?`${cameraName[c]} ${(p.at_ms-S.cursor)/1000>=0?'+':''}${((p.at_ms-S.cursor)/1000).toFixed(1)}s`:`${cameraName[c]} · 缺失`));b.addEventListener('click',()=>chooseCamera(c));host.append(b);}
  }catch(e){if(e.name!=='AbortError'){$('photoNote').textContent='照片时间查询失败：'+e.message;}}
}
function albumButton(p){
  const b=html('button','observation album-photo');b.dataset.id=p.id;b.title=`${date(p.at_ms)} · ${p.phase}`;
  const im=html('img');im.src=p.thumbnail_url;im.alt=`${cameraName[p.camera]} ${clock(p.at_ms)}`;im.loading='lazy';im.onerror=()=>{im.alt='图片缺失';};
  b.append(im,html('span','',clock(p.at_ms)));b.addEventListener('click',()=>{pause();seek(p.at_ms,{follow:true});showPhoto(p);});return b;
}
async function loadAlbum(reset=true){
  if(!S.range||S.mode!=='history')return;
  albumAbort?.abort();albumAbort=new AbortController();const token=albumAbort;
  if(reset){S.album=[];$('observationStrip').replaceChildren();}
  $('morePhotos').disabled=true;
  try{const result=await get('history/photos?'+params({camera:S.camera,offset:reset?0:S.album.length}),token.signal);if(albumAbort!==token||S.mode!=='history')return;
    S.album.push(...result.items);for(const p of result.items)$('observationStrip').append(albumButton(p));
    $('morePhotos').hidden=result.next_offset===null;$('morePhotos').textContent=`更多照片 · 已载入 ${S.album.length} / ${result.total}`;
  }catch(e){if(e.name!=='AbortError')message('照片列表读取失败：'+e.message);}finally{if(albumAbort===token)$('morePhotos').disabled=false;}
}
function chooseCamera(camera){S.camera=camera;document.querySelectorAll('[data-camera]').forEach(b=>b.setAttribute('aria-pressed',b.dataset.camera===camera));showPhoto(S.nearest[camera]?.[0]);loadAlbum();}
function eventList(){const host=$('eventList');host.replaceChildren();for(const e of S.result?.events||[]){const b=html('button',`history-event ${e.status==='failed'?'failed':''}`);b.dataset.id=e.id;b.append(html('time','',clock(e.end_ms)),html('span','',opName[e.op]||e.op),html('small','',e.status==='failed'?'失败 / 部分':e.label));b.addEventListener('click',()=>{pause();if(e.episode_id){S.episode=e.episode_id;$('episodeSelect').value=S.episode;sceneKey='';}seek(e.end_ms,{follow:true});});host.append(b);}}
async function loadRange({runs=true}={}){
  if(!S.range)return;pause();queryAbort?.abort();photoAbort?.abort();albumAbort?.abort();++S.photoVersion;
  queryAbort=new AbortController();const version=++S.queryVersion;message('正在读取历史……');$('rangeForm').setAttribute('aria-busy','true');
  S.result=null;S.nearest={};S.album=[];$('observationStrip').replaceChildren();$('nearestCameras').replaceChildren();if(S.mode==='history')showPhoto(null);
  $('morePhotos').hidden=true;$('historyStats').textContent='正在读取所选范围……';eventList();detail();environment.drawMap();
  try{
    const result=await get('history/query?'+params(),queryAbort.signal);if(version!==S.queryVersion)return;
    S.result=result;S.window=[...S.range];S.episode=result.tracks.at(-1)?.id||'';
    const select=$('episodeSelect');select.replaceChildren(new Option('全部独立段（各自起点重合）',''));
    result.tracks.forEach((t,i)=>select.add(new Option(`段 ${i+1} · ${clock(t.start_ms)} — ${clock(t.end_ms)}`,t.id)));select.value=S.episode;
    sceneKey='';eventList();
    const sum=result.summary;$('historyStats').textContent=`${result.total_events} 事件 · ${sum.translations} 平移 / ${sum.rotations} 转向 · 轮估累计 ${sum.distance_m_estimate.toFixed(2)} m · ${sum.failed_events} 失败 · ${result.tracks.length} 独立段`;
    message((result.truncated?'结果已截断，请缩小时间范围。 ':'')+(result.points_downsampled?'轨迹点已抽稀。 ':'')+(!result.total_events?'这段时间没有动作记录。 ':''));
    seek(S.cursor>=S.range[0]&&S.cursor<=S.range[1]?S.cursor:S.range[0]);loadAlbum();
    if(runs){const old=$('runFilter').value,select=$('runFilter');let offset=0,items=[],next;
      do{const page=await get('history/runs?'+params({offset,limit:200}),queryAbort.signal);items.push(...page.items);next=page.next_offset;offset=next;}while(next!==null&&items.length<2000);
      if(version!==S.queryVersion)return;select.replaceChildren(new Option('范围内全部运行',''));for(const r of items)select.add(new Option(`${clock(r.start_ms)} · ${r.title}`,r.id));select.value=old;
      if(next!==null)message('运行筛选列表已截断；请缩小时间范围。');
    }
  }catch(e){if(version===S.queryVersion&&e.name!=='AbortError')message('历史读取失败：'+e.message+'；可重新查看这段。');}
  finally{if(version===S.queryVersion){$('rangeForm').setAttribute('aria-busy','false');environment.drawMap();drawTimeline();}}
}
function range(lo,hi){S.range=[lo,hi];S.cursor=lo;writeRange();$('runFilter').value='';loadRange();}
function clampWindow(lo,hi){const span=Math.min(hi-lo,S.range[1]-S.range[0]);lo=Math.max(S.range[0],Math.min(lo,S.range[1]-span));return[lo,lo+span];}
function zoomTime(f,anchor=.5){if(!S.window)return;const [lo,hi]=S.window,center=lo+(hi-lo)*anchor,span=Math.min(S.range[1]-S.range[0],Math.max(1000,(hi-lo)*f));S.window=clampWindow(center-span*anchor,center+span*(1-anchor));drawTimeline();}
function pause(){S.playing=false;cancelAnimationFrame(playFrame);$('playPause').textContent='▶ 播放';$('playPause').setAttribute('aria-label','播放历史');}
function play(){if(!S.range||!S.result)return;if(S.playing){pause();return;}if(S.cursor>=S.range[1])seek(S.range[0]);S.playing=true;$('playPause').textContent='Ⅱ 暂停';$('playPause').setAttribute('aria-label','暂停历史');let last=performance.now(),paint=last;
  const tick=now=>{if(!S.playing)return;const ms=Math.min(500,now-last)*Number($('playSpeed').value);last=now;S.cursor=Math.min(S.range[1],S.cursor+ms);if(now-paint>=80){seek(S.cursor,{photos:false,follow:true});paint=now;}if(S.cursor>=S.range[1]){pause();seek(S.cursor);return;}playFrame=requestAnimationFrame(tick);};playFrame=requestAnimationFrame(tick);
}
setInterval(()=>{if(S.playing)loadNearest();},600);
function step(direction){pause();const events=S.result?.events||[];const e=direction>0?events.find(e=>e.end_ms>S.cursor+1):[...events].reverse().find(e=>e.end_ms<S.cursor-1);if(e)seek(e.end_ms,{follow:true});}
function mode(value){
  pause();S.mode=value;photoAbort?.abort();albumAbort?.abort();++S.photoVersion;sceneKey='';
  const history=value==='history';for(const id of ['rangeForm','historyStats','timelinePanel','trackLegend','cameraTabs','nearestCameras'])$(id).hidden=!history;
  for(const id of ['snapshotLabel','environmentLegend','environmentReview','overlayToggle'])$(id).hidden=history;
  $('historyMode').setAttribute('aria-pressed',history);$('environmentMode').setAttribute('aria-pressed',!history);
  $('spaceTitle').textContent=history?'历史轨迹':'周围空间';$('spaceScale').textContent=history?'各段独立起点 · 米':'相对方位示意';
  $('mapUnits').textContent=history?'轮估轨迹 · 会漂移':'比例和物品高度未测量';
  $('mapLimit').textContent=history?'双轮积分估计，可能打滑漂移。不同段各有独立起点；没有与环境快照做坐标标定。':environment.data?.map_limit||'环境几何与高度为示意，距离未测量。';
  $('photo').hidden=history;$('historyPhoto').hidden=!history;$('morePhotos').hidden=true;
  if(history){loadNearest();loadAlbum();}else{environment.drawStrip();environment.drawPhoto();}
  environment.drawMap();
}
function flatTrack(){
  const host=$('map');host.replaceChildren();const ts=tracks(),ps=ts.flatMap(t=>t.points);
  $('mapEmpty').hidden=!!ps.length;$('mapEmpty').textContent=S.result?'这个范围或轨迹段没有可重建的轮估位置':'正在读取历史轨迹……';
  if(!ps.length)return;
  const ys=ps.map(p=>p.y_m),xs=ps.map(p=>p.x_m),loY=Math.min(...ys)-.5,hiY=Math.max(...ys)+.5,loX=Math.min(...xs)-.5,hiX=Math.max(...xs)+.5;
  const scale=Math.min(640/(hiY-loY),470/(hiX-loX))*(view?.zoom||1),cx=(loY+hiY)/2,cy=(loX+hiX)/2;
  const p2=p=>[360-(p.y_m-cx)*scale,320-(p.x_m-cy)*scale];
  const grid=svg('g',{stroke:'#304032','stroke-width':.8});
  const spacing=Math.max(.1,10**Math.floor(Math.log10(Math.max(hiX-loX,hiY-loY)/8)));
  for(let v=Math.ceil(loX/spacing)*spacing;v<=hiX;v+=spacing){const y=p2({x_m:v,y_m:0})[1];grid.append(svg('line',{x1:25,x2:695,y1:y,y2:y}),svg('text',{x:28,y:y-3,fill:'#82927e','font-size':9},v.toFixed(1)+'m'));}
  for(let v=Math.ceil(loY/spacing)*spacing;v<=hiY;v+=spacing){const x=p2({x_m:0,y_m:v})[0];grid.append(svg('line',{x1:x,x2:x,y1:50,y2:560}));}host.append(grid);
  ts.forEach((t,i)=>{
    const color=`hsl(${(95+i*68)%360} 44% 67%)`;
    const paths=new Map();for(const p of t.points){if(!paths.has(p.event_id))paths.set(p.event_id,[]);paths.get(p.event_id).push(p);}
    for(const samples of paths.values()){
      const line=svg('polyline',{points:samples.map(p=>p2(p).join(',')).join(' '),fill:'none',stroke:samples[0].status==='failed'?'#f09d83':color,'stroke-width':3,'stroke-linecap':'round','stroke-dasharray':samples[0].status==='failed'?'6 4':'none',tabindex:0,role:'button','aria-label':'选择轨迹时间',class:'track-path'});
      const choose=()=>{pause();seek(samples.at(-1).ms,{follow:true});};line.addEventListener('click',choose);line.addEventListener('keydown',e=>{if(e.key==='Enter')choose();});host.append(line);
    }
    for(const[p,c,name]of [[t.points[0],'#d6f281','起'],[t.points.at(-1),'#f09d83','终']]){const[x,y]=p2(p);host.append(svg('circle',{cx:x,cy:y,r:5,fill:c}),svg('text',{x:x+8,y:y-8,fill:c,'font-size':11},name));}
  });
  const p=pose();if(p){const[x,y]=p2(p),robot=svg('g',{transform:`translate(${x},${y}) rotate(${-p.yaw_rad*180/Math.PI})`,'aria-label':'游标时刻机器人估计位置'});robot.append(svg('rect',{x:-.3*scale,y:-.4*scale,width:.6*scale,height:.8*scale,rx:4,fill:'#a6bf8636',stroke:'#b8d297','stroke-width':1}),svg('path',{d:'M0 -19 L9 4 L0 -1 L-9 4 Z',fill:'#d6f281'}));host.append(robot);}
}
async function ensureScene(){
  if(scene||sceneLoading)return;sceneLoading=true;
  try{const {SpaceScene}=await import('./scene.js');scene=new SpaceScene($('scene3d'),choice=>{if(choice.feature)environment.selectFeature(choice.feature);if(choice.ms){pause();seek(choice.ms,{follow:true});}},sceneFailure);sceneKey='';environment.drawMap();scene.reset();}
  catch(e){sceneFailure('3D不可用：'+e.message+'；平面回放仍可使用。');}finally{sceneLoading=false;}
}
function sceneFailure(reason){message(reason);const old=scene;scene=null;sceneKey='';if(old){old.disposing=true;old.dispose();}$('flatView').click();}
extension.render=input=>{
  view=input;const is3d=input.depth,history=S.mode==='history';$('map').hidden=is3d;$('scene3d').hidden=!is3d;
  $('mapLimit').textContent=history?'双轮积分估计，可能打滑漂移。不同段各有独立起点；未与环境快照标定对齐。':input.data?.map_limit||'环境几何与物品高度为示意，距离未测量。';
  if(!is3d){if(history){flatTrack();return true;}return false;}
  $('mapEmpty').hidden=true;
  if(!scene){ensureScene();return true;}
  const key=[S.mode,S.queryVersion,S.episode,input.selected,Array.from(input.hidden).join(','),input.data?.snapshot_id].join('|');
  if(key!==sceneKey){sceneKey=key;scene.update({...input,mode:S.mode,tracks:tracks(),pose:pose()});}
  else scene.pose(pose());scene.size();return true;
};
extension.photo=()=>S.mode==='history';extension.strip=()=>S.mode==='history';
extension.reset=()=>{scene?.reset();};extension.scale=f=>{if(view?.depth){scene?.scale(f);return true;}return false;};
$('environmentMode').addEventListener('click',()=>mode('environment'));$('historyMode').addEventListener('click',()=>mode('history'));
$('rangeForm').addEventListener('submit',e=>{e.preventDefault();const lo=new Date($('rangeStart').value).getTime(),hi=new Date($('rangeEnd').value).getTime();if(!Number.isFinite(lo)||!Number.isFinite(hi)||lo>=hi){message('结束时间需要晚于开始时间。');return;}range(lo,hi);});
$('runFilter').addEventListener('change',()=>loadRange({runs:false}));
$('episodeSelect').addEventListener('change',()=>{S.episode=$('episodeSelect').value;sceneKey='';environment.drawMap();if(S.episode){const t=tracks()[0];if(S.cursor<t.start_ms||S.cursor>t.end_ms)seek(t.start_ms,{follow:true});}scene?.reset();});
$('morePhotos').addEventListener('click',()=>loadAlbum(false));
document.querySelectorAll('[data-camera]').forEach(b=>b.addEventListener('click',()=>chooseCamera(b.dataset.camera)));
$('previousEvent').addEventListener('click',()=>step(-1));$('nextEvent').addEventListener('click',()=>step(1));$('playPause').addEventListener('click',play);
$('timeZoomIn').addEventListener('click',()=>zoomTime(.5));$('timeZoomOut').addEventListener('click',()=>zoomTime(2));$('timeFit').addEventListener('click',()=>{if(S.range){S.window=[...S.range];drawTimeline();}});
let drag;
const timeline=$('timeline'),fraction=e=>Math.max(0,Math.min(1,(e.clientX-timeline.getBoundingClientRect().left)/timeline.clientWidth));
timeline.addEventListener('pointerdown',e=>{if(!S.window)return;pause();timeline.setPointerCapture(e.pointerId);drag={x:e.clientX,pan:e.shiftKey||e.button===1,window:[...S.window]};if(!drag.pan)seek(S.window[0]+fraction(e)*(S.window[1]-S.window[0]));});
timeline.addEventListener('pointermove',e=>{if(!drag)return;if(drag.pan){const dt=(e.clientX-drag.x)/timeline.clientWidth*(drag.window[1]-drag.window[0]);S.window=clampWindow(drag.window[0]-dt,drag.window[1]-dt);drawTimeline();}else seek(S.window[0]+fraction(e)*(S.window[1]-S.window[0]));});
timeline.addEventListener('pointerup',()=>{drag=null;});timeline.addEventListener('pointercancel',()=>{drag=null;});
timeline.addEventListener('wheel',e=>{e.preventDefault();zoomTime(e.deltaY>0?1.3:1/1.3,fraction(e));},{passive:false});
timeline.addEventListener('keydown',e=>{if(['ArrowLeft','ArrowRight','Home','End',' '].includes(e.key))e.preventDefault();if(e.key==='ArrowLeft')step(-1);if(e.key==='ArrowRight')step(1);if(e.key==='Home')seek(S.range[0],{follow:true});if(e.key==='End')seek(S.range[1],{follow:true});if(e.key===' ')play();});
document.addEventListener('visibilitychange',()=>{if(document.hidden)pause();});
window.addEventListener('pagehide',()=>{pause();scene?.dispose();queryAbort?.abort();photoAbort?.abort();albumAbort?.abort();});
async function refreshStatus(){
  try{S.status=await get('history/status');const s=S.status;$('indexStatus').textContent=`${s.runs} 运行 / ${s.photos} 照片${s.indexing?' · 索引中':''}${s.coverage_issues.length?' · '+s.coverage_issues.reduce((n,i)=>n+i.n,0)+' 来源需留意':''}`;
    $('indexStatus').title=JSON.stringify({scan:s.scan,coverage:s.coverage_issues,roots:s.source_roots,error:s.error});
    const scan=s.scan;
    $('coverageText').textContent=scan?`已检查 ${scan.discovered} 个来源；${scan.errors} 个读取失败，${scan.skipped} 个未关联。另有 ${scan.unlinked_archives||0} 个归档缺少运行关联，${scan.ignored_aggregate_archives||0} 个聚合备份未重复导入。${scan.discovery_truncated?'发现预算已截断。':''}${!s.source_roots.evidence_available?'原始回执目录当前不可用。':''}${!s.source_roots.fieldtests_available?'过程归档目录当前不可用。':''}`:'首次索引进行中，完成后显示默认时间范围。';
    const latest=s.latest_motion_ms||s.end_ms;
    if(!S.range&&latest&&s.scan){range(Math.max(s.start_ms,latest-2*3600000),latest+1000);}else if(!S.range&&!s.indexing){message('尚无历史记录；可更新索引。');$('mapEmpty').textContent='尚无可回放的历史数据';}
  }catch(e){$('indexStatus').textContent='历史索引未响应';if(!S.range)message(e.message);}
}
$('refreshIndex').addEventListener('click',async()=>{try{const r=await fetch('/api/platform/fieldmap/history/refresh',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});if(!r.ok)throw Error(`刷新失败 ${r.status}`);message('已安排增量索引，原始记录保留；稍后重新查看这段。');refreshStatus();}catch(e){message(e.message);}});
$('latestRange').addEventListener('click',()=>{const latest=S.status?.latest_motion_ms||S.status?.end_ms;if(latest)range(Math.max(S.status.start_ms,latest-2*3600000),latest+1000);});
$('allRange').addEventListener('click',()=>{if(S.status?.end_ms)range(S.status.start_ms,S.status.end_ms+1000);});
$('todayRange').addEventListener('click',()=>{const d=new Date();d.setHours(0,0,0,0);range(d.valueOf(),d.valueOf()+86400000-1);});
$('snapshotSelect').addEventListener('change',()=>{sceneKey='';environment.load($('snapshotSelect').value);});
async function init(){
  // Keep scrubbing adjacent to the scene on a small laptop, with details below.
  const eventReview=document.querySelector('.event-review'),workspace=document.querySelector('.workspace');
  workspace.before($('timelinePanel'));workspace.after(eventReview);eventReview.classList.add('event-review-panel');
  $('timeZone').textContent=`时间：${Intl.DateTimeFormat().resolvedOptions().timeZone}（本机时区）`;
  mode('history');await refreshStatus();
  try{const result=await get('snapshots');$('snapshotSelect').replaceChildren(...result.items.map(s=>new Option(`${s.readonly?'历史':'当前'} · ${s.title}${s.at?' · '+date(new Date(s.at).valueOf()):''}`,s.id)));}catch(e){message('地图快照目录读取失败：'+e.message);}
  setInterval(refreshStatus,10000);
}
init();
