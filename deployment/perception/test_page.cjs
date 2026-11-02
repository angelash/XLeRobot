// Runs the obstacle page script against a minimal DOM and fake API (no browser).
const fs=require('fs');const path=require('path');const vm=require('vm');const assert=require('assert');
const html=fs.readFileSync(path.join(__dirname,'static','index.html'),'utf8');
const elements={};
for(const id of [...html.matchAll(/id="([^"]+)"/g)].map(m=>m[1]))elements[id]={id,innerHTML:'',textContent:'',hidden:false,className:'',disabled:false,checked:false,src:null,
  setAttribute(k,v){this[k]=v;},removeAttribute(k){this[k]=null;},addEventListener(){}};
const status={running:true,stream:'perception-1',camera:0,frames_in:120,frame_age_ms:40,published:110,publish_errors:0,processing_ms_median:18.2,processing_ms_p90:24,
  latest:{valid:false,metric:false,clear_m:null,blind_m:null,obstacles:[{x_m:0.8,y_m:0.02,kind:'person'}],reasons:['未完成地面标定：<b>']},
  head:{ok:false,reason:'地面标定没有记录头部姿态',positions:{'COM3:7':2005}},calibration:{status:'none'},floor_model:{ready:true,samples:40000},
  gate:{present:true,age_ms:60,max_age_ms:350,valid:false,metric:false,head_pose_ok:false,blocks:3}};
const calls=[];
const context={document:{getElementById:id=>elements[id]},fetch:async(url,o={})=>{calls.push([url,o.method||'GET']);return {ok:true,json:async()=>status};},
  AbortController,setTimeout,clearTimeout,setInterval:()=>0,confirm:()=>true,console,Date,Math,Number,String,Object,JSON,Error,Promise};
vm.createContext(context);
vm.runInContext(fs.readFileSync(path.join(__dirname,'static','app.js'),'utf8'),context);
setTimeout(()=>{
  assert.equal(elements.state.textContent,'阻断');assert.equal(elements.clear.textContent,'—');
  assert(elements.reasons.innerHTML.includes('&lt;b&gt;')&&!elements.reasons.innerHTML.includes('<b>'));
  assert(elements.facts.innerHTML.includes('人 0.8m')&&elements.gate.innerHTML.includes('阻断')&&elements.calibration.innerHTML.includes('未采用'));
  assert.equal(elements.live.src,'/perception/stream.mjpg');assert(elements.start.disabled&&!elements.stop.disabled);
  assert(calls.every(([,m])=>m==='GET'),'Page must not write on load');
  console.log('Obstacle page PASS: blocked state, escaping, gate and calibration panels, live stream, read-only load');
},50);
