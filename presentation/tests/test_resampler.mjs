import test from 'node:test';
import assert from 'node:assert/strict';
import {PcmResampler} from '../static/audio-resampler.js';

function convert(input,rate,chunk){
  const resampler=new PcmResampler(rate),frames=[];
  for(let i=0;i<input.length;i+=chunk)resampler.push(input.subarray(i,i+chunk),(pcm)=>frames.push(new Int16Array(pcm)));
  return Int16Array.from(frames.flatMap(frame=>Array.from(frame)));
}
test('stateful conversion is invariant to driver chunk boundaries and preserves frame sizes',()=>{
  const signal=Float32Array.from({length:48000},(_,i)=>.4*Math.sin(2*Math.PI*997*i/48000));
  const a=convert(signal,48000,128),b=convert(signal,48000,317);
  assert.deepEqual(a,b);assert.equal(a.length%320,0);assert.ok(a.length>=15680&&a.length<=16000);
});
test('anti-alias filter suppresses above-Nyquist energy and retains speech band',()=>{
  const rms=frequency=>{
    const input=Float32Array.from({length:48000},(_,i)=>.5*Math.sin(2*Math.PI*frequency*i/48000));
    const pcm=convert(input,48000,128).subarray(1000);
    return Math.sqrt(pcm.reduce((sum,x)=>sum+(x/32768)**2,0)/pcm.length);
  };
  assert.ok(rms(1000)>.34);assert.ok(rms(12000)<.002);
});
test('44.1 kHz keeps its fractional sampling phase across blocks; silence stays zero',()=>{
  const result=convert(new Float32Array(44100),44100,128);
  assert.equal(result.length,15680);assert.ok(result.every(x=>x===0));
});
