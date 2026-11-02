import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import * as THREE from '../static/vendor/three.module.min.js';
import {MossRig} from '../static/moss-rig.js';
import {MOSS_IDLE_CLIPS, sampleIdle} from '../static/moss-idle.js';

function assembly() {
  const bytes=readFileSync(new URL('../static/models/moss-550w-jom.glb', import.meta.url));
  const size=bytes.readUInt32LE(12),doc=JSON.parse(bytes.subarray(20,20+size).toString());
  const nodes=doc.nodes.map(n=>{const node=new THREE.Group();node.name=n.name;node.position.fromArray(n.translation||[0,0,0]);return node;});
  doc.nodes.forEach((n,i)=>(n.children||[]).forEach(child=>nodes[i].add(nodes[child])));
  const scene=new THREE.Group();doc.scenes[doc.scene].nodes.forEach(i=>scene.add(nodes[i]));
  scene.updateMatrixWorld(true);return scene;
}

test('body heading uses independent joints while ceiling and rigid link lengths remain fixed',()=>{
  const root=assembly(),rig=new MossRig(root);assert.ok(rig.ready);
  const position=node=>node.getWorldPosition(new THREE.Vector3());
  const fixed=rig.fixed.matrixWorld.toArray();
  const distances=()=>[position(rig.ceiling).distanceTo(position(rig.elbow)),position(rig.elbow).distanceTo(position(rig.wrist)),position(rig.wrist).distanceTo(position(rig.body))];
  const initial=distances();
  for(const yaw of [-.5,-.2,0,.2,.5]){
    rig.lookAt(yaw,.15);root.updateMatrixWorld(true);
    assert.deepEqual(rig.fixed.matrixWorld.toArray(),fixed);
    distances().forEach((value,i)=>assert.ok(Math.abs(value-initial[i])<1e-7));
    const direction=new THREE.Vector3(0,0,1).transformDirection(rig.body.matrixWorld);
    assert.ok(Math.abs(Math.atan2(direction.x,direction.z)-yaw)<1e-7);
  }
  rig.lookAt(10,-10);assert.equal(rig.yaw,.55);assert.equal(rig.pitch,-.20);
});

test('rest pose retains every mesh transform of the source geometry',()=>{
  const root=assembly(),rig=new MossRig(root);rig.lookAt(0,0);root.updateMatrixWorld(true);
  const identity=new THREE.Matrix4().toArray();
  root.traverse(n=>{if(n.name.endsWith(' Link')||['Moss_Glass','Moss_Main','Moss_Annexes'].includes(n.name)){
    n.matrixWorld.toArray().forEach((v,i)=>assert.ok(Math.abs(v-identity[i])<1e-7));
  }});
});

test('all idle poses preserve the fixed mount, root and rigid links with counter-swivel',()=>{
  const root=assembly(),rig=new MossRig(root);
  const position=node=>node.getWorldPosition(new THREE.Vector3());
  const links=()=>[position(rig.ceiling).distanceTo(position(rig.elbow)),position(rig.elbow).distanceTo(position(rig.wrist)),position(rig.wrist).distanceTo(position(rig.body))];
  const fixed=rig.fixed.matrixWorld.toArray(),rootMatrix=root.matrixWorld.toArray(),lengths=links();
  let minArm=0,maxArm=0;
  for(const clip of MOSS_IDLE_CLIPS)for(let i=0;i<=80;i++){
    const pose=sampleIdle(clip,i/80);rig.lookAt(pose.yaw,pose.pitch,pose.arm);root.updateMatrixWorld(true);
    assert.deepEqual(rig.fixed.matrixWorld.toArray(),fixed);
    assert.deepEqual(root.matrixWorld.toArray(),rootMatrix);
    links().forEach((value,j)=>assert.ok(Math.abs(value-lengths[j])<1e-7));
    const direction=new THREE.Vector3(0,0,1).transformDirection(rig.body.matrixWorld);
    assert.ok(Math.abs(Math.atan2(direction.x,direction.z)-pose.yaw)<1e-7);
    minArm=Math.min(minArm,rig.arm);maxArm=Math.max(maxArm,rig.arm);
  }
  assert.ok(minArm<-.05&&maxArm>.05);
  rig.lookAt(10,-10,10);assert.equal(rig.arm,.10);
});
