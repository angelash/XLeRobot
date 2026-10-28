const $=id=>document.getElementById(id);
const esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const timeText=v=>v?new Date(v).toLocaleTimeString('zh-CN'):'—';
const degrees=n=>(Math.max(0,n)*360/4096).toFixed(1)+'°';
let latestStatus=null, refreshing=false, commandBusy=false, lastSeq=0, inspectedKey='lb', fullMap=false;
let jointOptions='', previousUiButtons=null, settingsRevision='';
let lastStatusAt=0,localNotice=null;
const htmlCache=new Map(), recentPress=new Map();
function html(id,value){if(htmlCache.get(id)!==value){$(id).innerHTML=value;htmlCache.set(id,value);}}
const events=[];
const kindLabels={input:'收到按键',axes:'输入变化',lb:'LB使能',select:'选择关节',command:'发送目标',feedback:'实测反馈',no_motion:'未见移动',limit:'触及边界',blocked:'本次未执行',fault_pause:'故障暂停',hold:'停止本次动作',controller_start:'开启接管',controller_stop:'结束接管',resume:'恢复动作',calibration:'读取限位',error:'错误',settings:'参数更新',startup:'服务启动'};
async function api(path,body){
 const r=await fetch(path,{method:body===undefined?'GET':'POST',cache:'no-store',headers:body===undefined?{}:{'Content-Type':'application/json'},body:body===undefined?undefined:JSON.stringify(body)});
 const d=await r.json();if(!r.ok)throw new Error(typeof d.detail==='string'?d.detail:JSON.stringify(d.detail));return d;
}
function setView(full){fullMap=full;$('view-simple').className=full?'':'selected';$('view-full').className=full?'selected':'';if(latestStatus)renderMapping(latestStatus);}
function mappingValue(m,s){
 if(m.group==='system')return '系统限制';
 const connected=s.gamepad.connected&&s.input_age_ms<500;if(!connected)return '未连接';
 if(m.axis!==null&&m.axis!==undefined){const v=s.gamepad.axes?.[m.axis]??0;return m.axis>=4?Math.round(v*100)+'%':v.toFixed(2);}
 return s.gamepad.buttons?.[m.index]?'按下':Date.now()-(recentPress.get(m.index)||0)<900?'刚按过 · 松开':'松开';
}
function mappingPressed(m,s){
 if(!s.gamepad.connected||s.input_age_ms===null||s.input_age_ms>=500)return false;
 if(m.axis!==null&&m.axis!==undefined)return Math.abs(s.gamepad.axes?.[m.axis]??0)>.15;
 return !!s.gamepad.buttons?.[m.index];
}
function inspect(key){inspectedKey=key;if(latestStatus)renderMapping(latestStatus);}
function renderMapping(s){
 const map=s.control_map||[];
 $('mapping-mode-note').textContent=fullMap?'完整模式 · 已绑定、未绑定与系统边界分别说明':'简明模式 · LB、十字键、停止动作与结束接管';
 const visible=fullMap?map:map.filter(m=>m.group==='control');
 html('mapping-list',visible.map(m=>`<button class="mapping-card ${mappingPressed(m,s)?'pressed':''} ${m.key===inspectedKey?'inspected':''}" data-map-key="${m.key}"><span class="card-head"><strong>${esc(m.name)}</strong><span class="input-pill">${esc(mappingValue(m,s))}</span></span><b>${esc(m.role)}</b><p>${esc(m.gesture)}</p><small>${esc(m.target==='当前选中关节'?s.controller.selected_joint:m.target)}</small></button>`).join(''));
 const m=map.find(m=>m.key===inspectedKey)||map[0];
 if(m)html('map-detail',`<h3>${esc(m.name)} · ${esc(m.role)} <span class="live-value">${esc(mappingValue(m,s))}</span></h3><dl><dt>怎么按</dt><dd>${esc(m.gesture)}</dd><dt>控制对象</dt><dd>${esc(m.target==='当前选中关节'?s.controller.selected_joint:m.target)}</dd><dt>预期结果</dt><dd>${esc(m.result)}</dd><dt>生效条件</dt><dd>${esc(m.requirement)}</dd></dl>`);
 document.querySelectorAll('.map-key').forEach(g=>{const m=map.find(m=>m.key===g.dataset.mapKey);g.classList.toggle('pressed',!!m&&mappingPressed(m,s));g.classList.toggle('recent',!!m&&s.gamepad.connected&&Date.now()-(recentPress.get(m.index)||0)<900);g.classList.toggle('inspected',g.dataset.mapKey===inspectedKey);});
}
function renderPad(s){
 const p=s.gamepad,c=s.controller,connected=p.connected&&s.input_age_ms<500,lb=connected&&!!p.buttons?.[p.lb_index??4];
 $('pad-name').textContent=connected?p.name:'原生手柄未连接 · 连接后此处会显示实时状态';
 $('pad-state').textContent=connected?'手柄在线':'手柄未连接';
 $('input-source').textContent=`XInput · ${s.input_age_ms??'—'}ms`;
 $('enable-strip').className='enable-strip'+(c.armed&&!c.paused?' active':'');
 $('lb-status').textContent=c.paused?'动作暂停 · 接管保留':c.controller!=='online'?'尚未开启接管':c.armed?'LB按住 · 允许小步':'LB松开 · 等待使能';
 $('expected-action').textContent=c.paused?'排除故障后松开LB、B与方向键，点“恢复动作”。':c.controller!=='online'?'先开启接管；按手柄仍会显示输入，但不会移动。':c.armed?'点按 ↑/↓ 移动一小步；←/→ 选关节；B停止本次动作。':'接管仍开启。按住LB后点按方向键，松开LB即保持姿态。';
 $('axes').textContent='摇杆及扳机只显示输入，未绑定移动';$('buttons').textContent=`${p.buttons?.filter(Boolean).length||0} 个按下`;
 if(s.control_profile?.startsWith('original_')){
  $('lb-status').textContent='当前为原版Xbox映射 · LB/RB切腕姿';
  $('expected-action').textContent='请进入原版整机控制页面。B控制头部，Menu暂停，View结束接管。';
  $('axes').textContent='摇杆和扳机已按原版映射绑定，不是旧单关节点按模式';
 }
 $('button-grid').innerHTML=(s.control_map||[]).filter(m=>m.index!==null&&m.index!==undefined).map(m=>`<span class="${mappingPressed(m,s)?'pressed':''}"><b>${esc(m.name)}</b><em>${esc(mappingValue(m,s))}</em></span>`).join('');
 $('axis-grid').innerHTML=Array.from({length:6},(_,i)=>{const v=p.axes?.[i]??0;return `<label>${esc(p.axis_labels?.[i]||['左X','左Y','右X','右Y','LT','RT'][i])}<b>${connected?v.toFixed(2):'—'}</b><meter min="${i>=4?0:-1}" max="1" value="${v}"></meter></label>`;}).join('');
 const h=p.hat||[0,0];$('hat-state').textContent=`十字键 X=${h[0]} Y=${h[1]}`;
 $('stick-left').style.transform=`translate(${(p.axes?.[0]||0)*9}px,${(p.axes?.[1]||0)*9}px)`;
 $('stick-right').style.transform=`translate(${(p.axes?.[2]||0)*9}px,${(p.axes?.[3]||0)*9}px)`;
}
function consumeInput(batch,allowNavigation){
 for(const e of batch){
  if(e.kind!=='input')continue;
  const buttons=e.buttons||[];
  if(e.connected&&previousUiButtons&&Date.now()-new Date(e.time).getTime()<1500&&allowNavigation&&!latestStatus?.control_profile?.startsWith('original_')){
   const edge=i=>buttons[i]&&!previousUiButtons[i];
   buttons.forEach((pressed,i)=>{if(pressed&&!previousUiButtons[i])recentPress.set(i,Date.now());});
   // Consume native event edges so even a short tap between HTTP polls works.
  if(edge(0)){$('feedback-card').scrollIntoView({behavior:'smooth',block:'center'});inspect('up');}
  if(edge(3))setView(!fullMap);
  if(edge(7)){setView(true);$('mapping-section').scrollIntoView({behavior:'smooth',block:'start'});}
  }
  previousUiButtons=e.connected?[...buttons]:null;
 }
}
function renderTarget(s){
 const chosen=s.joints?.find(j=>j.label===s.controller.selected_joint);
 const port=chosen?.port, id=chosen?.id;
 const m=s.ports.find(p=>p.port===port)?.motors.find(m=>m.id===id);
 $('active-joint').textContent=s.controller.selected_joint||'未选关节';
 $('joint-identity').textContent=chosen?`${port} · ID ${id} · ${chosen.available?'只控制这个关节':'限位待校准'}`:'等待设备返回关节信息';
 $('pose-position').textContent=m?.position??'—';$('pose-target').textContent=m?.goal??'—';
 $('travel-low').textContent=chosen?.min??'—';$('travel-high').textContent=chosen?.max??'—';
 const pct=chosen&&m?Math.min(100,Math.max(0,(m.position-chosen.min)/Math.max(1,chosen.max-chosen.min)*100)):0;
 $('travel-fill').style.width=pct+'%';$('travel-marker').style.left=`calc(${pct}% - 2px)`;
 $('remaining-negative').textContent='反向余量 '+(chosen&&m?degrees(m.position-chosen.min):'—');
 $('remaining-positive').textContent='正向余量 '+(chosen&&m?degrees(chosen.max-m.position):'—');
 $('joint-limits').textContent=chosen?`设备限位 ${chosen.device_min??chosen.min}–${chosen.device_max??chosen.max} · 本轮 ${chosen.min}–${chosen.max}。${chosen.reason||'触及边界只提示，接管不会结束。'}`:'启动后显示关节范围';
 $('step-preview').textContent=`${s.settings.step_raw}计数 / ${degrees(s.settings.step_raw)}`;
 $('speed-preview').textContent=`${s.settings.speed_raw}计数/秒`;
 const revision=JSON.stringify(s.settings);
 if(settingsRevision!==revision){
  if(document.activeElement!==$('step-setting'))$('step-setting').value=s.settings.step_raw;
  if(document.activeElement!==$('speed-setting'))$('speed-setting').value=s.settings.speed_raw;
  settingsRevision=revision;
 }
 $('apply-settings').disabled=!!s.controller.armed||commandBusy;
 $('read-calibration').disabled=!!s.controller.armed||commandBusy;
 document.querySelectorAll('[data-preset]').forEach(b=>b.disabled=!!s.controller.armed||commandBusy);
 const cmd=s.last_command,n=s.notice;
 $('feedback-card').className='feedback-card'+(s.controller.paused?' error':n?.level==='warning'?' warning':'');
 $('feedback-title').textContent=s.controller.paused?'故障暂停 · 接管保留':n?.code==='limit'?'触及边界 · 可以反向':cmd?({pending:'目标已发出 · 等待实测',feedback:cmd.reached?'编码器变化 · 已进入目标容差':'编码器变化 · 仍有目标偏差',no_motion:'未检测到位置变化',interrupted:'本次动作已停止',hold_unconfirmed:'停止指令尚未确认'})[cmd.outcome]||'最新结果':'尚无本次动作';
 $('last-action').textContent=n?.message||s.controller.last_action||'—';
 const stage=cmd?(cmd.outcome==='pending'?3:4):0;
 const finalStage={no_motion:'编码器未变化',interrupted:'本次已中断',hold_unconfirmed:'保持未确认'}[cmd?.outcome]||'编码器反馈';
 $('command-track').innerHTML=['收到输入','动作放行','目标发送',finalStage].map((v,i)=>`<span class="${i<stage?'done':i===stage?'waiting':''}">${v}</span>`).join('');
 $('feedback-values').textContent=cmd?`${cmd.port!==port||cmd.id!==id?'上一关节 · ':''}${cmd.label||''} ${cmd.port}:${cmd.id} · 起点 ${cmd.origin} → 目标 ${cmd.target}${cmd.position!==undefined?' · 实测 '+cmd.position+' · 偏差 '+Math.abs(cmd.target-cmd.position)+'计数':''} · ${timeText(cmd.time)}`:'写入目标与实际移动分别显示。';
 html('joint-cards',(s.joints||[]).map(j=>`<button data-joint="${j.port}:${j.id}" class="joint-card ${j.label===s.controller.selected_joint?'active':''} ${j.available?'':'unavailable'}"><strong>${esc(j.label)}</strong><small>${j.port}:${j.id} · ${j.available?'范围 '+j.min+'–'+j.max:'限位待校准 · 查看原因'}</small></button>`).join(''));
 const options=(s.joints||[]).map(j=>`<option value="${j.port}:${j.id}" ${j.available?'':'disabled'}>${esc(j.label)} / ${j.port}:${j.id}${j.available?'':' · 限位待校准'}</option>`).join('');
 if(jointOptions!==options){$('joint-select').innerHTML=options;jointOptions=options;}
 if(chosen&&document.activeElement!==$('joint-select'))$('joint-select').value=`${port}:${id}`;
}
function render(s){
 latestStatus=s;const motors=s.ports.flatMap(p=>p.motors||[]),online=s.ports.filter(p=>p.connected&&!p.error).length;
 const torque=motors.filter(m=>m.torque).length,c=s.controller,active=c.controller==='online',paused=!!c.paused;
 const padReady=s.gamepad.connected&&s.input_age_ms!==null&&s.input_age_ms<500;
 const confirmed=$('wheels-lifted').checked&&$('map-confirmed').checked;
 const lbHeld=!!s.gamepad.buttons?.[s.gamepad.lb_index??4];
 const ready=online===2&&motors.length===16&&padReady&&s.calibration.exists&&confirmed&&!lbHeld;
 $('controller-state').textContent=active?(paused?'接管保留 · 动作暂停':'接管已开启'):'接管未开启';
 $('session-description').textContent=active?(paused?c.pause_reason:'由你主动结束。松开LB或触限只影响当前动作，接管继续保留。'):'开启接管不会自动移动；按住LB后，方向键才允许发送小步。';
 if(active&&s.control_profile?.startsWith('original_'))$('session-description').textContent='原版Xbox接管正在运行，请进入原版整机控制页；旧单关节说明不适用。';
 $('start-controller').hidden=active;$('start-controller').disabled=commandBusy||!ready;
 $('stop-controller').disabled=commandBusy||!active;$('resume-controller').hidden=!paused||!active;
 $('resume-controller').disabled=commandBusy||!padReady||!!s.gamepad.buttons?.[4]||!!s.gamepad.buttons?.[1]||s.gamepad.hat?.some(Boolean);
 $('ports-count').textContent=`控制板 ${online}/2`;$('motor-count').textContent=`舵机 ${motors.length}/16`;
 $('camera-count').textContent=`相机 ${s.cameras.filter(c=>c.state==='在线').length}/3`;
 $('summary-torque').textContent=`扭矩 ${torque}/${motors.length} ON`;
 $('readiness-updated').textContent=s.version;$('controller-time').textContent=timeText(c.updated_at);
 $('torque-state').textContent='扭矩保持与接管独立';$('torque-message').textContent=`${torque}个电机扭矩开启。接管运行时请先结束接管再改扭矩。`;
 ['torque-all-on','torque-all-off','torque-one-on','torque-one-off'].forEach(id=>$(id).disabled=active||commandBusy);
 const blockers=[];if(online!==2||motors.length!==16)blockers.push('双板/电机未齐');if(!padReady)blockers.push(s.gamepad.connected?'手柄输入过期':'手柄未连接');if(!s.calibration.exists)blockers.push('限位未读取');
 if(!$('wheels-lifted').checked)blockers.push('确认机械臂周围安全、线路无拉扯');
 if(!$('map-confirmed').checked)blockers.push('确认仅做现有限位内的小范围测试');
 if(lbHeld)blockers.push('先松开LB，再开启接管');
 $('blocker').textContent=active?'':commandBusy?'正在处理操作…':blockers.length?'尚未开启：'+blockers.join('；'):'条件已满足，可以点击“开启受限接管”';
 $('start-controller').title=blockers.length?blockers.join('；'):'开启不会自动移动，之后按住LB使能';
 $('wheels-lifted').disabled=active;$('map-confirmed').disabled=active;
 const notice=paused?{level:'error',message:c.pause_reason}:localNotice||s.notice;
 $('session-notice').hidden=!notice;$('session-notice').className='notice'+(notice?.level==='error'?' error':'');
 $('session-notice').textContent=notice?.message||'';
 $('motor-time').textContent=timeText(s.updated_at);
 $('ports').innerHTML=s.ports.map(p=>`<section><div class="port-head"><strong>${esc(p.port)}</strong><span>${p.motor_count}个舵机</span></div><table class="motor-table"><thead><tr><th>关节</th><th>扭矩</th><th>实际 → 目标</th><th>设备限位</th><th>电压 / 温度</th></tr></thead><tbody>${p.motors.map(m=>`<tr><td>${esc(m.label)} / ${m.id}</td><td>${m.torque?'ON':'OFF'}</td><td>${m.position} → ${m.goal}</td><td>${m.min}–${m.max}</td><td>${m.voltage}V / ${m.temperature}°C</td></tr>`).join('')}</tbody></table></section>`).join('');
 s.cameras.forEach(cam=>$('cam'+cam.camera).textContent=`${cam.state} · ${cam.frames}帧`);
 renderTarget(s);renderPad(s);renderMapping(s);
}
function renderEvents(){
 const filter=$('log-filter').value||'important';
 const keep={input:['input','axes','lb'],motion:['command','feedback','no_motion','select','hold'],warning:['limit','blocked','fault_pause','no_motion','error']};
 const shown=events.filter(e=>filter==='all'||(filter==='important'?!['input','axes'].includes(e.kind):keep[filter]?.includes(e.kind)));
 $('log-count').textContent=`当前 ${shown.length} 条 · 本页保留最近120条`;
 const opened=new Set([...$('diagnostic-log').querySelectorAll('details[open]')].map(d=>d.dataset.seq));
 html('diagnostic-log',shown.map(e=>`<li class="event-${esc(e.kind)}"><time>${timeText(e.time)}</time><span class="event-kind">${esc(kindLabels[e.kind]||e.kind)}</span><span>${esc(e.message)}</span><details data-seq="${e.seq}" ${opened.has(String(e.seq))?'open':''}><summary>原始字段</summary><code>${esc(JSON.stringify(Object.fromEntries(Object.entries(e).filter(([k])=>!['time','seq','kind','message'].includes(k))),null,2))}</code></details></li>`).join(''));
}
async function refresh(){if(refreshing)return;refreshing=true;
 try{render(await api('/api/status'));lastStatusAt=Date.now();const result=await api('/api/events?after='+lastSeq);if(result.last_seq<lastSeq){lastSeq=0;events.length=0;previousUiButtons=null;}
 consumeInput(result.events,lastSeq>0);renderMapping(latestStatus);
 for(const e of result.events){events.unshift(e);lastSeq=Math.max(lastSeq,e.seq);}events.length=Math.min(events.length,120);renderEvents();
 }catch(e){lastStatusAt=0;$('start-controller').disabled=true;$('resume-controller').disabled=true;$('controller-message').textContent='连接异常：'+e.message;$('session-notice').hidden=false;$('session-notice').className='notice error';$('session-notice').textContent='无法读取实时状态，画面可能过期；动作通道会暂停，恢复后需手动确认。';}
 finally{refreshing=false;}}
async function command(path,payload={}){localNotice=null;commandBusy=true;$('controller-message').textContent='正在处理…';
 try{const out=await api(path,payload);$('controller-message').textContent=out.message||'已处理';}
 catch(e){$('controller-message').textContent='未执行：'+e.message;}
 finally{commandBusy=false;await refresh();}}
$('start-controller').onclick=()=>command('/api/controller/start',{confirmed:$('wheels-lifted').checked&&$('map-confirmed').checked});
$('stop-controller').onclick=()=>command('/api/controller/stop');$('resume-controller').onclick=()=>command('/api/controller/resume');
$('view-simple').onclick=()=>setView(false);$('view-full').onclick=()=>setView(true);
function handleInspect(e){const key=e.target.closest('[data-map-key]')?.dataset.mapKey;if(key){inspect(key);$('map-detail').scrollIntoView({behavior:'smooth',block:'center'});}}
$('mapping-list').onclick=handleInspect;
document.querySelectorAll('.map-key').forEach(g=>{g.onclick=()=>inspect(g.dataset.mapKey);g.onkeydown=e=>{if(e.key==='Enter'||e.key===' '){e.preventDefault();inspect(g.dataset.mapKey);}};});
$('joint-cards').onclick=e=>{const id=e.target.closest('[data-joint]')?.dataset.joint;if(!id)return;const [port,raw]=id.split(':');const j=latestStatus.joints.find(j=>j.port===port&&j.id===Number(raw));
 if(!j.available){localNotice={level:'warning',message:j.label+'：'+j.reason+'。接管保持原状，未发送动作。'};render(latestStatus);return;}
 command('/api/controller/select',{port,id:Number(raw)});};
$('joint-select').onchange=()=>{const [port,id]=$('joint-select').value.split(':');command('/api/controller/select',{port,id:Number(id)});};
for(const id of ['wheels-lifted','map-confirmed'])$(id).onchange=()=>{if(latestStatus)render(latestStatus);};
for(const [id,enabled,scope]of[['torque-all-on',true,'all'],['torque-all-off',false,'all'],['torque-one-on',true,'single'],['torque-one-off',false,'single']])$(id).onclick=()=>command('/api/torque',{enabled,scope,port:$('torque-port').value,id:Number($('torque-id').value)});
$('apply-settings').onclick=()=>command('/api/controller/settings',{step_raw:Number($('step-setting').value),speed_raw:Number($('speed-setting').value)});
document.querySelectorAll('[data-preset]').forEach(b=>b.onclick=()=>{$('step-setting').value=b.dataset.preset;command('/api/controller/settings',{step_raw:Number(b.dataset.preset),speed_raw:Number($('speed-setting').value)});});
$('read-calibration').onclick=()=>command('/api/calibration/read');$('log-filter').onchange=renderEvents;
setInterval(refresh,300);setInterval(()=>{if(!document.hidden&&lastStatusAt&&Date.now()-lastStatusAt<2000)api('/api/controller/heartbeat',{}).catch(()=>{});},700);refresh();
