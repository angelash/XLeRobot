const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const html=fs.readFileSync(__dirname+'/static/original.html','utf8'),nodes=new Map(),requests=[];
for(const match of html.matchAll(/id="([^"]+)"/g))nodes.set(match[1],{innerHTML:'',textContent:''});
assert.ok(nodes.has('pose-body')&&nodes.has('pose-state')&&nodes.has('pose-message'),'Panel is on the control page');
assert.match(html,/manual-pose\.js\?v=\d+/);
let reply={count:0};const plain=x=>JSON.parse(JSON.stringify(x));
const context=vm.createContext({$:id=>nodes.get(id),esc:v=>String(v??'').replace(/[<>]/g,'?'),latest:null,Date,JSON,
 refresh:async()=>{},api:async(path,body)=>{requests.push({path,body});return reply;}});
vm.runInContext(fs.readFileSync(__dirname+'/static/manual-pose.js','utf8'),context);
const names=['肩水平','肩抬升','肘','腕俯仰','腕旋转','夹爪'];
const port=(name,side,extra)=>({port:name,motors:[...names.map((n,i)=>({id:i+1,label:side+n,torque:1,position:2000,min:1500,max:2500})),...extra]});
const s={preview_only:false,controller:{controller:'offline'},manual_calibration:{active:false},limit_selftest:{running:false},
 ports:[port('COM3','右臂',[{id:7,label:'头部水平',torque:1,position:2000,min:1500,max:2500},{id:8,label:'头部俯仰',torque:1,position:2000,min:1500,max:2500}]),
        port('COM4','左臂',[{id:9,label:'左轮',torque:1,position:0,min:0,max:4095},{id:10,label:'右轮',torque:1,position:0,min:0,max:4095}])]};
context.s=s;context.latest=s;
vm.runInContext('renderManualPose(s)',context);
let body=nodes.get('pose-body').innerHTML;
assert.equal((body.match(/class="pose-chip[ "]/g)||[]).length,16,'Every motor has its own button');
for(const g of ['左臂','右臂','头部','底盘轮'])assert.match(body,new RegExp('<strong>'+g+'</strong>'));
assert.equal(nodes.get('pose-state').textContent,'全部锁定');assert.equal(requests.length,0,'Rendering never writes');
(async()=>{
 await vm.runInContext('poseAct("release",{group:"left"})',context);
 assert.equal(requests.length,0,'Releasing a heavy arm group needs a second click');
 assert.match(nodes.get('pose-body').innerHTML,/托住后再点一次/);
 reply={count:6,motors:[]};
 await vm.runInContext('poseAct("release",{group:"left"})',context);
 assert.deepEqual(JSON.parse(JSON.stringify(requests[0])),{path:'/api/manual-pose',body:{action:'release',motors:{COM4:[1,2,3,4,5,6]},end_takeover:false}});
 await vm.runInContext('poseAct("release",{group:"head"})',context);
 assert.deepEqual(plain(requests[1].body.motors),{COM3:[7,8]},'Head releases on one click');
 await vm.runInContext('poseAct("release",{motor:"COM3:6"})',context);
 assert.deepEqual(plain(requests[2].body.motors),{COM3:[6]},'A gripper releases on one click');
 s.ports[1].motors[2]={...s.ports[1].motors[2],torque:0,position:2600};
 vm.runInContext('poseSignature="";renderManualPose(s)',context);body=nodes.get('pose-body').innerHTML;
 assert.match(body,/已卸力 · 点击锁定 · 超出原限位/);assert.equal(nodes.get('pose-state').textContent,'1/16 已卸力');
 reply={count:1,widened:[{port:'COM4',id:3,min:1500,max:2608}],skipped:[]};
 await vm.runInContext('poseAct("hold",{motor:"COM4:3"})',context);
 assert.deepEqual(plain(requests[3].body),{action:'hold',motors:{COM4:[3]},end_takeover:false});
 assert.match(nodes.get('pose-message').textContent,/左臂肘限位放宽到 1500–2608/);
 s.controller={controller:'online'};reply={count:16};
 await vm.runInContext('poseAct("hold",{group:"all"})',context);
 assert.deepEqual(plain(requests[4].body),{action:'hold',motors:null,end_takeover:true},'A takeover is ended first');
 s.controller={controller:'offline'};s.limit_selftest={running:true};
 vm.runInContext('poseSignature="";renderManualPose(s)',context);
 assert.match(nodes.get('pose-body').innerHTML,/限位自检进行中/);assert.match(nodes.get('pose-body').innerHTML,/disabled/);
 console.log('Manual pose dashboard PASS: 16 motor buttons in 4 groups, two-click arm release, one-click head/gripper, widened limits reported, takeover ended first, blocked during self-test');
})().catch(e=>{console.error(e);process.exit(1);});

