'use strict';
// PICO / WebXR teleoperation page. Robot motion only goes through the existing
// leased web session (/api/web/start, /vr-input, /stop) on the 8765 controller.
// Inputs are sent from the XR frame loop, so a removed headset or a paused page
// stops renewing the 650 ms lease and the robot stops by itself.
(() => {
  const M = window.VRMapping, $ = id => document.getElementById(id);
  const SEND_MS = 50, COMBO_MS = 1500, SHORT_MS = 600;
  const CAMERAS = {head: 0, left: 2, right: 1};
  const JOINTS = {1: '肩水平', 2: '肩抬升', 3: '肘', 4: '腕俯仰', 5: '腕旋转'};
  const JOG_ORDER = [2, 3, 4, 1, 5];
  const stored = (() => { try { return JSON.parse(localStorage.getItem('xlerobot-vr') || '{}'); } catch { return {}; } })();
  const settings = {scale: .5, ratio: 1, orient: true, scheme: 'standard', mode: 'preview', screenX: 0, screenY: 0, ...stored};
  for (const key of ['screenX', 'screenY']) settings[key] = M.clamp(Number(settings[key]) || 0, .6);
  const save = () => { try { localStorage.setItem('xlerobot-vr', JSON.stringify(settings)); } catch {} };

  let xr = null, refSpace = null, gl = null, renderer = null;
  let control = null, starting = null; // Pending starts are cancelled by every stop.
  let inFlight = false, lastSent = 0, rtt = null, robot = null, robotNote = '未开启';
  let bodyYaw = 0, placement = null, gear = 1, fine = false, engageCounter = 0, combo = null, mustRelease = false;
  let confirmInVR = false, readiness = null, frameTime = null, screenAdjusted = false;
  let jogMode = false, jogJoint = 2, jogToggleFired = false, soloXSince = null, inputReady = true;
  const hands = {left: newHand(), right: newHand()};
  const sim = {left: simArm(), right: simArm(), head: [0, 0], base: [0, 0, 0]};
  const cams = {};
  let lastPads = {left: [], right: []};

  function newHand() { return {engage: 0, origin: null, pad: {}, since: {}, roll: 0, tracked: false, pose: null, state: null}; }
  function simArm() { return {lift: 0, elbow: 40, wrist: -30, pan: 0, roll: 0, grip: 0, anchor: null}; }

  // ---------- 2D page ----------
  function setCheck(id, level, text) {
    const node = $(id); node.className = level; node.querySelector('[data-text]').textContent = text;
  }
  function status(text, bad = false) { const node = $('status'); node.textContent = text; node.classList.toggle('bad', bad); }
  function bindSettings() {
    for (const input of document.querySelectorAll('input[name=mode]')) {
      input.checked = input.value === settings.mode;
      input.addEventListener('change', () => chooseMode(input.value));
    }
    const range = (id, key, label) => {
      const node = $(id); node.value = settings[key];
      const show = () => { $(id + '-out').textContent = label(Number(node.value)); };
      node.addEventListener('input', () => { settings[key] = Number(node.value); show(); save(); }); show();
    };
    range('scale', 'scale', v => Math.round(v * 100) + '%');
    range('ratio', 'ratio', v => v.toFixed(2).replace(/0$/, '') + '×');
    range('screenY', 'screenY', v => Math.round(v * 100) + ' cm');
    $('orient').checked = settings.orient;
    $('orient').addEventListener('change', () => { settings.orient = $('orient').checked; save(); });
    $('scheme').value = settings.scheme;
    $('scheme').addEventListener('change', () => { settings.scheme = $('scheme').value; save(); });
    $('confirm').addEventListener('change', refreshButtons);
    $('enter').addEventListener('click', enterVR);
    $('stop').addEventListener('click', () => stopControl('已在页面结束实机控制'));
    $('choose-robot').addEventListener('click', () => chooseMode('robot'));
    $('screen-reset').addEventListener('click', () => { settings.screenX = settings.screenY = 0; placement = null; syncScreen(); });
  }
  function syncScreen() {
    $('screenY').value = settings.screenY;
    $('screenY-out').textContent = Math.round(settings.screenY * 100) + ' cm'; save();
  }
  function chooseMode(mode) {
    if (control || starting) stopControl('切换模式，已结束实机控制');
    settings.mode = mode; confirmInVR = false; $('confirm').checked = false;
    for (const input of document.querySelectorAll('input[name=mode]')) input.checked = input.value === mode;
    robotNote = mode === 'robot' ? '实机待开启：确认运动范围无人后，松开 X/A 再长按确认开启' : '预演模式';
    save(); refreshButtons();
  }
  async function pollReadiness() {
    if (starting) return;
    try {
      const response = await fetch('/api/web/vr-readiness', {signal: timeout(2500), cache: 'no-store'});
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || '暂未取得实机可用状态');
      readiness = data;
      $('live-channels').textContent = Object.entries(data.channels).map(([key, c]) => {
        const name = ({left: '左臂', right: '右臂', head: '头部', base: '底盘'})[key];
        if (!c.enabled) return `${name}：${c.reason}`;
        if (key === 'head' || key === 'base') return `${name}：可开启`;
        return `${name}：跟随${c.follow_ready ? '可用' : '待准备'}；夹爪${c.grip_ready ? '可用' : '未就绪'}；逐关节可选。` +
          (c.follow_reason ? c.follow_reason : '');
      }).join('\n') + (data.reason && !control ? '\n' + data.reason : '\n开启时只对可用通道保持当前位置并上扭矩。');
    } catch (e) { readiness = null; $('live-channels').textContent = '实机状态：' + e.message; }
  }
  let xrSupported = false;
  function refreshButtons() {
    $('enter').disabled = !xrSupported || !!xr;
    $('stop').disabled = !control && !(robot && robot.active && robot.mode === 'vr');
  }
  async function checkReady() {
    const secure = window.isSecureContext;
    setCheck('check-secure', secure ? 'ok' : 'bad', secure ? location.protocol.replace(':', '').toUpperCase() + ' 安全上下文' : '当前不是安全连接，浏览器不会开放 VR');
    $('secure-help').hidden = secure;
    $('https-url').textContent = 'https://' + location.hostname + ':8770/vr/';
    if (!navigator.xr) setCheck('check-xr', 'bad', secure ? '此浏览器没有 WebXR，请用 PICO 浏览器打开' : '需要先改用 HTTPS');
    else {
      try { xrSupported = await navigator.xr.isSessionSupported('immersive-vr'); } catch { xrSupported = false; }
      setCheck('check-xr', xrSupported ? 'ok' : 'warn', xrSupported ? '支持沉浸式 VR' : '浏览器不支持沉浸式 VR（桌面浏览器正常；请在头显里打开）');
    }
    refreshButtons();
    status(xrSupported ? '可以进入 VR。X+A 长按切换实机待确认，松开后再长按开启；B 返回预演。' : '在 PICO 浏览器里打开本页后进入 VR');
  }
  async function pollRobot() {
    if (control) return;
    try {
      const response = await fetch('/api/web/status', {signal: timeout(2500), cache: 'no-store'});
      if (!response.ok) {
        let detail = ''; try { detail = (await response.json()).detail; } catch {}
        throw new Error(typeof detail === 'string' && detail ? detail : 'HTTP ' + response.status);
      }
      robot = await response.json();
      const mine = robot.active && robot.mode === 'vr';
      setCheck('check-robot', robot.active && !mine ? 'warn' : 'ok',
        robot.active ? (mine ? 'VR控制进行中（另一页面或本页）' : '键盘网页控制进行中，VR不能同时开启') : '在线，未开启控制。' + (robot.reason || ''));
    } catch (e) {
      robot = null; setCheck('check-robot', 'bad', '控制服务未响应：' + e.message);
    }
    if (!confirmInVR && !robotNote.startsWith('开启失败'))
      robotNote = robot ? (robot.active ? (robot.mode === 'vr' ? 'VR控制中' : '键盘控制占用') : '实机待开启：确认周围无人后长按 X+A') : '控制服务未响应';
    refreshButtons();
  }

  // ---------- robot session ----------
  async function post(path, body, extra = {}) {
    const response = await fetch(path, {method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(body), cache: 'no-store', ...extra});
    let data = null; try { data = await response.json(); } catch {}
    if (!response.ok) throw new Error(data?.detail ? (typeof data.detail === 'string' ? data.detail : JSON.stringify(data.detail)) : '请求失败 ' + response.status);
    return data;
  }
  async function startControl() {
    if (control || starting || !xr || xr.visibilityState !== 'visible' || document.hidden || hands.right.pad.secondary) return;
    if (settings.mode !== 'robot') { flash('当前是预演模式；在页面上选择“实机”后再开启'); return; }
    if (!$('confirm').checked) { flash('请先在页面上勾选周围安全确认'); return; }
    if (Object.values(hands).some(h => !h.tracked)) { flash('需要左右手柄均被追踪后才能开启'); return; }
    if (Object.values(hands).some(h => h.engage)) { flash('请先松开握把'); return; }
    if (Object.values(hands).some(h => Math.hypot(h.pad.stickX || 0, h.pad.stickY || 0) > .15 || (h.pad.trigger || 0) > .05)) {
      robotNote = '请先松开摇杆和扳机，再长按 X+A'; flash(robotNote); return;
    }
    const attempt = starting = {session: xr};
    try {
      const data = await post('/api/web/start', {confirmed: true, mode: 'vr'}, {signal: timeout(2500)});
      if (starting !== attempt || xr !== attempt.session || xr.visibilityState !== 'visible' || document.hidden) {
        // The response can arrive after B, pagehide or XR end. Stop that exact
        // returned token; it must never revive an abandoned control session.
        await post('/api/web/stop', {session_id: data.session_id}, {keepalive: true});
        return;
      }
      control = {id: data.session_id, seq: 0}; robot = data; robotNote = data.reason;
      pulse(['left', 'right'], .6, 120); flash('实机控制已开启');
    } catch (e) { if (starting === attempt) { robotNote = '开启失败：' + e.message; flash(robotNote, true); } }
    finally { if (starting === attempt) starting = null; refreshButtons(); }
  }
  function stopControl(reason) {
    const id = control?.id || null; control = null;
    starting = null; combo = null; mustRelease = true;
    confirmInVR = false;
    for (const h of Object.values(hands)) h.engage = 0;
    robotNote = reason;
    if (id) post('/api/web/stop', {session_id: id}, {keepalive: true}).then(d => { robot = d; }).catch(() => {});
    else if (robot?.active && robot.mode === 'vr' && !xr) flash('本页没有持有该会话；请在开启它的页面或头显里结束');
    refreshButtons();
  }
  function payload() {
    const arms = {}, grip = {};
    for (const side of ['left', 'right']) {
      const h = hands[side];
      arms[side] = h.engage && h.state ? {engage: h.engage, pos: h.state.pos, pitch: h.state.pitch, roll: h.state.roll, orient: settings.orient} : null;
      // Missing tracking is unknown input, never a synthetic trigger release.
      grip[side] = h.tracked ? (h.pad.trigger ?? 0) : null;
    }
    if (hands.left.pad.secondary || !inputReady) return {base: [0, 0], head: [0, 0], arms: {left: null, right: null},
      grip: {left: null, right: null}, jog: null, scale: settings.scale, ratio: settings.ratio};
    if (jogMode) return {base: [0, 0], head: M.head(hands.right.pad.stickX, hands.right.pad.stickY),
      arms: {left: null, right: null}, grip: {left: null, right: null},
      jog: hands.left.engage && hands.left.tracked ? {side: 'left', joint: jogJoint,
        value: -M.deadzone(hands.left.pad.stickY), engage: hands.left.engage} : null,
      scale: settings.scale, ratio: settings.ratio};
    return {base: M.drive(hands.left.pad.stickX, hands.left.pad.stickY, gear),
      head: M.head(hands.right.pad.stickX, hands.right.pad.stickY), arms, grip,
      scale: settings.scale, ratio: Math.max(.25, settings.ratio * (fine ? .5 : 1))};
  }
  function send(now) {
    if (!control || inFlight || now - lastSent < SEND_MS) return;
    inFlight = true; lastSent = now;
    const session = control, started = performance.now();
    post('/api/web/vr-input', {session_id: session.id, seq: session.seq++, ...payload()}, {signal: timeout(1500)})
      .then(data => {
        rtt = Math.round(performance.now() - started);
        if (control !== session) return;
        const before = robot?.vr?.arms || {};
        robot = data; robotNote = data.reason;
        for (const side of ['left', 'right'])
          if (data.vr?.arms?.[side]?.state === 'boundary' && before[side]?.state !== 'boundary') pulse([side], .3, 40);
      })
      .catch(e => {
        if (control !== session) return;
        stopControl('控制已结束：' + e.message); pulse(['left', 'right'], 1, 300);
      })
      .finally(() => { inFlight = false; });
  }

  // ---------- XR session ----------
  async function enterVR() {
    if (xr) return;
    try {
      xr = await navigator.xr.requestSession('immersive-vr', {optionalFeatures: ['local-floor']});
    } catch (e) { status('无法进入 VR：' + e.message, true); return; }
    const session = xr;
    session.addEventListener('end', onEnd);
    session.addEventListener('visibilitychange', () => {
      if (session.visibilityState !== 'visible') stopControl('VR画面被系统界面遮挡，已停止');
    });
    try {
      const canvas = document.createElement('canvas');
      gl = canvas.getContext('webgl', {xrCompatible: true, antialias: true});
      if (!gl) throw new Error('浏览器没有 WebGL');
      if (gl.makeXRCompatible) await gl.makeXRCompatible();
      session.updateRenderState({baseLayer: new XRWebGLLayer(session, gl)});
      try { refSpace = await session.requestReferenceSpace('local-floor'); } catch { refSpace = await session.requestReferenceSpace('local'); }
      refSpace.addEventListener?.('reset', () => stopControl('VR坐标系已重置，请松开按键后重新校准并开启'));
      renderer = createRenderer(gl); placement = null;
    } catch (e) {
      status('VR 初始化失败：' + e.message, true);
      try { await session.end(); } catch {}
      xr = null; gl = null; renderer = null; refreshButtons();
      return;
    }
    for (const [name, index] of Object.entries(CAMERAS)) cameraLoop(name, index);
    status('已进入 VR'); refreshButtons();
    session.requestAnimationFrame(onFrame);
  }
  function onEnd() {
    stopControl('已退出VR');
    xr = null; gl = null; renderer = null;
    frameTime = null;
    for (const c of Object.values(cams)) c.stop = true;
    status('已退出 VR'); refreshButtons();
  }

  function onFrame(time, frame) {
    if (!xr) return;
    xr.requestAnimationFrame(onFrame);
    const viewer = frame.getViewerPose(refSpace);
    const dt = frameTime === null ? 0 : Math.min(.1, Math.max(0, (time - frameTime) / 1000)); frameTime = time;
    if (!viewer || viewer.emulatedPosition || xr.visibilityState !== 'visible' || document.hidden) {
      if (control || starting || combo) stopControl('头显追踪或可见性丢失，已停止');
      return;
    }
    if (viewer && !placement) recenter(viewer, false);
    readInputs(frame, viewer, time);
    if (hands.left.pad.secondary) {
      inputReady = false;
      const x = M.deadzone(hands.right.pad.stickX), y = -M.deadzone(hands.right.pad.stickY);
      if (x || y) {
        settings.screenX = M.clamp(settings.screenX + x * .4 * dt, .6);
        settings.screenY = M.clamp(settings.screenY + y * .4 * dt, .6);
        screenAdjusted = true; syncScreen();
      }
      for (const h of Object.values(hands)) { h.engage = 0; h.state = null; }
    }
    if (settings.mode === 'preview') simulate(time);
    send(time);
    if (viewer) renderer.draw(frame, viewer, time);
  }

  function recenter(viewer, reset = true) {
    bodyYaw = M.yaw(viewer.transform.orientation);
    placePanels(viewer, reset);
  }
  function placePanels(viewer, reset = true) {
    const p = viewer.transform.position;
    const q = viewer.transform.orientation;
    placement = mat.mul(mat.translate(p.x, p.y, p.z),
      mat.mul(mat.rotY(M.yaw(q)), mat.rotX(M.clamp(M.orientation(q).pitch, 60) * Math.PI / 180)));
    if (reset) { settings.screenX = settings.screenY = 0; syncScreen(); }
  }

  function readInputs(frame, viewer, now) {
    const seen = new Set();
    for (const source of xr.inputSources) {
      const side = source.handedness;
      if (side !== 'left' && side !== 'right' || !source.gamepad) continue;
      seen.add(side);
      const h = hands[side], pad = M.readPad(source.gamepad, settings.scheme), previous = h.pad;
      lastPads[side] = pad.pressed;
      const pose = source.gripSpace && frame.getPose(source.gripSpace, refSpace);
      h.tracked = !!pose && !pose.emulatedPosition; h.pose = pose; h.source = source;
      for (const key of ['primary', 'secondary', 'stickPress', 'squeeze'])
        if (pad[key] && !previous[key]) h.since[key] = now;
      h.pad = pad;
      // Clutch: a new grip re-anchors; releasing or losing tracking lets go.
      if (pad.squeeze && !previous.squeeze && h.tracked) {
        const o = M.orientation(pose.transform.orientation), p = pose.transform.position;
        h.engage = ++engageCounter; h.origin = [p.x, p.y, p.z]; h.roll = o.rollValid ? o.roll : 0;
        if (settings.mode === 'preview') simAnchor(side);
        pulse([side], .4, 30);
      }
      if (!pad.squeeze || !h.tracked) h.engage = 0;
      if (h.engage) {
        const p = pose.transform.position, o = M.orientation(pose.transform.orientation);
        if (o.rollValid) h.roll = o.roll;
        const body = M.toBody([p.x - h.origin[0], p.y - h.origin[1], p.z - h.origin[2]], bodyYaw);
        h.state = {pos: body.map(v => M.clamp(v, .99)), pitch: M.clamp(o.pitch, 180), roll: M.wrap(h.roll)};
      } else h.state = null;
      buttons(side, pad, previous, now, viewer);
    }
    for (const side of ['left', 'right']) if (!seen.has(side)) {
      hands[side].engage = 0; hands[side].pad = {}; hands[side].tracked = false;
      hands[side].state = null; hands[side].source = null;
      hands[side].pose = null;
    }
    if (!inputReady && Object.values(hands).every(h => !h.pad.squeeze && !h.pad.primary && !h.pad.secondary &&
        Math.hypot(h.pad.stickX || 0, h.pad.stickY || 0) < .15 && (h.pad.trigger || 0) < .05)) inputReady = true;
    // X alone is deliberately separate from X+A arming. A mode change never
    // carries an old grip/joystick command into the new interpretation.
    if (!hands.left.pad.primary) jogToggleFired = false;
    if (hands.left.pad.primary && !hands.right.pad.primary && !combo) soloXSince ??= now;
    else soloXSince = null;
    if (hands.left.pad.primary && !hands.right.pad.primary && !combo && !jogToggleFired &&
        soloXSince !== null && now - soloXSince >= COMBO_MS) {
      jogToggleFired = true;
      if (Object.values(hands).some(h => !h.tracked || h.pad.squeeze || h.pad.secondary ||
          Math.hypot(h.pad.stickX || 0, h.pad.stickY || 0) > .15 || (h.pad.trigger || 0) > .05)) {
        flash('先松开握把、扳机和摇杆，再单独长按 X 切换调整');
      } else {
        jogMode = !jogMode; inputReady = false; mustRelease = true;
        for (const h of Object.values(hands)) h.engage = 0;
        flash(jogMode ? '左臂逐关节：A 选关节，按住左握把＋左摇杆前后调整' : '已返回手部跟随；松开后重新握把对齐');
      }
    }
    // X + A held together starts robot control.
    const both = hands.left.pad.primary && hands.right.pad.primary;
    if (!hands.left.pad.primary && !hands.right.pad.primary) mustRelease = false;
    if (!both) combo = null;
    if (both && !combo && !mustRelease) combo = {since: now, fired: false};
    if (combo && !combo.fired && now - combo.since >= COMBO_MS) {
      combo.fired = true;
      if (hands.left.pad.secondary || hands.right.pad.secondary) return;
      if (settings.mode === 'preview' || (!$('confirm').checked && !confirmInVR)) {
        chooseMode('robot'); confirmInVR = true; mustRelease = true;
        flash('已选择实机。确认周围无人，松开 X/A 后再同时长按开启');
      } else {
        if (confirmInVR) { $('confirm').checked = true; confirmInVR = false; }
        startControl();
      }
    }
  }
  function buttons(side, pad, previous, now, viewer) {
    const released = key => !pad[key] && previous[key];
    const short = key => released(key) && now - (hands[side].since[key] || 0) < SHORT_MS;
    if (side === 'right' && pad.secondary && !previous.secondary) {
      stopControl('已按 B 急停'); flash('已急停：关节保持，底盘停车', true);
      chooseMode('preview');
      jogMode = false; inputReady = false;
      resetSim();
    }
    if (side === 'left' && pad.secondary && !previous.secondary) screenAdjusted = false;
    if (side === 'left' && released('secondary')) {
      if (!screenAdjusted && viewer) { placePanels(viewer); flash('画面已按当前视线摆放，含上下角度'); }
      else flash('屏幕位置已保存');
    }
    if (side === 'left' && pad.stickPress && !previous.stickPress) { gear = gear === 1 ? .5 : 1; flash(gear === 1 ? '底盘快档' : '底盘慢档 50%'); }
    if (side === 'right' && pad.stickPress && !previous.stickPress) { fine = !fine; flash(fine ? '手臂精细模式：位移减半' : '手臂正常比例'); }
    if (short('primary') && !combo && !mustRelease && viewer) {
      if (jogMode) {
        if (side === 'right') {
          if (hands.left.engage || Math.abs(hands.left.pad.stickY || 0) > .15) flash('先松开左握把和左摇杆，再选关节');
          else { jogJoint = JOG_ORDER[(JOG_ORDER.indexOf(jogJoint) + 1) % JOG_ORDER.length]; flash('逐关节：' + JOINTS[jogJoint]); }
        }
        return;
      }
      if (hands.left.engage || hands.right.engage) flash('松开握把后才能重新校准');
      else { recenter(viewer); flash('已重新校准正前方'); }
    }
  }

  // ---------- offline preview ----------
  function resetSim() { sim.left = simArm(); sim.right = simArm(); sim.head = [0, 0]; sim.base = [0, 0, 0]; }
  function simAnchor(side) {
    const a = sim[side];
    try { a.anchor = {g: M.anchor(a.lift, a.elbow, a.wrist), pan: a.pan, roll: a.roll, pitch0: null}; }
    catch (e) { a.anchor = null; flash('预演：' + e.message); }
  }
  let simTime = null;
  function simulate(now) {
    const dt = simTime === null ? 0 : Math.min(.1, (now - simTime) / 1000); simTime = now;
    const p = payload();
    if (p.jog) {
      const key = ({1: 'pan', 2: 'lift', 3: 'elbow', 4: 'wrist', 5: 'roll'})[p.jog.joint];
      sim.left[key] = M.clamp(sim.left[key] + p.jog.value * 6 * p.scale * dt, 160);
    }
    for (const side of ['left', 'right']) {
      const a = sim[side], input = p.arms[side];
      if (p.grip[side] !== null) a.grip = p.grip[side];
      if (!input || !a.anchor) { a.state = input ? '不可用' : '空闲'; continue; }
      if (a.anchor.pitch0 === null) { a.anchor.pitch0 = input.pitch; a.anchor.roll0 = input.roll; }
      const delta = input.pos.map(v => v * p.ratio);
      const pitch = settings.orient ? M.wrap(input.pitch - a.anchor.pitch0) : 0;
      try {
        if (input.pos.some(v => Math.abs(v) > .6)) throw new Error('手柄追踪跳变');
        const r = M.follow(a.anchor.g, delta, pitch);
        Object.assign(a, {lift: r.lift, elbow: r.elbow, wrist: r.wrist, pan: a.anchor.pan + r.pan,
          roll: settings.orient ? a.anchor.roll + M.wrap(input.roll - a.anchor.roll0) : a.roll, state: r.edge ? '到边界' : '跟随中'});
      } catch (e) { a.state = '到边界'; }
    }
    sim.head[0] = M.clamp(sim.head[0] + p.head[0] * 30 * dt, 90); sim.head[1] = M.clamp(sim.head[1] + p.head[1] * 30 * dt, 60);
    const v = p.base[0] * .2 * settings.scale, w = p.base[1] * .8 * settings.scale;
    sim.base[2] += w * dt; sim.base[0] += v * Math.cos(sim.base[2]) * dt; sim.base[1] += v * Math.sin(sim.base[2]) * dt;
  }

  // ---------- cameras ----------
  async function cameraLoop(name, index) {
    const c = cams[name] = {image: null, age: null, error: null, stop: false, fresh: false};
    while (!c.stop && xr) {
      try {
        const response = await fetch(`/api/platform/camera/${index}/live`, {signal: timeout(2500), cache: 'no-store'});
        if (!response.ok) throw new Error('无画面 ' + response.status);
        const age = response.headers.get('X-Frame-Age-Ms');
        c.age = age === null ? null : Number(age); c.receivedAt = performance.now();
        const next = await createImageBitmap(await response.blob());
        if (c.stop || !xr) { next.close(); break; }
        c.image?.close(); c.image = next; c.fresh = true; c.error = null;
        await sleep(90);
      } catch (e) { c.error = e.message; await sleep(1000); }
    }
    c.image?.close(); c.image = null;
  }
  const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
  // AbortSignal.timeout is missing on older headset browsers.
  function timeout(ms) { const c = new AbortController(); setTimeout(() => c.abort(), ms); return c.signal; }

  // ---------- feedback ----------
  let flashText = '', flashUntil = 0, flashBad = false;
  function flash(text, bad = false) { flashText = text; flashBad = bad; flashUntil = performance.now() + 2500; status(text, bad); }
  function pulse(sides, intensity, ms) {
    for (const side of sides) {
      const actuator = hands[side].source?.gamepad?.hapticActuators?.[0];
      try { actuator?.pulse?.(intensity, ms); } catch {}
    }
  }

  // ---------- rendering ----------
  const mat = {
    identity: () => new Float32Array([1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1]),
    translate(x, y, z) { const m = mat.identity(); m[12] = x; m[13] = y; m[14] = z; return m; },
    scale(x, y, z) { const m = mat.identity(); m[0] = x; m[5] = y; m[10] = z; return m; },
    rotY(t) { const m = mat.identity(), c = Math.cos(t), s = Math.sin(t); m[0] = c; m[2] = -s; m[8] = s; m[10] = c; return m; },
    rotX(t) { const m = mat.identity(), c = Math.cos(t), s = Math.sin(t); m[5] = c; m[6] = s; m[9] = -s; m[10] = c; return m; },
    mul(a, b) {
      const out = new Float32Array(16);
      for (let col = 0; col < 4; col++) for (let row = 0; row < 4; row++) {
        let sum = 0; for (let k = 0; k < 4; k++) sum += a[k * 4 + row] * b[col * 4 + k]; out[col * 4 + row] = sum;
      }
      return out;
    },
  };
  function createRenderer(gl) {
    const shader = (type, source) => { const s = gl.createShader(type); gl.shaderSource(s, source); gl.compileShader(s); return s; };
    const program = gl.createProgram();
    gl.attachShader(program, shader(gl.VERTEX_SHADER, 'attribute vec3 p;attribute vec2 uv;uniform mat4 P,V,W;varying vec2 t;void main(){t=uv;gl_Position=P*V*W*vec4(p,1.);}'));
    gl.attachShader(program, shader(gl.FRAGMENT_SHADER, 'precision mediump float;varying vec2 t;uniform sampler2D s;uniform vec4 c;uniform float u;void main(){gl_FragColor=u>.5?texture2D(s,t):c;}'));
    gl.linkProgram(program); gl.useProgram(program);
    const loc = name => gl.getUniformLocation(program, name), attr = name => gl.getAttribLocation(program, name);
    const buffer = data => { const b = gl.createBuffer(); gl.bindBuffer(gl.ARRAY_BUFFER, b); gl.bufferData(gl.ARRAY_BUFFER, new Float32Array(data), gl.STATIC_DRAW); return b; };
    const quad = buffer([-.5, -.5, 0, 0, 1, .5, -.5, 0, 1, 1, .5, .5, 0, 1, 0, -.5, -.5, 0, 0, 1, .5, .5, 0, 1, 0, -.5, .5, 0, 0, 0]);
    const faces = [[0, 1, 2, 0, 2, 3], [4, 6, 5, 4, 7, 6], [0, 4, 5, 0, 5, 1], [3, 2, 6, 3, 6, 7], [0, 3, 7, 0, 7, 4], [1, 5, 6, 1, 6, 2]];
    const corner = i => [(i & 1) ^ ((i >> 1) & 1) ? .5 : -.5, i & 2 ? .5 : -.5, i & 4 ? .5 : -.5];
    const cube = buffer(faces.flat().flatMap(i => [...corner(i), 0, 0]));
    const texture = () => {
      const t = gl.createTexture(); gl.bindTexture(gl.TEXTURE_2D, t);
      for (const [k, v] of [[gl.TEXTURE_MIN_FILTER, gl.LINEAR], [gl.TEXTURE_MAG_FILTER, gl.LINEAR], [gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE], [gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE]]) gl.texParameteri(gl.TEXTURE_2D, k, v);
      gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, 1, 1, 0, gl.RGBA, gl.UNSIGNED_BYTE, new Uint8Array([40, 44, 50, 255]));
      return t;
    };
    const textures = {head: texture(), left: texture(), right: texture(), status: texture(), preview: texture()};
    const statusCanvas = document.createElement('canvas'); statusCanvas.width = 1024; statusCanvas.height = 768;
    const previewCanvas = document.createElement('canvas'); previewCanvas.width = 1536; previewCanvas.height = 640;
    let statusDrawn = 0;
    const panels = [
      {name: 'head', at: [0, 0, -1.6], size: [1.28, .96]},
      {name: 'left', at: [-1.0, -.6, -1.45], size: [.56, .42]},
      {name: 'right', at: [1.0, -.6, -1.45], size: [.56, .42]},
      {name: 'status', at: [0, -.8, -1.3], size: [.88, .44]},
      {name: 'preview', at: [0, 0, -1.8], size: [1.65, .6875]},
    ];
    const bind = (b, stride) => {
      gl.bindBuffer(gl.ARRAY_BUFFER, b);
      gl.enableVertexAttribArray(attr('p')); gl.vertexAttribPointer(attr('p'), 3, gl.FLOAT, false, stride, 0);
      gl.enableVertexAttribArray(attr('uv')); gl.vertexAttribPointer(attr('uv'), 2, gl.FLOAT, false, stride, 12);
    };
    gl.enable(gl.DEPTH_TEST);
    return {
      draw(frame, viewer, now) {
        for (const name of ['head', 'left', 'right']) {
          const c = cams[name];
          if (c?.fresh && c.image) { gl.bindTexture(gl.TEXTURE_2D, textures[name]); gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, gl.RGBA, gl.UNSIGNED_BYTE, c.image); c.fresh = false; }
        }
        if (now - statusDrawn > 200) {
          drawStatus(statusCanvas); statusDrawn = now;
          gl.bindTexture(gl.TEXTURE_2D, textures.status); gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, gl.RGBA, gl.UNSIGNED_BYTE, statusCanvas);
          drawPreview(previewCanvas);
          gl.bindTexture(gl.TEXTURE_2D, textures.preview); gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, gl.RGBA, gl.UNSIGNED_BYTE, previewCanvas);
        }
        const layer = xr.renderState.baseLayer;
        gl.bindFramebuffer(gl.FRAMEBUFFER, layer.framebuffer);
        gl.clearColor(.07, .08, .1, 1); gl.clear(gl.COLOR_BUFFER_BIT | gl.DEPTH_BUFFER_BIT);
        for (const view of viewer.views) {
          const v = layer.getViewport(view); gl.viewport(v.x, v.y, v.width, v.height);
          gl.uniformMatrix4fv(loc('P'), false, view.projectionMatrix);
          gl.uniformMatrix4fv(loc('V'), false, view.transform.inverse.matrix);
          bind(quad, 20); gl.uniform1f(loc('u'), 1);
          for (const panel of panels) {
            const preview = settings.mode === 'preview';
            if (panel.name === 'preview' && !preview) continue;
            let at = panel.at, size = panel.size;
            if (preview && panel.name === 'head') { at = [0, .7, -1.8]; size = [.64, .48]; }
            if (panel.name === 'status') { at = [0, -1, -1.6]; size = [1.2, .9]; }
            gl.bindTexture(gl.TEXTURE_2D, textures[panel.name]);
            const screen = mat.mul(placement, mat.translate(settings.screenX, settings.screenY, 0));
            gl.uniformMatrix4fv(loc('W'), false, mat.mul(screen, mat.mul(mat.translate(...at), mat.scale(...size, 1))));
            gl.drawArrays(gl.TRIANGLES, 0, 6);
          }
          bind(cube, 20); gl.uniform1f(loc('u'), 0);
          for (const side of ['left', 'right']) {
            const h = hands[side]; if (!h.tracked || !h.pose) continue;
            const engaged = !!h.engage, base = side === 'left' ? [.2, .55, 1] : [1, .55, .2];
            gl.uniform4fv(loc('c'), engaged ? [...base, 1] : base.map(x => x * .45).concat(1));
            gl.uniformMatrix4fv(loc('W'), false, mat.mul(h.pose.transform.matrix, mat.scale(.035, .035, .09)));
            gl.drawArrays(gl.TRIANGLES, 0, 36);
          }
        }
      },
    };
  }

  const ARM_STATE = {idle: '空闲', following: '跟随中', adjusting: '逐关节调整', boundary: '到边界', unavailable: '不可用'};
  function drawStatus(canvas) {
    const g = canvas.getContext('2d'), W = canvas.width, H = canvas.height;
    g.fillStyle = '#11161c'; g.fillRect(0, 0, W, H);
    g.fillStyle = '#e8edf2'; g.font = 'bold 34px system-ui, sans-serif';
    const preview = settings.mode === 'preview';
    const head = preview ? '预演模式（不连机器人）' : control ? '实机控制中' : '实机控制未开启';
    g.fillText(head, 28, 52);
    g.font = '24px system-ui, sans-serif'; g.fillStyle = control ? '#86efac' : '#fbbf24';
    const hint = preview ? '三视图在正前方；长按 X+A 选择实机' : (robotNote || '确认周围无人后长按 X+A 开启；B 急停');
    wrapText(g, hint, 28, 90, W - 56, 30);
    g.fillStyle = '#cbd5e1'; g.font = '23px system-ui, sans-serif';
    const lines = [];
    lines.push(jogMode ? `左臂逐关节：${JOINTS[jogJoint]} · A 选择 · 左握把＋左摇杆前后` : '手部跟随 · 单独长按 X 切换左臂逐关节准备');
    for (const side of ['left', 'right']) {
      const name = side === 'left' ? '左臂' : '右臂';
      const c = readiness?.channels?.[side];
      if (preview) lines.push(`${name}：${sim[side].state || '空闲'}　夹爪 ${Math.round((hands[side].pad.trigger || 0) * 100)}%`);
      else if (c && !c.enabled) lines.push(`${name}受限：${c.reason}`);
      else {
        const a = robot?.vr?.arms?.[side], gr = robot?.vr?.grip?.[side];
        lines.push(`${name}：${ARM_STATE[a?.state] || '未开启'}　夹爪：${gr?.holding ? '轻夹保持' : gr?.active ? '跟随扳机' : '未接管'}`);
        if (a?.reason) lines.push(a.reason);
        if (gr?.reason) lines.push(gr.reason);
        if (c) lines.push(c.follow_ready ? `${name}跟随已就绪：可松开重握` : `${name}跟随待准备：${c.follow_reason}`);
      }
    }
    if (!preview) {
      for (const key of ['head', 'base']) {
        const c = readiness?.channels?.[key];
        if (c && !c.enabled) lines.push(`${key === 'head' ? '头部' : '底盘'}受限：${c.reason}`);
      }
      if (jogMode) lines.push(`当前编码器 ${readiness?.channels?.left?.joints?.[jogJoint]?.position ?? '—'} · 最大目标速率 6°/s × 速度比例 · 松开保持`);
      lines.push(`往返 ${rtt ?? '—'} ms　服务端 ${robot?.vr?.tick_ms ?? '—'} ms`);
    }
    lines.push(`底盘 ${gear === 1 ? '快档' : '慢档'}　手臂比例 ${(settings.ratio * (fine ? .5 : 1)).toFixed(2)}×　速度 ${Math.round(settings.scale * 100)}%`);
    lines.push('画面帧龄 ' + ['head', 'left', 'right'].map(n => `${({head: '头', left: '左', right: '右'})[n]} ${cams[n]?.error ? '无' : cams[n]?.age != null ? Math.round(cams[n].age + performance.now() - cams[n].receivedAt) + 'ms' : '—'}`).join('　'));
    lines.push('Y 按视线摆屏；Y＋右摇杆上下左右调屏；B 停止并返回预演');
    lines.push(`按键 左[${lastPads.left.join(',')}] 右[${lastPads.right.join(',')}]`);
    let y = 158;
    for (const line of lines) { y = wrapText(g, line, 28, y, W - 56, 28) + 34; }
    if (performance.now() < flashUntil) {
      g.fillStyle = flashBad ? '#7f1d1d' : '#1e3a8a'; g.fillRect(0, H - 84, W, 84);
      g.fillStyle = '#fff'; g.font = 'bold 23px system-ui, sans-serif';
      wrapText(g, flashText, 28, H - 50, W - 56, 28);
    }
  }

  function wrapText(g, text, x, y, width, line) {
    let current = '';
    for (const ch of String(text)) {
      if (g.measureText(current + ch).width > width) { g.fillText(current, x, y); y += line; current = ch; } else current += ch;
    }
    if (current) g.fillText(current, x, y);
    return y;
  }
  function drawPreview(canvas) {
    const g = canvas.getContext('2d'), W = canvas.width, H = canvas.height;
    g.fillStyle = '#11161c'; g.fillRect(0, 0, W, H);
    g.font = 'bold 30px "Microsoft YaHei", "Segoe UI", sans-serif'; g.fillStyle = '#e8edf2';
    g.fillText('双臂三视图 · 预演', 24, 42);
    g.font = '24px "Microsoft YaHei", "Segoe UI", sans-serif';
    g.fillStyle = '#60a5fa'; g.fillText('左臂', W - 280, 42);
    g.fillStyle = '#fb923c'; g.fillText('右臂', W - 180, 42);
    const views = [['front', '正视 · 左右／上下'], ['side', '侧视 · 前后／上下'], ['top', '俯视 · 左右／前后']];
    const gap = 16, boxW = (W - 4 * gap) / 3, boxH = H - 164;
    for (const [i, [view, title]] of views.entries()) {
      const x = gap + i * (boxW + gap), y = 64;
      g.strokeStyle = '#475569'; g.lineWidth = 2; g.strokeRect(x, y, boxW, boxH);
      g.fillStyle = '#e8edf2'; g.fillText(title, x + 16, y + 36);
      const area = {x: x + 12, y: y + 56, w: boxW - 24, h: boxH - 100};
      const ox = view === 'side' ? area.x + area.w * .25 : area.x + area.w / 2;
      const oy = view === 'top' ? area.y + area.h * .82 : area.y + area.h * .65;
      const scale = Math.min(area.w / .95, area.h / .65), grid = .05 * scale;
      g.save(); g.beginPath(); g.rect(area.x, area.y, area.w, area.h); g.clip();
      g.lineWidth = 1; g.strokeStyle = '#273444'; g.beginPath();
      for (let a = ox - Math.ceil((ox - area.x) / grid) * grid; a < area.x + area.w; a += grid) {
        if (a < area.x) continue; g.moveTo(a, area.y); g.lineTo(a, area.y + area.h);
      }
      for (let b = oy - Math.ceil((oy - area.y) / grid) * grid; b < area.y + area.h; b += grid) {
        if (b < area.y) continue; g.moveTo(area.x, b); g.lineTo(area.x + area.w, b);
      }
      g.stroke();
      const pixel = p => { const q = M.project(p, view); return [ox + q[0] * scale, oy + q[1] * scale]; };
      for (const side of ['left', 'right']) {
        const a = sim[side], pts = M.armPoints(side, a).map(pixel);
        g.strokeStyle = side === 'left' ? '#60a5fa' : '#fb923c';
        g.fillStyle = g.strokeStyle; g.lineWidth = 6;
        // Side projection can overlap; dash the right arm so both remain legible.
        g.setLineDash(view === 'side' && side === 'right' ? [12, 8] : []);
        g.beginPath(); pts.forEach((p, n) => n ? g.lineTo(...p) : g.moveTo(...p)); g.stroke(); g.setLineDash([]);
        for (const p of pts.slice(0, -1)) { g.beginPath(); g.arc(...p, 5, 0, 2 * Math.PI); g.fill(); }
        const jaw = pts[pts.length - 1]; g.beginPath(); g.arc(...jaw, 6 + 9 * (1 - a.grip), 0, 2 * Math.PI); g.stroke();
      }
      g.restore(); g.fillStyle = '#aebccd';
      g.font = '21px "Microsoft YaHei", "Segoe UI", sans-serif'; g.fillText('网格 5 cm', x + 16, y + boxH - 16);
      g.font = '24px "Microsoft YaHei", "Segoe UI", sans-serif';
    }
    g.fillStyle = '#cbd5e1'; g.font = '23px "Microsoft YaHei", "Segoe UI", sans-serif';
    g.fillText(`头部 水平 ${sim.head[0].toFixed(0)}° 俯仰 ${sim.head[1].toFixed(0)}°　底盘 ${sim.base[0].toFixed(2)} m / ${sim.base[1].toFixed(2)} m`, 24, H - 64);
    g.fillStyle = '#aebccd'; g.fillText(jogMode ? `左臂逐关节 ${JOINTS[jogJoint]} · 模型示意，非实机位置／碰撞判定` :
      '握把跟随 · 松开重置　模型示意，非实机位置／碰撞判定', 24, H - 24);
  }

  // ---------- lifecycle ----------
  document.addEventListener('visibilitychange', () => { if (document.hidden) stopControl('页面切到后台，已停止'); });
  window.addEventListener('pagehide', () => stopControl('页面关闭，已停止'));
  bindSettings(); checkReady(); pollRobot(); pollReadiness();
  setInterval(() => { pollRobot(); pollReadiness(); }, 1500);
  const preview2d = $('preview-views');
  drawPreview(preview2d); setInterval(() => drawPreview(preview2d), 200);
  window.XLeRobotVR = {hands, sim, settings, payload, drawStatus, drawPreview,
    state: () => ({control: !!control, robot, robotNote, readiness, confirmInVR, jogMode, jogJoint, inputReady,
      xrActive: !!xr, placement: placement ? Array.from(placement) : null})};
})();
