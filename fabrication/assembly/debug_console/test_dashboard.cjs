// Exercise the actual dashboard renderer with representative native inputs.
// No browser or hardware commands are sent by this test.
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const nodes=new Map();
function node(id){if(!nodes.has(id))nodes.set(id,{textContent:'',innerHTML:'',value:'',checked:true,
  disabled:false,style:{},nextElementSibling:{},querySelector:()=>({}),className:''});return nodes.get(id);}
const context=vm.createContext({document:{getElementById:node,activeElement:null,hidden:false},
  console,setInterval:()=>{},fetch:async()=>({ok:true,json:async()=>({ports:[],cameras:[],controller:{},gamepad:{},calibration:{},settings:{}})})});
vm.runInContext(fs.readFileSync(__dirname+'/static/app.js','utf8'),context);
const motors=Array.from({length:8},(_,i)=>({id:i+1,label:'测试',position:2050,goal:2050,
  torque:1,min:1800,max:2300,voltage:12.1,temperature:30}));
context.sample={ports:[{port:'COM3',connected:true,motor_count:8,motors},
  {port:'COM4',connected:true,motor_count:8,motors}],cameras:[],version:'test',
  controller:{controller:'offline',selected_joint:'右臂肩水平'},calibration:{exists:true},
  gamepad:{connected:true,name:'Xbox test',buttons:Array(16).fill(false),axes:[0,0,0,0,-1,-1],hat:[0,0]},
  input_age_ms:5,joints:[],settings:{step_raw:16,speed_raw:100}};
vm.runInContext('render(sample)',context);
assert.equal(node('start-controller').disabled,false,'Native pad + confirmation should permit start');
assert.equal((node('button-grid').innerHTML.match(/<span/g)||[]).length,16,'Render all native buttons');
assert.match(node('summary-torque').textContent,/保持姿态/);
context.sample.controller={controller:'online',armed:true,selected_joint:'右臂肩水平'};
context.sample.gamepad.buttons[4]=true;context.sample.gamepad.hat=[0,1];
vm.runInContext('render(sample)',context);
assert.match(node('pad-state').textContent,/LB 按住/);
assert.match(node('summary-torque').textContent,/允许小步/);
assert.match(node('hat-state').innerHTML,/Y=1/);
assert.equal(node('stop-controller').disabled,false);
context.sample.gamepad.connected=false;context.sample.controller={controller:'offline'};
vm.runInContext('render(sample)',context);
assert.equal(node('start-controller').disabled,true);
assert.match(node('pad-state').textContent,/未连接/);
console.log('Dashboard checks passed: all buttons, LB, D-pad, native readiness, offline gate');
