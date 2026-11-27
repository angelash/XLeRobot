'use strict';
// Lightweight, procedural display geometry. Units are metres; shapes and
// shoulder spacing are illustrative, never measured robot/collision geometry.
(function(root) {
  const M = typeof module !== 'undefined' && module.exports ? require('./vr-mapping.js') : root.VRMapping;
  const C = {left: [.376, .647, .980], right: [.984, .573, .235], metal: [.48, .57, .67],
    dark: [.12, .17, .23], black: [.055, .08, .11], white: [.89, .94, .98],
    top: [1, .82, .25], forward: [.29, .86, .70], jaw: [.58, .73, .85]};
  const add = (a,b) => a.map((v,i) => v+b[i]);
  const sub = (a,b) => a.map((v,i) => v-b[i]);
  const mul = (a,s) => a.map(v => v*s);
  const dot = (a,b) => a.reduce((v,x,i) => v+x*b[i],0);
  const cross = (a,b) => [a[1]*b[2]-a[2]*b[1],a[2]*b[0]-a[0]*b[2],a[0]*b[1]-a[1]*b[0]];
  const unit = a => mul(a,1/(Math.hypot(...a)||1));
  const ID = [[1,0,0],[0,1,0],[0,0,1]];
  const at = (frame,p) => p.reduce((v,x,i) => add(v,mul(frame.axes[i],x)),frame.origin);
  const vector = (frame,v) => v.reduce((out,x,i) => add(out,mul(frame.axes[i],x)),[0,0,0]);
  function face(points,color,role) {
    return {points,color,role,normal:unit(cross(sub(points[1],points[0]),sub(points[2],points[0])))};
  }
  function box(out,center,size,color,role,axes=ID) {
    const frame={origin:center,axes};
    const corners=Array.from({length:8},(_,i) => at(frame,size.map((s,k) => (i&(1<<k)?1:-1)*s/2)));
    for(const ids of [[0,4,6,2],[1,3,7,5],[0,1,5,4],[2,6,7,3],[0,2,3,1],[4,5,7,6]])
      out.push(face(ids.map(i=>corners[i]),color,role));
  }
  function beam(out,a,b,width,height,color,role,up=[0,0,1]) {
    const x=unit(sub(b,a));
    const y=unit(cross(Math.abs(dot(x,unit(up)))>.97?[0,1,0]:up,x));
    box(out,mul(add(a,b),.5),[Math.hypot(...sub(b,a)),width,height],color,role,[x,y,unit(cross(x,y))]);
  }
  function cylinder(out,center,axis,radius,length,color,role) {
    const x=unit(axis), y=unit(cross(Math.abs(x[2])>.9?[0,1,0]:[0,0,1],x)), z=cross(x,y);
    const ring=sign=>Array.from({length:12},(_,i)=>add(add(center,mul(x,sign*length/2)),
      add(mul(y,Math.cos(i*Math.PI/6)*radius),mul(z,Math.sin(i*Math.PI/6)*radius))));
    const a=ring(-1),b=ring(1);
    out.push(face([...a].reverse(),color,role),face(b,color,role));
    for(let i=0;i<12;i++) out.push(face([a[i],a[(i+1)%12],b[(i+1)%12],b[i]],color,role));
  }
  function transform(faces,frame) {
    return faces.map(f=>({...f,points:f.points.map(p=>at(frame,p)),normal:vector(frame,f.normal)}));
  }
  function toolFrame(a) {
    const pan=a.pan*Math.PI/180, pitch=(M.PSI_OFFSET-a.lift-a.elbow-a.wrist)*Math.PI/180;
    // Positive roll follows the clockwise controller convention in VRMapping.
    const roll=(a.roll||0)*Math.PI/180;
    const f=[Math.cos(pan)*Math.cos(pitch),Math.sin(pan)*Math.cos(pitch),Math.sin(pitch)];
    const l=[-Math.sin(pan),Math.cos(pan),0],u=cross(f,l);
    return {forward:f,left:add(mul(l,Math.cos(roll)),mul(u,Math.sin(roll))),
      up:add(mul(u,Math.cos(roll)),mul(l,-Math.sin(roll)))};
  }
  function gripper(side,grip=0,engaged=false) {
    const out=[],color=C[side],close=Math.max(0,Math.min(1,grip));
    const gap=.006+.066*(1-close);
    cylinder(out,[0,0,0],[1,0,0],.024,.022,C.dark,'wrist');
    cylinder(out,[-.012,0,0],[1,0,0],.025,.006,engaged?C.forward:color,'clutch');
    box(out,[.024,0,0],[.040,.055,.039],color,'housing');
    box(out,[.027,-.012,.021],[.027,.014,.005],C.top,'top-marker');
    box(out,[.042,-.012,.021],[.005,.022,.005],C.top,'top-marker');
    box(out,[.026,0,-.021],[.024,.037,.005],C.black,'bottom');
    for(const sign of [-1,1]) {
      const root=[.043,sign*.018,0], knuckle=[.071,sign*(gap/2+.014),0];
      const tip=[M.TOOL,sign*(gap/2+.006),0];
      const finger=sign===-1?C.white:C.jaw;
      beam(out,root,knuckle,.012,.018,finger,sign===-1?'finger-a':'finger-b');
      beam(out,knuckle,tip,.012,.016,finger,sign===-1?'finger-a':'finger-b');
      box(out,[M.TOOL,sign*(gap/2+.002),0],[.018,.004,.020],C.black,'pad');
      cylinder(out,root,[0,0,1],.009,.023,C.metal,'knuckle');
    }
    // Off-centre yellow witness mark makes even a 180 degree roll readable.
    box(out,[.078,-(gap/2+.015),.011],[.013,.010,.004],C.top,'finger-marker');
    return {faces:out,gap,tip:[M.TOOL,0,0]};
  }
  function arm(side,a) {
    const points=M.armPoints(side,a),out=[],color=C[side];
    const pan=a.pan*Math.PI/180,axle=[-Math.sin(pan),Math.cos(pan),0];
    cylinder(out,points[0],[0,0,1],.030,.048,C.dark,'base');
    beam(out,points[0],points[1],.042,.036,color,'shoulder');
    beam(out,points[1],points[2],.027,.036,color,'upper-arm',axle);
    beam(out,points[2],points[3],.024,.030,C.metal,'forearm',axle);
    for(const p of points.slice(1,4)) {
      cylinder(out,p,axle,.023,.046,C.dark,'joint');
      cylinder(out,add(p,mul(axle,-.025)),axle,.012,.006,color,'joint-cap');
      cylinder(out,add(p,mul(axle,-.029)),axle,.004,.007,C.white,'bolt');
    }
    const f=toolFrame(a),frame={origin:points[3],axes:[f.forward,f.left,f.up]};
    const tool=gripper(side,a.grip||0,!!a.anchor);
    const jaws=transform(tool.faces,frame);
    out.push(...jaws);
    return {faces:out,gripper:jaws,frame,points,gap:tool.gap,
      pitch:M.wrap(M.PSI_OFFSET-a.lift-a.elbow-a.wrist),roll:M.wrap(a.roll||0)};
  }
  function hand(side,grip=0,engaged=false) {
    const tool=gripper(side,grip,engaged),out=[...tool.faces];
    box(out,[-.031,0,-.025],[.038,.033,.070],C.dark,'handle');
    box(out,[-.022,-.019,-.025],[.020,.006,.037],C[side],'grip-button');
    box(out,[-.014,0,.019],[.019,.028,.012],C.black,'trigger');
    cylinder(out,[-.033,0,.029],[0,0,1],.009,.009,C[side],'thumbstick');
    // A small fixed tripod labels the tool's forward / jaw / top directions by
    // distinct positions and colours; it rotates with the grip pose, never yaw-only.
    const origin=[-.060,0,.008];
    beam(out,origin,add(origin,[.026,0,0]),.004,.004,C.forward,'axis-forward');
    beam(out,origin,add(origin,[0,.026,0]),.004,.004,C.white,'axis-jaw');
    beam(out,origin,add(origin,[0,0,.026]),.004,.004,C.top,'axis-up');
    return transform(out,{origin:[0,0,0],axes:[[0,0,-1],[-1,0,0],[0,1,0]]});
  }
  function shade(color,normal,light=[-.3,-.5,.8]) {
    const amount=.48+.52*Math.max(0,dot(unit(normal),unit(light)));
    return color.map(v=>Math.round(255*v*amount));
  }
  function vertices(faces) {
    const data=[];
    for(const f of faces) for(let i=1;i<f.points.length-1;i++)
      for(const p of [f.points[0],f.points[i],f.points[i+1]]) data.push(...p,...f.normal,...f.color);
    return new Float32Array(data);
  }
  const api={arm,hand,gripper,toolFrame,vertices,shade,at,vector};
  if(typeof module!=='undefined'&&module.exports)module.exports=api;
  else root.VRModel=api;
})(typeof self!=='undefined'?self:this);
