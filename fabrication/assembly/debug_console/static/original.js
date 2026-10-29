const $=id=>document.getElementById(id);
const esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
let latest=null,busy=false,refreshing=false,seq=0,eventRows=[],lastStatus=0,selected='lx';
let mappingSignature='',targetInitialized=false,settingsDirty=false,settingsSignature='';
const modeNames={position:'伸缩 / 平面高度',pan:'肩水平旋转',wrist:'腕俯仰 / 旋转'};
const directModeNames={position:'肩抬升 / 肘',pan:'肩水平',wrist:'腕俯仰 / 腕旋转'};
const channelNames={left_position:'左臂末端',right_position:'右臂末端',left_pan:'左肩水平',right_pan:'右肩水平',left_pitch:'左腕俯仰',right_pitch:'右腕俯仰',left_roll:'左腕旋转',right_roll:'右腕旋转',left_grip:'左夹爪',right_grip:'右夹爪',head_7:'头部水平',head_8:'头部俯仰',base:'双轮底盘',left_lift:'左肩抬升',right_lift:'右肩抬升',left_elbow:'左肘',right_elbow:'右肘'};
const jointNames=['肩水平','肩抬升','肘','腕俯仰','腕旋转','夹爪'];
const directHelp={preset:'预设直控：左杆左右=左肩抬升、前后=左肘；按住LB再拨=左腕（左右旋转、前后俯仰）；按住LS左右=左肩水平；LT压下闭合左夹爪。右侧同理。X/B/A/Y控制头部，十字键开底盘。方向反了或快慢不合适，到“控制设置”逐轴改，接管中保存即时生效。',joint:'低速试控：直接左右拨杆调肩抬升，前后调肘；按住 LB / RB 再左右拨调腕旋转，上下调腕俯仰。每关节仅限起点±2.8°。下方显示串口实测计数。'};
async function api(path,body){const r=await fetch(path,{method:body===undefined?'GET':'POST',headers:body===undefined?{}:{'Content-Type':'application/json'},body:body===undefined?undefined:JSON.stringify(body),cache:'no-store'});const data=await r.json();if(!r.ok)throw new Error(data.detail||'请求失败');return data;}
function isNeutral(s){return s.gamepad.connected&&s.input_age_ms!==null&&s.input_age_ms<500&&!s.gamepad.buttons?.some(Boolean)&&!s.gamepad.hat?.some(Boolean)&&!(s.gamepad.axes||[]).slice(0,4).some(v=>Math.abs(v)>.15)&&!(s.gamepad.axes||[]).slice(4,6).some(v=>v>.12);}
function point(q,origin){const a=(90-q.lift)*Math.PI/180-Math.atan2(.028,.11257),b=(q.elbow+90)*Math.PI/180-(Math.atan2(.0052,.1349)+Math.atan2(.028,.11257));const k=850;const e=[origin[0]+.1159*Math.cos(a)*k,origin[1]-.1159*Math.sin(a)*k];return [e,[e[0]+.135*Math.cos(a-b)*k,e[1]-.135*Math.sin(a-b)*k]];}
function armDiagram(o){let svg='';for(const [side,x,label]of[['left',65,'左臂'],['right',445,'右臂']]){const q=o.arms[side].joints,origin=[x,295],[e,t]=point(q,origin);svg+=`<path d="M${x-15} 295H${x+250} M${x} 330V60" stroke="var(--line)" fill="none"/><text x="${x}" y="35" fill="currentColor" font-size="19">${label} · ${esc(modeNames[o.arms[side].mode])}</text><text x="${x+210}" y="320" fill="var(--muted)" font-size="12">径向</text><text x="${x-20}" y="55" fill="var(--muted)" font-size="12">高度</text><path d="M${x} 295L${e[0]} ${e[1]}L${t[0]} ${t[1]}" fill="none" stroke="var(--green)" stroke-width="13" stroke-linecap="round"/><circle cx="${x}" cy="295" r="11" fill="var(--ink)"/><circle cx="${e[0]}" cy="${e[1]}" r="10" fill="var(--ink)"/><circle cx="${t[0]}" cy="${t[1]}" r="7" fill="var(--amber)"/><path d="M${t[0]+8} ${t[1]-q.grip*.16}h12 M${t[0]+8} ${t[1]+q.grip*.16}h12" stroke="var(--ink)" stroke-width="4"/><text x="${x}" y="370" fill="var(--muted)" font-size="12">肩 ${q.pan.toFixed(1)}° · 腕 ${q.wrist.toFixed(1)}° · 旋 ${q.roll.toFixed(1)}°</text>`;}return svg;}
function mapValue(m,s){if(m.group==='system')return '系统不提供';if(!s.gamepad.connected||s.input_age_ms>=500)return '离线';if(m.axis!==null&&m.axis!==undefined){const v=s.gamepad.axes?.[m.axis]||0;return m.axis>=4?Math.round(v*100)+'%':v.toFixed(2);}return s.gamepad.buttons?.[m.index]?'按下':'松开';}
function currentMap(s){const t=$('target').value;return (t==='joint'?s.trial_control_map:t==='preset'?s.preset_control_map:s.control_map)||s.control_map||[];}
function renderMapping(s){const map=currentMap(s);const signature=JSON.stringify(map);if(mappingSignature!==signature){$('mapping').innerHTML=map.map(m=>`<button class="mapping-card" data-key="${esc(m.key)}"><span class="card-head"><strong>${esc(m.name)}</strong><span class="input-pill" id="value-${m.key}"></span></span><b>${esc(m.role)}</b><p>${esc(m.gesture)}</p><small>${esc(m.target)}</small></button>`).join('');mappingSignature=signature;}
for(const m of map){const value=mapValue(m,s);$('value-'+m.key).textContent=value;const pressed=s.gamepad.connected&&s.input_age_ms<500&&(m.axis!==null&&m.axis!==undefined?Math.abs(s.gamepad.axes?.[m.axis]||0)>.15:!!s.gamepad.buttons?.[m.index]);document.querySelector(`[data-key="${m.key}"]`)?.classList.toggle('pressed',pressed);}
const m=map.find(m=>m.key===selected)||map[0];if(m)$('mapping-detail').innerHTML=`<h3>${esc(m.name)} · ${esc(m.role)}</h3><dl><dt>怎么操作</dt><dd>${esc(m.gesture)}</dd><dt>预期结果</dt><dd>${esc(m.result)}</dd><dt>生效条件</dt><dd>${esc(m.requirement)}</dd></dl>`;}
function axisKeys(settings){const ports=settings.logical_ports||{left:'COM3',right:'COM4'},rows=[];for(const side of ['left','right'])for(let i=1;i<=6;i++)rows.push({key:ports[side]+':'+i,prefix:side+'-'+i,label:(side==='left'?'左臂 ':'右臂 ')+jointNames[i-1],grip:i===6});rows.push({key:'COM3:7',prefix:'head-7',label:'头部水平'},{key:'COM3:8',prefix:'head-8',label:'头部俯仰'});return rows;}
function renderAxisSettings(settings){
 $('axis-overrides').innerHTML=axisKeys(settings).map(({key,prefix,label,grip})=>{const v=settings.axis_overrides?.[key]||{};const rate=grip?(v.grip_raw_s??''):(v.joint_rate_deg_s??'');
  return `<div class="axis-editor-row"><strong>${esc(label)}</strong><label>方向<select id="${prefix}-direction"><option value="1" ${v.direction!==-1?'selected':''}>按手柄正向</option><option value="-1" ${v.direction===-1?'selected':''}>反向</option></select></label><label>${grip?'速度 raw/s':'速度 °/s'}<input id="${prefix}-rate" type="number" min="${grip?20:1}" max="${grip?800:90}" placeholder="默认 ${grip?settings.grip_raw_s:settings.joint_rate_deg_s}" value="${rate}"></label>${grip?'':`<label>容差 raw<input id="${prefix}-tolerance" type="number" min="16" max="512" step="8" placeholder="默认 ${settings.follow_delta_raw}" value="${v.follow_delta_raw??''}"></label>`}</div>`;}).join('');
}
function collectAxisSettings(settings){
 const out={};
 // Store only values that differ from the defaults, so changing a default
 // still reaches every axis that was not tuned individually.
 for(const {key,prefix,grip} of axisKeys(settings)){const item={};const direction=+$(prefix+'-direction').value,rate=$(prefix+'-rate').value,tolerance=$(prefix+'-tolerance')?.value;
  if(direction===-1)item.direction=-1;if(rate!=='')item[grip?'grip_raw_s':'joint_rate_deg_s']=+rate;if(tolerance)item.follow_delta_raw=+tolerance;if(Object.keys(item).length)out[key]=item;}
 return out;
}
const sliders=[['joint-rate','joint_rate_deg_s','° / 秒'],['grip-rate','grip_raw_s','raw / 秒'],['wheel-rate','wheel_max_raw','raw / 秒'],['follow-delta','follow_delta_raw','raw']];
function renderSettings(settings,online){
 for(const id of ['arm-mapping','base-forward','base-turn'])$(id).disabled=online;
 $('settings-state').textContent=settingsDirty?'有未保存修改':online?'接管中 · 保存即时生效':'';
 const signature=JSON.stringify(settings);
 // Never overwrite a form the user is editing; polling runs every 250 ms.
 if(settingsDirty||signature===settingsSignature)return;
 settingsSignature=signature;
 $('arm-mapping').value=settings.arm_mapping;$('base-forward').value=settings.base_forward||'a';$('base-turn').value=settings.base_turn||'normal';
 for(const [id,key,label] of sliders){$(id).value=settings[key];$(id+'-out').textContent=settings[key]+' '+label;}
 renderAxisSettings(settings);
}
function encoderSummary(s){const ports=s.control_settings?.logical_ports||{left:'COM3',right:'COM4'};const out={};for(const side of ['left','right']){const motors=s.ports.find(p=>p.port===ports[side])?.motors||[];out[side]={port:ports[side],pos:id=>motors.find(m=>m.id===id)?.position??'—',range:id=>{const m=motors.find(m=>m.id===id);return m?`${m.min}–${m.max}`:'—';}};}return out;}
function render(s){latest=s;const c=s.controller,active=c.controller==='online',ours=s.control_profile?.startsWith('original_'),paused=!!c.paused,neutral=isNeutral(s),calibrating=!!s.manual_calibration?.active,settings=s.control_settings;
if(!targetInitialized){$('target').value=s.preferred_target||(s.preview_only?'preview':'preset');targetInitialized=true;}
if(active&&ours)$('target').value=s.control_profile==='original_hardware'?'hardware':s.control_profile==='original_joint'?'joint':s.control_profile==='original_preset'?'preset':'preview';
const target=$('target').value,direct=target==='preset'||target==='joint';const hardwareReady=s.ports.length===2&&s.ports.reduce((n,p)=>n+(p.motors?.length||0),0)===16&&!s.error;const setup=s.whole_robot_setup,wholeReady=!!setup?.ready;
const presetRows=Object.entries(s.preset_readiness||{}),presetReady=presetRows.filter(([k,v])=>v.ready).length,saved=(s.saved_ranges||[]).length;
$('session-state').textContent=active?(ours?(paused?'接管保留 · 动作暂停':target==='preset'?'预设直控接管中':'原版映射接管中'):'单关节调试接管保留'):'接管未开启';
$('session-description').textContent=active?(ours?(paused?c.pause_reason:target==='preset'?'拨杆直接驱动关节；LB/RB切腕，LS/RS切肩水平；Menu暂停，View结束。':'拨杆直接动作；LB/RB切腕姿，Menu暂停，View结束。'):'当前有单关节会话，请由你在原页面结束后再切换。'):'选择控制目标，手柄回中后开启；开启时保持当前位置，不归零。';
$('target').disabled=active||busy||calibrating;$('confirm').disabled=active||busy||calibrating;
if(s.preview_only){for(const v of ['hardware','joint','preset'])$('target').querySelector(`option[value="${v}"]`).disabled=true;}
$('prepare').hidden=true;
$('confirm-label').textContent=target==='preview'?'我确认当前只运行运动学预览':target==='preset'?'我确认手臂有支撑、周围有空间，按预设映射直接试动':target==='joint'?'我确认周围有空间、手臂重量有支撑，先做当前位置的小范围试控':'我确认周围安全，按已保存的标定操作整机';
$('start').textContent=target==='hardware'?'开启整机联动':target==='preset'?'开启预设直控':target==='joint'?'开启低速试控':'开启接管';
$('start').disabled=busy||active||calibrating||!neutral||!$('confirm').checked||(target==='hardware'&&(!hardwareReady||!wholeReady))||(direct&&!hardwareReady);$('start').hidden=active;
$('pause').disabled=busy||!active||!ours||paused;$('stop').disabled=busy||!active||!ours;
$('resume').hidden=!active||!ours||!paused;$('resume').disabled=busy||!neutral;
$('blocker').textContent=calibrating?'正在采集，扭矩全部关闭。要试控：在下方采集面板点“保存已采行程 · 开放直控”，或点“结束采集”后直接开启。':active?'':target!=='preview'&&!hardwareReady?'实机未就绪：需要双板16电机在线；当前可使用运动学预览':!s.gamepad.connected?'请连接Xbox手柄':!neutral?'请松开所有按键/扳机并将摇杆回中':target==='hardware'&&!wholeReady?'整机联动需要录完双臂姿态卡；现在可以先选“预设直控”直接控制':!$('confirm').checked?'勾选上方确认后即可开启':target==='preset'?`可以开启：${presetReady}/${presetRows.length}个部件可直接控制；未接入的部件见右侧状态，不影响其他部件`:target==='joint'?'条件满足：开启时先保持当前位置，再接入低速试控；不自动归零':'条件满足，可以开启；之后拨杆即可控制';
$('whole-setup').hidden=s.preview_only||target==='preview';
$('setup-title').textContent=target==='hardware'?(wholeReady?'整机联动已就绪':'整机联动还缺以下项目'):`直控范围：${saved}/14 个关节使用已保存的实测行程`;
$('setup-description').textContent=target==='hardware'?'行程、姿态卡、底盘可以任意顺序采集；录一项存一项，单项失败不影响其他项。':saved<14?'没保存行程的关节只能在舵机现有限位内转动（旧试控窗口只有几度）。打开采集，手动转动关节后点“保存已采行程”，不需要姿态卡。':'所有关节已使用实测行程。姿态卡只影响“整机联动”模式。';
$('setup-checks').innerHTML=target==='hardware'?(setup?.checks||[]).map(c=>`<div class="readiness-row ${c.ready?'ready':'blocked'}"><strong>${esc(c.label)} · ${c.ready?'就绪':'待完成'}</strong><small>${esc((c.reasons||[]).join('；'))}</small></div>`).join(''):'';
if(settings)renderSettings(settings,active);
$('notice').hidden=!s.notice&&!paused;$('notice').className='notice'+(paused?' error':'');$('notice').textContent=paused?c.pause_reason:s.notice?.message||'';
$('pad').textContent=s.gamepad.connected?(s.gamepad.name||'Xbox在线'):'手柄离线';$('input-age').textContent=`原生输入 ${s.input_age_ms??'—'}ms`;
$('backend').textContent=s.preview_only?'隔离预览 · 无串口访问':s.control_profile==='original_preset'?'预设直控 · 拨杆直接驱动关节 · 舵机限位内':s.control_profile==='original_joint'?'低速试控 · 起点±2.8° / 双轮短脉冲':s.control_profile==='original_hardware'?'整机联动 · 末端IK':s.control_profile==='diagnostic'?'设备监控 · 未开启接管':'运动学预览 · 不向硬件发送目标';
$('version').textContent=s.version;$('service-mode').textContent=s.preview_only?'当前服务只预览，现有实机接管保持':'单串口所有者';
const o=s.original;if(o){$('kind').textContent=direct?'实测编码器 · 不是末端姿态':s.control_profile==='original_hardware'?'计划值示意':'运动学预览';$('arm-view').innerHTML=armDiagram(o);$('speed').textContent=`底盘档位 ${o.speed_level}/3 · 上限 ${settings?.wheel_max_raw??'—'} raw/s`;
for(const side of ['left','right']){const a=o.arms[side];$(side+'-mode').textContent=modeNames[a.mode];$(side+'-position').textContent=`径向 ${(a.plane_m[0]*1000).toFixed(1)}mm · 高度 ${(a.plane_m[1]*1000).toFixed(1)}mm`;$(side+'-grip').textContent=`夹爪目标 ${a.joints.grip.toFixed(1)} / 100 · 松扳机打开`;}
const yaw=o.base_pose[2]*180/Math.PI;$('base-view').innerHTML=`<path d="M20 105H340 M180 10V200" stroke="var(--line)"/><g transform="translate(180,105) rotate(${-yaw})"><rect x="-50" y="-43" width="100" height="86" rx="10" fill="var(--paper)" stroke="var(--green)" stroke-width="2"/><rect x="-37" y="-56" width="32" height="16" fill="var(--ink)"/><rect x="-37" y="40" width="32" height="16" fill="var(--ink)"/><path d="M-15 0H35L21 -12 M35 0L21 12" fill="none" stroke="var(--green)" stroke-width="3"/></g><text x="20" y="195" font-size="12" fill="var(--muted)">X ${o.base_pose[0].toFixed(2)}m · Y ${o.base_pose[1].toFixed(2)}m · ${yaw.toFixed(1)}°</text>`;
$('wheel-state').textContent=`轮速目标 电机9 / 电机10：${o.wheel_raw.join(' / ')} raw/s`;$('head-state').textContent=`头部目标：水平 ${o.head_deg[0].toFixed(1)}° / 俯仰 ${o.head_deg[1].toFixed(1)}°`;}
const readinessSource=target==='preset'?s.preset_readiness:target==='joint'?s.trial_readiness:s.original_readiness;
$('readiness-title').textContent=target==='preset'?`部件状态 · ${presetReady}/${presetRows.length} 可直接控制`:'实机通道状态';
$('readiness').innerHTML=Object.entries(readinessSource||{}).map(([key,v])=>{const narrow=v.ready&&/旧试控窗口/.test(v.note||'');const fix=!v.ready&&(v.reasons||[]).some(r=>r.includes('保存已采行程'))||narrow;
 return `<div class="readiness-row ${v.ready?(narrow?'narrow':'ready'):'blocked'}"><strong>${esc(channelNames[key]||key)} · ${v.ready?(target==='preset'?(narrow?'可控 · 范围很小':'可直接控制'):'可受限测试'):'未接入'}</strong><small>${v.ready?esc(v.note||'仍受设备与本轮小范围限制'):esc((v.reasons||[]).join('；'))}</small>${fix?'<button type="button" class="link-button" data-open-calibration>去保存行程 ↗</button>':''}</div>`;}).join('');
const cmd=s.last_command;$('latest-command').textContent=cmd?`最近实机命令 ${cmd.port}:${cmd.id} · ${cmd.origin}→${cmd.target} · ${cmd.outcome}${cmd.position!==undefined?' · 实测 '+cmd.position:''}`:'尚无实机动作命令；预览不代表实机移动。';
$('joint-help').hidden=!direct;$('joint-help').textContent=directHelp[target]||'';$('arm-preview').hidden=direct;
if(direct){
  if(target==='joint')$('speed').textContent='关节≤5°/s · 双轮160raw / 180ms短脉冲';
  const enc=encoderSummary(s);
  for(const side of ['left','right']){const e=enc[side];
    $(side+'-mode').textContent=(directModeNames[o?.arms[side].mode]||'')+' · '+e.port;
    $(side+'-position').textContent=`实测计数：肩水平 ${e.pos(1)} · 肩抬升 ${e.pos(2)} · 肘 ${e.pos(3)} · 腕俯仰 ${e.pos(4)} · 腕旋转 ${e.pos(5)}`;
    $(side+'-grip').textContent=`夹爪实测 ${e.pos(6)} · 限位 ${e.range(6)}`;
  }
  const heads=s.ports.find(p=>p.port==='COM3')?.motors||[];
  $('head-state').textContent=`头部实测计数：${[7,8].map(id=>heads.find(m=>m.id===id)?.position??'—').join(' / ')}`;
}
$('hardware-time').textContent=s.updated_at||'未访问串口';$('motors').innerHTML=(s.ports||[]).map(p=>`<h3>${esc(p.port)}</h3><table class="motor-table"><thead><tr><th>关节</th><th>实测</th><th>目标</th><th>设备范围</th><th>模式</th><th>扭矩</th></tr></thead><tbody>${p.motors.map(m=>`<tr><td>${esc(m.label)}</td><td>${m.position}</td><td>${m.goal}</td><td>${m.min}–${m.max}</td><td>${m.mode}</td><td>${m.torque?'开':'关'}</td></tr>`).join('')}</tbody></table>`).join('');renderMapping(s);if(typeof renderCalibration==='function')renderCalibration(s);}
async function refresh(){if(refreshing)return;refreshing=true;try{render(await api('/api/original/status'));lastStatus=Date.now();const r=await api('/api/events?after='+seq);if(r.last_seq<seq){seq=0;eventRows=[];}for(const e of r.events){seq=Math.max(seq,e.seq);if(!['input','axes'].includes(e.kind))eventRows.unshift(e);}eventRows.length=Math.min(80,eventRows.length);$('events').innerHTML=eventRows.map(e=>`<li><time>${new Date(e.time).toLocaleTimeString('zh-CN')}</time><span>${esc(e.kind)}</span><span>${esc(e.message)}</span></li>`).join('');}catch(e){lastStatus=0;$('message').textContent='状态不可用：'+e.message;$('start').disabled=true;$('resume').disabled=true;}finally{refreshing=false;}}
async function command(path,body={}){busy=true;$('message').textContent='正在处理…';try{await api(path,body);$('message').textContent='已处理';}catch(e){$('message').textContent='未执行：'+e.message;}finally{busy=false;await refresh();}}
function openCalibration(){if(typeof calibrationOpen!=='undefined')calibrationOpen=true;if(latest&&typeof renderCalibration==='function')renderCalibration(latest);$('calibration-panel').scrollIntoView({behavior:'smooth',block:'start'});}
$('prepare').onclick=()=>command('/api/hardware/prepare',{confirmed:$('confirm').checked});
$('settings-form').oninput=e=>{settingsDirty=true;$('settings-state').textContent='有未保存修改';const slider=sliders.find(([id])=>id===e.target.id);if(slider)$(slider[0]+'-out').textContent=e.target.value+' '+slider[2];};
$('settings-form').onsubmit=async e=>{e.preventDefault();if(!latest)return;const online=latest.controller.controller==='online';
 const body={joint_rate_deg_s:+$('joint-rate').value,grip_raw_s:+$('grip-rate').value,wheel_max_raw:+$('wheel-rate').value,follow_delta_raw:+$('follow-delta').value,axis_overrides:collectAxisSettings(latest.control_settings)};
 if(!online)Object.assign(body,{arm_mapping:$('arm-mapping').value,base_forward:$('base-forward').value,base_turn:$('base-turn').value});
 busy=true;$('settings-message').textContent='正在保存…';
 try{await api('/api/control-settings',body);settingsDirty=false;settingsSignature='';$('settings-message').textContent=online?'已保存，下一次拨杆按新设置执行':'设置已保存';}
 catch(err){$('settings-message').textContent='未保存：'+err.message;}
 finally{busy=false;await refresh();}};
$('settings-reset').onclick=()=>{settingsDirty=false;settingsSignature='';$('settings-message').textContent='已恢复为当前保存值';if(latest)render(latest);};
$('readiness').onclick=e=>{if(e.target.closest('[data-open-calibration]'))openCalibration();};
$('start').onclick=()=>command('/api/original/start',{target:$('target').value,confirmed:$('confirm').checked});$('pause').onclick=()=>command('/api/original/pause');$('resume').onclick=()=>command('/api/controller/resume');$('stop').onclick=()=>command('/api/controller/stop');
$('target').onchange=()=>{$('confirm').checked=false;mappingSignature='';if(latest)render(latest);};$('confirm').onchange=()=>{if(latest)render(latest);};$('mapping').onclick=e=>{const key=e.target.closest('[data-key]')?.dataset.key;if(key){selected=key;renderMapping(latest);$('mapping-detail').scrollIntoView({behavior:'smooth',block:'center'});}};
setInterval(refresh,250);setInterval(()=>{if(!document.hidden&&lastStatus&&Date.now()-lastStatus<1500)api('/api/controller/heartbeat',{}).catch(()=>{});},600);refresh();
