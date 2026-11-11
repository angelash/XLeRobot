'use strict';
// Pure key mapping is shared by browser interaction tests.
const TeleopKeys = (() => {
  const common = {ArrowUp:'base.forward',ArrowDown:'base.back',ArrowLeft:'base.left',ArrowRight:'base.right',
    KeyI:'head.up',KeyJ:'head.left',KeyK:'head.down',KeyL:'head.right',KeyU:'arm.open',KeyO:'arm.close'};
  const integrated = {KeyW:'arm.up',KeyS:'arm.down',KeyA:'arm.retract',KeyD:'arm.extend',KeyQ:'arm.pan_left',
    KeyE:'arm.pan_right',KeyR:'arm.pitch_up',KeyF:'arm.pitch_down',KeyZ:'arm.roll_left',KeyC:'arm.roll_right'};
  function action(code, mode, joint) {
    if (common[code]) return common[code];
    if (mode === 'joints' && (code === 'BracketLeft' || code === 'BracketRight'))
      return `joint.${joint}.${code === 'BracketRight' ? 'plus' : 'minus'}`;
    return mode === 'integrated' ? integrated[code] : undefined;
  }
  const editing = node => Boolean(node?.closest?.('input,textarea,select,[contenteditable]:not([contenteditable="false"]),[role="textbox"]'));
  return {action, editing};
})();
if (typeof module !== 'undefined') module.exports = TeleopKeys;

if (typeof document !== 'undefined') (() => {
  const $ = id => document.getElementById(id);
  const labels = ['肩水平','肩抬升','肘关节','腕俯仰','腕旋转','夹爪'];
  const keys = new Set(), pointers = new Map();
  let token = null, seq = 0, arm = 'left', mode = 'integrated', joint = 1, status = null;
  let inFlight = 0, starting = false, generation = 0, cameraOn = false, pollBusy = false;
  let lastStatus = 0, ownedAt = 0, cameraSeq = -1, cameraBusy = false, cameraBlob = null, cameraGeneration = 0;
  let lastReason = '进入页面只读取状态，开启后按住才运动。';
  let pageActive = window.parent === window || window.parent.location.hash.split('/')[0] === '#teleop';

  async function api(path, payload, keepalive = false) {
    const res = await fetch(path, payload === undefined ? {signal:AbortSignal.timeout(2500)} : {
      method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(payload), keepalive,
      ...(keepalive ? {} : {signal:AbortSignal.timeout(path.endsWith('/start') ? 12000 : 1800)})});
    const data = await res.json();
    if (!res.ok) throw new Error(typeof data.detail === 'string' ? data.detail : '控制请求未通过');
    return data;
  }
  function actions() {
    return [...new Set([...keys].map(k => TeleopKeys.action(k,mode,joint)).filter(Boolean).concat([...pointers.values()].map(p=>p.action)))].sort();
  }
  function paintHeld() {
    const active = actions();
    document.querySelectorAll('[data-motion]').forEach(b => b.classList.toggle('pressed',active.includes(b.dataset.motion)));
    $('held-keys').textContent = active.length ? active.join(' · ') : '全部松开';
  }
  function clearHeld() {
    keys.clear(); pointers.clear(); paintHeld();
  }
  function release() { clearHeld(); if(token) send(true); }
  function end(reason = '已结束控制，保持当前位置') {
    generation++; const previous = token; token = null;
    const ending = generation;
    clearHeld(); lastReason = reason; render();
    if(previous) api('/api/web/stop',{session_id:previous},true).then(data => {
      if(generation===ending && /失败|未确认/.test(data.reason)){lastReason=data.reason;render();}
    }).catch(() => {
      if(generation===ending){lastReason = '停止请求未获回执，等待控制端租约停车；请检查连接'; render();}
    });
  }
  async function send(urgent = false) {
    if (!token || (!urgent && inFlight)) return;
    if (!pageActive || document.hidden || Date.now()-lastStatus > 2500) {end('页面或遥测不可用，已结束控制');return;}
    const current = token, number = seq++;
    const packet = {session_id:current,seq:number,actions:actions(),arm,
      scale:keys.has('ShiftLeft')||keys.has('ShiftRight') ? .2 : Number($('speed').value)/100};
    inFlight++;
    try {
      const data = await api('/api/web/input',packet);
      if(token === current && number === seq-1 && !data.active) end(data.reason);
    } catch(e) {
      // A newer neutral packet can overtake an old held packet. Its stale-seq
      // rejection is expected and must not revoke the accepted release.
      if(token === current && number === seq-1) end('输入中断：'+e.message);
    } finally { inFlight--; }
  }
  function render() {
    const online = Boolean(status?.upstream_connected), remote = status?.runtime?.web_control;
    const owned = Boolean(token), occupied = status?.runtime?.controller?.controller === 'online';
    $('connection').textContent = online ? `${status.runtime.ports?.reduce((n,p)=>n+p.motors.length,0)||0} / 16 电机在线` : '控制服务离线';
    $('connection').className = 'pill ' + (online ? 'live' : 'error');
    $('signal').classList.toggle('live',owned);
    $('session-state').textContent = owned ? '网页控制已开启 · 按住才运动' : occupied ? '其他控制会话正在使用' : '尚未开启控制';
    $('session-detail').textContent = lastReason;
    $('start').disabled = !online || !$('confirm').checked || owned || occupied || starting || Boolean(status?.recording);
    $('end').disabled = !owned; $('confirm').disabled = owned || starting;
    document.querySelectorAll('[data-motion]').forEach(b => {
      const needsIK = ['arm.extend','arm.retract','arm.up','arm.down'].includes(b.dataset.motion);
      b.disabled = !owned || (needsIK && !ikReady());
    });
    const ports = status?.runtime?.control_settings?.logical_ports || {};
    $('arm-port').textContent = arm === 'both' ? '双臂 · 同向控制' : `${arm==='left'?'物理左臂':'物理右臂'} ${ports[arm]||''}`;
    $('ik-status').textContent = ikReady() ? '已读取联动参考；在当前限位内按住微调。' : '此臂联动参考未就绪；可使用逐关节按钮。';
    const readings = selectedMotors();
    document.querySelectorAll('[data-reading]').forEach(el => {
      const i=Number(el.dataset.reading), values=readings.filter(m=>m.id===i);
      el.textContent = values.length ? values.map(m=>`${m.side} ${m.position} · ${m.min}–${m.max}`).join(' / ') : '等待读取编码器';
    });
    const wheels = status?.runtime?.ports?.find(p=>p.port==='COM4')?.motors.filter(m=>m.id>=9)||[];
    $('base-telemetry').textContent = wheels.length ? wheels.map(m=>`轮${m.id} 目标 ${m.goal_velocity} · ${m.voltage}V · ${m.temperature}°C`).join(' / ') : '轮速与设备状态等待读取';
    if(remote?.blocked && Object.keys(remote.blocked).length && owned)
      $('session-detail').textContent = '部分设备未接入：'+Object.entries(remote.blocked).map(([k,v])=>k+' '+v).join('；');
  }
  function selectedMotors() {
    const ports = status?.runtime?.control_settings?.logical_ports || {};
    return (arm==='both'?['left','right']:[arm]).flatMap(s =>
      (status?.runtime?.ports?.find(p=>p.port===ports[s])?.motors||[]).map(m=>({...m,side:s==='left'?'左':'右'})));
  }
  function ikReady() {
    const ready = status?.runtime?.original_readiness || {};
    return (arm==='both'?['left','right']:[arm]).every(s=>ready[s+'_position']?.ready);
  }
  function selectArm(value) {
    release(); arm=value;
    document.querySelectorAll('[data-arm]').forEach(b=>{b.classList.toggle('selected',b.dataset.arm===arm);b.setAttribute('aria-pressed',b.dataset.arm===arm);});
    render();
  }
  function selectMode(value) {
    release(); mode=value;
    $('integrated-controls').hidden=mode!=='integrated'; $('joint-controls').hidden=mode!=='joints';
    document.querySelectorAll('[data-mode]').forEach(b=>{b.classList.toggle('selected',b.dataset.mode===mode);b.setAttribute('aria-selected',b.dataset.mode===mode);});
  }
  $('joint-rows').innerHTML=labels.map((name,i)=>`<div class="joint-row"><input type="radio" name="joint" id="joint-${i+1}" value="${i+1}" ${i===0?'checked':''} aria-label="选择${name}"><label for="joint-${i+1}">${name}<small data-reading="${i+1}">等待读取编码器</small></label><button class="motion" data-motion="joint.${i+1}.minus" aria-label="${name}负向">−</button><button class="motion" data-motion="joint.${i+1}.plus" aria-label="${name}正向">+</button></div>`).join('');
  $('confirm').addEventListener('change',render);
  $('start').addEventListener('click',async()=>{
    if($('start').disabled)return;
    const current=++generation; starting=true; clearHeld(); render();
    try {
      const result=await api('/api/web/start',{confirmed:$('confirm').checked});
      if(current!==generation || !pageActive || document.hidden) {
        await api('/api/web/stop',{session_id:result.session_id},true); return;
      }
      token=result.session_id;seq=0;lastReason=result.reason;ownedAt=lastStatus=Date.now();
      document.activeElement?.blur();document.body.focus({preventScroll:true});
      await send(true);
    } catch(e) {lastReason=e.message;}
    finally {starting=false;render();}
  });
  $('end').addEventListener('click',()=>end());
  $('stop').addEventListener('click',()=>{
    if(token || starting)end('已点击立即停止');
    else api('/api/controller/stop',{}).then(()=>{lastReason='已请求停止当前控制';render();}).catch(e=>{lastReason=e.message;render();});
  });
  $('release').addEventListener('click',release);
  document.querySelectorAll('[data-arm]').forEach(b=>b.addEventListener('click',()=>selectArm(b.dataset.arm)));
  document.querySelectorAll('[data-mode]').forEach(b=>b.addEventListener('click',()=>selectMode(b.dataset.mode)));
  document.querySelectorAll('[name=joint]').forEach(b=>b.addEventListener('change',()=>{release();joint=Number(b.value);}));
  $('speed').addEventListener('input',()=>{$('speed-value').textContent=`${Number($('speed').value)<=50?'慢速':'标准'} · ${$('speed').value}%`;release();});
  document.querySelectorAll('[data-motion]').forEach(b=>{
    b.addEventListener('pointerdown',e=>{
      if(!token || b.disabled || e.button!==0)return;
      e.preventDefault();b.setPointerCapture(e.pointerId);pointers.set(e.pointerId,{action:b.dataset.motion});paintHeld();send(true);
    });
    const up=e=>{if(pointers.delete(e.pointerId)){paintHeld();send(true);}};
    for(const event of ['pointerup','pointercancel','lostpointercapture'])b.addEventListener(event,up);
    b.addEventListener('contextmenu',e=>{e.preventDefault();release();});
    // Keyboard activation of a focused hold button never latches a movement.
  });
  function keydown(e) {
    if((token||starting) && (e.code==='Space'||e.code==='Escape')){e.preventDefault();end('键盘立即停止');return;}
    if(!token || e.repeat || TeleopKeys.editing(e.target))return;
    if(e.ctrlKey||e.altKey||e.metaKey){release();return;}
    if(['Digit1','Digit2','Digit3'].includes(e.code)){e.preventDefault();selectArm({Digit1:'left',Digit2:'right',Digit3:'both'}[e.code]);return;}
    const motion=TeleopKeys.action(e.code,mode,joint);
    if(motion || e.code.startsWith('Shift')){
      if(motion && ['arm.up','arm.down','arm.extend','arm.retract'].includes(motion) && !ikReady())return;
      e.preventDefault();keys.add(e.code);paintHeld();send(true);
    }
  }
  function keyup(e){if(keys.delete(e.code)){e.preventDefault();paintHeld();send(true);}}
  window.addEventListener('keydown',keydown,true);window.addEventListener('keyup',keyup,true);
  document.addEventListener('focusin',e=>{if(TeleopKeys.editing(e.target))release();});
  window.addEventListener('blur',()=>{if(token||starting)end('页面失去焦点，已结束控制');});
  document.addEventListener('visibilitychange',()=>{if(document.hidden){end('页面进入后台，已结束控制');closeCamera();}});
  window.addEventListener('pagehide',()=>{end('页面已离开');closeCamera();});
  window.addEventListener('message',e=>{
    if(e.origin!==location.origin||e.source!==window.parent||e.data?.type!=='studio-page')return;
    pageActive=e.data.active===true;
    if(!pageActive){end('已离开键盘遥控页');closeCamera();}
  });
  if(window.parent!==window) {
    window.parent.addEventListener('blur',()=>{if(token||starting)end('浏览器失去焦点，已结束控制');});
    window.parent.document.addEventListener('keydown',e=>{
      if((token||starting)&&(e.code==='Space'||e.code==='Escape')){e.preventDefault();end('键盘立即停止');}
    },true);
    window.parent.addEventListener('hashchange',()=>{
      pageActive=window.parent.location.hash.split('/')[0]==='#teleop';
      if(!pageActive){end('已切换工作区');closeCamera();}
    });
  }
  async function poll() {
    if(pollBusy||document.hidden||!pageActive)return;
    pollBusy=true;const requestedAt=Date.now();
    try {
      status=await api('/api/platform/status');lastStatus=Date.now();
      if(token && requestedAt>=ownedAt && !status.runtime?.web_control?.active)end(status.runtime?.web_control?.reason||'控制端已结束会话');
    } catch(e){status=null;if(token)end('遥测中断，已结束控制');}
    finally{pollBusy=false;render();}
  }
  function closeCamera(){
    cameraOn=false;cameraGeneration++;$('camera-image').hidden=true;$('camera-image').removeAttribute('src');
    if(cameraBlob)URL.revokeObjectURL(cameraBlob);cameraBlob=null;
    $('camera-placeholder').hidden=false;$('camera-toggle').textContent='打开画面';
  }
  async function cameraFrame() {
    if(!cameraOn||cameraBusy||!pageActive||document.hidden)return;
    cameraBusy=true;const version=cameraGeneration;
    try {
      const response=await fetch(`/api/platform/camera/${$('camera').value}/frame?after=${cameraSeq}`,{signal:AbortSignal.timeout(4500)});
      if(!response.ok)throw new Error('相机暂未提供新帧');
      const blob=await response.blob();
      if(!cameraOn||version!==cameraGeneration)return;
      const previous=cameraBlob;cameraBlob=URL.createObjectURL(blob);$('camera-image').src=cameraBlob;
      $('camera-image').hidden=false;$('camera-placeholder').hidden=true;if(previous)URL.revokeObjectURL(previous);
      cameraSeq=Number(response.headers.get('X-Frame-Seq')||-1);$('camera-status').textContent='已读取相机快照 · '+new Date().toLocaleTimeString();
    } catch(e){if(version===cameraGeneration){$('camera-image').hidden=true;$('camera-placeholder').hidden=false;$('camera-status').textContent=e.message;}}
    finally{cameraBusy=false;}
  }
  $('camera-toggle').addEventListener('click',()=>{if(cameraOn)closeCamera();else{cameraOn=true;cameraGeneration++;cameraSeq=-1;$('camera-toggle').textContent='关闭画面';cameraFrame();}});
  $('camera').addEventListener('change',()=>{const resume=cameraOn;closeCamera();if(resume){cameraOn=true;cameraSeq=-1;$('camera-toggle').textContent='关闭画面';cameraFrame();}});
  setInterval(()=>send(),100);setInterval(poll,1000);setInterval(cameraFrame,600);render();poll();
})();
