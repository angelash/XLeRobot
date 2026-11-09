'use strict';
(() => {
  const api='/api/platform/phone-vision', ids=['iphone','android'];
  const labels={disabled:'未启用',starting:'连接中',stopping:'停止中',waiting:'等待手机',live:'实时',stale:'画面过期',error:'连接异常'};
  const $=id=>document.getElementById(id), view={}, busy=new Set();let polling=false,visible=true;
  async function request(path,options){const r=await fetch(api+path,options);if(!r.ok){let d;try{d=await r.json();}catch{d={detail:'连接暂不可用'};}throw new Error(d.detail||'请求失败');}return r;}
  function hide(device){for(const kind of (device==='iphone'?['rgb','depth']:['rgb'])){const img=$(device+'-'+kind);img.hidden=true;img.removeAttribute('src');$(device+'-'+kind+'-empty').hidden=false;}
    const old=view[device];if(old?.urls)old.urls.forEach(URL.revokeObjectURL);view[device]=null;
    if(device==='iphone')$('depth-readout').textContent='点击深度画面查看像素深度原值；距离精度尚待标定。';}
  function facts(device,state){const node=$(device+'-facts');node.replaceChildren();const m=state.meta;if(!m)return;
    const values=[['尺寸',m.width+' × '+m.height],['接收',m.fps==null?'—':m.fps+' fps'],['更新',Math.round(state.age_ms)+' ms']];
    if(device==='iphone')values.push(['数值深度',(m.positive_depth_fraction*100).toFixed(1)+'%'],['置信度图',m.confidence_available?'已提供':'未提供']);
    for(const [key,value] of values){const div=document.createElement('div'),dt=document.createElement('dt'),dd=document.createElement('dd');dt.textContent=key;dd.textContent=value;div.append(dt,dd);node.append(div);}}
  async function images(device,state){if(view[device]?.sequence===state.sequence&&view[device]?.session===state.session)return;
    const token=state.session+':'+state.sequence;view[device]??={};view[device].loading=token;
    const kinds=device==='iphone'?['rgb','depth']:['rgb'];let urls=[];
    try{const blobs=await Promise.all(kinds.map(async kind=>{const r=await request('/'+device+'/frame.jpg?'+new URLSearchParams({session:state.session,seq:state.sequence,kind}));return r.blob();}));
      if(view[device]?.loading!==token||!visible)return;
      urls=blobs.map(URL.createObjectURL);const old=view[device].urls||[];
      kinds.forEach((kind,i)=>{const img=$(device+'-'+kind);img.src=urls[i];img.hidden=false;$(device+'-'+kind+'-empty').hidden=true;});
      view[device]={session:state.session,sequence:state.sequence,urls,meta:state.meta,shownAt:Date.now()};old.forEach(URL.revokeObjectURL);
    }catch{urls.forEach(URL.revokeObjectURL);hide(device);}}
  async function poll(){if(polling||!visible)return;polling=true;
    try{const status=await (await request('/status')).json();$('host').textContent=status.host;
      const ready=status.readiness.worker_python;$('notice').hidden=ready;if(!ready)$('notice').textContent='这台电脑尚未安装手机采集环境，请运行随项目提供的手机视觉安装包。';
      for(const device of ids){const s=status.devices[device],state=$(device+'-state');state.textContent=labels[s.phase]||s.phase;state.className='state '+s.phase;
        $(device+'-reason').textContent=s.reason;facts(device,s);
        for(const b of document.querySelectorAll('[data-device="'+device+'"]'))b.disabled=busy.has(device)||(b.dataset.action==='start'?(s.enabled||s.stopping||!ready||(device==='android'&&!status.readiness.android_tools)):!s.enabled);
        if(!s.frame_available)hide(device);else await images(device,s);}
    }catch(e){$('notice').hidden=false;$('notice').textContent=e.message;ids.forEach(hide);}finally{polling=false;}}
  document.addEventListener('click',async event=>{const b=event.target.closest('button[data-device]');if(!b)return;
    const device=b.dataset.device;if(busy.has(device))return;busy.add(device);b.disabled=true;
    try{await request('/'+device+'/'+b.dataset.action,{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});if(b.dataset.action==='stop')hide(device);}
    catch(e){$('notice').hidden=false;$('notice').textContent=e.message;}finally{busy.delete(device);await poll();}});
  $('iphone-depth').addEventListener('click',async event=>{const current=view.iphone;if(!current?.meta)return;
    const img=event.currentTarget,rect=img.getBoundingClientRect(),m=current.meta,scale=Math.min(rect.width/m.width,rect.height/m.height);
    const x=Math.floor((event.clientX-rect.left-(rect.width-m.width*scale)/2)/scale),y=Math.floor((event.clientY-rect.top-(rect.height-m.height*scale)/2)/scale);
    if(x<0||y<0||x>=m.width||y>=m.height)return;
    try{const d=await(await request('/iphone/depth?'+new URLSearchParams({session:current.session,seq:current.sequence,x,y}))).json();
      $('depth-readout').textContent=d.numeric_valid?`像素 (${x}, ${y}) · 深度原值 ${d.depth_m.toFixed(3)} 米 · 尚未做距离误差标定`:`像素 (${x}, ${y}) 暂无数值深度。`;
    }catch(e){$('depth-readout').textContent=e.message;}});
  setInterval(()=>{for(const device of ids)if(view[device]?.shownAt&&Date.now()-view[device].shownAt>1000)hide(device);},200);
  document.addEventListener('visibilitychange',()=>{visible=!document.hidden;if(!visible)ids.forEach(hide);else poll();});
  window.addEventListener('pagehide',()=>ids.forEach(hide));poll();setInterval(poll,400);
})();
