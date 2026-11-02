import test from 'node:test';
import assert from 'node:assert/strict';
import {SpeechPlayer} from '../static/speech-player.js';

const event=(id,patch={})=>({event_id:id,kind:'message',phase:'completed',text:'公开回复',received_at:new Date().toISOString(),...patch});
const settings={enabled:false,voice_id:'Robot_Armor',volume:.8,speed:1,pitch:0,emotion:'auto'};
const tick=()=>new Promise(resolve=>setTimeout(resolve,5));
function audio(){return {src:'',volume:1,pause(){},removeAttribute(){this.src='';},load(){},play(){setTimeout(()=>this.onended?.(),0);return Promise.resolve();}};}
function reply(){return new Response(new Blob(['fake-wav']),{headers:{'X-Speech-Parts':'1','Content-Type':'audio/wav'}});}

test('off means no synthesis; enabling does not replay history or duplicated events',async()=>{
  const calls=[];const p=new SpeechPlayer({audio:audio(),request:async(path,value)=>{calls.push(JSON.parse(value.body));return reply();}});
  p.configure(settings);p.observe([event('history')],{epoch:'one',baseline:true});p.observe([event('off')],{epoch:'one'});
  assert.equal(calls.length,0);
  await p.arm();p.configure({...settings,enabled:true});
  p.observe([event('off'),event('live')],{epoch:'one'});await tick();await tick();
  assert.equal(calls.length,1);assert.equal(calls[0].event_id,'live');
  p.observe([event('live'),event('tool',{kind:'tool'}),event('demo',{evidence:'demo'}),event('old',{received_at:'2000-01-01'})],{epoch:'one'});
  await tick();assert.equal(calls.length,1);
  p.observe([event('restart')],{epoch:'two'});await tick();assert.equal(calls.length,1);p.stop();
});

test('stop aborts in-flight speech and a late response cannot start playback',async()=>{
  let release,signal,plays=0;
  const a=audio();a.play=()=>{plays++;return Promise.resolve();};
  const p=new SpeechPlayer({audio:a,request:async(path,value)=>{signal=value.signal;await new Promise(resolve=>release=resolve);return reply();}});
  p.configure({...settings,enabled:true});p.armed=true;p.observe([],{epoch:'one',baseline:true});
  p.observe([event('slow')],{epoch:'one'});await tick();p.stop();assert.equal(signal.aborted,true);
  release();await tick();assert.equal(plays,0);assert.equal(p.queue.length,0);
});

test('manual audition works with automatic speech still disabled',async()=>{
  const calls=[];const p=new SpeechPlayer({audio:audio(),request:async(path)=>{calls.push(path);return reply();}});
  p.configure(settings);await p.preview({voice_id:'Robot_Armor',text:'试听'});
  assert.deepEqual(calls,['/api/presence/speech/preview']);assert.equal(p.settings.enabled,false);p.stop();
});

test('a blocked play never loops or repeats synthesis',async()=>{
  let count=0,blocked=0;
  const a=audio();a.play=()=>Promise.reject(Object.assign(new Error('blocked'),{name:'NotAllowedError'}));
  const p=new SpeechPlayer({audio:a,blocked:()=>blocked++,request:async()=>{count++;return reply();}});
  p.configure({...settings,enabled:true});p.armed=true;p.observe([],{epoch:'one',baseline:true});p.observe([event('one'),event('two')],{epoch:'one'});
  await tick();assert.equal(blocked,1);assert.equal(count,1);assert.equal(p.armed,false);assert.equal(p.queue.length,0);p.stop();
});

test('streamed sentences play once, terminal fallback never repeats and stop mutes late sentences',async()=>{
  const calls=[];const p=new SpeechPlayer({audio:audio(),request:async(path,value)=>{calls.push({path,...JSON.parse(value.body)});return reply();}});
  p.configure({...settings,enabled:true});p.armed=true;
  const live={kind:'reply.delta',request_id:'stream',generation_id:'a'.repeat(32),part_count:1,updated_at:Date.now()/1000};
  p.observeLive(live);await tick();await tick();
  assert.equal(calls.length,1);assert.equal(calls[0].part,0);
  p.observeLive(live);p.observe([],{epoch:'one',baseline:true});
  p.observe([event('done',{task_id:'stream'})],{epoch:'one'});await tick();assert.equal(calls.length,1);
  p.stop();p.observeLive({...live,part_count:2});await tick();assert.equal(calls.length,1);
});

test('completed receipt arriving before its coalesced live reply chooses exactly one playback path',async()=>{
  const calls=[];const p=new SpeechPlayer({audio:audio(),request:async(path,value)=>{calls.push(path);return reply();}});
  p.configure({...settings,enabled:true});p.armed=true;p.observe([],{epoch:'one',baseline:true});
  p.observe([event('first-terminal',{task_id:'same-request'})],{epoch:'one'});
  p.observeLive({kind:'reply.completed',request_id:'same-request',generation_id:'b'.repeat(32),part_count:2,updated_at:Date.now()/1000});
  await tick();await tick();assert.deepEqual(calls,['/api/presence/speech']);p.stop();
});
