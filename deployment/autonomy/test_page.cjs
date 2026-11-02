// Runs the autonomy page script against a minimal DOM and fake API (no browser).
const fs=require('fs');const path=require('path');const vm=require('vm');const assert=require('assert');
const html=fs.readFileSync(path.join(__dirname,'static','index.html'),'utf8');
const ids=[...html.matchAll(/id="([^"]+)"/g)].map(m=>m[1]);
const elements={};const listeners={};
for(const id of ids)elements[id]={id,innerHTML:'',textContent:'',hidden:false,className:'',disabled:false,
  addEventListener(type,fn){(listeners[id+':'+type]??=[]).push(fn);}};
const status={nodes:[{node_id:'ASH-LAPTOP',name:'ASH-LAPTOP',online:true,heartbeat_age_s:0.4,inflight:0,max_parallel:2,address:'192.168.137.1',
  capabilities:['system.echo','speech.transcribe'],resources:{host:'ASH-LAPTOP',logical_cpus:32,cpu_percent:12,memory:{total_mb:65275,available_mb:30000,load_percent:54},gpu:[{name:'RTX 4060',memory_free_mb:3700,memory_total_mb:8188}]},
  details:{providers:{'llm.chat':{available:false,reason:'ConnectError <script>'}}}}],
  local:{resources:{host:'SASA-LAPTOP',logical_cpus:8,cpu_percent:9,memory:{total_mb:8038,available_mb:2100,load_percent:74,process_mb:180}},executor_threads:1},
  capabilities:[{name:'system.echo',title:'连通',tier:'laptop',note:'',available_on:['local','ASH-LAPTOP']},{name:'llm.chat',title:'本地模型',tier:'worker',note:'需服务',available_on:[]}],
  tiers:{laptop:'笔记本可做',worker:'仅工作端'},routes:{auto:'按能力'},jobs:{},stats:[]};
const jobs={jobs:[{created_at:'2026-11-02T20:00:00+08:00',capability:'system.echo',route:'prefer_worker',node:'local',status:'done',fallback:'没有在线的工作端节点',run_ms:1.2,total_ms:3}]};
const learning={protocols:require('./test_protocols.json'),validation:{},sessions:[{id:'a',protocol:'W2',title:'直线距离标定',created_at:'2026-11-02T20:00:00+08:00',status:'open',samples:3,with_truth:1,runs:1,photos:2,note:''}],
  models:[{id:'m',kind:'wheel_distance',status:'validated',created_at:'2026-11-02T20:00:00+08:00',metrics:{rmse_m:0.0012,n:5}}],
  calibrations:[{id:'c',kind:'intrinsics',camera:'head',status:'candidate',created_at:'2026-11-02T20:00:00+08:00',metric:'rms_px',metric_value:0.4,threshold:0.8,meets_threshold:true}],
  print_assets:['checkerboard-9x6-25mm.pdf']};
const replies={'/status':status,'/jobs':jobs,'/learning':learning};
const calls=[];
const context={document:{getElementById:id=>elements[id],addEventListener(type,fn){(listeners['document:'+type]??=[]).push(fn);}},
  fetch:async(url,options={})=>{calls.push([url,options.method||'GET']);const key=url.replace('/api/platform/autonomy','');return {ok:true,json:async()=>replies[key]??{}};},
  AbortController,setTimeout,clearTimeout,setInterval:()=>0,performance,confirm:()=>true,console,Date,Math,Number,String,Object,JSON,Error,Promise};
vm.createContext(context);
vm.runInContext(fs.readFileSync(path.join(__dirname,'static','app.js'),'utf8'),context);
setTimeout(()=>{
  assert(elements.nodes.innerHTML.includes('车载笔记本') && elements.nodes.innerHTML.includes('ASH-LAPTOP'));
  assert(elements.nodes.innerHTML.includes('&lt;script&gt;') && !elements.nodes.innerHTML.includes('<script>'));
  assert(elements.capabilities.innerHTML.includes('暂无') && elements.jobs.innerHTML.includes('回退'));
  assert(elements.protocols.innerHTML.includes('W2') && elements.sessions.innerHTML.includes('直线距离标定'));
  assert(elements.models.innerHTML.includes('由我采用') && elements.assets.innerHTML.includes('/autonomy/print/checkerboard-9x6-25mm.pdf'));
  assert.equal(elements.live.textContent,'平台已连接');
  assert.equal(elements.notice.textContent,'','No error notice on a healthy load');
  assert(calls.every(([,method])=>method==='GET'),'Page must not write on load');
  console.log('Autonomy page PASS: nodes, escaping, capabilities, jobs, protocols, review buttons, assets, read-only load');
},50);
