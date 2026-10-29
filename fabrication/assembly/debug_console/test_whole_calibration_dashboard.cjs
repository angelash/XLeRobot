const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const html=fs.readFileSync(__dirname+'/static/original.html','utf8'),nodes=new Map(),requests=[];
for(const match of html.matchAll(/id="([^"]+)"/g))nodes.set(match[1],{innerHTML:'',textContent:'',hidden:false,disabled:false,checked:false,value:'',querySelectorAll(){return[];},appendChild(n){nodes.set(n.id,n);},scrollIntoView(){}});
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
assert.match(body({...c,step:'review'}),/当前姿态在范围中部/);
assert.match(body({applied:true}),/开启整机控制/);
context.s={manual_calibration:c,controller:{controller:'offline'},ports:[{},{}]};
vm.runInContext('calibrationOpen=true;renderCalibration(s)',context);
assert.equal(nodes.get('calibration-panel').hidden,false);
assert.equal(nodes.get('cal-close').textContent,'结束标定 · 保持扭矩关闭');
assert.equal(requests.length,0,'Rendering must never release torque, capture a pose or start a takeover');
context.s.manual_calibration={...c,step:'left_b',error:'采样中断'};
vm.runInContext('renderCalibration(s)',context);assert.match(nodes.get('cal-error').textContent,/采样中断/);
console.log('Whole calibration dashboard PASS: all physical steps, SVG references, source labels, explicit captures, no automatic robot writes');
