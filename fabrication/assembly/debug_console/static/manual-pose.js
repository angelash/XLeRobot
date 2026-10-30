/* Manual posing: release torque so joints can be moved by hand, then lock them
   where they now sit. Shares original.js $, esc, api(), refresh() and latest. */
let posePending=false,poseArmed='',poseArmedAt=0,poseSignature='';
const POSE_GROUPS=[['left','左臂'],['right','右臂'],['head','头部'],['base','底盘轮']];
function poseGroups(s){
 const groups=POSE_GROUPS.map(([key,name])=>({key,name,motors:[]}));
 for(const p of s.ports||[])for(const m of p.motors||[]){
  const label=String(m.label||''),key=label.startsWith('左臂')?'left':label.startsWith('右臂')?'right':label.startsWith('头部')?'head':'base';
  groups.find(g=>g.key===key).motors.push({port:p.port,id:m.id,label,short:label.replace(/^(左臂|右臂|头部)/,'')||label,
   torque:!!m.torque,position:m.position,min:m.min,max:m.max,outside:m.position<m.min||m.position>m.max});
 }
 return groups.filter(g=>g.motors.length);
}
function poseSelection(motors){const out={};for(const m of motors)(out[m.port]=out[m.port]||[]).push(m.id);return out;}
// Arm joints carried by gravity drop when released; grippers and wrist roll do not.
function poseHeavy(motors){return motors.some(m=>/^(左臂|右臂)/.test(m.label)&&!/(夹爪|腕旋转)$/.test(m.label));}
function poseBlocked(s){
 if(s.preview_only)return '隔离预览不操作扭矩';
 if(s.limit_selftest?.running)return '限位自检进行中，结束后再手动摆位';
 if((s.ports||[]).length<2)return '控制板未全部在线';
 return '';
}
function renderManualPose(s){
 const body=$('pose-body');if(!body)return;
 if(poseArmed&&Date.now()-poseArmedAt>5000)poseArmed='';
 const groups=poseGroups(s),blocked=poseBlocked(s),online=s.controller?.controller==='online',calibrating=!!s.manual_calibration?.active;
 const all=groups.flatMap(g=>g.motors),released=all.filter(m=>!m.torque).length;
 $('pose-state').textContent=all.length?(released?released+'/'+all.length+' 已卸力':'全部锁定'):'未连接';
 const signature=JSON.stringify([groups,blocked,online,calibrating,poseArmed,posePending]);
 if(signature===poseSignature)return;poseSignature=signature;
 const relOff=blocked||posePending?' disabled':'',holdOff=blocked||calibrating||posePending?' disabled':'';
 const note=blocked||(calibrating?'采集中扭矩已全部关闭，可以直接用手摆；结束采集后再锁定。':online?'手柄接管中：点任一按钮会先结束接管再执行。':'卸力前先托住手臂。摆好后点“锁定当前位置”，就在新姿势上保持；被推出原限位的关节会自动放宽限位，不会弹回。');
 const chip=m=>{const key='m:'+m.port+':'+m.id,armed=poseArmed===key;
  return '<button class="pose-chip'+(m.torque?'':' released')+(m.outside?' outside':'')+(armed?' armed':'')+'" data-pose="'+(m.torque?'release':'hold')+'" data-motor="'+esc(m.port)+':'+m.id+'" title="'+esc(m.label)+' · 位置 '+m.position+' · 限位 '+m.min+'–'+m.max+'"'+(m.torque?relOff:holdOff)+'><b>'+esc(m.short)+'</b><small>'+(armed?'托住后再点一次':m.torque?'锁定 · 点击卸力':'已卸力 · 点击锁定')+(m.outside?' · 超出原限位':'')+'</small></button>';};
 body.innerHTML='<p class="fineprint">'+esc(note)+'</p><div class="cal-buttons"><button data-pose="release" data-group="all" class="outline"'+relOff+'>'+(poseArmed==='all'?'托住双臂后再点一次':'全部卸力')+'</button><button data-pose="hold" data-group="all" class="primary"'+holdOff+'>全部锁定当前位置</button></div><div class="pose-groups">'+groups.map(g=>{
  const off=g.motors.filter(m=>!m.torque).length;
  return '<div class="pose-group"><div class="pose-group-head"><strong>'+esc(g.name)+'</strong><span class="tag">'+(off?off+'/'+g.motors.length+' 已卸力':'已锁定')+'</span></div><div class="pose-chips">'+g.motors.map(chip).join('')+'</div><div class="cal-buttons"><button data-pose="release" data-group="'+g.key+'" class="outline"'+relOff+'>'+(poseArmed===g.key?'托住后再点一次':'卸力')+'</button><button data-pose="hold" data-group="'+g.key+'"'+holdOff+'>锁定当前位置</button></div></div>';}).join('')+'</div>';
}
async function poseAct(action,target){
 if(posePending||!latest)return;
 const groups=poseGroups(latest),everything=groups.flatMap(g=>g.motors);let motors,key;
 if(target.motor){const [port,id]=target.motor.split(':');motors=everything.filter(m=>m.port===port&&m.id===Number(id));key='m:'+target.motor;}
 else{key=target.group;motors=key==='all'?everything:(groups.find(g=>g.key===key)?.motors||[]);}
 if(!motors.length)return;
 if(action==='release'&&poseHeavy(motors)&&poseArmed!==key){poseArmed=key;poseArmedAt=Date.now();poseSignature='';renderManualPose(latest);return;}
 poseArmed='';posePending=true;poseSignature='';$('pose-message').textContent=action==='release'?'正在卸力…':'正在锁定当前位置…';
 const labelOf=(port,id)=>everything.find(m=>m.port===port&&m.id===id)?.label||port+':'+id;
 try{
  const r=await api('/api/manual-pose',{action,motors:key==='all'?null:poseSelection(motors),end_takeover:latest.controller?.controller==='online'});
  const widened=(r.widened||[]).map(w=>labelOf(w.port,w.id)+'限位放宽到 '+w.min+'–'+w.max);
  $('pose-message').textContent=(action==='release'?'已卸力 '+r.count+' 个电机，可以用手摆了':'已锁定 '+r.count+' 个电机在当前位置')+(widened.length?'；'+widened.join('；'):'')+((r.skipped||[]).length?'；跳过检修中的 '+r.skipped.length+' 个':'');
 }catch(err){$('pose-message').textContent='未执行：'+err.message;}
 finally{posePending=false;poseSignature='';await refresh();}
}
if(typeof document!=='undefined')document.addEventListener('click',e=>{
 const b=e.target.closest&&e.target.closest('[data-pose]');if(!b||b.disabled)return;
 poseAct(b.dataset.pose,{group:b.dataset.group,motor:b.dataset.motor});
});

