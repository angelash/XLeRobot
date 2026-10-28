const $ = id => document.getElementById(id);
let latestStatus = null, lastSeq = 0, refreshing = false, jointOptions = '';
let commandBusy = false;
const events = [];
const esc = value => String(value ?? '').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const timeText = value => value ? new Date(value).toLocaleTimeString('zh-CN') : '—';
const buttonNames = ['A','B','X','Y','LB','RB','View','Menu','LS','RS'];

async function api(path, body) {
  const response = await fetch(path, {method:body === undefined?'GET':'POST',cache:'no-store',
    headers:body === undefined?{}:{'Content-Type':'application/json'}, body:body === undefined?undefined:JSON.stringify(body)});
  const result = await response.json();
  if (!response.ok) throw new Error(typeof result.detail === 'string'?result.detail:JSON.stringify(result.detail));
  return result;
}
function setGate(id, good, title, detail) {
  const node=$(id);node.className=good?'ok':'blocked';
  node.querySelector('strong').textContent=title;node.querySelector('p').textContent=detail;
}
function renderMotor(port) {
  return `<section class="port"><div class="port-head"><strong>${esc(port.port)} · 1Mbps</strong><span>${port.error?esc(port.error):`${port.motor_count}个舵机`}</span></div>
    <table class="motor-table"><thead><tr><th>关节 / ID</th><th>扭矩</th><th>实际 → 目标</th><th>设备限位</th><th>电压</th><th>温度</th></tr></thead><tbody>${port.motors.map(m=>`<tr><td>${esc(m.label)} / ${m.id}</td><td>${m.torque?'ON':'OFF'}</td><td>${m.position} → ${m.goal}</td><td>${m.min}–${m.max}</td><td>${m.voltage.toFixed(1)}V</td><td>${m.temperature}°C</td></tr>`).join('')}</tbody></table></section>`;
}
function renderPad(pad, controller, age) {
  const connected=pad.connected && age!==null && age<500;
  $('pad-name').textContent=connected?pad.name:'原生控制通道未连接手柄';
  const lbIndex=pad.lb_index??4; const pressed=connected && !!pad.buttons?.[lbIndex];
  $('pad-state').textContent=!connected?'未连接':controller.controller!=='online'?'输入在线 · 尚未接管':pressed?'LB 按住 · 允许小步':'LB 松开 · 保持姿态';
  $('axes').textContent=`Axes ${pad.axes?.length||0}`;
  $('buttons').textContent=`按下 ${pad.buttons?.filter(Boolean).length||0} / ${pad.buttons?.length||0}`;
  $('input-source').textContent=`原生输入 · ${age===null?'—':age+'ms'}`;
  const rawButtons=pad.buttons?.length?pad.buttons:Array(17).fill(false);
  const labels = rawButtons.map((_,i)=>buttonNames[i]||(['LT','RT','十字键上','十字键下','十字键左','十字键右','Xbox'][i-10])||`按钮 ${i}`);
  const mapNames={a:'A',b:'B',x:'X',y:'Y',leftshoulder:'LB',rightshoulder:'RB',back:'View',start:'Menu',leftstick:'LS',rightstick:'RS',lefttrigger:'LT',righttrigger:'RT',guide:'Xbox',dpup:'十字键上',dpdown:'十字键下',dpleft:'十字键左',dpright:'十字键右'};
  Object.entries(pad.mapping||{}).forEach(([key,val])=>{if(/^b\d+$/.test(val))labels[Number(val.slice(1))]=mapNames[key]||key;});
  labels[lbIndex]='LB';
  const actions = {LB:'按住使能；松开停止并保持',View:'停止接管','十字键上':'当前关节正向一步','十字键下':'当前关节反向一步','十字键左':'上一个可用关节','十字键右':'下一个可用关节'};
  $('button-grid').innerHTML=labels.map((label,i)=>`<span class="${pad.buttons[i]?'pressed':''}"><b>${esc(label)} <small>B${i}</small></b><em>${!connected?'未连接':pad.unavailable_buttons?.includes(i)?'系统不提供':pad.buttons[i]?'按下':'松开'}</em><small>${esc(actions[label]||'只显示，未绑定动作')}</small></span>`).join('');
  const hat=pad.hat||[0,0];
  $('hat-state').innerHTML=`十字键 <b class="${hat[1]>0?'pressed':''}">↑</b><b class="${hat[1]<0?'pressed':''}">↓</b><b class="${hat[0]<0?'pressed':''}">←</b><b class="${hat[0]>0?'pressed':''}">→</b> X=${hat[0]} Y=${hat[1]}`;
  $('axis-grid').innerHTML=(pad.axes||[]).map((v,i)=>`<label>${esc(pad.axis_labels?.[i]||`轴 ${i}`)}<meter min="-1" max="1" value="${v}"></meter><b>${v.toFixed(3)}</b></label>`).join('');
  const left=pad.axes?.[0]||0, ly=pad.axes?.[1]||0, right=pad.axes?.[2]||0, ry=pad.axes?.[3]||0;
  $('stick-left').style.transform=`translate(${left*18}px,${ly*18}px)`;
  $('stick-right').style.transform=`translate(${right*18}px,${ry*18}px)`;
  $('expected-action').textContent=!connected?'原生输入尚未连接':controller.controller!=='online'?'手柄输入可见。先点击“启动受限控制”；按键日志仍会记录。':pressed?'LB已按住：十字键左右选关节，上下每次按下一步；摇杆暂不发动作。':'LB已松开：动作锁定，机械臂保留扭矩保持姿态。';
}
function render(s) {
  latestStatus=s;
  const online=s.ports.filter(p=>p.connected&&!p.error).length;
  const motors=s.ports.flatMap(p=>p.motors||[]), torque=motors.filter(m=>m.torque).length;
  const cameras=s.cameras.filter(c=>c.state==='在线').length;
  const c=s.controller, active=c.controller==='online', padReady=s.gamepad.connected && s.input_age_ms<500;
  $('ports-count').textContent=`${online} / 2`;$('motor-count').textContent=motors.length;$('camera-count').textContent=`${cameras} / 3`;
  $('ports-count').nextElementSibling.textContent=online===2?'双板已响应':s.error||'等待连接';
  $('summary-torque').textContent=active?(c.armed?'允许小步':'待按LB'):(torque?'保持姿态':'观测');
  $('summary-torque-note').textContent=`${torque}/${motors.length} 扭矩ON · ${active?'接管已启动':'接管未启动'}`;
  $('torque-state').textContent=`${torque} / ${motors.length} ON`;
  $('controller-state').textContent=active?'ONLINE':'OFFLINE';$('active-joint').textContent=c.selected_joint||'—';
  $('last-action').textContent=c.last_action||'—';$('controller-time').textContent=timeText(c.updated_at);
  $('mode-label').textContent=active?(c.armed?'ARMED / LB':'WAITING LB'):(torque?'TORQUE HOLD':'OBSERVE');
  $('ports').innerHTML=s.ports.map(renderMotor).join('');$('motor-time').textContent=timeText(s.updated_at);
  s.cameras.forEach(cam=>$('cam'+cam.camera).textContent=`${cam.state} · ${cam.frames}帧`);
  $('camera-time').textContent=`更新 ${timeText(s.updated_at)}`;
  setGate('gate-observe',motors.length===16,`${motors.length}/16 电机可读`,'相机与手臂控制独立；无视频订阅不阻断手臂小步');
  setGate('gate-calibrate',s.calibration.exists,s.calibration.exists?'设备限位已读取':'等待读取','这是实际寄存器快照；完整机械行程仍需人工确认，不使用虚构范围');
  const confirmed=$('wheels-lifted').checked&&$('map-confirmed').checked;
  const ready=online===2&&motors.length===16&&padReady&&s.calibration.exists&&confirmed;
  const blockers=[];
  if(online!==2||motors.length!==16)blockers.push('双板/16电机未齐');if(!padReady)blockers.push('原生手柄未在线');
  if(!s.calibration.exists)blockers.push('设备限位尚未读取');if(!confirmed)blockers.push('勾选小范围测试确认');if(s.error)blockers.push(s.error);
  setGate('gate-move',active||ready,active?(c.armed?'LB已使能':'接管在线，等待LB'):ready?'可以启动':'未启动',active?'十字键上下单步，松开LB停止并保持':blockers.join('；')||'点击启动；不会自动动作');
  $('blocker').textContent=active?'手柄接管已启动；仅在按住LB且方向键出现新按下时发送小步。':blockers.length?blockers.join(' · '):'前置条件通过。下一步：点击启动受限控制。';
  $('readiness-updated').textContent=s.version;
  $('start-controller').disabled=commandBusy||!ready||active;$('start-controller').textContent=active?'接管已启动':'启动受限控制';
  $('stop-controller').disabled=commandBusy||!active;
  $('torque-message').textContent=`${torque}个扭矩ON。关闭会失去支撑，请先托住机械臂。`;
  ['torque-all-on','torque-all-off','torque-one-on','torque-one-off'].forEach(id=>$(id).disabled=active||commandBusy);
  const options=(s.joints||[]).map(j=>`<option value="${j.port}:${j.id}" ${j.available?'':'disabled'}>${esc(j.label)} / ${j.port}:${j.id}${j.available?'':' — 限位待校准'}</option>`).join('');
  if(options!==jointOptions){$('joint-select').innerHTML=options;jointOptions=options;}
  const chosen=s.joints?.find(j=>j.label===c.selected_joint);
  if(chosen&&document.activeElement!==$('joint-select'))$('joint-select').value=`${chosen.port}:${chosen.id}`;
  $('joint-limits').textContent=chosen?`${chosen.label}：本轮 ${chosen.min}–${chosen.max}；单步 ${s.settings.step_raw}计数 ≈ ${(s.settings.step_raw*360/4096).toFixed(1)}°；${chosen.reason||'保留设备硬限位'}`:'启动后默认选择右肩水平；行程过窄的关节单独标明，底盘未开放。';
  renderPad(s.gamepad,c,s.input_age_ms);
}
async function command(path,payload={}) {
  commandBusy=true;$('controller-message').textContent='正在执行…';
  try {const out=await api(path,payload);$('controller-message').textContent=out.message||'已处理';}
  catch(e){$('controller-message').textContent='失败：'+e.message;}
  finally{commandBusy=false;await refresh();}
}
async function refresh(){
  if(refreshing)return;refreshing=true;
  try{
    render(await api('/api/status'));
    if($('controller-message').textContent.startsWith('服务异常：'))$('controller-message').textContent='连接已恢复';
    const result=await api('/api/events?after='+lastSeq);
    if(result.last_seq<lastSeq){events.length=0;lastSeq=0;}
    for(const event of result.events){events.unshift(event);lastSeq=Math.max(lastSeq,event.seq);}events.length=Math.min(events.length,120);
    $('diagnostic-log').innerHTML=events.map(e=>`<li class="event-${esc(e.kind)}"><time>${timeText(e.time)}</time><b>${esc(e.kind)}</b><span>${esc(e.message)}</span><code>${esc(JSON.stringify(Object.fromEntries(Object.entries(e).filter(([k])=>!['time','seq','kind','message'].includes(k)))))}</code></li>`).join('');
    $('event-log').innerHTML=events.filter(e=>['input','lb','blocked'].includes(e.kind)).slice(0,8).map(e=>`<li>${timeText(e.time)} ${esc(e.message)} ${e.hat?'十字键 '+esc(e.hat):''}</li>`).join('');
  } catch(e){$('controller-message').textContent='服务异常：'+e.message;}
  finally{refreshing=false;}
}
$('start-controller').onclick=()=>command('/api/controller/start',{confirmed:$('wheels-lifted').checked&&$('map-confirmed').checked});
$('stop-controller').onclick=()=>command('/api/controller/stop');
for(const [id,enabled,scope] of [['torque-all-on',true,'all'],['torque-all-off',false,'all'],['torque-one-on',true,'single'],['torque-one-off',false,'single']]){
  $(id).onclick=()=>command('/api/torque',{enabled,scope,port:$('torque-port').value,id:Number($('torque-id').value)});
}
$('read-calibration').onclick=()=>command('/api/calibration/read');
$('apply-settings').onclick=()=>command('/api/controller/settings',{step_raw:Number($('step-setting').value),speed_raw:Number($('speed-setting').value)});
$('joint-select').onchange=()=>{const [port,id]=$('joint-select').value.split(':');command('/api/controller/select',{port,id:Number(id)});};
for(const id of ['wheels-lifted','map-confirmed']){
  $(id).onchange=()=>{if(latestStatus)render(latestStatus);};
}
setInterval(refresh,400);
setInterval(()=>{if(!document.hidden)api('/api/controller/heartbeat',{}).catch(()=>{});},700);
refresh();
