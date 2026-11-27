// Exercise the real VR page with synthetic WebXR/HTTP; no hardware or browser extension.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const M = require('./platform_static/vr-mapping.js');
const Model = require('./platform_static/vr-model.js');
const Preview = require('./platform_static/vr-preview.js');
const source = fs.readFileSync(path.join(__dirname, 'platform_static/vr.js'), 'utf8');

const settle = async () => { for (let i = 0; i < 16; i++) await Promise.resolve(); };
const eventTarget = () => ({
  listeners: {},
  addEventListener(name, callback) { (this.listeners[name] ||= []).push(callback); },
  emit(name) { return Promise.all((this.listeners[name] || []).map(fn => fn())); },
});

async function page({delayStart = false, preview = false, feedback = null} = {}) {
  const nodes = new Map(), calls = [], drawn = [];
  const drawing = new Proxy({measureText: s => ({width: s.length * 10}), fillText: s => drawn.push(s)},
    {get: (o, k) => o[k] || (() => {})});
  const node = () => ({...eventTarget(), value: '', checked: false, hidden: false, width: 1536, height: 640,
    getContext: () => drawing,
    classList: {toggle() {}}, querySelector() { return this; }});
  const gl = new Proxy({getAttribLocation: () => 0},
    {get: (o, k) => o[k] || (() => ({}))});
  const document = {...eventTarget(), hidden: false,
    getElementById(id) { if (!nodes.has(id)) nodes.set(id, node()); return nodes.get(id); },
    querySelectorAll() { return []; },
    createElement() { return {getContext: name => name === '2d' ? drawing : gl}; }};
  const inputs = ['left', 'right'].map(handedness => ({handedness, gripSpace: {handedness},
    gamepad: {axes: [0, 0, 0, 0], buttons: Array.from({length: 6}, () => ({pressed: false, value: 0}))}}));
  const session = {...eventTarget(), visibilityState: 'visible', inputSources: inputs,
    renderState: {baseLayer: {framebuffer: {}, getViewport: () => ({x: 0, y: 0, width: 1, height: 1})}},
    requestAnimationFrame(fn) { this.frame = fn; },
    async requestReferenceSpace() { return {}; },
    updateRenderState() {}, async end() { await this.emit('end'); }};
  let resolveStart;
  const startReply = () => ({active: true, mode: 'vr', session_id: 'mock-session', vr: {arms: {}, feedback}});
  const fetch = async (url, opts = {}) => {
    const body = opts.body && JSON.parse(opts.body);
    if (opts.method === 'POST') calls.push({url, body});
    let data = {active: false, channels: {}, feedback};
    if (url === '/api/web/start') {
      data = delayStart ? await new Promise(resolve => { resolveStart = () => resolve(startReply()); }) : startReply();
    } else if (url === '/api/web/vr-input') data = {active: true, mode: 'vr', vr: {arms: {}, feedback}};
    return {ok: !url.includes('/camera/'), status: 503, json: async () => data};
  };
  const window = {...eventTarget(), VRMapping: M, VRModel: Model, VRPreview: Preview, isSecureContext: true};
  const context = {window, document, navigator: {xr: {
    isSessionSupported: async () => true, requestSession: async () => session}},
    localStorage: {getItem: () => null, setItem() {}}, location: {protocol: 'http:', hostname: 'localhost'},
    fetch, performance: {now: () => 0}, AbortController, setTimeout() {}, setInterval() {},
    XRWebGLLayer: function () {}, Float32Array, Uint8Array, console};
  vm.runInNewContext(source, context, {filename: 'vr.js'});
  await settle();
  const api = window.XLeRobotVR;
  api.settings.mode = preview ? 'preview' : 'robot'; document.getElementById('confirm').checked = !preview;
  await document.getElementById('enter').emit('click');
  const transform = {position: {x: 0, y: 1.6, z: 0}, orientation: {x: 0, y: 0, z: 0, w: 1}};
  const viewer = {transform, views: []};
  return {api, session, calls, document, window, drawn, viewer,
    async frame(time, visible = true) {
      session.frame(time, {getViewerPose: () => visible ? viewer : null,
        getPose: () => ({transform})});
      await settle();
    },
    button(side, index, pressed) { Object.assign(inputs[side === 'left' ? 0 : 1].gamepad.buttons[index],
      {pressed, value: pressed ? 1 : 0}); },
    stick(side, x, y) { inputs[side === 'left' ? 0 : 1].gamepad.axes = [0, 0, x, y]; },
    resolveStart: async () => { resolveStart(); await settle(); },
  };
}

async function arm(p) {
  p.button('left', 4, true); p.button('right', 4, true);
  await p.frame(0); await p.frame(1600);
}

const tests = {
  async 'both buttons must remain held for the whole arming interval'() {
    const p = await page();
    p.button('left', 4, true); p.button('right', 4, true); await p.frame(0);
    p.button('left', 4, false); await p.frame(700); await p.frame(1600);
    assert.equal(p.calls.filter(c => c.url === '/api/web/start').length, 0);
    p.button('left', 4, true); await p.frame(1700); await p.frame(2400);
    assert.equal(p.calls.length, 0);
    await p.frame(3300); assert.equal(p.api.state().control, true);
  },
  async 'B stops control and a held combo cannot rearm it'() {
    const p = await page(); await arm(p); assert.equal(p.api.state().control, true);
    await p.frame(1700);
    p.button('right', 5, true); await p.frame(1800); await p.frame(3600);
    assert.equal(p.api.state().control, false);
    assert.equal(p.calls.filter(c => c.url === '/api/web/stop').length, 1);
    assert.equal(p.calls.filter(c => c.url === '/api/web/start').length, 1);
  },
  async 'exit during a delayed start cancels and stops the returned session'() {
    const p = await page({delayStart: true}); await arm(p);
    await p.session.end(); await p.resolveStart();
    assert.equal(p.api.state().control, false);
    assert.equal(p.calls.filter(c => c.url === '/api/web/stop').length, 1);
  },
  async 'B cancels a delayed start before its reply'() {
    const p = await page({delayStart: true}); await arm(p);
    p.button('right', 5, true); await p.frame(1700); await p.resolveStart();
    assert.equal(p.api.state().control, false);
    assert.equal(p.calls.filter(c => c.url === '/api/web/stop').length, 1);
  },
  async 'losing the viewer pose stops driving'() {
    const p = await page(); await arm(p); p.stick('left', 0, -1); await p.frame(1700);
    assert.ok(p.calls.some(c => c.url === '/api/web/vr-input' && c.body.base[0] === 1));
    await p.frame(1800, false);
    assert.equal(p.api.state().control, false);
    assert.equal(p.calls.at(-1).url, '/api/web/stop');
  },
  async 'a hidden page stops and requires a new arming gesture'() {
    const p = await page(); await arm(p);
    p.document.hidden = true; await p.document.emit('visibilitychange'); await settle();
    await p.frame(1700); await p.frame(3500);
    assert.equal(p.api.state().control, false);
    assert.equal(p.calls.filter(c => c.url === '/api/web/start').length, 1);
  },
  async 'a missing controller cannot release an object by inventing a zero trigger'() {
    const p = await page(); await arm(p);
    p.button('left', 0, true); await p.frame(1700);
    p.session.inputSources = p.session.inputSources.filter(s => s.handedness !== 'left');
    await p.frame(1800);
    const input = p.calls.filter(c => c.url === '/api/web/vr-input').at(-1).body;
    assert.equal(input.grip.left, null);
    assert.equal(input.arms.left, null);
    assert.deepEqual(input.base, [0, 0]);
  },
  async 'preview buttons cannot switch to live control'() {
    const p = await page({preview: true}); await arm(p);
    assert.equal(p.api.settings.mode, 'preview'); assert.equal(p.api.state().confirmInVR, false);
    assert.equal(p.calls.filter(c => c.url === '/api/web/start').length, 0);
    await p.frame(3300); assert.equal(p.calls.length, 0);
    p.button('right', 5, true); await p.frame(3400);
    assert.equal(p.api.settings.mode, 'preview');
  },
  async 'live confirmation and B stop keep the live mode and comparison display'() {
    const p = await page(); p.document.getElementById('confirm').checked = false;
    await arm(p);
    assert.equal(p.api.settings.mode, 'robot'); assert.equal(p.api.state().confirmInVR, true);
    assert.equal(p.calls.filter(c => c.url === '/api/web/start').length, 0);
    p.button('left', 4, false); p.button('right', 4, false); await p.frame(3400);
    p.button('left', 4, true); p.button('right', 4, true); await p.frame(3500); await p.frame(5100);
    assert.equal(p.api.state().control, true);
    p.button('right', 5, true); await p.frame(5200);
    assert.equal(p.api.settings.mode, 'robot'); assert.equal(p.api.state().control, false);
    assert.deepEqual(Array.from(p.api.panelLayout(), p => p.name).sort(), ['head','left','preview','right','status']);
    // In live mode every panel is at the same depth; the video, model and
    // status rectangles must remain visible without covering one another.
    const panels = p.api.panelLayout();
    for (const [i,a] of panels.entries()) for (const b of panels.slice(i+1)) {
      assert.ok(Math.abs(a.at[0]-b.at[0]) >= (a.size[0]+b.size[0])/2 ||
        Math.abs(a.at[1]-b.at[1]) >= (a.size[1]+b.size[1])/2, `${a.name} overlaps ${b.name}`);
    }
    await p.document.getElementById('choose-robot').emit('click');
    assert.equal(p.api.settings.mode, 'robot');
  },
  async 'live model shows sent targets and encoder feedback rather than free hand simulation'() {
    const measured = {pan: 0, lift: -105, elbow: 98, wrist: 3.4, roll: 0, grip: .1};
    const target = {...measured, lift: -100, roll: 20};
    const feedback = {source:'encoder_cache', sampled_at:'2026-11-27T18:00:00+08:00', age_ms:50,
      arms:{left:{measured,target},right:{measured:null,target:null}},head:[0,0]};
    const p = await page({feedback}); await arm(p);
    p.stick('right', .8, 0); p.button('left', 0, true); await p.frame(1700);
    assert.equal(p.api.displayModel().left.lift, -100);
    assert.equal(p.api.displayModel().measured.left.lift, -105);
    assert.equal(p.api.displayModel().right, null);
    p.api.drawPreview(p.document.getElementById('preview-views'));
    assert.ok(p.drawn.includes('双臂三视图 · 实机目标与反馈'));
    assert.ok(p.drawn.some(t => t.includes('白线：编码器反馈')));
    assert.ok(p.drawn.some(t => t.includes('右：等待有效标定／编码器反馈')));
  },
  async 'live display labels stale feedback and preserves unknown arm data'() {
    const p = await page({feedback:{source:'encoder_cache',age_ms:2200,arms:{},head:[0,0]}});
    p.api.drawPreview(p.document.getElementById('preview-views'));
    assert.equal(p.api.displayModel().left, null);
    assert.ok(p.drawn.some(t => t.includes('反馈过期／未知')));
  },
  async 'screen recenter follows head pitch as well as yaw'() {
    const p = await page();
    p.viewer.transform.orientation = {x: Math.sin(Math.PI / 12), y: 0, z: 0, w: Math.cos(Math.PI / 12)};
    await p.frame(0);
    const matrix = p.api.state().placement;
    assert.ok(Math.abs(matrix[6] - .5) < 1e-5);
    assert.ok(Math.abs(matrix[9] + .5) < 1e-5);
  },
  async 'Y plus right stick moves screen vertically and suppresses all robot motion inputs'() {
    const p = await page(); await arm(p);
    p.button('left', 4, false); p.button('right', 4, false); await p.frame(1700);
    p.button('left', 5, true); p.stick('right', 0, -1); p.stick('left', 0, -1);
    await p.frame(1800); await p.frame(1900);
    assert.ok(p.api.settings.screenY > 0);
    const input = p.calls.filter(c => c.url === '/api/web/vr-input').at(-1).body;
    assert.deepEqual(input.head, [0, 0]); assert.deepEqual(input.base, [0, 0]);
    assert.equal(input.arms.left, null); assert.equal(input.grip.right, null);
    const y = p.api.settings.screenY;
    p.button('left', 5, false); await p.frame(2000);
    assert.equal(p.api.settings.screenY, y);
  },
  async 'three orthographic views render together'() {
    const p = await page({preview: true});
    for (const label of ['正视 · 左右／上下', '侧视 · 前后／上下', '俯视 · 左右／前后'])
      assert.ok(p.drawn.includes(label), label);
  },
  async 'page height setting survives initial XR placement'() {
    const p = await page(); p.api.settings.screenY = .3;
    await p.frame(0); assert.equal(p.api.settings.screenY, .3);
  },
  async 'live arming requires neutral triggers and sticks'() {
    const p = await page(); p.stick('left', 0, -1); await arm(p);
    assert.equal(p.api.state().control, false);
    assert.equal(p.calls.filter(c => c.url === '/api/web/start').length, 0);
  },
  async 'left joint adjustment requires a new grip and mutes base and hand following'() {
    const p = await page(); await arm(p);
    p.button('left', 4, false); p.button('right', 4, false); await p.frame(1700);
    p.button('left', 4, true); await p.frame(1800); await p.frame(3400);
    assert.equal(p.api.state().jogMode, true);
    p.button('left', 4, false); await p.frame(3500);
    p.stick('left', 0, -1); await p.frame(3600);
    assert.equal(p.api.payload().jog, null);
    p.button('left', 1, true); await p.frame(3700);
    const input = p.api.payload();
    assert.equal(input.jog.side, 'left'); assert.equal(input.jog.joint, 2); assert.equal(input.jog.value, 1);
    assert.deepEqual(Array.from(input.base), [0, 0]); assert.equal(input.arms.left, null); assert.equal(input.grip.left, null);
    p.button('left', 1, false); await p.frame(3800); assert.equal(p.api.payload().jog, null);
    p.stick('left', 0, 0); await p.frame(3900);
    p.button('right', 4, true); await p.frame(4000); p.button('right', 4, false); await p.frame(4100);
    assert.equal(p.api.state().jogJoint, 3);
  },
  async 'changing joint mode refuses held motion controls'() {
    const p = await page(); p.button('left', 1, true);
    p.button('left', 4, true); await p.frame(0); await p.frame(1600);
    assert.equal(p.api.state().jogMode, false);
  },
  async 'screen adjustment waits for neutral inputs before restoring robot controls'() {
    const p = await page(); await arm(p);
    p.button('left', 4, false); p.button('right', 4, false); await p.frame(1700);
    p.button('left', 5, true); p.stick('right', 0, -1); await p.frame(1800);
    p.button('left', 5, false); await p.frame(1900);
    assert.deepEqual(Array.from(p.api.payload().head), [0, 0]);
    p.stick('right', 0, 0); await p.frame(2000);
    p.stick('right', 0, -1); await p.frame(2100);
    assert.equal(p.api.payload().head[1], 1);
  },
  async 'a released X+A combo cannot reuse its hold time for a joint mode change'() {
    const p = await page(); await arm(p);
    p.button('right', 4, false); await p.frame(1700);
    assert.equal(p.api.state().jogMode, false);
    await p.frame(1800); assert.equal(p.api.state().jogMode, false);
  },
};
(async () => {
  let failures = 0;
  for (const [name, test] of Object.entries(tests)) {
    try { await test(); console.log('PASS', name); }
    catch (error) { failures++; console.error('FAIL', name, error.message); }
  }
  console.log(`${Object.keys(tests).length - failures}/${Object.keys(tests).length} VR client checks passed`);
  process.exitCode = failures ? 1 : 0;
})();
