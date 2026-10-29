const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict'),{execFileSync}=require('node:child_process'),path=require('node:path');
const root=path.resolve(__dirname,'../../..');
const data=JSON.parse(execFileSync(path.join(root,'.venv-debug/Scripts/python.exe'),['-X','utf8','-c','import json;from fabrication.assembly.debug_console.upstream_teleop import UpstreamTeleop;from fabrication.assembly.debug_console.original_mapping import ORIGINAL_MAP,JOINT_MAP,PRESET_MAP;from fabrication.assembly.debug_console.control_settings import DEFAULTS;print(json.dumps(dict(original=UpstreamTeleop().snapshot(),control_map=ORIGINAL_MAP,trial_control_map=JOINT_MAP,preset_control_map=PRESET_MAP,defaults=DEFAULTS)))'],{cwd:root,encoding:'utf8'}));
const html=fs.readFileSync(__dirname+'/static/original.html','utf8'),nodes=new Map(),cards=new Map();
const generated=/^(value-|(left|right|head)-\d+-(direction|rate|tolerance)$)/;
function node(id){if(!nodes.has(id)){assert(html.includes('id="'+id+'"')||generated.test(id),'Real HTML id: '+id);nodes.set(id,{innerHTML:'',textContent:'',disabled:false,checked:false,hidden:false,value:id==='target'?'preview':'',className:'',querySelector:()=>({disabled:false}),scrollIntoView(){},classList:{toggle(){}}});}return nodes.get(id);}
const requests=[];
const context=vm.createContext({document:{getElementById:node,hidden:false,querySelector:selector=>{if(!cards.has(selector))cards.set(selector,{classList:{toggle(){}}});return cards.get(selector);}},console,setInterval(){},fetch:(url,options)=>{requests.push({url,options});return new Promise(()=>{});}});
vm.runInContext(fs.readFileSync(__dirname+'/static/original.js','utf8'),context);
const settings=(extra={})=>({...data.defaults,logical_ports:{left:'COM3',right:'COM4'},...extra});
context.s={...data,ports:[],controller:{controller:'offline'},gamepad:{connected:true,buttons:Array(17).fill(false),axes:[0,0,0,0,0,0],hat:[0,0]},input_age_ms:10,control_profile:'diagnostic',preview_only:true,version:'3.6',original_readiness:{base:{ready:false,reasons:['需要速度模式']}},control_settings:settings()};
function render(){vm.runInContext('render(s)',context);}
render();assert(node('start').disabled);assert.match(node('blocker').textContent,/勾选上方确认/);
node('confirm').checked=true;render();assert.equal(node('start').disabled,false);
assert.match(node('mapping').innerHTML,/B不再是停止键|头部水平减少/);
assert.match(node('mapping-detail').innerHTML,/平面高度/);
assert.match(node('arm-view').innerHTML,/左臂/);assert.match(node('arm-view').innerHTML,/右臂/);
assert.match(node('readiness').innerHTML,/需要速度模式/);
context.s.gamepad.axes[4]=.8;render();assert(node('start').disabled);assert.match(node('blocker').textContent,/松开所有/);
context.s.gamepad.axes[4]=0;context.s.controller={controller:'online',paused:false};context.s.control_profile='original_preview';render();
assert.equal(node('pause').disabled,false);assert.equal(node('stop').disabled,false);assert.equal(node('start').hidden,true);
context.s.controller.paused=true;render();assert.equal(node('resume').hidden,false);assert.equal(node('resume').disabled,false);
context.s.gamepad.buttons[1]=true;render();assert.equal(node('resume').disabled,true);
context.s.control_profile='diagnostic';render();assert.equal(node('stop').disabled,true);assert.match(node('session-description').textContent,/由你在原页面结束/);
const before=requests.length;node('target').onchange();assert.equal(node('confirm').checked,false);assert.equal(requests.length,before);

context.s.controller={controller:'offline'};context.s.gamepad.buttons=Array(17).fill(false);node('target').value='hardware';node('confirm').checked=true;render();
assert(node('start').disabled);assert.match(node('blocker').textContent,/实机未就绪/);
node('target').value='preview';render();assert.equal(node('start').disabled,false,'Preview does not require serial boards');
context.s.preview_only=false;render();assert.match(node('backend').textContent,/设备监控/);

context.s.control_profile='original_hardware';context.s.controller={controller:'online',paused:false};node('target').value='preview';render();
assert.equal(node('target').value,'hardware','A reloaded page must show the actual active execution target');

// Direct modes show physical encoder counts on the logical side's real bus.
const motors=port=>Array.from({length:8},(_,k)=>({id:k+1,position:port==='COM3'?1000+k:3000+k,min:0,max:4095,goal:0,mode:0,torque:1,label:port+':'+(k+1)}));
context.s.ports=[{port:'COM3',motors:motors('COM3')},{port:'COM4',motors:motors('COM4')}];
context.s.control_profile='original_joint';render();
assert.equal(node('target').value,'joint');assert.match(node('backend').textContent,/低速试控/);
assert.equal(node('joint-help').hidden,false);assert.equal(node('arm-preview').hidden,true);
assert.match(node('right-position').textContent,/实测计数：肩水平 3000/);
context.s.control_settings=settings({arm_mapping:'swapped',logical_ports:{left:'COM4',right:'COM3'}});render();
assert.match(node('left-position').textContent,/肩水平 3000/,'Swapped arms read the physical COM4 board for the left stick');

// Preset is the default when the IK mapping is not ready; unusable parts are listed with a fix.
const presetContext=vm.createContext({document:context.document,console,setInterval(){},fetch:(url,options)=>{requests.push({url,options});return new Promise(()=>{});}});
vm.runInContext(fs.readFileSync(__dirname+'/static/original.js','utf8'),presetContext);
presetContext.s={...context.s,preferred_target:'preset',control_profile:'diagnostic',controller:{controller:'offline'},control_settings:settings(),saved_ranges:['COM3:1'],
  preset_readiness:{left_lift:{ready:true,note:'设备限位只有5.6°，仍是旧试控窗口；保存已采行程后放开'},left_pan:{ready:false,reasons:['当前位置3010不在设备限位1800–1864内；在整机标定里点“保存已采行程”后放开']},base:{ready:true,note:'资料值'}},
  whole_robot_setup:{ready:false,checks:[{key:'left',label:'左臂联动与夹爪',ready:false,reasons:['需要参考姿态']}]}};
node('confirm').checked=false;vm.runInContext('targetInitialized=false;render(s)',presetContext);
assert.equal(node('target').value,'preset');assert.match(node('start').textContent,/预设直控/);
assert(html.includes('<option value="preset" selected>'));
assert.match(node('readiness').innerHTML,/去保存行程/);assert.match(node('readiness').innerHTML,/范围很小/);
assert.match(node('readiness-title').textContent,/2\/3 可直接控制/);
assert.match(node('setup-title').textContent,/1\/14/);assert.equal(node('whole-setup').hidden,false);
assert.match(node('mapping-detail').innerHTML,/肩抬升/,'Preset shows the direct-joint mapping');
node('confirm').checked=true;vm.runInContext('render(s)',presetContext);
assert.equal(node('start').disabled,false);assert.match(node('blocker').textContent,/2\/3个部件可直接控制/);
node('target').value='hardware';vm.runInContext('render(s)',presetContext);
assert(node('start').disabled);assert.match(node('blocker').textContent,/先选“预设直控”/);assert.match(node('setup-checks').innerHTML,/需要参考姿态/);
node('target').value='preset';

// Polling every 250 ms must never overwrite a settings form being edited.
vm.runInContext('render(s)',presetContext);assert.equal(node('joint-rate').value,12);
node('joint-rate').value=40;node('settings-form').oninput({target:node('joint-rate')});
presetContext.s.control_settings=settings({joint_rate_deg_s:20});vm.runInContext('render(s)',presetContext);
assert.equal(node('joint-rate').value,40);assert.match(node('settings-state').textContent,/未保存/);
node('settings-reset').onclick();assert.equal(node('joint-rate').value,20);
// Only individually tuned axes are stored, so later default changes still apply.
node('left-2-direction').value='-1';node('left-2-rate').value='';node('left-3-rate').value='45';
for(const k of ['left-1','right-1','right-2','right-3','left-4','right-4','left-5','right-5','left-6','right-6','head-7','head-8'])node(k+'-direction').value='1';
const tuned=vm.runInContext('collectAxisSettings(s.control_settings)',presetContext);
assert.deepEqual(JSON.parse(JSON.stringify(tuned)),{'COM3:2':{direction:-1},'COM3:3':{joint_rate_deg_s:45}});
presetContext.s.controller={controller:'online',paused:false};presetContext.s.control_profile='original_preset';vm.runInContext('render(s)',presetContext);
assert.equal(node('base-forward').disabled,true,'Chassis directions change only between takeovers');assert.match(node('settings-state').textContent,/即时生效/);
assert(!requests.some(r=>r.options?.method==='POST'),'Rendering never sends a command');
console.log('Original dashboard PASS: preset default, per-part readiness with fixes, live tuning without overwrite, logical arm encoders, visible gates');
