// Run the shipped renderer against the actual HTML IDs and server mapping.
// The fake DOM records UI navigation; all network requests remain in memory.
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const path=require('node:path'),{execFileSync}=require('node:child_process');
const root=path.resolve(__dirname,'../../..');
const controlMap=JSON.parse(execFileSync(path.join(root,'.venv-debug/Scripts/python.exe'),['-X','utf8','-c','import json;from fabrication.assembly.debug_console.control_mapping import CONTROL_MAP;print(json.dumps(CONTROL_MAP))'],{cwd:root,encoding:'utf8'}));
const html=fs.readFileSync(__dirname+'/static/index.html','utf8');
const nodes=new Map();
function makeNode(id,dataset={}){return {id,dataset,textContent:'',innerHTML:'',value:'',checked:false,hidden:false,
 disabled:false,style:{},className:'',scrolled:0,scrollIntoView(){this.scrolled++;},
 querySelectorAll:()=>[],classList:{values:new Set(),toggle(k,on){if(on)this.values.add(k);else this.values.delete(k);},contains(k){return this.values.has(k);}}};}
for(const m of html.matchAll(/id="([^"]+)"/g))nodes.set(m[1],makeNode(m[1]));
function node(id){assert(nodes.has(id),'JS references a real HTML id: '+id);return nodes.get(id);}
const mapNodes=Array.from(html.matchAll(/class="map-key[^\"]*" data-map-key="([^\"]+)"/g),m=>makeNode('',{mapKey:m[1]}));
const presets=['4','16','32'].map(preset=>makeNode('',{preset}));
const requests=[],intervals=[];
const context=vm.createContext({document:{getElementById:node,activeElement:null,hidden:false,
 querySelectorAll:selector=>selector==='.map-key'?mapNodes:selector==='[data-preset]'?presets:[]},
 console,setInterval:(fn,ms)=>intervals.push({fn,ms}),fetch:(url,options)=>{requests.push({url,options});return new Promise(()=>{});}});
vm.runInContext(fs.readFileSync(__dirname+'/static/app.js','utf8'),context);
const motors=Array.from({length:8},(_,i)=>({id:i+1,label:'测试',position:2050,goal:2050,torque:1,min:1800,max:2300,voltage:12.1,temperature:30}));
context.sample={ports:[{port:'COM3',connected:true,motor_count:8,motors},{port:'COM4',connected:true,motor_count:8,motors}],
 cameras:[],version:'test',controller:{controller:'offline',selected_joint:'右臂肩水平'},calibration:{exists:true},
 gamepad:{connected:true,name:'Xbox test',buttons:Array(17).fill(false),axes:[0,0,0,0,0,0],hat:[0,0]},input_age_ms:5,
 joints:[{port:'COM4',id:1,label:'右臂肩水平',available:true,min:1954,max:2146,device_min:1800,device_max:2300},
 {port:'COM3',id:1,label:'左臂肩水平',available:false,min:2044,max:2051,reason:'设备行程过窄'}],
 control_map:controlMap,settings:{step_raw:16,speed_raw:100}};
function render(){vm.runInContext('render(sample)',context);}
render();assert.equal(node('start-controller').disabled,true,'Unconfirmed start is blocked');
node('wheels-lifted').checked=node('map-confirmed').checked=true;render();
assert.equal(node('start-controller').disabled,false);
assert.equal((node('mapping-list').innerHTML.match(/class="mapping-card/g)||[]).length,7);
assert.equal((node('button-grid').innerHTML.match(/<span/g)||[]).length,17);
node('view-full').onclick();
assert.equal((node('mapping-list').innerHTML.match(/class="mapping-card/g)||[]).length,22);
assert.match(node('mapping-list').innerHTML,/系统限制/);
const before=requests.length;
for(const n of mapNodes)n.onclick();
assert.equal(requests.length,before,'Clicking the diagram never sends hardware commands');
mapNodes.find(n=>n.dataset.mapKey==='up').onclick();
assert.match(node('map-detail').innerHTML,/右臂肩水平/);
assert.match(node('map-detail').innerHTML,/编码器/);
context.sample.controller={controller:'online',armed:true,selected_joint:'右臂肩水平'};
context.sample.gamepad.buttons[4]=true;context.sample.gamepad.hat=[0,1];render();
assert.equal(node('start-controller').hidden,true);assert.equal(node('stop-controller').disabled,false);
assert.match(node('lb-status').textContent,/LB按住/);
assert(mapNodes.find(n=>n.dataset.mapKey==='lb').classList.contains('pressed'));
context.sample.notice={code:'limit',level:'warning',message:'已到正向边界，接管保留'};render();
assert.match(node('feedback-title').textContent,/可以反向/);
assert.match(node('controller-state').textContent,/接管已开启/);
assert.equal(node('resume-controller').hidden,true);
context.sample.controller.paused=true;context.sample.controller.armed=false;context.sample.controller.pause_reason='模拟断连';render();
assert.match(node('controller-state').textContent,/接管保留/);
assert.equal(node('resume-controller').hidden,false);assert.equal(node('resume-controller').disabled,true);
context.sample.gamepad.buttons[4]=false;context.sample.gamepad.hat=[0,0];render();
assert.equal(node('resume-controller').disabled,false);
context.sample.gamepad.buttons[1]=true;render();assert.equal(node('resume-controller').disabled,true);
context.sample.gamepad.buttons[1]=false;
context.inputEvents=[{kind:'input',connected:true,buttons:Array(17).fill(false),time:new Date().toISOString()}];
vm.runInContext('consumeInput(inputEvents,false)',context);
for(const i of [0,3,7]){
 const pressed=Array(17).fill(false);pressed[i]=true;
 context.inputEvents=[{kind:'input',connected:true,buttons:pressed,time:new Date().toISOString()},
 {kind:'input',connected:true,buttons:Array(17).fill(false),time:new Date().toISOString()}];
 vm.runInContext('consumeInput(inputEvents,true)',context);
}
render();assert.equal(node('feedback-card').scrolled,1);
assert.equal(node('mapping-section').scrolled,1);
assert.match(node('mapping-mode-note').textContent,/完整/);
assert.match(node('mapping-list').innerHTML,/刚按过/);
assert.equal(requests.length,before,'A/Y/Menu only navigate the UI');
context.sample.last_command={outcome:'feedback',origin:2050,target:2066,position:2065,reached:true,time:new Date().toISOString(),port:'COM4',id:1};
context.sample.controller.paused=false;context.sample.notice=null;render();
assert.match(node('feedback-title').textContent,/进入目标容差/);
assert.match(node('feedback-values').textContent,/实测 2065/);
context.sample.input_age_ms=800;render();assert.equal(node('start-controller').disabled,true);
assert(!mapNodes.find(n=>n.dataset.mapKey==='lb').classList.contains('pressed'));
context.sample.gamepad.connected=false;render();assert.match(node('pad-state').textContent,/未连接/);
context.logSample=[{seq:1,kind:'input',message:'pressed'},{seq:2,kind:'limit',message:'boundary'},{seq:3,kind:'feedback',message:'encoder'}];
vm.runInContext('events.push(...logSample)',context);
for(const [filter,count] of [['important',2],['all',3],['input',1],['motion',1],['warning',1]]){
 node('log-filter').value=filter;vm.runInContext('renderEvents()',context);
 assert.match(node('log-count').textContent,new RegExp('当前 '+count+' 条'));
}
intervals.find(x=>x.ms===700).fn();assert.equal(requests.length,before,'No heartbeat from stale status');
console.log('Dashboard PASS: actual HTML IDs, 22 mappings, diagram inspection, live LB, limits, paused/resume gates, UI shortcuts, telemetry, stale input, log filters');
