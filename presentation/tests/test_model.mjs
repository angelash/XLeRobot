import test from 'node:test';
import assert from 'node:assert/strict';
import {mergeEvents, visualFrame, filterEvents, viewState} from '../static/model.js';

test('reconnection deduplicates and a new server epoch replaces old history', () => {
  const first = {epoch:'one', seq:1, events:[{event_id:'a',kind:'tool'}]};
  const next = mergeEvents(first, {epoch:'one',seq:2,events:[{event_id:'a'},{event_id:'b',kind:'vision'}]});
  assert.deepEqual(next.events.map(x=>x.event_id), ['a','b']);
  assert.equal(filterEvents(next.events,'vision').length,1);
  assert.deepEqual(mergeEvents(next,{epoch:'two',seq:0,events:[]}).events,[]);
});

test('a vision result keeps its analyzed input and never implies a current live frame', () => {
  const old = visualFrame({event_id:'e', media_id:'a.png',frame_id:'frame-7',overlays:[{label:'old frame object'}],captured_at:'2026-11-02T00:00:00Z'});
  assert.equal(old.id,'frame-7');
  assert.equal(old.url,'/api/presence/media/a.png');
  assert.equal(visualFrame({event_id:'e',media_id:'a.png',overlays:[{label:'unbound'}]}).overlays.length,0);
  assert.equal(visualFrame({event_id:'no media'}),null);
});

test('history is bounded and failure stays visible without a fictional retry', () => {
  const current = mergeEvents({events:[],seq:0}, {epoch:'one',seq:800,events:Array.from({length:800},(_,i)=>({event_id:String(i),kind:'tool',phase:i===799?'failed':'completed'}))});
  assert.equal(current.events.length,500);
  assert.equal(viewState(current.events),'error');
});
