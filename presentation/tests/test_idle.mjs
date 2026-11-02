import test from 'node:test';
import assert from 'node:assert/strict';
import {MOSS_IDLE_CLIPS, MossIdlePlayer, sampleIdle} from '../static/moss-idle.js';

const random = () => {
  let seed = 451;
  return () => ((seed = (Math.imul(seed,1664525)+1013904223) >>> 0)/2**32);
};
const close = (a,b) => assert.ok(Math.abs(a-b)<1e-8, `${a} != ${b}`);

test('ten distinct routines have ordered poses, neutral bookends and 5–30 second ranges', () => {
  assert.equal(MOSS_IDLE_CLIPS.length,10);
  assert.equal(new Set(MOSS_IDLE_CLIPS.map(clip=>clip.id)).size,10);
  assert.equal(new Set(MOSS_IDLE_CLIPS.map(clip=>JSON.stringify(clip.frames))).size,10);
  for(const clip of MOSS_IDLE_CLIPS){
    assert.ok(clip.seconds[0]>=5 && clip.seconds[1]<=30 && clip.seconds[0]<clip.seconds[1]);
    assert.deepEqual(clip.frames[0],[0,0,0,0]);
    assert.deepEqual(clip.frames.at(-1),[1,0,0,0]);
    clip.frames.slice(1).forEach((frame,i)=>assert.ok(frame[0]>clip.frames[i][0]));
  }
});

test('both mirrored variations stay within rig limits and have continuous, settled joins', () => {
  for(const clip of MOSS_IDLE_CLIPS)for(const mirror of [-1,1]){
    let previous=sampleIdle(clip,0,mirror);
    for(let i=1;i<=2000;i++){
      const pose=sampleIdle(clip,i/2000,mirror);
      for(const [axis,limit] of [['yaw',.55],['pitch',.20],['arm',.10]]){
        assert.ok(Number.isFinite(pose[axis])&&Math.abs(pose[axis])<=limit);
        assert.ok(Math.abs(pose[axis]-previous[axis])<.015);
      }
      previous=pose;
    }
    for(const t of [0,1])for(const value of Object.values(sampleIdle(clip,t,mirror)))close(value,0);
    for(const frame of clip.frames.slice(1,-1)){
      const before=sampleIdle(clip,frame[0]-1e-5,mirror),after=sampleIdle(clip,frame[0]+1e-5,mirror);
      for(const axis of ['yaw','pitch','arm'])close(before[axis],after[axis]);
    }
  }
});

test('each shuffled cycle contains all ten routines with no adjacent repeat across cycles', () => {
  const player=new MossIdlePlayer(random()),ids=[],durations=[];
  for(let i=0;i<40;i++){
    const value=player.play();ids.push(value.clip.id);durations.push(value.duration);
    assert.ok(value.duration>=value.clip.seconds[0]&&value.duration<=value.clip.seconds[1]);
    if(i)assert.notEqual(ids[i],ids[i-1]);
  }
  for(let i=0;i<40;i+=10)assert.equal(new Set(ids.slice(i,i+10)).size,10);
  assert.ok(Math.max(...durations)-Math.min(...durations)>15);
});

test('elapsed active time is independent of display frame rate', () => {
  const a=new MossIdlePlayer(random()),b=new MossIdlePlayer(random());
  a.play('sneeze');b.play('sneeze');
  let pa,pb;
  for(let i=0;i<90;i++)pa=a.advance(1/30,{loop:false});
  for(let i=0;i<180;i++)pb=b.advance(1/60,{loop:false});
  for(const axis of ['yaw','pitch','arm'])close(pa[axis],pb[axis]);
  close(a.current.elapsed,b.current.elapsed);
});

test('single preview finishes at rest; a paused director does not advance or restart', () => {
  const player=new MossIdlePlayer(random());
  const duration=player.play('fluster').duration;
  assert.deepEqual(player.advance(duration+.1,{loop:false}),{yaw:0,pitch:0,arm:0});
  assert.equal(player.current,null);
  assert.deepEqual(player.advance(20,{loop:false}),{yaw:0,pitch:0,arm:0});
  player.play('inspect');player.advance(2);player.reset();
  assert.equal(player.current,null);
  assert.deepEqual(player.advance(0),{yaw:0,pitch:0,arm:0});
  assert.throws(()=>player.play('unknown'));
});
