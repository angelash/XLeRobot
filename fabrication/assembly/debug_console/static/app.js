const $ = (id) => document.getElementById(id);
let streamFrame = 0;
let latestStatus = null;
let controllerSafetyConfirmed = false;

async function refresh() {
  try {
    const response = await fetch('/api/status', {cache: 'no-store'});
    const data = await response.json();
    renderStatus(data);
  } catch (error) {
    $('camera-time').textContent = `monitor offline · ${error.message}`;
  }
}

function motorRows(port) {
  if (!port.motors.length) return '<p class="empty">没有读到可应答电机</p>';
  return `<table class="motor-table" aria-label="${port.port} 舵机状态"><thead><tr><th>ID</th><th>型号</th><th>扭矩</th><th>位置</th><th>电压</th><th>温度</th></tr></thead><tbody>${port.motors.map((motor) => {
    const percent = Math.max(0, Math.min(100, ((motor.position ?? 0) / 4095) * 100));
    return `<tr><td>${motor.id}</td><td>${motor.model_name ?? '—'}</td><td>${motor.torque === 0 ? 'OFF' : motor.torque ?? '—'}</td><td><div class="bar" aria-label="位置 ${percent.toFixed(1)}%"><i style="width:${percent.toFixed(1)}%"></i></div></td><td>${Number.isFinite(motor.voltage) ? motor.voltage.toFixed(1) + ' V' : '—'}</td><td>${motor.temperature ?? '—'} °C</td></tr>`;
  }).join('')}</tbody></table>`;
}

function renderStatus(data) {
  latestStatus = data;
  const onlinePorts = data.ports.filter((port) => port.connected && !port.error).length;
  const totalMotors = data.ports.reduce((sum, port) => sum + (port.motor_count || 0), 0);
  const onlineCameras = data.cameras.filter((cam) => cam.state === '在线').length;
  $('ports-count').textContent = `${onlinePorts} / ${data.ports.length || 2}`;
  $('ports-count').nextElementSibling.textContent = onlinePorts < 2 ? '有一侧异常' : '双板已响应';
  $('motor-count').textContent = totalMotors;
  $('camera-count').textContent = `${onlineCameras} / 3`;
  $('motor-time').textContent = data.updated_at ? new Date(data.updated_at).toLocaleTimeString('zh-CN') : '—';
  $('ports').innerHTML = data.ports.map((port) => `<section class="port"><div class="port-head"><strong>${port.port} · 1 Mbps</strong><span class="${port.error ? 'err' : ''}">${port.error ? '打开失败' : `${port.motor_count} 个舵机`}</span></div>${port.error ? `<div class="error-text">${escapeHtml(port.error)}</div>` : motorRows(port)}</section>`).join('');
  data.cameras.forEach((camera) => { $('cam' + camera.camera).textContent = `${camera.state} · ${camera.frames} 帧`; });
  $('camera-time').textContent = `刷新 ${new Date().toLocaleTimeString('zh-CN')}`;
  const osPads = data.gamepad?.devices || [];
  if (osPads.length && $('pad-state').textContent === '按任意键唤醒') {
    $('pad-name').textContent = `Windows 已识别：${osPads.join(' / ')}`;
  }
  renderReadiness(data);
}

function setGate(id, state, title, detail) {
  const node = $(id);
  node.className = state;
  node.querySelector('strong').textContent = title;
  node.querySelector('p').textContent = detail;
}

function renderReadiness(data) {
  const ports = data.ports || [];
  const online = ports.filter((port) => port.connected && !port.error);
  const motorCount = ports.reduce((sum, port) => sum + (port.motor_count || 0), 0);
  const camerasOnline = (data.cameras || []).filter((cam) => cam.state === '在线').length;
  const pads = data.gamepad?.devices || [];
  const controller = data.controller || {};
  const controllerFresh = controller.updated_at && (Date.now() - Date.parse(controller.updated_at) < 3000);
  const controllerOnline = controller.controller === 'online' && controllerFresh;
  const calibrated = Boolean(controller.calibration);
  const torque = Boolean(controller.torque);
  const bothBoards = online.length === 2 && !ports.some((port) => port.error);
  const observed = bothBoards && motorCount === 16 && camerasOnline === 3;
  setGate('gate-observe', bothBoards ? 'ok' : 'blocked',
    observed ? '可观测' : bothBoards ? `${motorCount}/16 电机` : `${online.length}/2 控制板`,
    observed ? '双板、16 电机与三相机在线' : '查看下方尚未通过的硬件通道');
  setGate('gate-calibrate', bothBoards && motorCount === 16 ? 'ok' : 'blocked',
    bothBoards && motorCount === 16 ? '可准备校准' : '不能开始校准',
    bothBoards && motorCount === 16 ? '手臂必须支撑；启动后扭矩保持 OFF' : '必须双板与全部电机应答');
  const moveReady = observed && pads.length > 0 && calibrated && controllerSafetyConfirmed;
  setGate('gate-move', moveReady ? 'ok' : 'blocked', moveReady ? '具备启动条件' : '锁定',
    moveReady ? '仍须在控制器中选择正确关节并按住 LB' :
      !calibrated ? '校准文件未完成' : !controllerSafetyConfirmed ? '需勾选安全确认' : !pads.length ? '手柄不可见' : '硬件在线条件不足');

  const blockers = [];
  if (!bothBoards) blockers.push('两块 CH343 须同时在线（当前 ' + online.length + '/2）');
  if (motorCount !== 16) blockers.push(`舵机应为 16 个（当前 ${motorCount}）`);
  if (!pads.length) blockers.push('未检测到手柄');
  if (!calibrated) blockers.push('尚无本机校准文件');
  if (!controllerSafetyConfirmed) blockers.push('未勾选离地与关节映射确认');
  if (!pads.length && controllerOnline) blockers.push('控制器运行中但手柄可能已断开，立即停止');
  $('blocker').textContent = blockers.length ? '阻断：' + blockers.join(' · ') : '全部条件通过；仍以低速、离地、LB 保持使能方式测试。';
  $('controller-state').textContent = controllerOnline ? 'ONLINE' : 'OFFLINE';
  $('torque-state').textContent = torque ? 'ENABLED' : 'OFF';
  $('active-joint').textContent = controller.active_joint || controller.selected_joint || '—';
  $('last-action').textContent = controller.last_action || '尚无动作';
  $('controller-time').textContent = controller.updated_at ? new Date(controller.updated_at).toLocaleTimeString('zh-CN') : '控制器未启动';
  $('start-controller').disabled = !moveReady || controllerOnline;
  $('stop-controller').disabled = !controllerOnline;
  $('start-controller').textContent = moveReady ? '启动受限控制' : '条件未满足';
  $('controller-message').textContent = controller.error || (controllerOnline ?
    '控制器运行中；Web 仅发送启动/停止控制，不发送关节目标。' :
    '当前为只读观测；勾选安全条件后可单独启动受限控制器。');
  if (controller.controller === 'online' && !controllerFresh) {
    $('controller-state').textContent = 'STALE';
    $('controller-message').textContent = '控制器状态已超过 3 秒未更新，可能异常退出；动作不得依赖此状态。';
  }
  $('mode-label') && ($('mode-label').textContent = controllerOnline ? 'GUARDED ACTIVE' : 'READ ONLY');
}

function updateSafetyConfirmation() {
  controllerSafetyConfirmed = Boolean($('wheels-lifted').checked && $('map-confirmed').checked);
  if (latestStatus) renderReadiness(latestStatus);
}

try {
  $('wheels-lifted').checked = localStorage.getItem('xlerobot-wheels-lifted') === 'done';
  $('map-confirmed').checked = localStorage.getItem('xlerobot-map-confirmed') === 'done';
} catch (_) {}
updateSafetyConfirmation();
$('wheels-lifted').addEventListener('change', () => {
  try { localStorage.setItem('xlerobot-wheels-lifted', $('wheels-lifted').checked ? 'done' : ''); } catch (_) {}
});
$('map-confirmed').addEventListener('change', () => {
  try { localStorage.setItem('xlerobot-map-confirmed', $('map-confirmed').checked ? 'done' : ''); } catch (_) {}
});

$('wheels-lifted').addEventListener('change', updateSafetyConfirmation);
$('map-confirmed').addEventListener('change', updateSafetyConfirmation);

async function controllerCommand(action) {
  $('controller-message').textContent = action === 'start' ? '正在启动受限控制器…' : '正在停止控制器…';
  try {
    const response = await fetch(`/api/controller/${action}`, {method: 'POST'});
    const data = await response.json();
    $('controller-message').textContent = data.message || data.detail || data.status || '命令已处理';
    setTimeout(refresh, 500);
  } catch (error) {
    $('controller-message').textContent = `请求失败：${error.message}`;
  }
}

$('start-controller').addEventListener('click', () => controllerCommand('start'));
$('stop-controller').addEventListener('click', () => controllerCommand('stop'));

function escapeHtml(value) {
  return String(value).replace(/[&<>"']/g, (character) => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[character]));
}

function gamepadLoop() {
  const pads = navigator.getGamepads ? navigator.getGamepads() : [];
  const pad = Array.from(pads).find(Boolean);
  if (!pad) {
    $('pad-state').textContent = '按任意键唤醒';
    const osPads = latestStatus?.gamepad?.devices || [];
    $('pad-name').textContent = osPads.length ? `Windows 已识别：${osPads.join(' / ')}` : 'Windows 未发现手柄';
  } else {
    $('pad-state').textContent = '已连接 · DRY RUN';
    $('pad-name').textContent = `${pad.id} · ${pad.connected ? 'connected' : 'offline'}`;
    $('axes').textContent = `Axes ${pad.axes.length}`;
    $('buttons').textContent = `Buttons ${pad.buttons.filter((button) => button.pressed).length}/${pad.buttons.length} pressed`;
    const left = pad.axes[0] || 0; const leftY = pad.axes[1] || 0;
    const right = pad.axes[2] || 0; const rightY = pad.axes[3] || 0;
    $('stick-left').style.transform = `translate(${left * 18}px,${leftY * 18}px)`;
    $('stick-right').style.transform = `translate(${right * 18}px,${rightY * 18}px)`;
  }
  requestAnimationFrame(gamepadLoop);
}

window.addEventListener('gamepadconnected', (event) => {
  $('pad-state').textContent = `${event.gamepad.id} 已连接`;
});

document.querySelectorAll('[data-step]').forEach((checkbox) => {
  const key = `xlerobot-bringup-${checkbox.dataset.step}`;
  try {
    checkbox.checked = localStorage.getItem(key) === 'done';
    checkbox.addEventListener('change', () => {
      if (checkbox.checked) localStorage.setItem(key, 'done');
      else localStorage.removeItem(key);
    });
  } catch (_) {}
});

setInterval(refresh, 1000);
refresh();
gamepadLoop();
