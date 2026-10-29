const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const html=fs.readFileSync(__dirname+'/static/original.html','utf8'),nodes=new Map(),requests=[];
for(const match of html.matchAll(/id="([^"]+)"/g))nodes.set(match[1],{innerHTML:'',textContent:'',hidden:false,disabled:false,checked:false,value:'',querySelectorAll(){return[];},appendChild(n){nodes.set(n.id,n);},scrollIntoView(){},showModal(){this.open=true;},close(){this.open=false;}});
const context=vm.createContext({$:id=>nodes.get(id)||null,esc:v=>String(v??'').replace(/[<>]/g,'?'),latest:null,busy:false,refresh:async()=>{},render(){},api:async(path,body)=>{requests.push({path,body});},document:{createElement:()=>({dataset:{},remove(){nodes.delete(this.id);}})}});
vm.runInContext(fs.readFileSync(__dirname+'/static/whole-calibration.js','utf8'),context);
function body(c){context.c=c;return vm.runInContext('calibrationBody(c)',context);}
assert.match(body({}),/备份并关闭扭矩/);assert.match(body({}),/127 mm/);
const c={active:true,id:'test',step:'ranges',ranges:[{key:'COM3:1',label:'左肩水平',position:2000,span_deg:80,span_raw:910}]};
assert.match(body(c),/data-range="COM3:1"/);assert.match(body(c),/完整可用行程/);
for(const side of ['left','right'])for(const pose of ['a','b','c','open','closed']){
 const result=body({...c,step:side+'_'+pose});assert.match(result,/data-cal="capture"|data-cal="capture-item"/);
 if(['a','b','c'].includes(pose))assert.match(result,/<svg[^>]+aria-label="侧视参考姿态/);
}
assert.match(body({...c,step:'cards',captured:[]}),/data-cal="base-forward"/);
assert.match(body({...c,step:'cards',captured:[]}),/底盘（可选）/);
assert.match(body({...c,step:'cards',captured:[]}),/前进定义/);
assert.match(body({...c,step:'cards',captured:[]}),/左轮=电机9/);
assert.doesNotMatch(body({...c,step:'cards',captured:[]}),/左轮前进方向/);
assert.match(body({...c,step:'review'}),/当前姿态在范围中部/);
assert.match(body({applied:true}),/开启整机控制/);
context.s={manual_calibration:c,controller:{controller:'offline'},ports:[{},{}]};
vm.runInContext('calibrationOpen=true;renderCalibration(s)',context);
assert.equal(nodes.get('calibration-panel').hidden,false);
assert.equal(nodes.get('cal-close').textContent,'结束标定 · 保持扭矩关闭');
assert.equal(requests.length,0,'Rendering must never release torque, capture a pose or start a takeover');
context.s.manual_calibration={...c,step:'left_b',error:'采样中断'};
vm.runInContext('renderCalibration(s)',context);assert.match(nodes.get('cal-error').textContent,/采样中断/);
const pose={active:true,id:'pose',step:'cards',captured:[],apply_ready:false,apply_blockers:[{item:'left_a',message:'尚未录制：左臂 姿态 A'}],arm_ports:{left:'COM4',right:'COM3'},ranges:[{key:'COM4:2',label:'左臂肩抬升',position:2000},{key:'COM4:3',label:'左臂肘',position:2000},{key:'COM4:4',label:'左臂腕俯仰',position:2000}]};
const poseBody=body(pose);assert.match(poseBody,/尚未录制：左臂 姿态 A/);assert.match(poseBody,/data-cal="open-pose" data-item="left_a"/);
context.s={...context.s,manual_calibration:pose,ports:[{},{}],gamepad:{connected:true},input_age_ms:10};vm.runInContext('openPoseDialog("left_a");renderPoseDialog(s)',context);
assert.equal(nodes.get('pose-dialog').open,true);assert.match(nodes.get('pose-dialog-art').innerHTML,/侧视参考姿态A/);assert.match(nodes.get('pose-dialog-live').innerHTML,/物理 COM4/);assert.equal(nodes.get('pose-dialog-capture').disabled,true);assert.equal(requests.length,0);
nodes.get('pose-dialog-steady').checked=true;vm.runInContext('renderPoseDialog(s)',context);assert.equal(nodes.get('pose-dialog-capture').disabled,false);
console.log('Whole calibration dashboard PASS: all physical steps, SVG references, source labels, explicit captures, no automatic robot writes');
