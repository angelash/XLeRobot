import test from 'node:test';
import assert from 'node:assert/strict';
import {describeOperator,OperatorStatus} from '../static/operator-status.js';

const offline={version:1,connected:true,input_fresh:true,phase:'offline',active:false,mode:null,whole_ready:true};

test('connection, session and actual mode remain separate, including degraded and unknown states',()=>{
  assert.equal(describeOperator(offline).session,'未接管');
  assert.equal(describeOperator(offline).mode,'整机联动优先');
  const fallback=describeOperator({...offline,phase:'active',active:true,mode:'original_preset',fallback_reason:'姿态卡未完成'});
  assert.equal(fallback.session,'控制中');assert.match(fallback.mode,/预设直控/);assert.equal(fallback.reason,'姿态卡未完成');
  assert.equal(fallback.state,'warning');
  assert.equal(describeOperator({...offline,connected:false}).device,'手柄未连接');
  assert.equal(describeOperator({...offline,input_fresh:false}).device,'手柄输入超时');
  assert.equal(describeOperator(null).session,'接管未知');
  assert.equal(describeOperator({...offline,phase:'unknown'}).session,'接管未知');
});

test('event and heartbeat updates need no polling or commands; stale/error/close never claim stopped',async()=>{
  const fields=Object.fromEntries(['device','session','mode','hint'].map(key=>[key,{textContent:''}]));
  const host={dataset:{},querySelector:selector=>fields[selector.match(/=(\w+)/)[1]]};
  const listeners={};let closed=0,created=0;
  const source={addEventListener:(key,fn)=>listeners[key]=fn,close:()=>closed++};
  const view=new OperatorStatus(host,{createSource:url=>{created++;assert.equal(url,'/api/presence/operator-stream');return source;},timeout:20});
  view.start();view.start();assert.equal(created,1);
  listeners.operator({data:JSON.stringify(offline)});assert.equal(fields.session.textContent,'未接管');
  listeners.heartbeat({data:JSON.stringify({...offline,phase:'active',active:true,mode:'original_hardware'})});
  assert.equal(fields.session.textContent,'控制中');assert.equal(fields.mode.textContent,'整机联动');
  source.onerror();assert.equal(fields.session.textContent,'接管未知');
  listeners.operator({data:JSON.stringify(offline)});
  await new Promise(resolve=>setTimeout(resolve,30));assert.equal(fields.session.textContent,'接管未知');
  view.stop();assert.equal(closed,1);
  listeners.operator({data:JSON.stringify(offline)});assert.equal(fields.session.textContent,'接管未知');
});
