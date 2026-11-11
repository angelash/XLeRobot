const test = require('node:test');
const assert = require('node:assert/strict');
const keys = require('./platform_static/web-teleop.js');

test('arrows always drive chassis, independently of arm mode',()=>{
  for(const mode of ['integrated','joints']) {
    assert.equal(keys.action('ArrowUp',mode,1),'base.forward');
    assert.equal(keys.action('ArrowDown',mode,1),'base.back');
    assert.equal(keys.action('ArrowLeft',mode,1),'base.left');
    assert.equal(keys.action('ArrowRight',mode,1),'base.right');
  }
});
test('joint mode cannot accidentally invoke a plane move',()=>{
  assert.equal(keys.action('KeyW','joints',3),undefined);
  assert.equal(keys.action('BracketLeft','joints',3),'joint.3.minus');
  assert.equal(keys.action('BracketRight','joints',6),'joint.6.plus');
  assert.equal(keys.action('KeyU','joints',6),'arm.open');
});
test('shortcuts do not map browser or unrelated keys',()=>{
  assert.equal(keys.action('Enter','integrated',1),undefined);
  assert.equal(keys.action('Tab','integrated',1),undefined);
  assert.equal(keys.action('KeyD','integrated',1),'arm.extend');
});
