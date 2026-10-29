/* First-use commissioning shares the original console's API and state poll. */
let calibrationOpen=false,calibrationView='',calibrationPending=false,calibrationLatest=null;
const calSteps=['ranges','left_a','left_b','left_c','left_open','left_closed','right_a','right_b','right_c','right_open','right_closed','base','review','complete'];
const calTitles={ranges:'01 · 手动采集可用行程',left_a:'02 · 左臂姿态 A',left_b:'03 · 左臂姿态 B',left_c:'04 · 左臂姿态 C',left_open:'05 · 左夹爪全开',left_closed:'06 · 左夹爪闭合',right_a:'07 · 右臂姿态 A',right_b:'08 · 右臂姿态 B',right_c:'09 · 右臂姿态 C',right_open:'10 · 右夹爪全开',right_closed:'11 · 右夹爪闭合',base:'12 · 底盘方向与停车',review:'13 · 保存实测标定',complete:'标定已保存'};
const calCheck=(id,text)=>`<label class="cal-check"><input type="checkbox" id="${id}"><span>${text}</span></label>`;
function calibrationPose(pose){const a=pose==='a',c=pose==='c',e=a?[75,80]:[146,109],t=[e[0]+105,e[1]],tip=c?[t[0]+39,t[1]-39]:[t[0]+55,t[1]];
 return `<svg class="cal-pose" viewBox="0 0 390 255" role="img" aria-label="侧视参考姿态${pose.toUpperCase()}，按肩肘腕轴中心连线摆放"><path d="M50 190H345M75 205V28" stroke="var(--line)" fill="none" stroke-dasharray="4 5"/><path d="M35 195H95V220H35Z" fill="var(--ink)"/><path d="M75 180L${e.join(' ')}L${t.join(' ')}" fill="none" stroke="var(--green)" stroke-width="14" stroke-linecap="round"/><path d="M${t.join(' ')}L${tip.join(' ')}" stroke="var(--amber)" stroke-width="9" stroke-linecap="round"/>${[[75,180],e,t].map(([x,y])=>`<circle cx="${x}" cy="${y}" r="9" fill="var(--paper)" stroke="var(--ink)" stroke-width="3"/>`).join('')}<text x="18" y="175">肩</text><text x="${e[0]-22}" y="${e[1]-22}">肘</text><text x="${t[0]-10}" y="${t[1]+30}">腕</text><text x="105" y="238">朝外 →　上方 ↑</text><text x="255" y="35">姿态 ${pose.toUpperCase()}</text></svg>`;
}
function calibrationBody(c){
 if(!c?.active&&!c?.applied)return `<div class="cal-intro"><div><h3>先释放，再按图摆放。</h3><p>这一步把真实关节角度和行程对应到手柄。开始后会关闭双臂、夹爪、头部和双轮的扭矩；手动移动时不会发送位置目标。另一人或支撑物持续托住双臂重量。</p><p>按顺序完成：14个关节的可用行程 → 左臂三姿态和夹爪 → 右臂 → 底盘。可刷新页面；运行进程内的采集状态仍保留。结束标定会保留扭矩关闭。</p>${calCheck('cal-supported','双臂重量已有支撑，线路留有余量，我准备开始手动标定')}<button class="primary" data-cal="start">备份并关闭扭矩，开始采集</button></div><div class="cal-reference"><b>底盘采用资料参数</b><p>轮径 127 mm · 轮距 250 mm</p><p>轮径来自0.4装配页的5英寸轮；轮距来自双轮源码配置。方向与停车在最后一步现场确认。</p></div></div>`;
 if(c.applied)return `<h3>真实标定已保存，整机模式已重新核对。</h3><p>当前扭矩仍关闭。把手柄回中，在上方确认周围安全并点“开启整机控制”。启动时保持当前位置，然后用完整手柄映射操作。</p><button class="primary" data-cal="done">回到整机控制</button>`;
 if(c.step==='ranges')return `<h3>${calTitles.ranges}</h3><p>一次只动一个关节，缓慢走过你要使用的完整范围，再回到中部。包括两臂各6轴和头部2轴；不要顶住硬止挡或拉紧线缆。读数会实时记录，跨零也会连续累计。</p><div class="cal-range-groups">${[['COM3',1,6,'左臂'],['COM4',1,6,'右臂'],['COM3',7,8,'头部']].map(([port,lo,hi,label])=>`<div><h4>${label}</h4>${(c.ranges||[]).filter(x=>x.key.startsWith(port+':')&&+x.key.split(':')[1]>=lo&&+x.key.split(':')[1]<=hi).map(x=>`<label class="cal-range-row"><input type="checkbox" data-range="${esc(x.key)}"><span>${esc(x.label)}<small id="cal-range-${x.key.replace(':','-')}"></small></span></label>`).join('')}</div>`).join('')}</div><p class="fineprint">勾选表示你已亲手走过该关节完整可用行程。软件只检查采集有效性，不能代替这项现场确认。</p><button class="primary" data-cal="capture">14项已采集，继续参考姿态</button>`;
 if(c.step==='base')return `<h3>${calTitles.base}</h3><p>双臂放稳在支撑上，头部和夹爪保持不动。每次只发160raw、约180ms的短脉冲，并回读零速与扭矩关闭。</p><div class="cal-base-layout"><div><p class="cal-reference">资料参数：轮径127 mm · 轮距250 mm。未做实机尺寸测量。</p><div class="cal-signs"><label>左轮前进方向 <select id="cal-left-sign"><option value="-1">负方向（原版）</option><option value="1">正方向</option></select></label><label>右轮前进方向 <select id="cal-right-sign"><option value="1">正方向（原版）</option><option value="-1">负方向</option></select></label></div>${calCheck('cal-base-safe','双臂放稳，轮子周围有空位，可以小幅移动')}<div class="cal-buttons"><button data-cal="base-forward">短测前进</button><button data-cal="base-left">短测左转</button></div><p id="cal-base-results"></p></div><div>${calCheck('cal-forward-observed','刚才前进测试，车体实际向前移动')}${calCheck('cal-left-observed','刚才左转测试，车体实际向左转动')}<p class="fineprint">若方向不符，修改上方方向并重测两项；按现场所见确认。</p><button class="primary" data-cal="base-confirm">方向正确，继续保存</button></div></div>`;
 if(c.step==='review')return `<h3>${calTitles.review}</h3><p>把双臂放回姿态B附近，夹爪半开，肩水平、腕旋转和头部移到各自行程中部，支撑好并静止约1秒。</p><p>保存会将实测范围留出少量端点余量，重设坐标并逐颗回读；失败则回滚原寄存器。成功后扭矩仍关闭。</p>${calCheck('cal-save-confirm','全部行程和参考姿态按图采集，当前姿态在范围中部，可以保存')}<button class="primary" data-cal="apply">保存并核对整机模式</button>`;
 const [side,pose]=c.step.split('_'),label=side==='left'?'左':'右';
 const info={a:['上臂竖直向上，前臂水平朝外。','按肩→肘的轴心连线摆成竖直；肘→腕水平，夹爪朝向也保持水平。'],b:['上臂朝外倾45°，前臂仍水平。','从A只改变肩和肘，让上臂向外倾45°，前臂和夹爪仍水平；腕相对前臂保持不变。'],c:['保持B，只抬夹爪尖端45°。','上臂和前臂保持B姿态，手动转腕俯仰，使夹爪尖端从水平抬起约45°。'],open:['手动把夹爪打开到可用端点。','只移动'+label+'夹爪，不强推止挡；稍等稳定，再采集全开端点。'],closed:['手动把夹爪闭合到可用端点。','让夹持面轻轻合拢，不用力挤压；稍等稳定，再采集闭合端点。']}[pose];
 return `<div class="cal-pose-layout"><div><h3>${calTitles[c.step]}</h3><p class="cal-instruction">${info[0]}</p><p>${info[1]}</p><p class="fineprint">侧视示意。以肩、肘、腕旋转轴的中心连线为准，不以外壳边缘为准。一次只摆${label}臂，另一臂保留支撑。</p><p id="cal-live-pose" class="cal-live-pose"></p><button class="primary" data-cal="capture">姿态已摆好，采集并继续</button></div>${['a','b','c'].includes(pose)?calibrationPose(pose):`<div class="cal-grip-symbol" aria-label="夹爪${pose==='open'?'全开':'闭合'}">${pose==='open'?'〈　〉':'〉〈'}</div>`}</div>`;
}
function renderCalibration(s){
 const c=s.manual_calibration||{},active=s.controller.controller==='online';calibrationLatest=c;
 if(c.active)calibrationOpen=true;
 $('open-calibration').disabled=active||busy||s.preview_only||s.ports.length!==2;
 $('open-calibration').textContent=c.active?'继续整机标定':'开始整机标定向导';
 $('calibration-panel').hidden=!calibrationOpen;if(!calibrationOpen)return;
 const signature=(c.active?'active':c.applied?'complete':'intro')+':'+c.id+':'+c.step;
 if(signature!==calibrationView){calibrationView=signature;$('cal-body').innerHTML=calibrationBody(c);}
 const index=calSteps.indexOf(c.step);$('cal-progress').innerHTML=['ranges','left_a','right_a','base','review'].map((key,i)=>`<li class="${c.active&&index>=calSteps.indexOf(key)||c.applied?'current':''}"><span>0${i+1}</span>${['行程','左臂','右臂','底盘','保存'][i]}</li>`).join('');
 $('cal-error').textContent=c.error||'';
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
 try{await api('/api/whole-calibration/'+action,body);$('cal-message').textContent=action==='start'?'扭矩已关闭，可以按下方步骤手动移动。':'已保存本步';}
 catch(e){$('cal-message').textContent='未执行：'+e.message;}
 finally{calibrationPending=false;busy=false;await refresh();}
}
$('open-calibration').onclick=()=>{calibrationOpen=true;if(latest)renderCalibration(latest);$('calibration-panel').scrollIntoView({behavior:'smooth',block:'start'});};
$('cal-close').onclick=()=>{if(calibrationLatest?.active)calibrationCommand('cancel');else{calibrationOpen=false;$('calibration-panel').hidden=true;}};
$('cal-body').onclick=e=>{
 const action=e.target.closest('[data-cal]')?.dataset.cal,c=calibrationLatest;if(!action)return;
 if(action==='done'){calibrationOpen=false;$('calibration-panel').hidden=true;$('target').value='hardware';$('confirm').checked=false;render(latest);$('start').scrollIntoView({behavior:'smooth',block:'center'});return;}
 if(action==='start')return calibrationCommand('start',{confirmed:$('cal-supported').checked});
 if(action==='capture')return calibrationCommand('capture',{step:c.step,confirmed_keys:Array.from($('cal-body').querySelectorAll('[data-range]:checked')).map(x=>x.dataset.range)});
 if(action==='back')return calibrationCommand('rewind',{step:calSteps[calSteps.indexOf(c.step)-1]});
 if(action==='base-forward'||action==='base-left')return calibrationCommand('base-test',{kind:action==='base-forward'?'forward':'left',confirmed:$('cal-base-safe').checked,left_sign:+$('cal-left-sign').value,right_sign:+$('cal-right-sign').value});
 if(action==='base-confirm')return calibrationCommand('base-confirm',{forward_observed:$('cal-forward-observed').checked,left_observed:$('cal-left-observed').checked});
 if(action==='apply')return calibrationCommand('apply',{confirmed:$('cal-save-confirm').checked});
};
