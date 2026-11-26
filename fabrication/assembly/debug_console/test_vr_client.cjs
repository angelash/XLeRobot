// Exercise the real VR page with synthetic WebXR/HTTP; no hardware or browser extension.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const M = require('./platform_static/vr-mapping.js');
const source = fs.readFileSync(path.join(__dirname, 'platform_static/vr.js'), 'utf8');

const settle = async () => { for (let i = 0; i < 16; i++) await Promise.resolve(); };
const eventTarget = () => ({
  listeners: {},
  addEventListener(name, callback) { (this.listeners[name] ||= []).push(callback); },
  emit(name) { return Promise.all((this.listeners[name] || []).map(fn => fn())); },
});

async function page({delayStart = false} = {}) {
  const nodes = new Map(), calls = [];
  const node = () => ({...eventTarget(), value: '', checked: false, hidden: false,
    classList: {toggle() {}}, querySelector() { return this; }});
  const drawing = new Proxy({measureText: s => ({width: s.length * 10})},
    {get: (o, k) => o[k] || (() => {})});
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
  const startReply = () => ({active: true, mode: 'vr', session_id: 'mock-session', vr: {arms: {}}});
  const fetch = async (url, opts = {}) => {
    const body = opts.body && JSON.parse(opts.body);
    if (opts.method === 'POST') calls.push({url, body});
    let data = {active: false};
    if (url === '/api/web/start') {
      data = delayStart ? await new Promise(resolve => { resolveStart = () => resolve(startReply()); }) : startReply();
    } else if (url === '/api/web/vr-input') data = {active: true, mode: 'vr', vr: {arms: {}}};
    return {ok: !url.includes('/camera/'), status: 503, json: async () => data};
  };
  const window = {...eventTarget(), VRMapping: M, isSecureContext: true};
  const context = {window, document, navigator: {xr: {
    isSessionSupported: async () => true, requestSession: async () => session}},
    localStorage: {getItem: () => null, setItem() {}}, location: {protocol: 'http:', hostname: 'localhost'},
    fetch, performance: {now: () => 0}, AbortController, setTimeout() {}, setInterval() {},
    XRWebGLLayer: function () {}, Float32Array, Uint8Array, console};
  vm.runInNewContext(source, context, {filename: 'vr.js'});
  await settle();
  const api = window.XLeRobotVR;
  api.settings.mode = 'robot'; document.getElementById('confirm').checked = true;
  await document.getElementById('enter').emit('click');
  const transform = {position: {x: 0, y: 1.6, z: 0}, orientation: {x: 0, y: 0, z: 0, w: 1}};
  const viewer = {transform, views: []};
  return {api, session, calls, document, window,
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
