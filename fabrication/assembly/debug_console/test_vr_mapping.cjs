// PICO page mapping: buttons, body frame, and parity with vr_kinematics.py.
const assert = require('assert');
const fs = require('fs');
const path = require('path');
const M = require('./platform_static/vr-mapping.js');

const near = (a, b, eps = 1e-4) => assert.ok(Math.abs(a - b) < eps, `${a} != ${b}`);

// Reference values produced by fabrication/assembly/debug_console/vr_kinematics.py.
const python = [
  {q: [0, 40, -30], d: [.03, .02, -.01], p: 10, r: [3.887272, 20.951851, 24.621695, -45.573546], edge: false},
  {q: [20, 60, 10], d: [-.02, 0, .04], p: -15, r: [0, -2.262092, 60.320454, 46.941637], edge: false},
  {q: [0, 40, -30], d: [.3, 0, 0], p: 0, r: [0, 66.593433, -63.560302, 6.966869], edge: true},
];
for (const c of python) {
  const r = M.follow(M.anchor(...c.q), c.d, c.p);
  [r.pan, r.lift, r.elbow, r.wrist].forEach((v, i) => near(v, c.r[i]));
  assert.strictEqual(r.edge, c.edge);
}
// Zero displacement reproduces the anchor; jaw moves by the hand displacement.
const g = M.anchor(0, 40, -30), still = M.follow(g, [0, 0, 0]);
near(still.lift, 0); near(still.elbow, 40); near(still.wrist, -30); near(still.pan, 0);
const up = M.follow(g, [0, 0, .05]), jaw0 = M.jawPoint(0, 40, -30), jaw1 = M.jawPoint(up.lift, up.elbow, up.wrist);
near(jaw1[0], jaw0[0]); near(jaw1[1] - jaw0[1], .05);
assert.throws(() => M.follow(g, [.7, 0, 0]), /0.6/);
assert.throws(() => M.anchor(0, 175, 0));

// Buttons: xr-standard and the upstream XLeVR order; the active stick pair wins.
const button = (pressed, value = pressed ? 1 : 0) => ({pressed, value});
const pad = {buttons: [button(false, .4), button(true), button(false), button(true), button(true), button(false)], axes: [0, 0, .5, -.8]};
const standard = M.readPad(pad);
assert.deepStrictEqual([standard.trigger, standard.squeeze, standard.stickPress, standard.primary, standard.secondary],
  [.4, true, true, true, false]);
assert.deepStrictEqual([standard.stickX, standard.stickY], [.5, -.8]);
assert.deepStrictEqual(standard.pressed, [1, 3, 4]);
const upstream = M.readPad(pad, 'xlevr');
assert.deepStrictEqual([upstream.stickPress, upstream.primary, upstream.secondary], [false, true, true]);
assert.deepStrictEqual([M.readPad({buttons: [], axes: [.3, .9]}).stickX, M.readPad({buttons: [], axes: [.3, .9]}).stickY], [.3, .9]);
assert.strictEqual(M.readPad(undefined).trigger, 0);

// Sticks: pushing forward (negative y) drives forward and tilts the head up; left turns left.
assert.deepStrictEqual(M.drive(0, -1), [1, 0]);
assert.deepStrictEqual(M.drive(-1, 0, .5), [0, .5]);
assert.deepStrictEqual(M.drive(.1, .1), [0, 0]);
assert.deepStrictEqual(M.head(1, -1), [1, 1]);

// Body frame: facing -Z, -Z is forward and -X is left; turned 90° left, -X is forward.
const turn = a => [0, Math.sin(a / 2), 0, Math.cos(a / 2)];
near(M.yaw(turn(0)), 0); near(M.yaw(turn(Math.PI / 2)), Math.PI / 2);
assert.deepStrictEqual(M.toBody([0, .1, -.2], 0).map(v => +v.toFixed(6)), [.2, 0, .1]);
assert.deepStrictEqual(M.toBody([-.3, 0, 0], 0).map(v => +v.toFixed(6)), [0, .3, 0]);
assert.deepStrictEqual(M.toBody([-.3, 0, 0], Math.PI / 2).map(v => +v.toFixed(6)), [.3, 0, 0]);

// Orientation: pointing up is positive pitch; turning the hand clockwise is positive roll.
const about = (axis, a) => [axis[0] * Math.sin(a / 2), axis[1] * Math.sin(a / 2), axis[2] * Math.sin(a / 2), Math.cos(a / 2)];
near(M.orientation(about([1, 0, 0], Math.PI / 6)).pitch, 30);
const clockwise = M.orientation(about([0, 0, -1], Math.PI / 9));
near(clockwise.roll, 20); assert.ok(clockwise.rollValid);
assert.strictEqual(M.orientation(about([1, 0, 0], 1.45)).rollValid, false);
near(M.wrap(190), -170); near(M.wrap(-190), 170);

// The page loads only local scripts and keeps the controls the guide describes.
const html = fs.readFileSync(path.join(__dirname, 'platform_static/vr.html'), 'utf8');
assert.ok(!/https?:\/\//.test(html.replace(/https:\/\/…:8770\/vr\//, '')), 'no external resources');
for (const id of ['enter', 'stop', 'confirm', 'scale', 'ratio', 'orient', 'scheme']) assert.ok(html.includes(`id="${id}"`), id);
const app = fs.readFileSync(path.join(__dirname, 'platform_static/vr.js'), 'utf8');
assert.ok(app.includes("'/api/web/vr-input'") && app.includes("mode: 'vr'"));
assert.ok(!app.includes('AbortSignal.timeout('), 'older headset browsers lack AbortSignal.timeout');
new Function(app);
console.log('VR mapping PASS: kinematics parity, buttons, sticks, body frame, orientation, page');
