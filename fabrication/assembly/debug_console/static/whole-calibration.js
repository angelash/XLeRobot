/* First-use commissioning shares the original console's API and state poll. */
let calibrationOpen=false,calibrationView='',calibrationPending=false,calibrationLatest=null,calibrationRecoveryView='';
let calibrationPoseItem=null,calibrationPoseView='';
const calSteps=['ranges','left_a','left_b','left_c','left_open','left_closed','right_a','right_b','right_c','right_open','right_closed','base','review','complete'];
const calTitles={ranges:'01 · 手动采集可用行程',left_a:'02 · 左臂姿态 A',left_b:'03 · 左臂姿态 B',left_c:'04 · 左臂姿态 C',left_open:'05 · 左夹爪全开',left_closed:'06 · 左夹爪闭合',right_a:'07 · 右臂姿态 A',right_b:'08 · 右臂姿态 B',right_c:'09 · 右臂姿态 C',right_open:'10 · 右夹爪全开',right_closed:'11 · 右夹爪闭合',base:'12 · 底盘方向与停车',review:'13 · 保存实测标定',complete:'标定已保存'};
const calCheck=(id,text)=>`<label class="cal-check"><input type="checkbox" id="${id}"><span>${text}</span></label>`;
function calibrationPose(pose){const a=pose==='a',c=pose==='c',e=a?[75,80]:[146,109],t=[e[0]+105,e[1]],tip=c?[t[0]+39,t[1]-39]:[t[0]+55,t[1]];
 return `<svg class="cal-pose" viewBox="0 0 390 255" role="img" aria-label="侧视参考姿态${pose.toUpperCase()}，按肩肘腕轴中心连线摆放"><path d="M50 190H345M75 205V28" stroke="var(--line)" fill="none" stroke-dasharray="4 5"/><path d="M35 195H95V220H35Z" fill="var(--ink)"/><path d="M75 180L${e.join(' ')}L${t.join(' ')}" fill="none" stroke="var(--green)" stroke-width="14" stroke-linecap="round"/><path d="M${t.join(' ')}L${tip.join(' ')}" stroke="var(--amber)" stroke-width="9" stroke-linecap="round"/>${[[75,180],e,t].map(([x,y])=>`<circle cx="${x}" cy="${y}" r="9" fill="var(--paper)" stroke="var(--ink)" stroke-width="3"/>`).join('')}<text x="18" y="175">肩</text><text x="${e[0]-22}" y="${e[1]-22}">肘</text><text x="${t[0]-10}" y="${t[1]+30}">腕</text><text x="105" y="238">朝外 →　上方 ↑</text><text x="255" y="35">姿态 ${pose.toUpperCase()}</text></svg>`;
}
function calibrationBody(c){
 if(c?.active&&c?.step==='cards'){
  const cards=[]
  for(const side of ['left','right']){
   const label=side==='left'?'左臂':'右臂';const port=c.arm_ports?.[side]||(side==='left'?'COM3':'COM4');
   const items=[['a','姿态 A · 上臂竖直'],['b','姿态 B · 上臂外倾45°'],['c','姿态 C · 夹爪抬高'],['open','夹爪全开'],['closed','夹爪闭合']];
   cards.push(`<article class="cal-card"><h3>${label}</h3>${items.map(([kind,title])=>`<div class="cal-card-row ${c.captured.includes(side+'_'+kind)?'recorded':''}"><div><strong>${title}</strong><small>${c.captured.includes(side+'_'+kind)?'已录制 · 可覆盖重录':'未录制 · 可任意时候采集'}</small></div><button data-cal="capture-item" data-item="${side}_${kind}">${c.captured.includes(side+'_'+kind)?'重录':'录制'}</button>${c.captured.includes(side+'_'+kind)?`<button class="outline" data-cal="delete-item" data-item="${side}_${kind}">删除</button>`:''}</div>`).join('')}</article>`)
  }
  const short=(c.ranges||[]).filter(x=>x.span_raw<48);
 const blockers=c.apply_blockers||[];const ready=!!c.apply_ready;
 return `<div class="cal-intro-card"><h3>自由采集</h3><p>任意顺序录制；录一项存一项。保存只使用已录项目与实测范围，底盘独立。</p>${blockers.length?`<div class="cal-blockers" role="status"><strong>保存前还缺 ${blockers.length} 项</strong>${blockers.map(x=>`<button class="cal-blocker-link" data-cal="open-pose" data-item="${esc(x.item||'')}">${esc(x.message)}${x.item?' ↗':''}</button>`).join('')}</div>`:'<p class="cal-ok">手臂与行程已齐，可以保存。</p>'}<div class="cal-buttons"><button class="primary" data-cal="capture-range">确认全部14项行程</button><button class="primary" data-cal="apply-cards" ${ready?'':'disabled'}>${ready?'保存当前手臂标定':'补齐上方项目后保存'}</button>${(c.captured||[]).length?'<button class="outline" data-cal="clear-captures">清空姿态并重录</button>':''}</div></div><div class="cal-card-grid">${cards.join('')}</div><div class="cal-reference"><h3>底盘（可选）</h3><p>轮径127mm、轮距250mm沿用资料值。方向、停车和速度可单独设置，不影响双臂录制与保存。</p><p><strong>你看到的车身正前方定义为“前进”。</strong>若方向相反，直接选择以下前进定义；无需理解正/负编码器。</p>${calCheck('cal-base-safe','双臂已放稳，车轮周围有空位，可以测试底盘')}<div class="cal-signs"><label>前进定义<select id="cal-forward-mode"><option value="a">按当前设定，看到车体向正前方</option><option value="reverse">看到车体后退，需要反向</option></select></label><label>左右轮物理位置<select id="cal-wheel-mapping"><option value="normal">左轮=电机9，右轮=电机10</option><option value="swapped">左轮=电机10，右轮=电机9</option></select></label></div><div class="cal-buttons"><button data-cal="base-forward">短测：前进</button><button data-cal="base-left">短测：左转</button><button data-cal="wheels-unverified">暂不开放底盘</button></div>${calCheck('cal-forward-observed','短测观察：车体朝我选定的正前方移动')}${calCheck('cal-left-observed','短测观察：车体向左侧转动')}<button class="primary" data-cal="wheels-verified">方向与停车正确，开放底盘</button></div>`
 }
 if(!c?.active&&!c?.applied)return `<div class="cal-intro"><div><h3>先释放，再按图摆放。</h3><p>这一步把真实关节角度和行程对应到手柄。开始后会关闭双臂、夹爪、头部和双轮的扭矩；手动移动时不会发送位置目标。另一人或支撑物持续托住双臂重量。</p><p>按顺序完成：14个关节的可用行程 → 左臂三姿态和夹爪 → 右臂 → 底盘。可刷新页面；运行进程内的采集状态仍保留。结束标定会保留扭矩关闭。</p>${calCheck('cal-supported','双臂重量已有支撑，线路留有余量，我准备开始手动标定')}<button class="primary" data-cal="start">备份并关闭扭矩，开始采集</button></div><div class="cal-reference"><b>底盘采用资料参数</b><p>轮径 127 mm · 轮距 250 mm</p><p>轮径来自0.4装配页的5英寸轮；轮距来自双轮源码配置。方向与停车在最后一步现场确认。</p></div></div>`;
 if(c.applied)return `<h3>真实标定已保存，整机模式已重新核对。</h3><p>当前扭矩仍关闭。把手柄回中，在上方确认周围安全并点“开启整机控制”。启动时保持当前位置，然后用完整手柄映射操作。</p><button class="primary" data-cal="done">回到整机控制</button>`;
 if(c.step==='ranges')return `<h3>${calTitles.ranges}</h3><p>一次只动一个关节，缓慢走过你要使用的完整范围，再回到中部。包括两臂各6轴和头部2轴；不要顶住硬止挡或拉紧线缆。读数会实时记录，跨零也会连续累计。</p><div class="cal-range-groups">${[['COM3',1,6,'左臂'],['COM4',1,6,'右臂'],['COM3',7,8,'头部']].map(([port,lo,hi,label])=>`<div><h4>${label}</h4>${(c.ranges||[]).filter(x=>x.key.startsWith(port+':')&&+x.key.split(':')[1]>=lo&&+x.key.split(':')[1]<=hi).map(x=>`<label class="cal-range-row"><input type="checkbox" data-range="${esc(x.key)}"><span>${esc(x.label)}<small id="cal-range-${x.key.replace(':','-')}"></small></span></label>`).join('')}</div>`).join('')}</div><p class="fineprint">勾选表示你已亲手走过该关节完整可用行程。软件只检查采集有效性，不能代替这项现场确认。</p><button class="primary" data-cal="capture">14项已采集，继续参考姿态</button>`;
 if(c.step==='review')return `<h3>${calTitles.review}</h3><p>把双臂放回姿态B附近，夹爪半开，肩水平、腕旋转和头部移到各自行程中部，支撑好并静止约1秒。</p><p>保存会将实测范围留出少量端点余量，重设坐标并逐颗回读；失败则回滚原寄存器。成功后扭矩仍关闭。</p>${calCheck('cal-save-confirm','全部行程和参考姿态按图采集，当前姿态在范围中部，可以保存')}<button class="primary" data-cal="apply">保存并核对整机模式</button>`;
 if(!c?.active||c.step==='cards')return '';
 const [side,pose]=String(c.step||'').split('_'),label=side==='left'?'左':'右';
 const preset={a:['上臂竖直向上，前臂水平朝外。','按肩→肘的轴心连线摆成竖直；肘→腕水平，夹爪朝向也保持水平。'],b:['上臂朝外倾45°，前臂仍水平。','从A只改变肩和肘，让上臂向外倾45°，前臂和夹爪仍水平；腕相对前臂保持不变。'],c:['保持B，只抬夹爪尖端45°。','上臂和前臂保持B姿态，手动转腕俯仰，使夹爪尖端从水平抬起约45°。'],open:['手动把夹爪打开到可用端点。','只移动'+label+'夹爪，不强推止挡；稍等稳定，再采集全开端点。'],closed:['手动把夹爪闭合到可用端点。','让夹持面轻轻合拢，不用力挤压；稍等稳定，再采集闭合端点。']};const info=preset[pose]||['选择要录制的项目','若当前是自由采集卡片，可在上方任意选择独立项目。'];
 return `<div class="cal-pose-layout"><div><h3>${calTitles[c.step]}</h3><p class="cal-instruction">${info[0]}</p><p>${info[1]}</p><p class="fineprint">侧视示意。以肩、肘、腕旋转轴的中心连线为准，不以外壳边缘为准。一次只摆${label}臂，另一臂保留支撑。</p><p id="cal-live-pose" class="cal-live-pose"></p><button class="primary" data-cal="capture">姿态已摆好，采集并继续</button></div>${['a','b','c'].includes(pose)?calibrationPose(pose):`<div class="cal-grip-symbol" aria-label="夹爪${pose==='open'?'全开':'闭合'}">${pose==='open'?'〈　〉':'〉〈'}</div>`}</div>`;
}
function renderCalibration(s){
 const c=s.manual_calibration||{},active=s.controller.controller==='online';calibrationLatest=c;
 if(calibrationPoseItem)renderPoseDialog(s);
 if(c.active)calibrationOpen=true;
 $('open-calibration').disabled=active||busy||s.preview_only||s.ports.length!==2;
 $('open-calibration').textContent=c.active?'继续整机标定':'开始整机标定向导';
 $('calibration-panel').hidden=!calibrationOpen;if(!calibrationOpen)return;
 const signature=(c.active?'active':c.applied?'complete':'intro')+':'+c.id+':'+c.step+':'+(c.captured||[]).join(',')+':'+(c.error||'');
 if(signature!==calibrationView){calibrationView=signature;$('cal-body').innerHTML=calibrationBody(c);}
 const index=calSteps.indexOf(c.step);$('cal-progress').innerHTML=['ranges','left_a','right_a','base','review'].map((key,i)=>`<li class="${c.active&&index>=calSteps.indexOf(key)||c.applied?'current':''}"><span>0${i+1}</span>${['行程','左臂','右臂','底盘','保存'][i]}</li>`).join('');
 $('cal-error').textContent=c.error||'';
 const recovery=c.recoverable,pending=(c.recovery_reset||[]).filter(x=>(c.ranges||[]).find(r=>r.key===x.key)?.span_raw<48),recoverySig=JSON.stringify([recovery?.id,pending.map(x=>x.key),c.step]);
 $('cal-recovery').hidden=!recovery&&!pending.length;
 if(recoverySig!==calibrationRecoveryView){calibrationRecoveryView=recoverySig;let merge='';if(c.active&&c.step==='cards'&&!(c.captured||[]).length)merge=`<p class="fineprint">有先前已完成的手臂卡片可合并，电机状态不会改变。</p><button class="outline" id="cal-merge-button">从已验证备份合并姿态卡片</button>`;$('cal-recovery').hidden=!recovery&&!pending.length&&!merge;$('cal-recovery').innerHTML=recovery?`<strong>已保存 ${recovery.observed_joints} 项有效行程，可以恢复。</strong><p>会先核对设备身份和寄存器。当前位置超出旧记录的关节单独补采，其余有效范围保留；扭矩保持关闭。参考姿态重新采集。</p><button class="primary" id="cal-recover-button">恢复保留的行程</button>${merge}`:pending.length?`<strong>有效记录已恢复，请补采：${pending.map(x=>esc(x.label)).join('、')}。</strong><p>这些关节的当前位置不在旧记录里，已单独重新开始记录；其他行程保留。</p>`:merge;}
 const recoverButton=$('cal-recover-button');if(recoverButton){recoverButton.disabled=calibrationPending||active;recoverButton.onclick=()=>calibrationCommand('recover',{confirmed:true,session_id:calibrationLatest.recoverable.id});}
 const mergeButton=$('cal-merge-button');if(mergeButton){mergeButton.disabled=calibrationPending||active;mergeButton.onclick=()=>calibrationCommand('merge-checkpoint',{confirmed:true,session_id:c.merge_checkpoint_id});}
 for(const item of c.ranges||[]){const node=$('cal-range-'+item.key.replace(':','-'));if(node)node.textContent=`${item.span_deg}° · ${item.span_raw}计数 · 当前${item.position}`;}
 const poseNode=$('cal-live-pose');if(poseNode){const port=c.step?.startsWith('left_')?'COM3':'COM4';poseNode.textContent=(c.ranges||[]).filter(x=>x.key.startsWith(port+':')&&[2,3,4,6].includes(+x.key.split(':')[1])).map(x=>x.label+' '+x.position).join(' / ');}
 const resultNode=$('cal-base-results');if(resultNode)resultNode.textContent=c.base_running?'短脉冲执行中，等待停车回读…':`前进：${c.base_tests?.forward?'位移与停车已回读':'未测'} / 左转：${c.base_tests?.left?'位移与停车已回读':'未测'}`;
 $('cal-close').textContent=c.active?'结束标定 · 保持扭矩关闭':'收起';
 for(const button of $('cal-body').querySelectorAll('button'))button.disabled=calibrationPending||!!(c.active&&c.error)||!!c.base_running;
 const old=$('cal-back');
 if(c.active&&index>0&&index<12&&!c.base_running){if(!old){const b=document.createElement('button');b.id='cal-back';b.className='outline';b.dataset.cal='back';b.textContent='返回上一步';$('cal-body').appendChild(b);}$('cal-back').disabled=calibrationPending;}else if(old)old.remove();
}
async function calibrationCommand(action,body={}){
 if(calibrationPending)return;calibrationPending=true;busy=true;$('cal-message').textContent='正在处理…';
 try{const result=await api('/api/whole-calibration/'+action,body);$('cal-message').textContent=[action==='start'?'扭矩已关闭，可以独立录制任意项目。':'已保存',...(result.operation_warnings||[])].join(' · ');}
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
  $('pose-dialog-title').textContent=sideName+' · '+poseNames[item].replace(/^姿态 [ABC] · /,'');$('pose-dialog-step').textContent='唯一的现场操作入口；打开弹窗本身不会录制或写入数据。';
  $('pose-dialog-instructions').innerHTML=`<p class="cal-instruction">${poseHelp[kind][0]}</p><p>${poseHelp[kind][1]}</p><p class="fineprint">按肩、肘、腕转轴中心连线摆放。姿态图只提供方向；下方编码器实时值才是这台机器人的真实读数。</p>`;
  $('pose-dialog-art').innerHTML=['a','b','c'].includes(kind)?calibrationPose(kind):`<div class="cal-grip-symbol" aria-label="夹爪${kind==='open'?'全开':'闭合'}">${kind==='open'?'〈　〉':'〉〈'}</div>`;
 }
 const ids=kind==='open'||kind==='closed'?[6]:[2,3,4];
 $('pose-dialog-live').innerHTML=`<strong>物理 ${esc(port)} 实时编码器</strong>${ids.map(i=>{const r=(c.ranges||[]).find(x=>x.key===port+':'+i);return `<span>${esc(r?.label||port+':'+i)} · <b>${r?.position??'读取中'}</b></span>`;}).join('')}<small>${c.base_running?'底盘测试进行中，暂不能录制':s.input_age_ms<500?'状态刷新中，摆好后保持约1秒':'状态过旧，等待刷新'}</small>`;
 $('pose-dialog-capture').disabled=calibrationPending||!$('pose-dialog-steady').checked||!!c.error;
}
$('pose-dialog-close').onclick=()=>$('pose-dialog').close();
$('pose-dialog-steady').onchange=()=>{if(latest)renderPoseDialog(latest);};
$('pose-dialog-capture').onclick=async()=>{if(!calibrationPoseItem)return;await calibrationCommand('capture-item',{item:calibrationPoseItem,confirmed:$('pose-dialog-steady').checked});$('pose-dialog-message').textContent=$('cal-message').textContent;if(!/未执行/.test($('cal-message').textContent)){$('pose-dialog').close();calibrationPoseItem=null;}};
$('open-calibration').onclick=()=>{calibrationOpen=true;if(latest)renderCalibration(latest);$('calibration-panel').scrollIntoView({behavior:'smooth',block:'start'});};
$('cal-close').onclick=()=>{if(calibrationLatest?.active)calibrationCommand('cancel');else{calibrationOpen=false;$('calibration-panel').hidden=true;}};
$('cal-body').onclick=e=>{
 const action=e.target.closest('[data-cal]')?.dataset.cal,c=calibrationLatest;if(!action)return;
 if(action==='done'){calibrationOpen=false;$('calibration-panel').hidden=true;$('target').value='hardware';$('confirm').checked=false;render(latest);$('start').scrollIntoView({behavior:'smooth',block:'center'});return;}
 if(action==='start')return calibrationCommand('start',{confirmed:$('cal-supported').checked});
 if(action==='capture')return calibrationCommand('capture',{step:c.step,confirmed_keys:Array.from($('cal-body').querySelectorAll('[data-range]:checked')).map(x=>x.dataset.range)});
 if(action==='capture-range')return calibrationCommand('capture-item',{item:'range',confirmed:true});
 if(action==='capture-item'||action==='open-pose')return openPoseDialog(e.target.closest('[data-item]').dataset.item);
 if(action==='delete-item')return calibrationCommand('delete-item',{item:e.target.closest('[data-item]').dataset.item});
 if(action==='clear-captures')return calibrationCommand('clear-captures',{confirmed:true});
 if(action==='wheels-unverified')return calibrationCommand('wheels',{left_sign:$('cal-forward-mode').value==='reverse'?1:-1,right_sign:$('cal-forward-mode').value==='reverse'?-1:1,confirmed:true,wheels_verified:false,wheel_mapping:$('cal-wheel-mapping').value});
 if(action==='wheels-verified')return calibrationCommand('wheels',{left_sign:$('cal-forward-mode').value==='reverse'?1:-1,right_sign:$('cal-forward-mode').value==='reverse'?-1:1,confirmed:$('cal-forward-observed').checked&&$('cal-left-observed').checked,wheels_verified:true,wheel_mapping:$('cal-wheel-mapping').value});
 if(action==='apply-cards')return calibrationCommand('apply',{confirmed:true});
 if(action==='back')return calibrationCommand('rewind',{step:calSteps[calSteps.indexOf(c.step)-1]});
 if(action==='base-forward'||action==='base-left')return calibrationCommand('base-test',{kind:action==='base-forward'?'forward':'left',confirmed:$('cal-base-safe').checked,left_sign:$('cal-forward-mode').value==='reverse'?1:-1,right_sign:$('cal-forward-mode').value==='reverse'?-1:1,wheel_mapping:$('cal-wheel-mapping').value});
 if(action==='base-confirm')return calibrationCommand('base-confirm',{forward_observed:$('cal-forward-observed').checked,left_observed:$('cal-left-observed').checked});
 if(action==='apply')return calibrationCommand('apply',{confirmed:$('cal-save-confirm').checked});
};
