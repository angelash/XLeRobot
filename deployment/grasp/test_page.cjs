// Runs the grasp page script against a minimal DOM and fake API (no browser).
const fs=require('fs');const path=require('path');const vm=require('vm');const assert=require('assert');
const html=fs.readFileSync(path.join(__dirname,'static','index.html'),'utf8');
const elements={};
for(const id of [...html.matchAll(/id="([^"]+)"/g)].map(m=>m[1]))elements[id]={id,innerHTML:'',textContent:'',hidden:false,className:'',disabled:false,checked:false,value:'0',src:null,
  setAttribute(k,v){this[k]=v;},removeAttribute(k){this[k]=null;},addEventListener(){},getContext(){return null;},getBoundingClientRect(){return {left:0,top:0,width:640,height:480};}};
const status={cameras_on:true,cameras:[{camera:2,mode:'stream',age_ms:40,seq:10},{camera:1,mode:'shared',age_ms:90,error:'<x>'}],operation:'pick-place',
  track:{active:true,ok:true,score:0.83,bbox:[300,200,90,110]},tracking_ms_median:6.1,
  brain:{phase:'close',note:'闭合夹爪',attempt:1,events:[{t:3.2,kind:'phase',phase:'align',note:'对准'},{t:9.5,kind:'step',label:'闭合到1930',contact:null},{t:9.9,kind:'verify',why:'<b>'}]},
  executor:{active:true,steps:12},last_result:{ok:false,error:'推进时受阻：<i>'},config:{desk_z:-8,approach_clearance:1.4,in_top:215,ref_x_far:395,ref_x_near:440,pan_gain:-1.9,lift_raw:150,hold_s:1.5},
  selection_source:'local:plug',knowledge:{objects:{'<p>':{templates:2,successes:3,attempts:4,by_source:{llm:{attempts:3,successes:2},local:{attempts:1,successes:1}},agreement_n:2,agreement_ok_rate:1}},base_legs:3}};
const calls=[];
const context={document:{getElementById:id=>elements[id],querySelectorAll:()=>[]},fetch:async(url,o={})=>{calls.push([url,o.method||'GET']);return {ok:true,json:async()=>url.endsWith('/runs')?[{id:'20261103-120000-pick-place',result:{ok:true},keyframes:[{files:['01-start-cam2.jpg','01-start-cam2-overlay.jpg']}]}]:status};},
  AbortController,setTimeout,clearTimeout,setInterval:()=>0,confirm:()=>true,console,Date,Math,Number,String,Object,JSON,Error,Promise,encodeURIComponent};
vm.createContext(context);
vm.runInContext(fs.readFileSync(path.join(__dirname,'static','app.js'),'utf8'),context);
setTimeout(()=>{
  assert.equal(elements.phase.textContent,'闭合');assert.equal(elements.state.textContent,'运行中');
  assert(elements.facts.innerHTML.includes('&lt;x&gt;')&&!elements.facts.innerHTML.includes('<x>'),'Camera errors are escaped');
  assert(elements.facts.innerHTML.includes('&lt;i&gt;')&&elements.events.innerHTML.includes('&lt;b&gt;'),'Results and events are escaped');
  assert(elements.run.disabled&&elements.survey.disabled&&!elements.stop.disabled,'Only stop stays available while running');
  assert(elements.locate.disabled&&elements.reposition.disabled,'Local find and reposition are blocked while running');
  assert(elements.knowledge.innerHTML.includes('&lt;p&gt;')&&elements.knowledge.innerHTML.includes('本地框选 1/1')&&elements.knowledge.innerHTML.includes('100%'),'Knowledge is shown and escaped');
  assert(elements.target.textContent.includes('本地识别'));
  assert.equal(elements.live.src,'/grasp/stream.mjpg');
  assert(elements.config.innerHTML.includes('data-key="desk_z"')&&elements.runs.innerHTML.includes('overlay'));
  assert(calls.every(([,m])=>m==='GET'),'Page must not write on load');
  console.log('Grasp page PASS: running state, escaping, stop availability, live stream, config and run thumbnails, read-only load');
},60);
