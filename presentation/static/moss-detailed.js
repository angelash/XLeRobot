import * as THREE from './vendor/three.module.min.js';
import {mergeGeometries} from './vendor/addons/utils/BufferGeometryUtils.js';

// Original, reference-built geometry. No downloaded print mesh or film texture.
// Source observations and the unavailable MakerWorld asset are documented in
// docs/presence/MOSS_FIDELITY_ZH.md. Units describe proportions, not engineering mm.

const TAU = Math.PI * 2;

function polygon(points, depth, bevel = .012) {
  const shape = new THREE.Shape(points.map(([x,y]) => new THREE.Vector2(x,y)));
  const geometry = new THREE.ExtrudeGeometry(shape, {depth, steps:1, bevelEnabled:bevel>0,
    bevelSize:bevel, bevelThickness:bevel, bevelSegments:2, curveSegments:5});
  geometry.translate(0,0,-depth/2);
  return geometry;
}

function rounded(w,h,d,r=.025) {
  const x=-w/2,y=-h/2;
  const shape=new THREE.Shape();
  shape.moveTo(x+r,y); shape.lineTo(x+w-r,y);
  shape.quadraticCurveTo(x+w,y,x+w,y+r); shape.lineTo(x+w,y+h-r);
  shape.quadraticCurveTo(x+w,y+h,x+w-r,y+h); shape.lineTo(x+r,y+h);
  shape.quadraticCurveTo(x,y+h,x,y+h-r); shape.lineTo(x,y+r);
  shape.quadraticCurveTo(x,y,x+r,y);
  const geometry=new THREE.ExtrudeGeometry(shape,{depth:d,steps:1,bevelEnabled:true,
    bevelSize:.006,bevelThickness:.006,bevelSegments:2,curveSegments:4});
  geometry.translate(0,0,-d/2); return geometry;
}

class Batch {
  constructor(){this.materials=new Map();this.parts=0;}
  add(geometry,material,position=[0,0,0],rotation=[0,0,0],scale=[1,1,1]){
    const matrix=new THREE.Matrix4().compose(new THREE.Vector3(...position),
      new THREE.Quaternion().setFromEuler(new THREE.Euler(...rotation)),new THREE.Vector3(...scale));
    geometry.applyMatrix4(matrix);
    if(geometry.index){const expanded=geometry.toNonIndexed();geometry.dispose();geometry=expanded;}
    if(!this.materials.has(material))this.materials.set(material,[]);
    this.materials.get(material).push(geometry);this.parts++;
  }
  box(w,h,d,material,x=0,y=0,z=0,r=.025,rotation=[0,0,0]){
    this.add(rounded(w,h,d,r),material,[x,y,z],rotation);
  }
  plate(points,depth,material,x=0,y=0,z=0,bevel=.012){
    this.add(polygon(points,depth,bevel),material,[x,y,z]);
  }
  cylinder(radius,height,material,x,y,z,axis='z',segments=48){
    const rotation=axis==='z'?[Math.PI/2,0,0]:axis==='x'?[0,0,Math.PI/2]:[0,0,0];
    this.add(new THREE.CylinderGeometry(radius,radius,height,segments),material,[x,y,z],rotation);
  }
  ring(radius,tube,material,x,y,z,axis='z',arc=TAU,angle=0){
    const rotation=axis==='x'?[0,Math.PI/2,angle]:axis==='y'?[Math.PI/2,0,angle]:[0,0,angle];
    this.add(new THREE.TorusGeometry(radius,tube,6,Math.max(16,Math.ceil(64*arc/TAU)),arc),material,[x,y,z],rotation);
  }
  finish(name){
    const group=new THREE.Group();group.name=name;
    for(const [material,parts] of this.materials){
      const merged=mergeGeometries(parts,false);
      for(const geometry of parts)geometry.dispose();
      if(!merged)throw new Error('Detailed model geometry could not be batched');
      merged.computeBoundingSphere();
      const mesh=new THREE.Mesh(merged,material);mesh.name=material.name;
      mesh.castShadow=!material.transparent;mesh.receiveShadow=true;group.add(mesh);
    }
    return group;
  }
}

function canvasTexture(w,h,paint){
  const canvas=document.createElement('canvas');canvas.width=w;canvas.height=h;
  paint(canvas.getContext('2d'),w,h);
  const texture=new THREE.CanvasTexture(canvas);texture.colorSpace=THREE.SRGBColorSpace;
  texture.anisotropy=2;return texture;
}

function labelMaterial(text,background='#b8bcb2',ink='#374146',size=256){
  const map=canvasTexture(size,96,(ctx,w,h)=>{
    ctx.fillStyle=background;ctx.fillRect(0,0,w,h);
    ctx.fillStyle=ink;ctx.font='500 37px Bahnschrift, sans-serif';ctx.textBaseline='middle';
    ctx.fillText(text,12,39);
    ctx.font='12px Consolas, monospace';ctx.fillText('OPTICAL / INTELLIGENT SYSTEM',12,77);
  });
  const material=new THREE.MeshStandardMaterial({map,roughness:.82,metalness:.1});
  material.name='Engraved identity plate';return material;
}

function screenMaterial(){
  const map=canvasTexture(384,200,(ctx,w,h)=>{
    ctx.fillStyle='#102a30';ctx.fillRect(0,0,w,h);
    ctx.strokeStyle='#7baca8';ctx.lineWidth=2;ctx.strokeRect(14,14,w-28,h-28);
    ctx.font='16px Consolas, monospace';ctx.fillStyle='#c0d4bd';ctx.fillText('550W / H50',26,40);
    for(let i=0;i<3;i++){
      const x=68+i*98;ctx.beginPath();ctx.arc(x,103,24,0,TAU);ctx.stroke();
      ctx.beginPath();ctx.moveTo(x-30,103);ctx.lineTo(x+30,103);ctx.moveTo(x,73);ctx.lineTo(x,133);ctx.stroke();
    }
    ctx.fillStyle='#7aada6';ctx.fillRect(30,158,200,3);ctx.fillRect(246,153,100,12);
  });
  const material=new THREE.MeshStandardMaterial({map,emissiveMap:map,emissive:'#91b8b0',
    emissiveIntensity:.65,roughness:.28,metalness:.15});material.name='Static instrument display';return material;
}

export function makeDetailedMoss(accent='#ef493e'){
  const mats={
    shell:new THREE.MeshStandardMaterial({color:'#bfc2b8',metalness:.26,roughness:.58}),
    face:new THREE.MeshStandardMaterial({color:'#9fa69f',metalness:.35,roughness:.60}),
    trim:new THREE.MeshStandardMaterial({color:'#6a777b',metalness:.75,roughness:.40}),
    dark:new THREE.MeshStandardMaterial({color:'#27363a',metalness:.65,roughness:.45}),
    black:new THREE.MeshStandardMaterial({color:'#0d171c',metalness:.35,roughness:.55}),
    rubber:new THREE.MeshStandardMaterial({color:'#202a2b',metalness:.05,roughness:.95}),
    gold:new THREE.MeshStandardMaterial({color:'#9d9373',metalness:.67,roughness:.50}),
    glass:new THREE.MeshPhysicalMaterial({color:'#172330',metalness:.42,roughness:.055,clearcoat:1,clearcoatRoughness:.05}),
    red:new THREE.MeshStandardMaterial({color:accent,emissive:accent,emissiveIntensity:1.7,roughness:.2}),
    led:new THREE.MeshStandardMaterial({color:'#c8f0ef',emissive:'#a5e9f0',emissiveIntensity:1.2,roughness:.24}),
  };
  for(const [name,material] of Object.entries(mats))material.name='MOSS '+name;
  const body=new Batch(),mount=new Batch();
  const {shell,face,trim,dark,black,rubber,gold,glass,red,led}=mats;
  const outline=[[-.73,-1.18],[-.73,.84],[-.31,1.25],[.69,1.25],[.76,1.17],
    [.76,.10],[.60,-.06],[.60,-.85],[.75,-1.03],[.75,-1.20],[.41,-1.20],[.31,-1.35],[-.56,-1.35]];
  body.plate(outline,.69,shell,0,0,0,.026);
  const inset=outline.map(([x,y])=>[x*.90,(y-.02)*.92+.02]);
  body.plate(inset,.028,dark,0,0,.385,.008);
  const inner=inset.map(([x,y])=>[x*.982,y*.982]);
  body.plate(inner,.022,face,0,0,.414,.005);

  // Deep rear casing and side access doors establish the physical volume.
  body.box(1.27,1.47,.045,trim,.015,.17,-.382,.045);
  body.box(1.21,1.41,.025,shell,.015,.17,-.422,.04);
  body.box(.92,.05,.028,dark,.015,.82,-.445,.01);
  body.box(.10,1.28,.022,shell,-.47,.17,-.45,.01);
  for(const y of [-.36,.68])for(const x of [-.49,.52]){
    body.cylinder(.031,.018,dark,x,y,-.45);body.cylinder(.018,.023,trim,x,y,-.461,'z',12);
  }
  // Rear service bays and lower ventilation remain visible during a full turn.
  body.box(.49,.44,.026,trim,-.22,-.95,-.380,.025);
  body.box(.44,.39,.025,shell,-.22,-.95,-.407,.020);
  body.box(.33,.23,.018,gold,-.22,-.95,-.432,.014);
  for(const y of [-.79,-1.11])for(const x of [-.40,-.04])body.cylinder(.014,.012,dark,x,y,-.430,'z',12);
  for(let row=0;row<4;row++)for(let col=0;col<5;col++)body.cylinder(.011,.012,black,.25+col*.045,-.77-row*.045,-.367,'z',10);
  body.box(.12,.055,.022,trim,.43,.71,-.448,.008);
  body.box(.083,.024,.023,black,.43,.71,-.467,.004);
  body.box(.045,1.15,.38,shell,-.767,.05,-.02,.02);
  body.box(.022,.77,.28,trim,-.798,.10,-.015,.02);
  for(let i=0;i<7;i++)body.box(.024,.012,.22,dark,-.812,.43-i*.055,-.015,.003);
  body.box(.022,.58,.30,trim,-.772,-.79,-.025,.02);
  body.box(.018,.53,.25,shell,-.793,-.79,-.025,.015);
  body.box(.06,.54,.39,face,.758,.55,-.035,.02);
  body.box(.035,.38,.27,trim,.797,.53,-.035,.013);
  body.box(.06,.32,.36,dark,.752,-.91,-.02,.015);
  body.box(.028,.26,.30,shell,.795,-.91,-.02,.01);

  // The teardrop optical plate joins the large lens and upper-right microphone.
  const optical=[[-.30,.15],[-.36,.43],[-.30,.71],[.47,1.03],[.59,1.01],[.66,.90],
    [.56,.29],[.44,.03],[.15,-.08],[-.09,-.02]];
  body.plate(optical,.018,dark,0,0,.445,.009);
  body.plate(optical.map(([x,y])=>[.14+(x-.14)*.963,.46+(y-.46)*.963]),.018,shell,0,0,.467,.005);
  const cx=.13,cy=.45;
  body.cylinder(.351,.055,dark,cx,cy,.502,'z',64);
  body.ring(.35,.012,trim,cx,cy,.535);
  body.cylinder(.316,.072,black,cx,cy,.554,'z',64);
  body.ring(.296,.015,trim,cx,cy,.594);
  body.ring(.277,.016,black,cx,cy,.606);
  for(let i=0;i<6;i++){
    const angle=i*TAU/6+.18;
    body.box(.084,.039,.026,trim,cx+Math.cos(angle)*.332,cy+Math.sin(angle)*.332,.558,.005,[0,0,angle]);
    body.box(.038,.014,.009,black,cx+Math.cos(angle)*.332,cy+Math.sin(angle)*.332,.578,.002,[0,0,angle]);
  }
  for(const [angle,arc] of [[.2,1.15],[2.1,1.35],[4.0,1.2]])body.ring(.410,.014,dark,cx,cy,.478,'z',arc,angle);
  body.add(new THREE.SphereGeometry(.249,48,24),glass,[cx,cy,.592],[0,0,0],[1,1,.13]);
  body.ring(.215,.005,black,cx,cy,.627);
  body.cylinder(.106,.009,black,cx,cy,.629,'z',48);
  body.ring(.108,.009,gold,cx,cy,.639);
  body.ring(.088,.006,trim,cx,cy,.645);
  for(let i=0;i<24;i++){
    const angle=i*TAU/24;
    body.box(.017,.008,.010,trim,cx+Math.cos(angle)*.139,cy+Math.sin(angle)*.139,.631,.001,[0,0,angle]);
  }
  body.add(new THREE.SphereGeometry(.072,32,16),new THREE.MeshPhysicalMaterial({color:'#42212e',
    metalness:.35,roughness:.04,clearcoat:1}),[cx,cy,.641],[0,0,0],[1,1,.25]);
  body.ring(.048,.011,red,cx,cy,.666);
  body.cylinder(.033,.005,black,cx,cy,.667,'z',32);
  body.ring(.057,.003,new THREE.MeshBasicMaterial({color:'#ff8972'}),cx,cy,.670,'z',1.25,.3);
  const glowTexture=canvasTexture(128,128,(ctx,w,h)=>{
    const gradient=ctx.createRadialGradient(w/2,h/2,0,w/2,h/2,w/2);
    gradient.addColorStop(0,'rgba(255,115,55,0.5)');gradient.addColorStop(.18,'rgba(255,45,20,.25)');gradient.addColorStop(1,'rgba(255,20,10,0)');
    ctx.fillStyle=gradient;ctx.fillRect(0,0,w,h);
  });
  const halo=new THREE.MeshBasicMaterial({map:glowTexture,transparent:true,depthWrite:false,blending:THREE.AdditiveBlending});
  halo.name='Optical core halo';body.add(new THREE.PlaneGeometry(.32,.32),halo,[cx,cy,.671]);
  const reflection=new THREE.MeshBasicMaterial({color:'#a8c7d5',transparent:true,opacity:.20,depthWrite:false});
  reflection.name='Optical reflections';body.ring(.18,.007,reflection,cx,cy,.634,'z',.65,1.8);
  body.ring(.234,.003,reflection,cx,cy,.631,'z',.4,3.6);

  // Secondary lens, microphone grille, status lamps and identification rail.
  body.cylinder(.091,.018,trim,-.17,.99,.45);
  body.cylinder(.077,.021,black,-.17,.99,.47);
  body.add(new THREE.SphereGeometry(.055,24,16),glass,[-.17,.99,.483],[0,0,0],[1,1,.35]);
  body.ring(.113,.012,dark,-.17,.99,.451,'z',4.6,.5);
  body.cylinder(.080,.018,trim,.565,.935,.497);
  body.cylinder(.062,.022,black,.565,.935,.512);
  for(let row=-2;row<=2;row++)for(let column=-2;column<=2;column++)if(row*row+column*column<6){
    body.cylinder(.006,.006,trim,.565+column*.017,.935+row*.017,.527,'z',8);
  }
  body.box(.035,.016,.010,led,-.26,.82,.458,.003,[0,0,.65]);
  body.box(.032,.011,.011,led,.105,-.014,.451,.002);
  body.box(.34,.075,.016,trim,-.05,1.16,.450,.005);
  body.add(new THREE.PlaneGeometry(.32,.062),labelMaterial('550W'),[-.05,1.16,.471]);
  for(const x of [.49,-.24])body.box(.043,.115,.025,dark,x,1.19,.442,.006);

  // Standalone left optical/audio module with recessed grille and vertical LEDs.
  body.box(.54,.405,.17,dark,-.64,.54,.48,.035);
  body.box(.52,.385,.10,shell,-.66,.54,.586,.027);
  body.box(.040,.34,.035,trim,-.70,.54,.651,.007);
  body.cylinder(.126,.030,trim,-.775,.54,.655,'z',48);
  body.cylinder(.106,.030,black,-.775,.54,.675,'z',48);
  body.ring(.096,.005,dark,-.775,.54,.695);
  for(let i=-3;i<=3;i++){
    body.box(.008,.057,.006,red,-.775+i*.018,.54,.698,.002);
    body.box(.008,.016,.007,dark,-.775+i*.018,.573,.701,.002);
  }
  for(const x of [-.91,-.44])for(const y of [.40,.68])body.cylinder(.010,.007,dark,x,y,.648,'z',10);
  body.box(.12,.012,.006,trim,-.48,.49,.650,.002);
  body.box(.12,.006,.006,trim,-.48,.45,.650,.002);

  // Vented left recess, cross braces, screen and recessed diagnostic sockets.
  const vent=[[-.56,-.10],[-.38,-.10],[-.24,-.28],[-.24,-1.10],[-.55,-1.10],[-.60,-1.02],[-.60,-.22]];
  body.plate(vent,.018,dark,0,0,.444,.012);
  body.plate(vent.map(([x,y])=>[-.43+(x+.43)*.87,-.61+(y+.61)*.94]),.018,black,0,0,.457,.009);
  for(const y of [-.35,-.65,-.94]){
    body.box(.275,.037,.023,face,-.423,y,.480,.006);
    body.box(.24,.036,.025,face,-.425,y+.13,.480,.005,[0,0,-.72]);
  }
  for(let i=0;i<12;i++)body.box(.045,.010,.015,dark,-.63,-.20-i*.069,.442,.002);
  const control=[[-.03,-.35],[.42,-.35],[.42,-.89],[.25,-1.04],[-.03,-1.04]];
  body.plate(control,.021,trim,0,0,.445,.013);
  body.plate(control.map(([x,y])=>[.19+(x-.19)*.94,-.70+(y+.70)*.96]),.018,dark,0,0,.465,.008);
  body.box(.331,.204,.023,black,.19,-.50,.486,.017);
  body.box(.289,.165,.012,trim,.19,-.50,.503,.009);
  const display=screenMaterial();body.add(new THREE.PlaneGeometry(.267,.144),display,[.19,-.50,.523]);
  for(const y of [-.72,-.85]){
    body.cylinder(.034,.008,gold,.065,y,.494,'z',24);
    body.cylinder(.023,.010,black,.065,y,.501,'z',24);
    body.cylinder(.011,.006,trim,.065,y,.509,'z',12);
  }
  body.add(new THREE.PlaneGeometry(.20,.08),labelMaterial('H50','#27363a','#83928c'),[.275,-.79,.498]);
  for(const [x,y] of [[-.01,-.37],[.40,-.37],[.40,-.88],[.01,-1.00]])body.cylinder(.012,.012,black,x,y,.487,'z',12);
  body.box(.021,.17,.022,trim,-.11,-.88,.465,.004);

  // Bottom service bridge, connector bank and angled fold-out equipment pod.
  body.box(1.20,.21,.66,shell,.02,-1.28,-.015,.035);
  body.box(.48,.10,.27,black,-.05,-1.335,.21,.012);
  body.box(.50,.045,.39,trim,-.06,-1.365,.02,.007);
  body.box(.54,.12,.25,shell,-.05,-1.48,.18,.022);
  body.box(.40,.054,.018,black,-.07,-1.49,.315,.007);
  body.box(.045,.032,.008,red,.086,-1.49,.33,.003);
  for(let i=0;i<7;i++)body.box(.014,.025,.012,trim,-.23+i*.038,-1.49,.329,.001);
  body.box(.13,.13,.16,black,.37,-1.29,.24,.013);
  for(const x of [.345,.395])body.cylinder(.018,.016,gold,x,-1.27,.334,'z',16);
  body.box(.28,.38,.45,shell,.61,-1.42,-.03,.05,[.18,0,.61]);
  body.box(.23,.31,.025,trim,.62,-1.43,.214,.025,[0,0,.61]);
  body.box(.18,.24,.018,dark,.62,-1.43,.236,.02,[0,0,.61]);
  body.box(.12,.18,.019,face,.62,-1.43,.251,.015,[0,0,.61]);
  for(const y of [-1.16,-.92])body.box(.27,.025,.24,trim,.13,y,-.376,.004);

  // Front fasteners are deliberately recessed and small, rather than ornaments.
  for(const [x,y] of [[-.51,.98],[-.24,1.18],[.68,1.12],[.65,.13],[.48,-.18],
    [.50,-1.10],[-.63,-1.13],[-.63,.13],[-.15,.10],[-.16,-1.13],[-.52,-.04]]){
    body.cylinder(.020,.008,dark,x,y,.442,'z',12);
    body.cylinder(.010,.010,trim,x,y,.449,'z',6);
  }
  body.box(.018,.07,.015,gold,.735,.48,.403,.002);
  body.box(.018,.07,.015,gold,.735,.30,.403,.002);

  // Right pitch joint, elbow enclosure and the tall, ribbed suspension spindle.
  mount.cylinder(.26,.29,rubber,.87,-.41,0,'x');
  mount.ring(.257,.018,trim,1.02,-.41,0,'x');
  mount.box(.84,.44,.61,shell,1.36,-.41,0,.065);
  mount.box(.62,.34,.022,face,1.39,-.41,.323,.042);
  mount.box(.42,.21,.020,shell,1.47,-.39,.341,.030);
  mount.box(.20,.033,.020,trim,1.14,-.42,.341,.010);
  mount.box(.20,.018,.021,shell,1.14,-.42,.357,.006);
  for(const [x,y] of [[1.08,-.25],[1.69,-.25],[1.08,-.57],[1.69,-.57]])mount.cylinder(.018,.015,dark,x,y,.335,'z',12);
  mount.cylinder(.235,.15,dark,1.62,-.72,0,'y');
  mount.cylinder(.21,.095,shell,1.62,-.79,0,'y');
  for(let i=0;i<5;i++)mount.cylinder(.012,.014,black,1.56+i*.028,-.843,.03,'y',10);
  const sx=1.62;
  mount.cylinder(.223,1.40,trim,sx,.50,0,'y');
  mount.cylinder(.241,1.13,shell,sx,.48,0,'y');
  mount.box(.15,.91,.036,face,sx,.47,.238,.018);
  mount.box(.12,.84,.018,shell,sx,.47,.264,.012);
  mount.box(.11,.15,.018,gold,sx,.81,.278,.012);
  mount.box(.09,.10,.014,face,sx,.81,.291,.007);
  for(const y of [-.06,.94]){
    mount.ring(.244,.012,dark,sx,y,0,'y');
    mount.cylinder(.25,.045,shell,sx,y+.035,0,'y');
  }
  mount.cylinder(.205,.43,trim,sx,1.22,0,'y');
  for(let i=0;i<5;i++)mount.cylinder(.227,.025,rubber,sx,1.07+i*.051,0,'y');
  mount.cylinder(.25,.24,shell,sx,1.48,0,'y');
  mount.ring(.251,.008,trim,sx,1.38,0,'y');
  mount.box(.12,.08,.026,trim,sx,1.48,.241,.014);
  mount.box(.11,.05,.018,black,sx,1.48,.262,.010);
  for(const y of [.09,.95,1.54])for(const x of [-.13,.13])mount.cylinder(.014,.010,dark,sx+x,y,.208,'z',12);
  // Swivel hubs and dog-leg link support the camera from an overhead rail.
  mount.cylinder(.29,.39,shell,sx,1.79,0,'y');
  mount.cylinder(.29,.40,shell,.75,1.79,0,'y');
  mount.box(.84,.28,.45,shell,1.18,1.79,.0,.045);
  mount.box(.70,.19,.021,face,1.18,1.79,.244,.025);
  for(const x of [.75,sx]){
    mount.cylinder(.26,.045,trim,x,1.565,0,'y');
    mount.cylinder(.22,.016,dark,x,1.535,0,'y');
    mount.cylinder(.21,.012,black,x,1.522,0,'y');
    for(let row=-2;row<=2;row++)for(let col=-2;col<=2;col++)if(row*row+col*col<6)mount.cylinder(.008,.008,trim,x+col*.044,1.512,row*.044,'y',8);
    mount.ring(.291,.007,trim,x,1.93,0,'y');
    mount.box(.075,.12,.020,gold,x,1.79,.291,.008);
  }
  mount.cylinder(.24,.17,rubber,.75,2.075,0,'y');
  mount.ring(.25,.016,trim,.75,2.02,0,'y');
  mount.ring(.28,.017,dark,.75,2.12,0,'y');
  mount.box(1.45,.23,.91,shell,.75,2.30,0,.045);
  mount.box(1.24,.04,.74,dark,.75,2.16,0,.028);
  for(const z of [-.45,.45]){
    mount.box(1.53,.13,.075,shell,.75,2.45,z,.015);
    mount.box(1.26,.028,.022,dark,.75,2.47,z*1.08,.005);
  }
  mount.box(1.12,.035,.045,trim,.75,2.30,.493,.007);
  for(const x of [.14,1.35]){
    mount.box(.095,.14,.035,dark,x,2.30,.489,.006);
    mount.cylinder(.028,.035,trim,x,2.30,.514,'z',20);
    mount.cylinder(.013,.038,black,x,2.30,.531,'z',12);
  }
  const mastTag=labelMaterial('M-01','#bfc2b8','#535c58');
  mount.add(new THREE.PlaneGeometry(.23,.07),mastTag,[sx,1.78,.299]);

  const head=body.finish('MOSS optical body');
  const pitch=new THREE.Group();pitch.name='Right-side pitch joint';pitch.position.set(.87,-.41,0);
  head.position.set(-.87,.41,0);pitch.add(head);
  const assembly=new THREE.Group();assembly.name='MOSS 550W reference-built assembly';
  assembly.add(pitch,mount.finish('Overhead rail and suspension'));
  const bounds=new THREE.Box3().setFromObject(assembly),size=bounds.getSize(new THREE.Vector3());
  assembly.position.sub(bounds.getCenter(new THREE.Vector3()));
  const result=new THREE.Group();result.add(assembly);result.scale.setScalar(3.15/Math.max(size.x,size.y,size.z));
  result.name='MOSS detailed v2';
  result.userData.glow=red;result.userData.pitchJoint=pitch;
  result.userData.provenance='Reference-built from official multi-view artwork; MakerWorld 924464 not imported.';
  let triangles=0,meshes=0;
  result.traverse(node=>{if(node.isMesh){meshes++;triangles+=node.geometry.attributes.position.count/3;}});
  result.userData.statistics={parts:body.parts+mount.parts,meshes,triangles};
  return result;
}
