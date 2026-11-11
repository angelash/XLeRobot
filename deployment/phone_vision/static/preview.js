/* One shared, passive phone view for Studio workspaces. It never starts cameras. */
(() => {
  let active=false,busy=false,version=0,url=null,expires=0,chosen='auto';
  const host=()=>document.getElementById('phone-watch');
  function clear(){const h=host();if(!h)return;const img=h.querySelector('img');img.hidden=true;img.removeAttribute('src');h.querySelector('[data-phone-empty]').hidden=false;if(url)URL.revokeObjectURL(url);url=null;expires=0;}
  async function poll(){
    const h=host();if(!h||!active||document.hidden||busy)return;busy=true;const stamp=version,asked=Date.now(),abort=new AbortController(),timer=setTimeout(()=>abort.abort(),2000);
    try{
      const response=await fetch('/api/platform/phone-vision/status',{signal:abort.signal});if(!response.ok)throw Error('手机视觉服务暂不可用');const state=await response.json();
      const device=chosen==='auto'?['android','iphone'].find(d=>state.devices[d].frame_available)||'android':chosen,s=state.devices[device];
      h.querySelector('[data-phone-label]').textContent=(device==='android'?'红米':'iPhone')+' · '+(s.camera_choices?.find(c=>c.id===s.camera_id)?.label||'彩色与深度')+' · '+(s.transport==='wifi'?'Wi-Fi':'USB');
      h.querySelector('[data-phone-state]').textContent=s.frame_available?Math.round(s.age_ms)+' ms · 实时':s.reason;
      if(!s.frame_available){clear();h.querySelector('[data-phone-empty]').textContent=s.reason;return;}
      const result=await fetch('/api/platform/phone-vision/'+device+'/preview.jpg',{signal:abort.signal});if(!result.ok)throw Error('等待下一张新鲜手机画面');const blob=await result.blob();
      const deadline=asked+Math.max(0,state.freshness_limit_ms-Number(result.headers.get('X-Phone-Age-Ms')));
      if(stamp!==version||!active||document.hidden||Date.now()>=deadline)return;
      const next=URL.createObjectURL(blob),previous=url;url=next;expires=deadline;const img=h.querySelector('img');img.src=next;img.hidden=false;h.querySelector('[data-phone-empty]').hidden=true;if(previous)URL.revokeObjectURL(previous);
    }catch(e){if(stamp===version){clear();h.querySelector('[data-phone-state]').textContent=e.name==='AbortError'?'等待手机画面':e.message;}}
    finally{clearTimeout(timer);busy=false;}
  }
  window.PhoneVisionPreview={setActive(value){active=value;version++;const h=host();if(h)h.hidden=!value;if(!value)clear();else poll();}};
  function mount(){
    const workspace=document.getElementById('workspace');if(!workspace)return;
    if(!host()){
      const h=document.createElement('section');h.id='phone-watch';h.className='phone-watch';h.hidden=true;
      h.innerHTML='<header><div><strong data-phone-label>手机视角</strong><span data-phone-state>等待读取</span></div><div><select id="phone-watch-device" aria-label="手机预览来源"><option value="auto">当前可用手机</option><option value="android">红米</option><option value="iphone">iPhone</option></select><a href="#phones">镜头、成像与三维空间 ↗</a></div></header><details open><summary aria-label="折叠或展开手机画面">手机实时画面 ▾</summary><div class="phone-watch-viewport"><img alt="当前手机的实时彩色画面" hidden><p data-phone-empty>等待手机的新鲜画面</p></div></details>';
      workspace.prepend(h);
    }
    window.PhoneVisionPreview.setActive(['devices','control','teleop','fieldmap','grasp','vision','obstacle'].includes(location.hash.slice(1).split('/')[0]));
  }
  async function exploration(){
    if(document.hidden)return;let banner=document.getElementById('exploration-banner');const workspace=document.getElementById('workspace');if(!workspace)return;
    if(!banner){banner=document.createElement('section');banner.id='exploration-banner';banner.className='exploration-banner';banner.hidden=true;banner.innerHTML='<div><strong data-exploration-title>持续探索</strong><span data-exploration-note></span><small data-exploration-counts></small></div><button id="exploration-stop" type="button">■ 停止探索</button>';workspace.prepend(banner);
      banner.querySelector('button').addEventListener('click',async()=>{const b=banner.querySelector('button');b.disabled=true;try{const r=await fetch('/api/platform/fieldmap/exploration/stop',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});const data=await r.json();if(!r.ok||data.errors?.length)throw Error('已关闭探索，停车回执需核对：'+(data.errors||[]).join('；'));window.ExplorationState=data.state;banner.querySelector('[data-exploration-note]').textContent='已请求停止，查看实时轮组回读';}catch(e){banner.querySelector('[data-exploration-note]').textContent=e.message;}finally{exploration();}});
    }
    try{const r=await fetch('/api/platform/fieldmap/exploration');if(!r.ok)return;const s=await r.json();window.ExplorationState=s;banner.hidden=!s.session_id;banner.classList.toggle('stopped',!s.enabled);
      const labels={observing:'观察通路',planning:'选择路线',moving:'分段移动',mapping:'更新空间',updating_ui:'更新显示功能',guarded_stop:'保护停车，正在诊断',stopped:'已停止'};
      banner.querySelector('[data-exploration-title]').textContent='持续探索 · '+(s.enabled&&!s.supervisor_connected?'等待观察接续':labels[s.phase]||s.phase);
      banner.querySelector('[data-exploration-note]').textContent=s.note;banner.querySelector('[data-exploration-counts]').textContent=(s.translation_legs||0)+'段移动 / '+(s.turn_attempts||0)+'次转向 · 轮估路径 '+Number(s.wheel_path_m||0).toFixed(2)+'m';banner.querySelector('button').disabled=!s.enabled;
    }catch{}
  }
  document.addEventListener('change',e=>{if(e.target.id==='phone-watch-device'){chosen=e.target.value;version++;clear();poll();}});
  document.addEventListener('visibilitychange',()=>{version++;if(document.hidden)clear();else poll();});
  window.addEventListener('hashchange',mount);document.addEventListener('DOMContentLoaded',mount);mount();
  setInterval(poll,400);setInterval(()=>{if(expires&&Date.now()>=expires)clear();},100);setInterval(exploration,1500);exploration();
})();
