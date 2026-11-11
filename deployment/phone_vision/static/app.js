'use strict';
(() => {
  const api='/api/platform/phone-vision', ids=['iphone','android'];
  const labels={disabled:'未启用',starting:'连接中',stopping:'停止中',waiting:'等待手机',live:'实时',stale:'画面过期',error:'连接异常'};
  const $=id=>document.getElementById(id), view={}, busy=new Set();let polling=false,visible=true,appliedView='',lastStatus=null,spaceViewer=null,spaceBusy=false,spaceKey='',spaceEpoch=0,lastSpaceAt=0,spaceLiveExpiresAt=0;
  async function request(path,options){const r=await fetch(api+path,options);if(!r.ok){let d;try{d=await r.json();}catch{d={detail:'连接暂不可用'};}throw new Error(d.detail||'请求失败');}return r;}
  function hide(device){$(device+'-device').classList.remove('holding-frame');for(const kind of ['rgb','depth']){const img=$(device+'-'+kind);img.hidden=true;img.removeAttribute('src');$(device+'-'+kind+'-empty').hidden=false;}
    const old=view[device];if(old?.urls)old.urls.forEach(URL.revokeObjectURL);view[device]=null;
    if(device==='iphone')$('depth-readout').textContent='点击深度画面查看像素深度原值；距离精度尚待标定。';
    if(device==='android')$('android-depth-readout').hidden=true;}
  function waitFrame(device,note='等待更新'){
    const current=view[device];if(!current?.urls?.length){hide(device);return;}
    $(device+'-device').classList.add('holding-frame');
    $(device+'-reason').textContent=note+' · 保留上一帧（'+Math.max(0,(Date.now()-current.shownAt)/1000).toFixed(1)+'秒前）';
  }
  function facts(device,state){const node=$(device+'-facts');node.replaceChildren();const m=state.meta;
    const values=[['连接',state.transport==='wifi'?'Wi-Fi':'USB']];
    if(m)values.push(['彩色尺寸',(m.rgb_width||m.width)+' × '+(m.rgb_height||m.height)],['接收',m.fps==null?'—':m.fps+' fps'],['更新',Math.round(state.age_ms)+' ms']);
    if(m?.rgbd)values.push(['深度尺寸',m.width+' × '+m.height]);
    if(m?.relative_backlog_ms!=null)values.push(['流内积压',Math.round(m.relative_backlog_ms)+' ms']);
    if(device==='iphone'&&m)values.push(['数值深度',(m.positive_depth_fraction*100).toFixed(1)+'%'],['置信度图',m.confidence_available?'已提供':'未提供'],['深度来源',m.sensor_raw_depth===false?'Wi-Fi 色相还原':'USB 浮点数组']);
    if(device==='android'&&m?.android_mode==='arcore'){
      values.push(['跟踪',m.tracking],['深度',m.rgbd?'ARCore 算法':'等待新估计']);
      if(m.rgbd){values.push(['深度帧差',Math.round(m.depth_age_ms)+' ms'],['原始深度置信度',m.raw_depth_confidence_available?'已读取，仅对应原始深度':'未提供']);
        if(m.positive_depth_fraction!=null)values.push(['正值深度',(m.positive_depth_fraction*100).toFixed(1)+'%']);
        if(m.depth_02_to_5m_fraction!=null)values.push(['0.2—5米内',(m.depth_02_to_5m_fraction*100).toFixed(1)+'% · 未标定']);
        if(m.raw_depth_high_confidence_fraction!=null)values.push(['原始高置信覆盖',(m.raw_depth_high_confidence_fraction*100).toFixed(1)+'% · 不代表自动深度置信度']);}
    }
    for(const [key,value] of values){const div=document.createElement('div'),dt=document.createElement('dt'),dd=document.createElement('dd');dt.textContent=key;dd.textContent=value;div.append(dt,dd);node.append(div);}}
  function settings(state){const select=$('android-camera'),choices=state.camera_choices||[{id:'0',label:'后置主摄'}],signature=JSON.stringify(choices);
    if(select.dataset.choices!==signature){select.replaceChildren();for(const c of choices){const option=document.createElement('option');option.value=c.id;option.textContent=c.label;select.append(option);}select.dataset.choices=signature;appliedView='';lensPresets(choices);}
    const current=state.camera_id+':'+state.rotation_deg+':'+state.mode;if(current!==appliedView){select.value=state.camera_id||'0';$('android-rotation').value=String(state.rotation_deg||0);$('android-mode').value=state.mode||'camera';appliedView=current;}
    $('android-camera-name').textContent=state.mode==='arcore'?'ARCore 主摄':choices.find(c=>c.id===state.camera_id)?.label||'所选相机';
    $('android-view-apply').disabled=busy.has('android')||state.stopping;select.disabled=busy.has('android')||$('android-mode').value==='arcore';$('android-mode').disabled=busy.has('android');$('android-rotation').disabled=busy.has('android');
    for(const b of $('lens-presets').querySelectorAll('button')){b.disabled=busy.has('android')||state.stopping;b.setAttribute('aria-pressed',String(b.dataset.camera===state.camera_id&&b.dataset.mode===state.mode));}}
  async function images(device,state){if(view[device]?.sequence===state.sequence&&view[device]?.session===state.session)return;
    const token=state.session+':'+state.sequence;view[device]??={};view[device].loading=token;
    const kinds=state.meta.rgbd?['rgb','depth']:['rgb'];let urls=[];
    if(device==='android'){$('android-depth-figure').hidden=!state.meta.rgbd;$('android-depth-readout').hidden=!state.meta.rgbd;$('android-feeds').classList.toggle('feeds',Boolean(state.meta.rgbd));
      if(!state.meta.rgbd){$('android-depth').hidden=true;$('android-depth').removeAttribute('src');}}
    try{const blobs=await Promise.all(kinds.map(async kind=>{const r=await request('/'+device+'/frame.jpg?'+new URLSearchParams({session:state.session,seq:state.sequence,kind}));return r.blob();}));
      if(view[device]?.loading!==token||!visible)return;
      urls=blobs.map(URL.createObjectURL);
      await Promise.all(urls.map(url=>{const image=new Image();image.src=url;return image.decode();}));
      if(view[device]?.loading!==token||!visible){urls.forEach(URL.revokeObjectURL);return;}
      const old=view[device].urls||[];$(device+'-device').classList.remove('holding-frame');
      kinds.forEach((kind,i)=>{const img=$(device+'-'+kind);if(device==='android')img.parentElement.classList.toggle('portrait-feed',state.meta.height>state.meta.width);img.src=urls[i];img.hidden=false;$(device+'-'+kind+'-empty').hidden=true;});
      view[device]={session:state.session,sequence:state.sequence,urls,meta:state.meta,shownAt:Date.now(),expiresAt:state.expiresAt};old.forEach(URL.revokeObjectURL);
    }catch{urls.forEach(URL.revokeObjectURL);waitFrame(device,'等待完整的新画面');}}
  async function poll(){if(polling||!visible)return;polling=true;
    try{const askedAt=Date.now(),status=await (await request('/status')).json();lastStatus=status;$('host').textContent=status.host;
      const rear=status.devices.android.role==='rear';$('android-role').textContent=status.devices.android.mode==='arcore'?'ARCore 算法深度':rear?'后视与机体观察':'辅助视角';settings(status.devices.android);
      $('iphone-depth-caption').textContent=status.devices.iphone.transport==='wifi'?'Wi-Fi 编码还原 · 近似深度':'0.2—1.5 米 · 黑色为无数值区域';
      $('android-transport').textContent=(status.devices.android.transport==='wifi'?'Wi-Fi 回传':'USB 连接')+(status.devices.android.mode==='arcore'?' · ARCore RGB-D':' · 彩色视频');
      const order=[...ids].sort((a,b)=>(Number(status.devices[b].frame_available)*8+Number(status.devices[b].enabled)*4+Number(b==='android'))-(Number(status.devices[a].frame_available)*8+Number(status.devices[a].enabled)*4+Number(a==='android')));
      const sections=order.map(d=>$(d+'-device'));if(sections[0].nextElementSibling!==sections[1])sections[1].before(sections[0]);
      const ready=status.readiness.worker_python;$('notice').hidden=ready;if(!ready)$('notice').textContent='这台电脑尚未安装手机采集环境，请运行随项目提供的手机视觉安装包。';
      for(const device of ids){const s=status.devices[device],state=$(device+'-state');state.textContent=labels[s.phase]||s.phase;state.className='state '+s.phase;
        $(device+'-reason').textContent=s.reason;facts(device,s);$(device+'-device').classList.toggle('active',s.enabled);$(device+'-device').classList.toggle('inactive',!s.enabled);
        for(const b of document.querySelectorAll('[data-capture="'+device+'"]'))b.disabled=!s.frame_available||busy.has(device);
        s.expiresAt=askedAt+Math.max(0,status.freshness_limit_ms-(s.age_ms??status.freshness_limit_ms));
        for(const b of document.querySelectorAll('[data-device="'+device+'"]'))b.disabled=busy.has(device)||(b.dataset.action==='start'?(s.enabled||s.stopping||!ready||(device==='android'&&!status.readiness.android_tools)):!s.enabled);
        if(!s.frame_available)waitFrame(device,s.reason);else await images(device,s);}
      if($('space-source').value==='live'&&Date.now()-lastSpaceAt>1000)loadSpace();
    }catch(e){$('notice').hidden=false;$('notice').textContent=e.message;ids.forEach(d=>waitFrame(d,e.message));}finally{polling=false;}}
  document.addEventListener('click',async event=>{const b=event.target.closest('button[data-device]');if(!b)return;
    const device=b.dataset.device;if(busy.has(device))return;busy.add(device);b.disabled=true;
    try{await request('/'+device+'/'+b.dataset.action,{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});if(b.dataset.action==='stop')hide(device);}
    catch(e){$('notice').hidden=false;$('notice').textContent=e.message;}finally{busy.delete(device);await poll();}});
  $('android-view-apply').addEventListener('click',async()=>{if(busy.has('android'))return;busy.add('android');$('android-view-apply').disabled=true;hide('android');
    $('android-view-note').textContent='正在退出旧镜头并应用视角…';
    try{await request('/android/view',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({camera_id:$('android-camera').value,rotation_deg:Number($('android-rotation').value),mode:$('android-mode').value})});$('android-view-note').textContent='视角已设置；ARCore 模式需保持手机采集App在前台。';}
    catch(e){$('android-view-note').textContent=e.message;}finally{busy.delete('android');await poll();}});
  $('android-mode').addEventListener('change',()=>{const arcore=$('android-mode').value==='arcore';$('android-camera').disabled=arcore;if(arcore)$('android-camera').value='0';});
  for(const device of ids)$(device+'-depth').addEventListener('click',async event=>{const current=view[device];if(!current?.meta?.rgbd)return;
    if(Date.now()>=current.expiresAt){$(device==='iphone'?'depth-readout':'android-depth-readout').textContent='等待新深度帧，当前显示保留画面。';return;}
    const readout=$(device==='iphone'?'depth-readout':'android-depth-readout');
    const img=event.currentTarget,rect=img.getBoundingClientRect(),m=current.meta,scale=Math.min(rect.width/m.width,rect.height/m.height);
    const x=Math.floor((event.clientX-rect.left-(rect.width-m.width*scale)/2)/scale),y=Math.floor((event.clientY-rect.top-(rect.height-m.height*scale)/2)/scale);
    if(x<0||y<0||x>=m.width||y>=m.height)return;
    try{const d=await(await request('/'+device+'/depth?'+new URLSearchParams({session:current.session,seq:current.sequence,x,y}))).json();
      readout.textContent=d.numeric_valid?(device==='android'?`像素 (${x}, ${y}) · ARCore 估计 ${d.depth_m.toFixed(3)} 米 · 尚未标定`:
        d.sensor_raw_depth===false?`像素 (${x}, ${y}) · Wi-Fi 编码还原约 ${d.depth_m.toFixed(2)} 米 · 非原始传感器深度`:`像素 (${x}, ${y}) · 深度原值 ${d.depth_m.toFixed(3)} 米 · 尚未做距离误差标定`):`像素 (${x}, ${y}) 暂无数值深度。`;
    }catch(e){readout.textContent=e.message;}});
  function lensPresets(choices){
    const host=$('lens-presets');host.replaceChildren();
    for(const c of choices){const b=document.createElement('button');b.className='quiet';b.dataset.camera=c.id;b.dataset.mode='camera';b.textContent=({'0':'主摄','1':'前置','2':'超广角','3':'近摄'})[c.id]||c.label;b.title=c.label;
      const small=document.createElement('small');small.textContent='ID '+c.id;b.append(small);b.addEventListener('click',()=>{$('android-mode').value='camera';$('android-camera').value=c.id;$('android-view-apply').click();});host.append(b);}
    if(choices.some(c=>c.id==='0')){const b=document.createElement('button');b.textContent='主摄 · AR深度';b.dataset.camera='0';b.dataset.mode='arcore';b.addEventListener('click',()=>{$('android-mode').value='arcore';$('android-camera').value='0';$('android-view-apply').click();});host.append(b);}
  }
  async function saveObservation(device){
    const current=view[device];if(!current||Date.now()>=current.expiresAt)throw Error('请等待一张新鲜画面再保存');
    const state=lastStatus.devices[device],label=(device==='android'?'红米 · '+state.camera_id+'号镜头':'iPhone')+' · '+(current.meta.rgbd?'RGB-D':'彩色');
    const result=await(await request('/captures',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({device,session:current.session,sequence:current.sequence,label})})).json();
    $('gallery-message').textContent='已保存 '+label+' · '+new Date(result.saved_at).toLocaleString('zh-CN');await loadArchives();
    if(result.cloud_available){spaceEpoch++;$('space-source').value='captures:'+result.id;await loadSpace(true);}return result;
  }
  async function viewer(){if(!spaceViewer){const {PhoneSpace}=await import('/phone-vision/assets/space.js');spaceViewer=new PhoneSpace($('space-view'));}return spaceViewer;}
  async function loadSpace(force=false){
    if(spaceBusy)return;const source=$('space-source').value,epoch=spaceEpoch;let data,url,key;
    spaceBusy=true;lastSpaceAt=Date.now();
    try{
      if(source==='live'){
        const device=ids.find(d=>lastStatus?.devices[d].frame_available&&lastStatus.devices[d].meta?.rgbd);
        if(!device){if(spaceViewer)spaceViewer.empty();$('space-empty').hidden=false;$('space-facts').textContent='当前手机为彩色或暂无新深度；可选择已保存的成像空间。';return;}
        const s=lastStatus.devices[device];key=s.session+':'+s.sequence;if(!force&&spaceKey===key)return;
        const query=new URLSearchParams({session:s.session,seq:s.sequence,world:false});data=await(await request('/'+device+'/points?'+query)).json();url=api+'/'+device+'/frame.jpg?'+new URLSearchParams({session:s.session,seq:s.sequence});
        if(Date.now()>=s.expiresAt)return;spaceLiveExpiresAt=s.expiresAt;
      }else{
        key=source;if(!force&&spaceKey===key)return;const[kind,id]=source.split(':');data=await(await request('/evidence/'+kind+'/'+id+'/cloud.json')).json();url=api+'/evidence/'+kind+'/'+id+'/'+(kind==='models'?'preview.jpg':'rgb.jpg');
      }
      if(epoch!==spaceEpoch||source!==$('space-source').value)return;
      const scene=await viewer();if(epoch!==spaceEpoch)return;await scene.set(data,url);if(epoch!==spaceEpoch)return;
      spaceKey=key;$('space-empty').hidden=data.count>0;$('space-empty').textContent=data.count?'':'这一帧没有分析范围内的深度点';
      const quality=data.depth_quality,coverage=quality?' · 0.2—5米覆盖 '+(quality.analysis_range_fraction*100).toFixed(1)+'%':'';
      $('space-facts').textContent=(source==='live'?'当前深度 · ':'已保存的场景 · ')+(data.count??data.positions.length/3).toLocaleString('zh-CN')+' 点 · '+(data.coordinates==='arcore_world_opengl'?'AR会话空间':'相机空间')+coverage+' · 估计尺度，未标定';
    }catch(e){$('space-empty').hidden=false;$('space-empty').textContent=e.message;$('space-facts').textContent='空间结果暂不可用，彩色画面与保存的照片仍可查看。';}
    finally{spaceBusy=false;if(epoch!==spaceEpoch)queueMicrotask(()=>loadSpace(true));}
  }
  async function loadArchives(){
    try{
      const[photos,models]=await Promise.all(['/captures','/models'].map(async path=>(await request(path)).json())),select=$('space-source'),prior=select.value;
      select.replaceChildren(new Option('当前手机深度','live'));
      for(const[kind,rows]of [['models',models.items],['captures',photos.items.filter(p=>p.cloud_available)]])for(const row of rows){select.add(new Option((kind==='models'?'场景存档 · ':'单次观察 · ')+row.label+' · '+new Date(row.saved_at).toLocaleString('zh-CN'),kind+':'+row.id));}
      if([...select.options].some(o=>o.value===prior))select.value=prior;
      if(prior==='live'&&models.items.length&&!ids.some(d=>lastStatus?.devices[d].meta?.rgbd))select.value='models:'+models.items[0].id;
      $('capture-gallery').replaceChildren();
      for(const[kind,rows]of [['models',models.items],['captures',photos.items]])for(const row of rows){
        const b=document.createElement('button');b.className='capture-card';const im=document.createElement('img');im.src=api+'/evidence/'+kind+'/'+row.id+'/'+(kind==='models'?'preview.jpg':'rgb.jpg');im.alt=row.label;im.loading='lazy';
        const title=document.createElement('strong');title.textContent=row.label;const small=document.createElement('small');small.textContent=new Date(row.saved_at).toLocaleString('zh-CN')+' · 历史记录'+(row.point_count?' · '+row.point_count.toLocaleString('zh-CN')+'点':row.meta?.rgbd?' · 分析范围内无深度点':'');b.append(im,title,small);
        b.addEventListener('click',()=>{if(kind==='models'||row.cloud_available){select.value=kind+':'+row.id;spaceEpoch++;spaceKey='';loadSpace(true);$('space-title').scrollIntoView({behavior:'smooth',block:'start'});}else{const a=document.createElement('a');a.href=im.src;a.target='_blank';a.rel='noopener';a.click();}});$('capture-gallery').append(b);
      }
      await loadSpace();
    }catch(e){$('gallery-message').textContent=e.message;}
  }
  document.addEventListener('click',async e=>{const save=e.target.closest('[data-capture]'),full=e.target.closest('[data-fullscreen]');if(full)$(full.dataset.fullscreen).parentElement.requestFullscreen?.();if(save){save.disabled=true;try{await saveObservation(save.dataset.capture);}catch(error){$('gallery-message').textContent=error.message;}finally{await poll();}}});
  $('space-source').addEventListener('change',()=>{spaceEpoch++;spaceKey='';spaceViewer?.empty();loadSpace(true);});
  $('space-refresh').addEventListener('click',()=>loadSpace(true));$('space-reset').addEventListener('click',()=>spaceViewer?.reset());$('space-size').addEventListener('input',e=>spaceViewer?.pointSize(Number(e.target.value)));
  $('gallery-refresh').addEventListener('click',loadArchives);$('space-save').addEventListener('click',async()=>{try{await saveObservation(ids.find(d=>view[d]?.meta?.rgbd)||'android');}catch(e){$('gallery-message').textContent=e.message;}});
  $('space-view').addEventListener('space-error',e=>{$('space-empty').hidden=false;$('space-empty').textContent=e.detail;});
  setInterval(()=>{for(const device of ids)if(view[device]&&Date.now()>=view[device].expiresAt)waitFrame(device);if($('space-source').value==='live'&&spaceLiveExpiresAt&&Date.now()>=spaceLiveExpiresAt){spaceViewer?.empty();spaceLiveExpiresAt=0;$('space-empty').hidden=false;$('space-empty').textContent='等待新的深度帧';}},100);
  document.addEventListener('visibilitychange',()=>{visible=!document.hidden;if(!visible)ids.forEach(hide);else poll();});
  window.addEventListener('pagehide',()=>ids.forEach(hide));poll().then(loadArchives);setInterval(poll,400);
})();
