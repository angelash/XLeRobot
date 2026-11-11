/* Phone space is part of the observation archive. This module never drives. */
const base='/api/platform/fieldmap',phone='/api/platform/phone-vision';
const $=id=>document.getElementById(id),write=(id,v)=>{$(id).textContent=v;};
let space,spaceFailed=false,cloudKey='',displayedSource='',epoch=0,busy=false,liveEpoch=0,liveExpiry,lastLiveKey='',liveStamp='';
const libraryRows=new Map();
const percent=v=>typeof v==='number'?(v*100).toFixed(1)+'%':'—';
const when=v=>v?new Date(v).toLocaleString('zh-CN',{hour12:false}):'时间未记录';
const cellState=f=>f===null?'unseen':f<.5?'depth_gap':'estimated';
async function get(url,timeout=12000){const r=await fetch(url,{cache:'no-store',signal:AbortSignal.timeout(timeout)});if(!r.ok){const e=await r.json().catch(()=>({}));throw Error(e.detail||'空间记录暂不可读');}return r.json();}

async function renderer(){
  if(spaceFailed)return null;
  if(!space)try{const {PhoneSpace}=await import('/phone-vision/assets/space.js');space=new PhoneSpace($('phoneSpaceCanvas'));}
  catch{spaceFailed=true;$('phoneSpaceFallback').hidden=false;}
  return space;
}
$('phoneSpaceCanvas').addEventListener('space-error',()=>{spaceFailed=true;$('phoneSpaceFallback').hidden=false;});
function pixels(report){
  const grid=$('phonePixelGrid');grid.replaceChildren();
  if(!report){write('phoneDepthFraction','无数值深度');write('phoneDepthDetail','当前没有新鲜数值深度；已有照片与点云保留。');return;}
  write('phoneDepthFraction',percent(report.fraction)+' 范围内估计');
  for(const c of report.cells){const el=document.createElement('i');el.dataset.state=cellState(c.fraction);el.title=`第${c.row+1}行 / 第${c.col+1}列：${c.in_range}/${c.pixels}像素；缺失${c.missing}，过近${c.too_near}，过远${c.too_far}`;grid.append(el);}
  const missing=report.cells.reduce((a,c)=>a+c.missing,0),far=report.cells.reduce((a,c)=>a+c.too_far,0);
  write('phoneDepthDetail',`${report.width}×${report.height}深度格 · 无值 ${percent(missing/report.pixels)} · 超过5米 ${percent(far/report.pixels)}。原始深度高置信占比 ${percent(report.raw_high_confidence_fraction)}（与平滑深度分别统计）。`);
}
function clearLive(note){clearTimeout(liveExpiry);lastLiveKey='';write('phoneLiveState','已过期 · '+note);$('phoneLiveState').dataset.stale='true';write('phoneLiveStamp',liveStamp?'保留最后确认图：'+liveStamp+'；不用于当前通行判断。':'尚未取得通过时效检查的手机画面。');write('phoneGapTitle','先核对新鲜画面');write('phoneGapNote','当前手机帧已过期，所示照片与点阵作为上次观察保留；不据此发出行驶指令。');}
function image(id,url,version){const im=$(id);im.onload=()=>{if(version===liveEpoch)im.hidden=false;};im.onerror=()=>{if(version===liveEpoch)clearLive('本次图像未通过读取检查');};im.src=url;}
async function pollLive(){
  if(document.hidden){setTimeout(pollLive,1000);return;}
  try{
    const status=await get(phone+'/status'),s=status.devices.android;
    if(!s.frame_available){clearLive(s.phase==='live'?'源帧已过期':s.reason||s.phase||'手机未采集');}
    else{
      const key=s.session+':'+s.sequence;if(key===lastLiveKey)return;
      const q=new URLSearchParams({session:s.session,seq:s.sequence});
      const live=await get(base+'/phone-space/live?'+q);
      const version=++liveEpoch;lastLiveKey=key;
      liveStamp=new Date().toLocaleTimeString('zh-CN',{hour12:false})+' 校验 / #'+s.sequence;
      image('phoneLiveRgb',phone+'/android/frame.jpg?'+q,version);
      if(live.coverage)image('phoneLiveDepth',phone+'/android/frame.jpg?'+q+'&kind=depth',version);
      else{$('phoneLiveDepth').hidden=true;$('phoneLiveDepth').removeAttribute('src');}
      write('phoneLiveState','本次新帧 #'+s.sequence+' · 源龄 '+Math.round(s.age_ms||0)+'ms');
      $('phoneLiveState').dataset.stale='false';write('phoneLiveStamp',liveStamp+' · 后续过期时保留此图并标明状态。');
      clearTimeout(liveExpiry);liveExpiry=setTimeout(()=>clearLive('等待下一份新帧，保留空间存档'),Math.max(1,live.remaining_fresh_ms));
      write('phoneLens','镜头'+(live.meta.camera_id??'未知')+' / '+(live.meta.android_mode||'RGB')+' / '+(live.meta.rotation_deg||0)+'°');
      write('phoneDepthSize',live.coverage?live.meta.width+'×'+live.meta.height:'无数值深度');pixels(live.coverage);
      if(live.coverage?.regions?.length){const target=live.coverage.regions[0];write('phoneGapTitle',target.label+'优先复查');write('phoneGapNote',`当前新帧这里缺少范围内深度 ${percent(target.missing_fraction)}。先用普通照片和近地视角辨认遮挡，再补采；不能向空洞直接驶入。`);}
      else{write('phoneGapTitle','先恢复数值深度');write('phoneGapNote','当前仅有普通照片；用可见地面与原运动保护规划，不按历史点阵放行。');}
    }
  }catch(e){clearLive('当前帧未确认：'+e.message);write('phoneGapTitle','先取得新鲜观察');write('phoneGapNote','已保存点云继续展示，当前源帧未确认时不生成行驶依据。');}
  finally{setTimeout(pollLive,500);}
}
function angular(report){
  const grid=$('phoneAngularGrid');grid.replaceChildren();
  for(let e=5;e>=0;e--)for(let a=0;a<24;a++){
    const c=report.cells.find(c=>c.azimuth===a&&c.elevation===e),el=document.createElement('i');
    el.dataset.state=c?.state||'unseen';el.title=`会话方位 ${a*15}—${(a+1)*15}° / 仰角 ${e*30-90}—${(e+1)*30-90}°：${c?.view_frames||0}帧，范围内估计 ${percent(c?.fraction)}`;grid.append(el);
  }
  write('phoneCoverageScope',`${report.source_frames}帧同会话方向统计 · 只包含所选分卷。补采候选按最后机位附近0.5米内${report.target_source_frames}帧计算；位姿尺度未标定，未测房间覆盖率。`);
  const targets=$('phoneGapTargets');targets.replaceChildren();
  for(const t of report.targets.slice(0,3)){
    const row=document.createElement('div');row.className='phone-gap-target';
    const turn=Math.abs(t.relative_camera_deg)<12?'接近原视向':(t.relative_camera_deg<0?'向左':'向右')+Math.abs(t.relative_camera_deg).toFixed(0)+'°';
    row.textContent='存档候选视向 · '+turn;
    const meta=document.createElement('small');meta.textContent=t.state==='unseen'?'未采视向':'缺点 '+percent(t.missing_fraction);row.append(meta);targets.append(row);
  }
  if(!report.targets.length)targets.textContent='这一记录缺少有效同会话Pose，只保留照片与图像覆盖。';
}
async function loadSelection(force=false){
  const value=$('phoneSpaceSource').value;if(!value)return;
  const version=++epoch, [kind,identifier]=value.split(':');
  try{
    const row=libraryRows.get(value),initialKey=value+':'+row?.source_frames+':'+row?.point_count;
    if(row?.point_count&&(force||cloudKey!==initialKey)){
      write('phoneSpaceStatus',`正在显示${row.point_count.toLocaleString()}点，并从保留的源深度补算覆盖…`);
      const url=phone+'/evidence/'+kind+'/'+identifier+'/cloud.json',view=await renderer(),cloud=await get(url);
      if(version!==epoch)return;if(view)await view.set(cloud,phone+'/evidence/'+kind+'/'+identifier+'/'+(kind==='models'?'preview.jpg':'rgb.jpg'));
      if(version!==epoch)return;if(view&&displayedSource!==value)view.reset();displayedSource=value;cloudKey=initialKey;
    }
    const report=await get(base+'/phone-space/coverage?'+new URLSearchParams({kind,identifier}),45000);
    if(version!==epoch||value!==$('phoneSpaceSource').value)return;
    angular(report);
    const unavailable=report.unavailable_captures.length;
    write('phoneSpaceStatus',`${report.label} · ${report.point_count.toLocaleString()}展示点 / ${report.source_captures}组源观察 · 会话 ${(report.capture_session||'未记录').slice(-10)}${unavailable?' · '+unavailable+'组源深度不可读':''}。这是保存的空间估计，未配准轮估地图。`);
    write('phoneCloudCount',report.point_count.toLocaleString()+'点 · '+(report.kind==='models'?'同会话累积':'单帧观察'));
    $('phoneCloudRaw').hidden=!report.cloud_url;if(report.cloud_url)$('phoneCloudRaw').href=report.cloud_url;
    const currentCloudKey=value+':'+(kind==='models'?report.source_captures:undefined)+':'+report.point_count;
    if(force||cloudKey!==currentCloudKey){
      const view=await renderer();
      if(report.cloud_url){const cloud=await get(report.cloud_url);if(version!==epoch)return;if(view)await view.set(cloud,report.preview_url);}
      else if(view)view.empty();
      if(version!==epoch)return;if(view&&displayedSource!==value)view.reset();displayedSource=value;cloudKey=currentCloudKey;
    }
    const strip=$('phoneSourcePhotos');strip.replaceChildren();
    const sources=report.sources.slice(-80).reverse();write('phoneSourcesCount',`${report.sources.length}组RGB-D来源 · 展示最近${sources.length}张 · 点击原图`);
    for(const source of sources){const a=document.createElement('a');a.href=source.rgb_url;a.target='_blank';a.rel='noopener';a.title=when(source.saved_at)+' / 范围内深度 '+percent(source.fraction);const im=document.createElement('img');im.src=source.rgb_url;im.alt='点云来源照片 '+when(source.saved_at);im.loading='lazy';const label=document.createElement('span');label.textContent=when(source.saved_at).split(' ').slice(-1)[0]+' · '+percent(source.fraction);a.append(im,label);strip.append(a);}
  }catch(e){if(version!==epoch)return;write('phoneSpaceStatus','所选记录读取失败：'+e.message+'。旧资料保留。');}
}
async function refresh(){
  if(busy)return;busy=true;$('phoneSpaceRefresh').disabled=true;
  try{
    const library=await get(base+'/phone-space'),select=$('phoneSpaceSource'),prior=select.value;
    const options=[];
    for(const[kind,rows]of [['models',library.models],['captures',library.captures.filter(v=>v.meta?.rgbd)]])for(const row of rows){
      const label=(kind==='models'?'累积点云 · ':'单次RGB-D · ')+row.label+' / '+(row.point_count||0)+'点 / '+when(row.updated_at||row.saved_at)+' / '+row.id.slice(-5);
      options.push(new Option(label,kind+':'+row.id));
      libraryRows.set(kind+':'+row.id,row);
    }
    select.replaceChildren(...options);
    if(options.some(o=>o.value===prior))select.value=prior;
    else select.value=options.find(o=>o.value.startsWith('models:')&&library.models.find(m=>'models:'+m.id===o.value&&m.point_count>0))?.value||options[0]?.value||'';
    if(!options.length){write('phoneSpaceStatus','尚无已保存的手机点云或RGB-D，持续记录启用后会在这里累积。');return;}
    await loadSelection();
  }catch(e){write('phoneSpaceStatus','记录列表暂不可读：'+e.message);}
  finally{busy=false;$('phoneSpaceRefresh').disabled=false;}
}
async function motion(){
  if(!document.hidden)try{const s=await get(base+'/exploration');write('phoneMotionState',!s.enabled?'探索已停止':s.phase==='guarded_stop'?'保护停车 · '+s.note:(s.supervisor_connected?'监督探索 · ':'监督未连接 · ')+s.note);}catch{write('phoneMotionState','探索状态未确认');}
  setTimeout(motion,3000);
}
$('phoneSpaceRefresh').addEventListener('click',()=>refresh());
$('phoneSpaceSource').addEventListener('change',()=>loadSelection());
$('phoneSpaceReset').addEventListener('click',()=>space?.reset());
$('phonePointSize').addEventListener('input',()=>space?.pointSize(Number($('phonePointSize').value)));
refresh();pollLive();motion();setInterval(()=>{if(!document.hidden)refresh();},15000);
