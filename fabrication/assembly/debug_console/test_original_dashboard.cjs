const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict'),{execFileSync}=require('node:child_process'),path=require('node:path');
const root=path.resolve(__dirname,'../../..');
const data=JSON.parse(execFileSync(path.join(root,'.venv-debug/Scripts/python.exe'),['-X','utf8','-c','import json;from fabrication.assembly.debug_console.upstream_teleop import UpstreamTeleop;from fabrication.assembly.debug_console.original_mapping import ORIGINAL_MAP;print(json.dumps(dict(original=UpstreamTeleop().snapshot(),control_map=ORIGINAL_MAP)))'],{cwd:root,encoding:'utf8'}));
const html=fs.readFileSync(__dirname+'/static/original.html','utf8'),nodes=new Map(),cards=new Map();
function node(id){if(!nodes.has(id)){assert(html.includes('id="'+id+'"')||id.startsWith('value-'),'Real HTML id: '+id);nodes.set(id,{innerHTML:'',textContent:'',disabled:false,checked:false,hidden:false,value:id==='target'?'preview':'',className:'',querySelector:()=>({disabled:false}),scrollIntoView(){},classList:{toggle(){}}});}return nodes.get(id);}
const requests=[];
const context=vm.createContext({document:{getElementById:node,hidden:false,querySelector:selector=>{if(!cards.has(selector))cards.set(selector,{classList:{toggle(){}}});return cards.get(selector);}},console,setInterval(){},fetch:(url,options)=>{requests.push({url,options});return new Promise(()=>{});}});
vm.runInContext(fs.readFileSync(__dirname+'/static/original.js','utf8'),context);
context.s={...data,ports:[],controller:{controller:'offline'},gamepad:{connected:true,buttons:Array(17).fill(false),axes:[0,0,0,0,0,0],hat:[0,0]},input_age_ms:10,control_profile:'diagnostic',preview_only:true,version:'3.0',original_readiness:{base:{ready:false,reasons:['需要速度模式']}}};
function render(){vm.runInContext('render(s)',context);}
render();assert(node('start').disabled);assert.match(node('blocker').textContent,/确认上方/);
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
console.log('Original dashboard PASS: mapping, linkage diagrams, visible gates, neutral start/resume, existing-session protection, target confirmation reset');

context.s.controller={controller:'offline'};context.s.gamepad.buttons=Array(17).fill(false);node('target').value='hardware';node('confirm').checked=true;render();
assert(node('start').disabled);assert.match(node('blocker').textContent,/实机未就绪/);
node('target').value='preview';render();assert.equal(node('start').disabled,false,'Preview does not require serial boards');
context.s.preview_only=false;render();assert.match(node('backend').textContent,/设备监控/);

context.s.control_profile='original_hardware';context.s.controller={controller:'online',paused:false};node('target').value='preview';render();
assert.equal(node('target').value,'hardware','A reloaded page must show the actual active execution target');
assert.match(node('confirm-label').textContent,/操作实机/);
