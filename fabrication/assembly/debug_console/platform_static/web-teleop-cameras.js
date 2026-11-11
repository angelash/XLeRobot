'use strict';
// One wall owns its preview refreshes. Motion control remains in web-teleop.js.
window.TeleopCameras = (() => {
  const $ = id => document.getElementById(id), storageKey = 'xlerobot.teleop.views.v2';
  const sources = [{id:'head',name:'头部',camera:0},{id:'left',name:'左腕',camera:2},
    {id:'right',name:'右腕',camera:1},{id:'android',name:'手机主摄'},{id:'iphone',name:'iPhone'}];
  let visible = true, saved = {}, phoneState = null, phoneBusy = false, phoneChange = false, desiredLens = '0';
  try {saved = JSON.parse(localStorage.getItem(storageKey)) || {};} catch {}
  const views = sources.map(s => ({...s,open:typeof saved[s.id]==='boolean'?saved[s.id]:s.id!=='iphone',
    busy:false,version:0,url:null,key:null,expires:0,abort:null}));
  let releaseInput = () => {};

  function persist(){try {localStorage.setItem(storageKey,JSON.stringify(Object.fromEntries(views.map(v=>[v.id,v.open]))));}catch{}}
  function clear(v, note='等待新画面'){
    const node=$(v.id+'-view');if(!node)return;
    node.querySelector('img').hidden=true;node.querySelector('img').removeAttribute('src');
    node.querySelector('[data-empty]').hidden=false;node.querySelector('[data-empty-text]').textContent=note;
    if(v.url)URL.revokeObjectURL(v.url);v.url=null;v.key=null;v.expires=0;
    node.querySelector('[data-state]').textContent=note;node.querySelector('[data-state]').classList.remove('live');
  }
  function inactive(v){v.version++;v.abort?.abort();v.abort=null;clear(v,'已折叠');}
  function layout(){
    for(const v of views){$(v.id+'-view').hidden=!v.open;document.querySelector(`[data-view="${v.id}"]`).setAttribute('aria-pressed',String(v.open));}
    const count=views.filter(v=>v.open).length;
    $('camera-grid').style.setProperty('--views',Math.max(1,count));$('camera-grid').hidden=!count;
    $('camera-count').textContent=`${count} 路展开`;$('camera-empty').hidden=!!count;
    $('camera-collapse').textContent=count?'全部折叠':'恢复默认';
  }
  function toggle(v){releaseInput();v.open=!v.open;if(!v.open)inactive(v);persist();layout();if(v.open)refresh(v);}
  function mount(onInteract){
    releaseInput=onInteract;
    $('camera-selectors').replaceChildren();$('camera-grid').replaceChildren();
    for(const v of views){
      const selector=document.createElement('button');selector.dataset.view=v.id;selector.textContent=v.name;selector.title='展开或折叠'+v.name;selector.addEventListener('click',()=>toggle(v));$('camera-selectors').append(selector);
      const card=document.createElement('article');card.className='camera-card';card.id=v.id+'-view';
      card.innerHTML=`<header><h2>${v.name}${v.camera===undefined?'':'相机'}</h2><span>${v.camera===undefined?'PHONE':'UVC '+v.camera}</span><button title="折叠${v.name}" aria-label="折叠${v.name}">−</button></header><div class="camera-viewport"><img alt="${v.name}实时画面" hidden><div class="camera-placeholder" data-empty><span>◎</span><p data-empty-text>正在读取画面…</p></div></div><div class="camera-meta"><span data-state role="status">等待连接</span><span data-source>${v.camera===undefined?'USB / Wi-Fi':'640 × 480'}</span></div>`;
      card.querySelector('header button').addEventListener('click',()=>toggle(v));
      if(v.id==='android'){
        const options=document.createElement('div');options.className='camera-options';
        options.innerHTML='<select id="phone-lens" aria-label="手机镜头"><option value="0">主摄</option></select><button id="phone-apply" class="button" title="切换镜头或启用采集">启用手机</button>';
        options.querySelector('select').addEventListener('change',e=>{desiredLens=e.target.value;releaseInput();applyPhone();});
        options.querySelector('button').addEventListener('click',applyPhone);card.append(options);
      }
      if(v.id==='iphone'){
        const options=document.createElement('div');options.className='camera-options';options.innerHTML='<span class="subtle">前置 TrueDepth</span><button class="button">启用采集</button>';
        options.querySelector('button').addEventListener('click',()=>startPhone('iphone'));card.append(options);
      }
      $('camera-grid').append(card);
    }
    $('camera-collapse').addEventListener('click',()=>{
      releaseInput();const collapse=views.some(v=>v.open);
      for(const v of views){v.open=!collapse&&v.id!=='iphone';if(!v.open)inactive(v);}persist();layout();tick();
    });
    layout();setInterval(tick,250);setInterval(phonePoll,550);
    setInterval(()=>{for(const v of views)if(v.expires&&performance.now()>=v.expires)clear(v,'画面已过期，等待新帧');},100);
    tick();phonePoll();
  }
  async function request(path,payload){
    const response=await fetch('/api/platform/phone-vision'+path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload),signal:AbortSignal.timeout(14000)});
    if(!response.ok){const d=await response.json();throw Error(d.detail||'手机操作失败');}
    return response.json();
  }
  async function startPhone(device){
    releaseInput();if(phoneChange)return;phoneChange=true;const v=views.find(v=>v.id===device);inactive(v);
    try{await request('/'+device+'/start',{});}catch(e){clear(v,e.message);}finally{phoneChange=false;phonePoll();}
  }
  async function applyPhone(){
    releaseInput();if(phoneChange||!phoneState)return;
    phoneChange=true;const v=views.find(v=>v.id==='android');inactive(v);$('phone-apply').disabled=true;
    try{
      const s=phoneState.devices.android;
      if(s.camera_id!==desiredLens||s.mode==='arcore')await request('/android/view',{camera_id:desiredLens,rotation_deg:Number(s.rotation_deg||0),mode:'camera'});
      if(!s.enabled)await request('/android/start',{});
    }catch(e){clear(v,e.message);}finally{phoneChange=false;$('phone-apply').disabled=false;phonePoll();}
  }
  async function phonePoll(){
    if(!visible||document.hidden||phoneBusy||!views.some(v=>v.camera===undefined&&v.open))return;
    phoneBusy=true;const started=performance.now();
    try{
      const res=await fetch('/api/platform/phone-vision/status',{signal:AbortSignal.timeout(1600)});
      if(!res.ok)throw Error('手机状态暂不可用');
      const state=await res.json();if(!visible||document.hidden)return;phoneState=state;
      for(const v of views.filter(v=>v.camera===undefined&&v.open)){
        const s=state.devices[v.id];if(!s)continue;
        v.phone=s;v.phoneDeadline=started+Math.max(0,state.freshness_limit_ms-Number(s.age_ms||0));
        const node=$(v.id+'-view');node.querySelector('[data-source]').textContent=s.transport==='wifi'?'Wi-Fi':'USB';
        if(v.id==='android'){
          const choices=s.camera_choices?.length?s.camera_choices:[{id:'0',label:'主摄'}],select=$('phone-lens');
          const signature=JSON.stringify(choices);
          if(select.dataset.choices!==signature){select.replaceChildren(...choices.map(c=>{const o=document.createElement('option');o.value=c.id;o.textContent=({'0':'主摄','1':'前置','2':'超广角','3':'近摄'})[c.id]||c.label;return o;}));select.dataset.choices=signature;select.value=desiredLens;}
          const actual=choices.find(c=>c.id===s.camera_id)?.label||'主摄';node.querySelector('h2').textContent='手机 · '+actual;
          const mismatch=s.camera_id!==desiredLens;
          const arcore=s.mode==='arcore';
          $('phone-apply').textContent=mismatch?'切换镜头':arcore?'彩色主摄':s.enabled?'已启用':'启用手机';
          $('phone-apply').disabled=phoneChange||s.stopping||!mismatch&&s.enabled&&!arcore;select.disabled=phoneChange||s.stopping;
          if(mismatch){clear(v,'当前为'+actual+'，选择主摄后点击“切换镜头”');continue;}
        }
        if(!s.frame_available){clear(v,s.reason||'等待手机连接');continue;}
        refresh(v);
      }
    }catch(e){if(visible)for(const v of views.filter(v=>v.camera===undefined&&v.open))clear(v,e.message);}
    finally{phoneBusy=false;}
  }
  async function refresh(v){
    if(!visible||document.hidden||!v.open||v.busy||phoneChange&&v.camera===undefined)return;
    const isPhone=v.camera===undefined,s=v.phone;
    if(isPhone&&(!s?.frame_available||v.id==='android'&&s.camera_id!==desiredLens))return;
    v.busy=true;const version=v.version,started=performance.now(),abort=v.abort=new AbortController();
    const timer=setTimeout(()=>abort.abort(),1800);
    try{
      const url=isPhone?`/api/platform/phone-vision/${v.id}/preview.jpg`:`/api/platform/camera/${v.camera}/live`;
      const res=await fetch(url,{signal:abort.signal});
      if(!res.ok)throw Error(isPhone?'等待手机新帧':'等待相机新帧');
      const blob=await res.blob();
      if(isPhone&&res.headers.get('X-Phone-Session')!==s.session)return;
      const key=isPhone?s.session+':'+res.headers.get('X-Phone-Sequence'):res.headers.get('X-Captured-At')+':'+res.headers.get('X-Frame-Sequence');
      const ttl=isPhone?phoneState.freshness_limit_ms-Number(res.headers.get('X-Phone-Age-Ms')):Number(res.headers.get('X-Frame-Ttl-Ms'));
      const deadline=started+ttl;
      if(version!==v.version||!v.open||!visible||document.hidden||!Number.isFinite(ttl)||ttl<=0||performance.now()>=deadline)return;
      if(v.key===key){v.expires=Math.min(v.expires,deadline);return;}
      const previous=v.url;v.url=URL.createObjectURL(blob);v.key=key;v.expires=deadline;
      const node=$(v.id+'-view'),img=node.querySelector('img');img.src=v.url;img.hidden=false;node.querySelector('[data-empty]').hidden=true;
      if(previous)URL.revokeObjectURL(previous);
      const age=isPhone?res.headers.get('X-Phone-Age-Ms'):res.headers.get('X-Frame-Age-Ms');
      node.querySelector('[data-state]').textContent='● 实时 · '+Math.round(Number(age)||0)+' ms';
    }catch(e){if(version===v.version&&v.open&&visible&&(!v.expires||performance.now()>=v.expires))clear(v,e.name==='AbortError'?'连接较慢，等待新帧':e.message);}
    finally{clearTimeout(timer);if(v.abort===abort)v.abort=null;v.busy=false;}
  }
  function tick(){if(visible&&!document.hidden)views.filter(v=>v.open).forEach(refresh);}
  function setActive(value){visible=value;for(const v of views)if(!value)inactive(v);if(value){tick();phonePoll();}}
  document.addEventListener('visibilitychange',()=>{if(document.hidden)for(const v of views)inactive(v);else{tick();phonePoll();}});
  window.addEventListener('pagehide',()=>setActive(false));
  return {mount,setActive};
})();
