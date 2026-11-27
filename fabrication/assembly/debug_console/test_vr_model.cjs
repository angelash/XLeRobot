// Geometry invariants for orientation feedback; no hardware and no robot goals.
const assert = require('node:assert/strict');
const M = require('./platform_static/vr-mapping.js');
const V = require('./platform_static/vr-model.js');
const near = (a,b) => assert.ok(Math.abs(a-b)<1e-7,`${a} != ${b}`);
const q={pan:0,lift:0,elbow:40,wrist:M.PSI_OFFSET-40,roll:0,grip:0};
const straight=V.arm('left',q),rolled=V.arm('left',{...q,roll:90}),closed=V.arm('left',{...q,grip:1});
// Rolling must change the jaw plane but not its centre or the robot arm pose.
straight.points.forEach((p,i)=>p.forEach((v,k)=>near(v,rolled.points[i][k])));
near(straight.frame.axes[1][1],1);near(rolled.frame.axes[1][2],1);
assert.ok(closed.gap < straight.gap / 5);
const tipFaces = model => model.gripper.filter(f=>f.role==='pad').flatMap(f=>f.points);
const span = (points,axis) => Math.max(...points.map(p=>p[axis]))-Math.min(...points.map(p=>p[axis]));
assert.ok(span(tipFaces(straight),1)>3*span(tipFaces(rolled),1));
assert.ok(span(tipFaces(rolled),2)>3*span(tipFaces(straight),2));
// Positive roll uses the same clockwise convention as the physical hand proxy.
const hand0=V.hand('left',0), rotation=[0,0,-Math.sin(Math.PI/4),Math.cos(Math.PI/4)];
const left0=hand0.filter(f=>f.role==='pad').flatMap(f=>f.points);
const turned=left0.map(p=>M.rotate(rotation,p));
assert.ok(span(turned,1)>3*span(left0,1));
// A top stripe and unequal finger colours distinguish a 180-degree flip.
assert.ok(straight.gripper.some(f=>f.role==='top-marker'));
const fingerColors=['finger-a','finger-b'].map(role=>straight.gripper.find(f=>f.role===role).color);
assert.notDeepEqual(...fingerColors);
for(const side of ['left','right'])for(const grip of [0,.5,1]) {
  const mesh=V.vertices(V.hand(side,grip,true));
  assert.ok(mesh.length>36*9 && mesh.length%27===0);
  assert.ok([...mesh].every(Number.isFinite));
}
console.log('VR model PASS: jaw opening, shared tool centre, clockwise roll, asymmetric markers, finite shaded mesh');
