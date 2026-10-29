/* Free commissioning shares the original console's API and state poll. */
let calibrationOpen=false,calibrationView='',calibrationPending=false,calibrationLatest=null,calibrationRecoveryView='';
let calibrationPoseItem=null,calibrationPoseView='';
const calCheck=(id,text)=>`<label class="cal-check"><input type="checkbox" id="${id}"><span>${text}</span></label>`;
const calPoseKinds=[['a','姿态 A · 上臂竖直'],['b','姿态 B · 上臂外倾45°'],['c','姿态 C · 夹爪抬高'],['open','夹爪全开'],['closed','夹爪闭合']];
function calibrationPose(pose){const a=pose==='a',c=pose==='c',e=a?[75,80]:[146,109],t=[e[0]+105,e[1]],tip=c?[t[0]+39,t[1]-39]:[t[0]+55,t[1]];
 return `<svg class="cal-pose" viewBox="0 0 390 255" role="img" aria-label="侧视参考姿态${pose.toUpperCase()}，按肩肘腕轴中心连线摆放"><path d="M50 190H345M75 205V28" stroke="var(--line)" fill="none" stroke-dasharray="4 5"/><path d="M35 195H95V220H35Z" fill="var(--ink)"/><path d="M75 180L${e.join(' ')}L${t.join(' ')}" fill="none" stroke="var(--green)" stroke-width="14" stroke-linecap="round"/><path d="M${t.join(' ')}L${tip.join(' ')}" stroke="var(--amber)" stroke-width="9" stroke-linecap="round"/>${[[75,180],e,t].map(([x,y])=>`<circle cx="${x}" cy="${y}" r="9" fill="var(--paper)" stroke="var(--ink)" stroke-width="3"/>`).join('')}<text x="18" y="175">肩</text><text x="${e[0]-22}" y="${e[1]-22}">肘</text><text x="${t[0]-10}" y="${t[1]+30}">腕</text><text x="105" y="238">朝外 →　上方 ↑</text><text x="255" y="35">姿态 ${pose.toUpperCase()}</text></svg>`;
}
function calCounts(c){const ranges=c.ranges||[],ok=ranges.filter(x=>x.span_raw>=48).length,cards=side=>(c.captured||[]).filter(x=>x.startsWith(side+'_')).length;
 return {ok,total:ranges.length||14,left:cards('left'),right:cards('right'),base:c.wheels?.verified?'已现场确认':Object.keys(c.base_tests||{}).length?'已短测':'未测（不影响手柄驱动）'};}
function calibrationBody(c,s){
 const settings=s?.control_settings||{};
 if(c?.active){
  const n=calCounts(c),cards=[];
  for(const side of ['left','right']){
   const label=side==='left'?'左臂':'右臂';const port=c.arm_ports?.[side]||(side==='left'?'COM3':'COM4');
   cards.push(`<article class="cal-card"><h3>${label} <small class="tag">${esc(port)}</small></h3>${calPoseKinds.map(([kind,title])=>{const item=side+'_'+kind,done=(c.captured||[]).includes(item);return `<div class="cal-card-row ${done?'recorded':''}"><div><strong>${title}</strong><small>${done?'已录制 · 可覆盖重录':'未录制 · 点“录制”看示意与实时读数'}</small></div><button data-cal="capture-item" data-item="${item}">${done?'重录':'录制'}</button>${done?`<button class="outline" data-cal="delete-item" data-item="${item}">删除</button>`:''}</div>`;}).join('')}</article>`);
  }
  const groups=[['COM3',1,6],['COM4',1,6],['COM3',7,8]];
  const rangeCells=groups.flatMap(([port,lo,hi])=>(c.ranges||[]).filter(x=>x.key.startsWith(port+':')&&+x.key.split(':')[1]>=lo&&+x.key.split(':')[1]<=hi)).map(x=>`<div class="cal-range-cell" id="cal-range-${x.key.replace(':','-')}"></div>`).join('');
  const blockers=(c.apply_blockers||[]).filter(x=>x.item);const ready=!!c.apply_ready;
  return `<div class="cal-intro-card"><h3>① 行程：手动转动关节，读数实时累计</h3><p>扭矩已全部关闭。一次转一个关节，缓慢走过你要用的范围再回中部，不顶硬止挡。≥48计数（约4°）即算有效，可以随时保存，没转到的关节保持原限位。</p><div class="cal-range-table">${rangeCells}</div>
<div class="cal-buttons"><button class="primary" data-cal="apply-ranges" id="cal-apply-ranges">保存已采行程 · 开放直控</button></div><p class="fineprint">保存会把转过的关节写入舵机限位（逐颗回读，失败整批回滚、已录内容保留），然后结束采集。之后选“预设直控”就能在完整范围里拨杆控制。下次打开采集会自动带入已保存的行程和卡片。</p></div>
<div class="cal-intro-card"><h3>② 姿态卡（只用于“整机联动”末端IK，可以以后再录）</h3>${blockers.length?`<div class="cal-blockers" role="status"><strong>整机联动还缺 ${blockers.length} 项（不影响预设直控）</strong>${blockers.map(x=>`<button class="cal-blocker-link" data-cal="open-pose" data-item="${esc(x.item)}">${esc(x.message)} ↗</button>`).join('')}</div>`:'<p class="cal-ok">姿态卡已齐。</p>'}<div class="cal-buttons"><button class="primary" data-cal="apply-cards" ${ready?'':'disabled'}>${ready?'保存完整标定（整机联动）':'录齐姿态卡后可保存完整标定'}</button>${(c.captured||[]).length?'<button class="outline" data-cal="clear-captures">清空姿态并重录</button>':''}</div></div><div class="cal-card-grid">${cards.join('')}</div>
<div class="cal-reference"><h3>③ 底盘方向（随时可改，手柄立即按此驱动）</h3><p>轮径127mm、轮距250mm沿用资料值。按↑应朝车身正前方；前进和转向各自独立切换。</p><div class="cal-signs"><label>前进方向<select id="cal-base-forward"><option value="a" ${settings.base_forward!=='reverse'?'selected':''}>方案A（短测“前进”时车体向前）</option><option value="reverse" ${settings.base_forward==='reverse'?'selected':''}>反向</option></select></label><label>转向方向<select id="cal-base-turn"><option value="normal" ${settings.base_turn!=='reverse'?'selected':''}>正常（短测“左转”时车体左转）</option><option value="reverse" ${settings.base_turn==='reverse'?'selected':''}>反向</option></select></label></div>${calCheck('cal-base-safe','双臂已放稳，车轮周围有空位，可以短测')}<div class="cal-buttons"><button data-cal="base-forward">短测：前进</button><button data-cal="base-left">短测：左转</button></div><p id="cal-base-results" class="fineprint"></p>${calCheck('cal-forward-observed','看到车体朝正前方移动')}${calCheck('cal-left-observed','看到车体向左转')}<button class="outline" data-cal="wheels-verified">记录：方向与停车正确</button></div>`;
 }
 if(c?.applied&&c.applied_kind==='ranges')return `<h3>已把实测行程写入舵机限位，扭矩保持关闭。</h3><p>现在 ${(s?.saved_ranges||[]).length}/14 个关节使用实测行程。回到上方选“预设直控”，手柄回中、勾选确认即可开启。</p><p>以后想录姿态卡（整机联动用），再点“打开采集”：已保存的行程和已录卡片会自动带入，不用重来。</p><button class="primary" data-cal="done-preset">去预设直控</button>`;
 if(c?.applied)return `<h3>完整标定已保存，整机联动已重新核对。</h3><p>当前扭矩仍关闭。把手柄回中，在上方确认后点“开启整机联动”。预设直控同样使用这些行程。</p><button class="primary" data-cal="done">回到整机控制</button>`;
 return `<div class="cal-intro"><div><h3>释放扭矩，手动采集。</h3><p>开始后关闭双臂、夹爪、头部和双轮的扭矩，手动移动时不会发送任何位置目标。另一人或支撑物持续托住双臂重量。</p><p>行程、姿态卡、底盘方向可以任意顺序采集，录一项存一项，单项失败不影响其他项。只需要直控时，转完关节点“保存已采行程”就够了。已保存过的行程和卡片会自动带入。</p>${calCheck('cal-supported','双臂重量已有支撑，线路留有余量')}<button class="primary" data-cal="start">备份并关闭扭矩，开始采集</button></div><div class="cal-reference"><b>底盘采用资料参数</b><p>轮径 127 mm · 轮距 250 mm</p><p>轮径来自0.4装配页的5英寸轮；轮距来自双轮源码配置。方向在“控制设置”或采集面板里切换。</p></div></div>`;
}
function renderCalibration(s){
 const c=s.manual_calibration||{},active=s.controller.controller==='online';calibrationLatest=c;
 if(calibrationPoseItem)renderPoseDialog(s);
 if(c.active)calibrationOpen=true;
 $('open-calibration').disabled=active||busy||s.preview_only||s.ports.length!==2;
 $('open-calibration').textContent=c.active?'继续采集':'打开采集';
 $('calibration-panel').hidden=!calibrationOpen;if(!calibrationOpen)return;
 const settings=s.control_settings||{};
 const signature=(c.active?'active':c.applied?'complete:'+c.applied_kind:'intro')+':'+c.id+':'+(c.captured||[]).join(',')+':'+(c.error||'')+':'+settings.base_forward+':'+settings.base_turn+':'+(s.saved_ranges||[]).length;
 if(signature!==calibrationView){calibrationView=signature;$('cal-body').innerHTML=calibrationBody(c,s);}
 const n=calCounts(c);$('cal-progress').innerHTML=c.active?['行程 '+n.ok+'/'+n.total,'左臂 '+n.left+'/5','右臂 '+n.right+'/5','底盘 '+n.base].map((x,i)=>`<li class="current"><span>0${i+1}</span>${esc(x)}</li>`).join(''):'';
 $('cal-error').textContent=c.error||'';
 const recovery=c.recoverable,pending=(c.recovery_reset||[]).filter(x=>(c.ranges||[]).find(r=>r.key===x.key)?.span_raw<48),recoverySig=JSON.stringify([recovery?.id,recovery?.dismissed,pending.map(x=>x.key),c.active]);
 if(recoverySig!==calibrationRecoveryView){calibrationRecoveryView=recoverySig;let merge='';if(c.active&&!(c.captured||[]).length&&c.merge_checkpoint_id)merge=`<p class="fineprint">有先前已完成的手臂卡片可合并，电机状态不会改变。</p><button class="outline" id="cal-merge-button">从已验证备份合并姿态卡片</button>`;$('cal-recovery').hidden=!recovery&&!pending.length&&!merge;$('cal-recovery').innerHTML=recovery?`<strong>有一次未保存的采集（${recovery.observed_joints}项有效行程）${recovery.dismissed?'，是你结束的':''}。</strong><p>恢复会先核对设备身份和寄存器；当前位置超出旧记录的关节单独补采，其余保留；扭矩保持关闭。</p><button class="primary" id="cal-recover-button">恢复这次采集</button>${merge}`:pending.length?`<strong>有效记录已恢复，请补采：${pending.map(x=>esc(x.label)).join('、')}。</strong><p>这些关节的当前位置不在旧记录里，已单独重新开始记录；其他行程保留。</p>`:merge;}
 const recoverButton=$('cal-recover-button');if(recoverButton){recoverButton.disabled=calibrationPending||active;recoverButton.onclick=()=>calibrationCommand('recover',{confirmed:true,session_id:calibrationLatest.recoverable.id});}
 const mergeButton=$('cal-merge-button');if(mergeButton){mergeButton.disabled=calibrationPending||active;mergeButton.onclick=()=>calibrationCommand('merge-checkpoint',{confirmed:true,session_id:c.merge_checkpoint_id});}
 for(const item of c.ranges||[]){const node=$('cal-range-'+item.key.replace(':','-'));if(node){node.className='cal-range-cell '+(item.span_raw>=48?'ok':'short');node.textContent=`${item.label} · ${item.span_deg}° · 当前${item.position}`;}}
 const apply=$('cal-apply-ranges');if(apply){apply.textContent=n.ok?`保存已采行程 · 开放直控（${n.ok}项）`:'先转动关节，再保存行程';}
 const resultNode=$('cal-base-results');if(resultNode)resultNode.textContent=c.base_running?'短测执行中，等待停车回读…':`前进短测：${c.base_tests?.forward?'有位移且已停车':'未测'} · 左转短测：${c.base_tests?.left?'有位移且已停车':'未测'}`;
 $('cal-close').textContent=c.active?'结束采集 · 保持扭矩关闭':'收起';
 for(const button of $('cal-body').querySelectorAll('button'))button.disabled=calibrationPending||!!(c.active&&c.error)||!!c.base_running||(button.id==='cal-apply-ranges'&&!n.ok)||(button.dataset.cal==='apply-cards'&&!c.apply_ready);
}
async function calibrationCommand(action,body={}){
 if(calibrationPending)return;calibrationPending=true;busy=true;$('cal-message').textContent='正在处理…';
 try{const result=await api('/api/whole-calibration/'+action,body);$('cal-message').textContent=[action==='start'?'扭矩已关闭，可以任意顺序采集。':action==='apply-ranges'?'行程已写入并回读，采集已结束；可以直接预设直控。':'已保存',...(result.operation_warnings||[])].join(' · ');}
 catch(e){$('cal-message').textContent='未执行：'+e.message;}
 finally{calibrationPending=false;busy=false;await refresh();}
}
const poseNames={left_a:'姿态 A · 上臂竖直',left_b:'姿态 B · 上臂外倾45°',left_c:'姿态 C · 夹爪抬高',left_open:'夹爪全开',left_closed:'夹爪闭合',right_a:'姿态 A · 上臂竖直',right_b:'姿态 B · 上臂外倾45°',right_c:'姿态 C · 夹爪抬高',right_open:'夹爪全开',right_closed:'夹爪闭合'};
const poseHelp={a:['上臂轴线竖直向上。','肩→肘竖直，肘→腕水平朝外，夹爪方向水平。'],
 b:['上臂向外倾斜约45°。','保持前臂与夹爪水平；肩与肘同时变化。'],
 c:['保持B，只让夹爪尖端抬高。','上臂和前臂不动；只转腕俯仰，让夹爪尖端向上约45°。'],
 open:['把这只夹爪打开到可用端点。','不要强推机械止挡；保持约1秒。'],closed:['把这只夹爪闭合到可用端点。','让夹持面轻轻合拢；保持约1秒。']};
function openPoseDialog(item){
 if(!item||!poseNames[item])return;
 calibrationPoseItem=item;calibrationPoseView='';$('pose-dialog-steady').checked=false;$('pose-dialog-message').textContent='';$('pose-dialog').showModal();
 if(latest)renderPoseDialog(latest);
}
function renderPoseDialog(s){
 const item=calibrationPoseItem;if(!item)return;const c=s.manual_calibration||{},kind=item.slice(item.indexOf('_')+1);
 const side=item.startsWith('left')?'left':'right',sideName=side==='left'?'左臂':'右臂';const port=c.arm_ports?.[side]||'COM3';
 const signature=item+':'+(c.ranges||[]).map(x=>x.key+':'+x.position).join(',');
 if(signature!==calibrationPoseView){calibrationPoseView=signature;
  $('pose-dialog-title').textContent=sideName+' · '+poseNames[item].replace(/^姿态 [ABC] · /,'');$('pose-dialog-step').textContent=c.active?'打开弹窗不会录制或写入数据；摆好后勾选并录制。':'采集未开启：先在采集面板点“开始采集”关闭扭矩，才能手动摆姿态。';
  $('pose-dialog-instructions').innerHTML=`<p class="cal-instruction">${poseHelp[kind][0]}</p><p>${poseHelp[kind][1]}</p><p class="fineprint">按肩、肘、腕转轴中心连线摆放。姿态图只提供方向；下方编码器实时值才是这台机器人的真实读数。</p>`;
  $('pose-dialog-art').innerHTML=['a','b','c'].includes(kind)?calibrationPose(kind):`<div class="cal-grip-symbol" aria-label="夹爪${kind==='open'?'全开':'闭合'}">${kind==='open'?'〈　〉':'〉〈'}</div>`;
 }
 const ids=kind==='open'||kind==='closed'?[6]:[2,3,4];
 $('pose-dialog-live').innerHTML=`<strong>物理 ${esc(port)} 实时编码器</strong>${ids.map(i=>{const r=(c.ranges||[]).find(x=>x.key===port+':'+i);return `<span>${esc(r?.label||port+':'+i)} · <b>${r?.position??'读取中'}</b></span>`;}).join('')}<small>${c.base_running?'底盘测试进行中，暂不能录制':s.input_age_ms<500?'状态刷新中，摆好后保持约1秒':'状态过旧，等待刷新'}</small>`;
 $('pose-dialog-capture').disabled=calibrationPending||!c.active||!$('pose-dialog-steady').checked||!!c.error;
}
$('pose-dialog-close').onclick=()=>{$('pose-dialog').close();calibrationPoseItem=null;};
$('pose-dialog-steady').onchange=()=>{if(latest)renderPoseDialog(latest);};
$('pose-dialog-capture').onclick=async()=>{if(!calibrationPoseItem)return;await calibrationCommand('capture-item',{item:calibrationPoseItem,confirmed:$('pose-dialog-steady').checked});$('pose-dialog-message').textContent=$('cal-message').textContent;if(!/未执行/.test($('cal-message').textContent)){$('pose-dialog').close();calibrationPoseItem=null;}};
$('open-calibration').onclick=()=>{calibrationOpen=true;if(latest)renderCalibration(latest);$('calibration-panel').scrollIntoView({behavior:'smooth',block:'start'});};
$('cal-close').onclick=()=>{if(calibrationLatest?.active)calibrationCommand('cancel');else{calibrationOpen=false;$('calibration-panel').hidden=true;}};
$('cal-body').onchange=e=>{
 const key=e.target.id==='cal-base-forward'?'base_forward':e.target.id==='cal-base-turn'?'base_turn':null;
 if(key)command('/api/control-settings',{[key]:e.target.value}).then(()=>{$('cal-message').textContent='底盘方向已保存，手柄与短测都按新设置';});
};
$('cal-body').onclick=e=>{
 const action=e.target.closest('[data-cal]')?.dataset.cal;if(!action)return;
 if(action==='done'||action==='done-preset'){calibrationOpen=false;$('calibration-panel').hidden=true;$('target').value=action==='done'?'hardware':'preset';$('confirm').checked=false;mappingSignature='';render(latest);$('start').scrollIntoView({behavior:'smooth',block:'center'});return;}
 if(action==='start')return calibrationCommand('start',{confirmed:$('cal-supported').checked});
 if(action==='apply-ranges')return calibrationCommand('apply-ranges',{confirmed:true});
 if(action==='capture-item'||action==='open-pose')return openPoseDialog(e.target.closest('[data-item]').dataset.item);
 if(action==='delete-item')return calibrationCommand('delete-item',{item:e.target.closest('[data-item]').dataset.item});
 if(action==='clear-captures')return calibrationCommand('clear-captures',{confirmed:true});
 if(action==='wheels-verified')return calibrationCommand('wheels',{confirmed:$('cal-forward-observed').checked&&$('cal-left-observed').checked,wheels_verified:true});
 if(action==='apply-cards')return calibrationCommand('apply',{confirmed:true});
 if(action==='base-forward'||action==='base-left')return calibrationCommand('base-test',{kind:action==='base-forward'?'forward':'left',confirmed:$('cal-base-safe').checked});
};
