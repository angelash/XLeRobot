'use strict';
const Studio = (() => {
  const pages=[['home','工作台总览','01'],['control','控制中心','02'],['devices','设备与相机','03'],['data','数据工作室','04'],['simulation','仿真实验室','05'],['vision','视觉实验室','06'],['learning','学习实验室','07'],['manual','图文手册','08'],['capabilities','能力全景','09']];
  const labels={integrated:'已接入',adaptation:'待适配',guide:'图文指南',reference:'社区参考'};
  const groups={control:'遥操作',hardware:'硬件与设备',data:'数据',simulation:'仿真',vision:'视觉',learning:'学习',application:'应用与架构'};
  const $=id=>document.getElementById(id), esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const date=s=>s?new Date(s).toLocaleString('zh-CN',{month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit'}):'—';
  const fmt=n=>Number(n||0).toFixed(1), badge=s=>`<span class="tag ${s}">${labels[s]||esc(s)}</span>`;
  let catalog,status,library={recordings:[],poses:[],experiments:[]},route='home',routeVersion=0,sourceRaw='',capState='all',capGroup='all',player=null,playerFrame=0,playing=false,playStart=0,playOrigin=0,sandbox=null,hold=null,holdBusy=false,qualityBlob=null,pollBusy=false;
  let toastTimer,lastControlStatus=0;
  function heartbeatAllowed(s,page,visible,age){return page==='control'&&visible&&age<2500&&s?.upstream_connected&&s.runtime?.controller?.controller==='online'&&!s.runtime.controller.paused;}
  async function controlHeartbeat(){if(heartbeatAllowed(status,route,!document.hidden,Date.now()-lastControlStatus))await api('/api/controller/heartbeat',{}).catch(()=>{});}
  async function api(path,payload,raw=false){
    const options=payload===undefined?{}:raw?{method:'POST',body:payload,headers:{'Content-Type':payload.type||'application/octet-stream'}}:{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)};
    const res=await fetch(path,options);let data;
    try{data=await res.json();}catch{data={detail:'服务未返回有效数据'};}
    if(!res.ok)throw new Error(typeof data.detail==='string'?data.detail:JSON.stringify(data.detail||data));
    return data;
  }
  function toast(message,error=false){const t=$('toast');t.textContent=message;t.hidden=false;t.className='toast'+(error?' error':'');clearTimeout(toastTimer);toastTimer=setTimeout(()=>t.hidden=true,error?8500:4500);}
  async function action(button,fn){if(button?.disabled)return;const old=button?.textContent;if(button){button.disabled=true;button.textContent='处理中…';}try{return await fn();}catch(e){toast(e.message,true);}finally{if(button?.isConnected){button.disabled=false;button.textContent=old;}updateStatusUI();}}
  const heading=(k,t,p,extra='')=>`<div class="page-heading"><div><p class="eyebrow">${k}</p><h1>${t}</h1><p>${p}</p></div>${extra}</div>`;
  const guideButton=(id,label='阅读指南 ↗')=>`<button class="button compact" data-guide="${id}">${label}</button>`;
  function sourceButtons(paths){return `<div class="sources">${paths.map(p=>catalog.source_ids[p]?`<button class="source-link" data-source="${catalog.source_ids[p]}">${esc(p.split('/').pop())} ↗</button>`:'').join('')}</div>`;}
  const empty=(t,p)=>`<div class="empty"><strong>${t}</strong>${p}</div>`;
  function feature(id,title,desc,target){return `<a class="card feature" href="#${target||id}"><div class="feature-icon">${StudioVisuals.icon(id)}</div><span class="arrow">↗</span><h3>${title}</h3><p>${desc}</p><span class="subtle">进入工作区 →</span></a>`;}
  function home(){
    const integrated=catalog.capabilities.filter(c=>c.state==='integrated').length;
    return `<section class="hero"><div class="hero-copy"><p class="eyebrow">BUILD. CONTROL. LEARN.</p><h1>你的机器人，<br>在这里<em>学会更多。</em></h1><p>连接双臂与双轮，留下每一次尝试。<br>从遥操作、仿真到视觉与学习，在同一个工作台推进。</p><div class="actions"><a class="button primary" href="#control">进入控制中心 ↗</a><a class="button" href="#manual/start">从这里开始</a></div></div><div class="hero-art"><img src="${catalog.media.robot}" alt="XLeRobot 双臂移动推车官方插画"><span class="hero-note">TWO ARMS. MANY POSSIBILITIES.</span></div></section>
    <section class="grid four"><div class="card stat"><div class="stat-label">控制服务 <span>↗</span></div><div class="value" id="stat-service">—</div><span class="caption" id="stat-session">读取会话…</span></div><div class="card stat"><div class="stat-label">实测电机 <span>◎</span></div><div class="value"><span id="stat-motors">—</span><small>/ 16</small></div><span class="caption">两块控制板 · 实时回读</span></div><div class="card stat"><div class="stat-label">平台工具 <span>⌘</span></div><div class="value">${integrated}<small>/ ${catalog.capabilities.length}项能力</small></div><span class="caption">其余能力附适配路线与来源</span></div><div class="card stat"><div class="stat-label">我的资产 <span>▤</span></div><div class="value" id="stat-assets">—</div><span class="caption">状态轨迹 · 姿态 · 实验配置</span></div></section>
    <div class="section-heading"><h2>继续探索</h2><a href="#capabilities">查看全部 ${catalog.capabilities.length} 项能力 ↗</a></div><section class="grid three">${feature('control','双臂，协同在你手中','原版Xbox整机映射、逐键反馈、受限实机与独立预览。')}${feature('data','把操作，变成经验','记录状态、拖动时间轴回看、保存姿态并导出轨迹。')}${feature('simulation','先在模型里，试一试','使用独立运动学沙盒，回顾推车高度与机械臂布局研究。')}</section>
    <div class="section-heading"><h2>让下一步更清楚</h2><a href="#manual">打开图文手册 ↗</a></div><section class="grid two"><div class="card"><p class="eyebrow">A PRACTICAL PATH</p><h2>从一个小任务开始。</h2>${[['01','连上并看清','核对电机、相机与手柄状态。','devices'],['02','遥操作一遍','固定底盘和目标，感受控制与限位。','control'],['03','留下可学习的演示','先回看状态，再升级同步图像采集。','manual/record']].map(x=>`<div class="step-row"><span class="step-num">${x[0]}</span><div><h3>${x[1]}</h3><p>${x[2]}</p></div><a href="#${x[3]}">前往 ↗</a></div>`).join('')}</div><div class="card dark-card"><p class="eyebrow">YOUR ROBOT, YOUR WAY</p><h2>Windows上车，<br>远程管理工坊。</h2><p>按你的新方案，把Windows笔记本作为车载中控。通过SSH部署与查看状态，家中台式机可继续提供额外算力；迁移步骤与设备验收都放进同一份指南。</p><a class="button lime" href="#manual/mobile">查看迁移与远程指南 ↗</a><div class="sources">${guideButton('vr','探索PICO遥操作')}</div></div></section>`;
  }
  function devicePage(){return heading('DEVICES & TELEMETRY','每个设备，都有清楚的状态。','实时读取沿用现有串口所有者；不会重复打开设备。',guideButton('assembly','拼装与维护手册 ↗'))+
    `<div class="grid two"><div class="card"><p class="eyebrow">CONNECTION TOPOLOGY</p><h2>设备连接</h2><div id="device-connections"></div><div class="notice" id="device-error">读取设备状态…</div><div class="actions"><button class="button compact" id="read-limits">读取设备限位</button>${guideButton('limits')}</div></div><div class="card"><p class="eyebrow">HOLD POSITION</p><h2>扭矩与支撑</h2><p>开启时读取当前位置并写为保持目标。关闭扭矩后机械臂可能失去支撑。</p><div class="field-inline"><label class="field">范围<select id="torque-scope"><option value="all">全部电机</option><option value="single">单个电机</option></select></label><label class="field">控制板<select id="torque-port"><option>COM3</option><option>COM4</option></select></label><label class="field">ID<input id="torque-id" type="number" min="1" max="10" value="1"></label></div><label class="check"><input id="torque-confirm" type="checkbox">我已支撑机械臂，并确认所选设备与操作范围。</label><div class="actions"><button class="button primary" id="torque-on" disabled>保持当前位置</button><button class="button warn" id="torque-off" disabled>关闭扭矩</button></div><p class="subtle" id="torque-reason">接管在线时此处阻挡扭矩操作；由你结束接管后再操作。</p></div></div>
    <div class="section-heading"><h2>实测编码器</h2><span class="tag">实测值 / 非模型计划值</span></div><div class="card" id="motor-readings"></div><div class="section-heading"><h2>相机视图</h2><span class="subtle">点击后读取视频 · 物理对应需实际遮挡确认</span></div><div class="grid three">${[0,1,2].map(i=>`<div class="card"><h3>UVC / ${String(i+1).padStart(2,'0')}</h3><span class="subtle" id="cam-state-${i}">等待状态</span><div class="camera"><img id="camera-${i}" alt="第${i+1}路相机实时画面" hidden><span class="placeholder">NO STREAM</span><span class="camera-label">CAM ${i}</span></div><button class="button compact" data-camera="${i}">打开画面</button></div>`).join('')}</div>`;}
  function dataPage(){return heading('DATA STUDIO','让每次尝试，都留下经验。','录制、回看与姿态收藏，把调试过程变成可检查的资料。',guideButton('record','数据采集图解 ↗'))+
    `<section class="grid two"><div class="card"><p class="eyebrow">CAPTURE A SESSION</p><h2>记录状态轨迹</h2><form id="record-form"><label class="field">轨迹名称<input name="name" required maxlength="80" placeholder="例如：左臂伸取 / 第一次尝试"></label><div class="field-inline"><label class="field">任务描述<input name="task" maxlength="400" placeholder="这次想验证什么？"></label><label class="field">采样频率<select name="fps"><option value="2">2 Hz</option><option value="5" selected>5 Hz</option><option value="10">10 Hz</option></select></label></div><div class="actions"><button class="button primary" id="record-start">● 开始录制</button><button type="button" class="button" id="record-stop" disabled>■ 停止并保存</button></div></form><div class="record-status" id="record-status"><i class="record-dot"></i><span>等待一次新的尝试</span></div><p class="subtle" style="margin-top:14px">包含计划姿态、实测编码器、手柄输入与来源。上限30分钟或10000帧。</p></div><div class="card"><p class="eyebrow">POSE BOOKMARKS</p><h2>保存当前姿态</h2><form id="pose-form"><label class="field">姿态名称<input name="name" required maxlength="80" placeholder="例如：桌面上方 / 预备位"></label><label class="field">来源<select name="source"><option value="runtime">当前控制服务</option><option value="sandbox">独立沙盒</option></select></label><label class="field">备注<input name="note" maxlength="500" placeholder="保留你的观察与用途"></label><button class="button">＋ 保存姿态快照</button></form><p class="subtle" style="margin-top:16px">当前可收藏、查看和载入沙盒。快照不能直接执行实机复位。</p></div></section><div class="notice amber">状态轨迹没有同步相机图像，不能直接当作LeRobot训练数据。回看只更新可视化，不控制实机。</div>
    <div class="section-heading"><h2>轨迹回看</h2><span class="tag">VISUAL REPLAY ONLY</span></div><div class="card"><div id="trajectory-viewer">${empty('选择一份轨迹','保存后点击下方“回看”，或先到仿真实验室体验演示。')}</div><div id="player-controls" hidden><input class="timeline" id="timeline" aria-label="轨迹时间轴" type="range" min="0" max="1" step="1" value="0"><div class="actions"><button class="button compact" id="play-toggle">▶ 播放回看</button><span class="subtle" id="player-time"></span></div><div class="metric-line" id="frame-metrics"></div></div></div><div class="section-heading"><h2>本地资料库</h2><button class="button compact" id="library-refresh">刷新资料</button></div><div class="grid two"><div class="card"><h3>已录制轨迹</h3><div id="recordings-list"></div></div><div class="card"><h3>姿态收藏</h3><div id="poses-list"></div></div></div>`;}
  function simulationPage(){return heading('SIMULATION LAB','先尝试，再让它成为现实。','独立运动学沙盒复用原版控制内核；结构仿真研究保留原始图表与证据。',guideButton('simulation'))+
    `<div class="grid two"><div class="card"><div class="actions" style="justify-content:space-between"><h2>双臂沙盒</h2><span class="tag integrated">独立预览</span></div><div id="sandbox-view" class="viewer"></div><div class="metric-line" id="sandbox-metrics"></div><p class="scope-caption">KINEMATICS ONLY / 不含动力学与碰撞</p><div class="actions"><button class="button primary compact" data-demo="reach">▶ 双臂伸取演示</button><button class="button compact" data-demo="drive">▶ 底盘驾驶演示</button><button class="button compact" id="sandbox-reset">重置沙盒</button></div><div id="demo-progress" class="subtle" style="margin-top:12px"></div></div><div class="card"><p class="eyebrow">EXPLORE THE MAPPING</p><h2>按住，感受映射。</h2><p>这些按钮只改变独立沙盒，不接管真实控制器。先选择手臂和模式，再按住方向按钮。</p><div class="field-inline"><label class="field">手臂<select id="sandbox-side"><option value="left">左臂</option><option value="right">右臂</option><option value="both">双臂</option></select></label><label class="field">模式<select id="sandbox-mode"><option value="position">默认 · 伸缩与高度</option><option value="pan">LS/RS · 肩水平</option><option value="wrist">LB/RB · 腕姿</option></select></label></div><div class="sandbox-buttons">${[['extend','前推杆 ↑'],['retract','后拉杆 ↓'],['up','右拨杆 →'],['down','左拨杆 ←'],['grip','压下扳机']].map(x=>`<button class="button" data-hold="${x[0]}">${x[1]}</button>`).join('')}</div><div class="notice">默认：前后拨杆改变伸缩，左右拨杆改变平面高度。肩水平模式只响应左右，腕姿模式调整俯仰与旋转。</div><div id="sandbox-blockers" class="subtle"></div><div class="actions"><button class="button compact" id="sandbox-save">＋ 收藏沙盒姿态</button>${guideButton('xbox','查看完整映射')}</div></div></div><div class="section-heading"><h2>推车与机械臂布局研究</h2><span class="subtle">本地MuJoCo研究产物 · 点击查看大图</span></div><div class="grid three">${[['two-layer','两层推车对比','比较安装高度与底层取物范围。'],['schemes','候选结构方案','低位、前伸、升降等布局的可达性筛选。'],['scores','方案评分','简化场景下的结构研究指标。']].map(([id,t,p])=>`<div class="card"><h3>${t}</h3>${catalog.media[id]?`<img class="research-image" data-zoom="${id}" src="${catalog.media[id]}" alt="${t}">`:empty('研究图片未在本机','请从仓库源文档查看原始结论。')}<p class="subtle">${p}</p>${guideButton('fabrication','阅读结构改造说明')}</div>`).join('')}</div><div class="notice amber">现有MuJoCo图表是简化碰撞与可达性研究，不代表夹持、倾覆稳定性或当前双轮整机已验收。ManiSkill、VR与强化学习入口见手册。</div>`;}
  function visionPage(){return heading('PERCEPTION LAB','先看得清，再理解目标。','检查输入画面质量，逐步接入检测、分割与辅助对准。',guideButton('vision'))+
    `<div class="grid two"><div class="card"><p class="eyebrow">IMAGE CHECK</p><h2>单帧图像检查</h2><p>选择本地图片，或读取已连接相机的当前帧。图像在本机内存分析，不保存到资料库。</p><label class="file-pick"><input id="quality-file" type="file" accept="image/*" aria-label="选择要分析的图片"></label><div class="actions" style="margin-top:18px"><select class="button compact" id="quality-camera"><option value="0">相机0</option><option value="1">相机1</option><option value="2">相机2</option></select><button class="button compact" id="quality-capture">读取一帧</button><button class="button primary compact" id="quality-analyze" disabled>检查画面质量</button></div><img id="quality-preview" class="quality-preview" alt="本次分析图像" hidden><div id="quality-result">${empty('等待一张画面','分析分辨率、亮度、高频细节和暗部 / 高亮比例。')}</div></div><div class="card"><p class="eyebrow">FROM PIXELS TO ACTIONS</p><h2>接下来，让它识别目标。</h2><div class="diagram">${StudioVisuals.diagram('vision')}</div>${[['检测','知道画面中有哪些物体。'],['分割','得到目标形状与可选抓取区域。'],['对准与抓取','结合手眼标定、三维定位与可达性。']].map((x,i)=>`<div class="step-row"><span class="step-num">0${i+1}</span><div><h3>${x[0]}</h3><p>${x[1]}</p></div>${badge('adaptation')}</div>`).join('')}<div class="notice amber">原YOLO示例会直接连接并可能驱动手臂；本版提供源码与适配步骤。</div>${sourceButtons(catalog.capabilities.find(c=>c.id==='yolo')?.sources||[])}</div></div>`;}
  function learningPage(){return heading('LEARNING LAB','让演示，变成下一种技能。','先固定一个小任务，再用ACT或SmolVLA建立可重复的实验。',guideButton('act','从ACT开始 ↗'))+
    `<div class="grid two"><div class="card"><p class="eyebrow">EXPERIMENT BUILDER</p><h2>创建实验配置</h2><form id="experiment-form"><label class="field">实验名称<input name="name" required maxlength="80" placeholder="例如：玩具入篮 / ACT基线"></label><div class="field-inline"><label class="field">策略<select name="policy"><option value="act">ACT · 先学一个动作</option><option value="smolvla">SmolVLA · 视觉语言动作</option></select></label><label class="field">训练步数<input name="steps" type="number" min="100" max="1000000" value="20000" required></label><label class="field">Batch size<input name="batch_size" type="number" min="1" max="128" value="4" required></label></div><label class="field">原生LeRobot数据集<input name="dataset" required pattern="[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+" placeholder="owner/dataset_name"><small>需要包含对齐的图像、状态与动作；状态轨迹不是训练数据集。</small></label><label class="field">任务描述<textarea name="task" maxlength="400" placeholder="固定工位、目标、成功条件…"></textarea></label><button class="button primary">＋ 保存配置并生成命令</button></form><div id="experiment-output"></div></div><div class="card"><p class="eyebrow">ENVIRONMENT READINESS</p><h2>本机环境检查</h2><p>读取各虚拟环境的软件包版本。安装版本存在不等于GPU、依赖和任务已验收。</p><div id="environment-list" class="env-list"></div><div class="notice amber">本页生成配置，不启动训练或下载模型。训练命令要在匹配版本的LeRobot环境运行；8GB级显存需实际测试。</div><div class="actions">${guideButton('act','ACT流程')}${guideButton('smolvla','SmolVLA与pi0.5')}${guideButton('agent','语言技能编排')}</div></div></div><div class="section-heading"><h2>我的实验配置</h2><span class="tag">CONFIGURATION / NOT EXECUTED</span></div><div class="card" id="experiments-list"></div><div class="section-heading"><h2>学习路径</h2></div><div class="grid three">${feature('data','01 · 成功演示','先建立同步采集链路、成功标准与独立评估集。','manual/record')}${feature('learning','02 · 一个技能','从固定相机、固定底盘的单臂ACT基线开始。','manual/act')}${feature('vision','03 · 更多表达','研究SmolVLA、异步推理和已验证技能的语言编排。','manual/smolvla')}</div>`;}
  function manualPage(id){const g=catalog.guides.find(x=>x.id===id)||catalog.guides[0];return heading('FIELD MANUAL','把复杂的事，讲清楚。','本机实践、上游能力与后续适配，各有依据。')+`<div class="manual-layout"><nav class="manual-nav" aria-label="手册目录">${catalog.guides.map((x,i)=>`<a class="${x.id===g.id?'active':''}" href="#manual/${x.id}"><span>${String(i+1).padStart(2,'0')}</span>${x.title}</a>`).join('')}</nav><article class="manual-article"><p class="eyebrow">${g.kicker}</p><h1>${g.title}</h1><p>${g.summary}</p><div class="diagram">${StudioVisuals.diagram(g.diagram)}</div>${g.steps.map((s,i)=>`<section class="manual-step"><span class="step-num">0${i+1}</span><div><h3>${s[0]}</h3><p>${s[1]}</p></div></section>`).join('')}${g.image&&catalog.media[g.image]?`<figure class="manual-photo"><img src="${catalog.media[g.image]}" alt="${g.title}的仓库参考图片" loading="lazy"><figcaption>仓库收录的项目或社区示例图片，非本机功能验收结果。</figcaption></figure>`:''}<div class="notice amber">${g.limits}</div><div class="section-heading"><h3>从原文继续阅读</h3><span class="subtle">本地检出版本</span></div>${sourceButtons(g.sources)}<div class="actions" style="margin-top:30px"><a class="button" href="#${({xbox:'control',record:'data',vision:'vision',simulation:'simulation',act:'learning',smolvla:'learning',assembly:'devices',limits:'devices'})[g.id]||'capabilities'}">进入相关工作区 ↗</a></div></article></div>`;}
  function capabilitiesPage(){return heading('CAPABILITY ATLAS','能力全景，每一步都有来处。',`${catalog.capabilities.length}项能力 · ${catalog.guides.length}篇手册 · ${catalog.sources.length}份源文件。当前本地版本，按实际集成程度标注。`)+`<div class="filter-row" id="state-filters"><label>集成程度</label>${[['all','全部'],...Object.entries(labels)].map(([s,t])=>`<button class="filter ${capState===s?'active':''}" data-state-filter="${s}">${t}</button>`).join('')}</div><div class="filter-row" id="group-filters"><label>能力领域</label>${[['all','全部'],...Object.entries(groups)].map(([s,t])=>`<button class="filter ${capGroup===s?'active':''}" data-group-filter="${s}">${t}</button>`).join('')}</div><div class="grid three" id="capability-grid">${capCards()}</div><div class="section-heading"><h2>仓库源文件索引</h2><span class="subtle">只读 · 不执行脚本</span></div><div class="card"><input class="text-input" id="source-filter" placeholder="筛选源文件路径，例如 xbox、VR、assemble…" aria-label="筛选源文件"><div class="source-index" id="source-index">${sourceIndex('')}</div></div>`;}
  function capCards(){const caps=catalog.capabilities.filter(c=>(capState==='all'||c.state===capState)&&(capGroup==='all'||c.group===capGroup));return caps.map(c=>`<article class="card cap-card">${badge(c.state)}<h3>${c.title}</h3><p>${c.summary}</p><span class="subtle">${groups[c.group]||esc(c.group)}</span><div class="actions"><a class="button compact ${c.state==='integrated'?'primary':''}" href="#${c.route==='manual'?'manual/'+c.guide:c.route}">${c.state==='integrated'?'进入工具 ↗':'查看路线 ↗'}</a>${guideButton(c.guide,'使用说明')}</div>${sourceButtons(c.sources)}</article>`).join('')||empty('没有符合条件的能力','调整筛选条件继续浏览。');}
  function sourceIndex(query){return catalog.sources.filter(s=>s.path.toLowerCase().includes(query.toLowerCase())).map(s=>`<button class="source-link" data-source="${s.id}">${esc(s.path)} ↗</button>`).join('')||empty('未找到源文件','试试其他关键字。');}
  async function navigate(){
    const version=++routeVersion;stopHold();cancelDemo();playing=false;const parts=location.hash.slice(1).split('/');route=pages.some(x=>x[0]===parts[0])?parts[0]:'home';
    $('current-page').textContent=pages.find(x=>x[0]===route)[1];document.title=`${$('current-page').textContent} · XLeRobot Studio`;
    $('navigation').querySelectorAll('a').forEach(a=>{const active=a.hash==='#'+route;a.classList.toggle('active',active);if(active)a.setAttribute('aria-current','page');else a.removeAttribute('aria-current');});
    $('sidebar').classList.remove('open');$('control-page').hidden=route!=='control';$('page').hidden=route==='control';window.scrollTo({top:0});
    if(route==='control'){mountRuntime('original');return;}
    const renderers={home,devices:devicePage,data:dataPage,simulation:simulationPage,vision:visionPage,learning:learningPage,manual:()=>manualPage(parts[1]),capabilities:capabilitiesPage};
    $('page').innerHTML=renderers[route]();updateStatusUI();
    try{
      if(['data','learning'].includes(route)){library=await api('/api/platform/library');if(version!==routeVersion)return;renderLibrary();}
      if(route==='simulation'){sandbox=await api('/api/platform/sandbox');if(version!==routeVersion)return;renderSandbox();}
    }catch(e){toast(e.message,true);}
  }
  function mountRuntime(name){
    const id='runtime-'+name;let frame=$(id);
    if(!frame){
      frame=document.createElement('iframe');frame.id=id;frame.title=name==='original'?'原版Xbox整机遥操作':'单关节诊断控制';frame.src='/runtime/'+name;frame.className='runtime-frame';
      frame.addEventListener('load',()=>{
        const doc=frame.contentDocument;if(!doc?.body)return;
        frame.studioResize=()=>{if(!frame.hidden&&!$('control-page').hidden)frame.style.height=Math.ceil(doc.querySelector('.desk').getBoundingClientRect().height+4)+'px';};
        new ResizeObserver(frame.studioResize).observe(doc.body);frame.studioResize();
      });
      $('control-frames').append(frame);
    }
    $('control-frames').querySelectorAll('iframe').forEach(f=>f.hidden=f!==frame);
    $('control-tabs').querySelectorAll('button').forEach(b=>b.classList.toggle('active',b.dataset.runtime===name));
    frame.studioResize?.();
  }
  function sessionInfo(s){const c=s?.runtime?.controller||{};if(!s?.upstream_connected)return {text:'控制服务离线',detail:'保留本地手册与独立沙盒；控制状态暂不可确认。',warn:true};if(c.controller==='online')return {text:c.paused?'接管保留 · 已暂停':'接管在线 · 动作可用',detail:c.paused?c.pause_reason||'等待你恢复动作':'由你主动结束；触限与故障提示见控制中心。',warn:!!c.paused};return {text:'等待开启接管',detail:'选择目标、检查条件后由你开启；浏览工作台不会自动接管。',warn:false};}
  function updateStatusUI(){
    if(!status)return;const r=status.runtime||{},c=r.controller||{},info=sessionInfo(status),motors=(r.ports||[]).reduce((a,p)=>a+(p.motors?.length||0),0);
    $('connection-dot').classList.toggle('online',!!status.upstream_connected);$('connection-text').textContent=status.upstream_connected?'服务已连接':'服务未连接';
    const strip=$('session-strip');strip.classList.toggle('warn',info.warn);strip.innerHTML=`<strong>${esc(info.text)}</strong><span>${esc(info.detail)}</span><span class="tag">${r.control_profile==='original_preview'?'运动学预览':r.control_profile==='original_joint'?'关节实机 · 起点±2.8°':r.control_profile==='original_hardware'?'末端实机':r.control_profile==='diagnostic'?'单关节调试 · LB＋方向键':'未选择目标'}</span>`;
    $('global-pause').disabled=!status.upstream_connected||c.controller!=='online'||c.paused;
    if(route==='home'){$('stat-service').textContent=status.upstream_connected?'已连接':'离线';$('stat-session').textContent=info.text;$('stat-motors').textContent=motors;$('stat-assets').textContent=Object.values(status.counts||{}).reduce((a,b)=>a+b,0);}
    if(route==='devices')renderDevices(r);
    if(route==='data'){
      const rec=status.recording;$('record-start').disabled=!!rec||!status.upstream_connected;$('record-stop').disabled=!rec;
      $('record-status').classList.toggle('live',!!rec);$('record-status').innerHTML=`<i class="record-dot"></i><span>${rec?`${esc(rec.name)} · ${rec.frame_count}帧 · ${fmt(rec.duration_s)}秒 · ${rec.errors}次读取失败`:esc(status.recording_error||'等待一次新的尝试')}</span>`;
    }
    if(route==='learning')$('environment-list').innerHTML=(status.environment||[]).map(e=>`<div class="env-row"><strong>${esc(e.name)}</strong> <span>${e.present?'环境存在':'未找到'}</span><div>${Object.entries(e.packages).filter(([,v])=>v).map(([k,v])=>`<span>${esc(k)} ${esc(v)}</span>`).join('')||'没有可索引的软件包'}</div></div>`).join('');
  }
  function deviceConnections(r,serviceConnected=true){
    const rows=[['COM3','左臂 1–6 / 头部 7、8'],['COM4','右臂 1–6 / 双轮 9、10']].map(([role,detail])=>{
      const port=(r.ports||[]).find(p=>p.port===role),count=port?.motors?.length||0;
      const connected=serviceConnected&&!r.preview_only&&port?.connected!==false&&count>0;
      return {title:'逻辑'+role+' / '+(port?.physical_port?'实际'+port.physical_port:'实际端口待识别'),detail,connected,
        state:!serviceConnected?'服务离线':r.preview_only?'隔离预览':connected?count+'个电机':r.error||port?.error?'读取失败':'等待读取'};
    });
    const pad=serviceConnected&&!!r.gamepad?.connected;
    return [...rows,{title:'Xbox',detail:'Windows XInput 原生输入',connected:pad,state:pad?'在线':'未连接'}];
  }
  function renderDevices(r){
    const ports=r.ports||[],online=r.controller?.controller==='online';
    $('device-connections').innerHTML=deviceConnections(r,status.upstream_connected).map(row=>`<div class="device-line"><i class="status-dot ${row.connected?'good':''}"></i><div><strong>${esc(row.title)}</strong><small>${esc(row.detail)}</small></div><span class="tag">${esc(row.state)}</span></div>`).join('');
    $('device-error').textContent=r.error||(r.preview_only?'当前为隔离预览，尚未读取控制板和电机；需切换为设备监控。手柄与相机状态单独显示。':ports.length?'已读取设备状态；监控不会自动开启扭矩或接管。':'等待设备状态；请检查供电、USB和设备身份。');
    $('torque-reason').textContent=online?'接管仍在线：请由你在控制中心结束接管后再操作扭矩。':'扭矩操作需设备在线并确认人工支撑，结果以寄存器回读为准。';
    const allowed=!r.preview_only&&!online&&ports.length>0&&$('torque-confirm').checked&&status.upstream_connected;$('torque-on').disabled=!allowed;$('torque-off').disabled=!allowed;
    $('read-limits').disabled=r.preview_only||online||!ports.length;
    $('motor-readings').innerHTML=ports.length?ports.map(p=>`<h3>${esc(p.port)} <span class="subtle">${esc(p.identity||'')}</span></h3><div class="table-wrap"><table class="motor-table"><thead><tr><th>ID</th><th>当前位置</th><th>目标</th><th>设备范围</th><th>扭矩</th><th>模式</th><th>温度 / 电压</th></tr></thead><tbody>${p.motors.map(m=>`<tr><td class="mono">${m.id}</td><td>${esc(m.position)}</td><td>${esc(m.goal??'—')}</td><td>${esc(m.min)} – ${esc(m.max)}</td><td>${m.torque?'开启':'关闭'}</td><td>${esc(m.mode)}</td><td>${esc(m.temperature??'—')}° / ${esc(m.voltage??'—')}V</td></tr>`).join('')}</tbody></table></div>`).join(''):r.preview_only?empty('隔离预览未读取电机','设备监控会读取位置、电压、温度和限位，启动时不会自动接管。'):empty('当前没有实测电机','检查供电、USB和设备身份。');
    for(let i=0;i<3;i++)$('cam-state-'+i).textContent=r.cameras?.find(c=>c.camera===i)?.state||'设备状态未知';
  }
  function renderLibrary(){
    if(route==='data'){
      $('recordings-list').innerHTML=library.recordings.map(x=>`<div class="library-item"><span class="item-icon">REC</span><div><strong>${esc(x.name)}</strong><p>${date(x.created_at)} · ${x.frame_count}帧 · ${fmt(x.duration_s)}秒 · ${x.status==='recording'?'录制中':x.status==='interrupted'?'异常中断，已有帧可读':'已保存'}</p><p>${esc((x.source_profiles||[]).join(' / '))} · 读取失败 ${x.errors||0}次</p></div><div class="actions"><button class="button compact" data-recording="${x.id}">回看</button><a class="button compact" href="/api/platform/recordings/${x.id}/export">导出</a></div></div>`).join('')||empty('还没有轨迹','为这次尝试起个名字，开始第一份记录。');
      $('poses-list').innerHTML=library.poses.map(x=>`<div class="library-item"><span class="item-icon">POS</span><div><strong>${esc(x.name)}</strong><p>${date(x.created_at)} · ${esc(x.profile)}</p><p>${esc(x.note)}</p></div><div class="actions"><button class="button compact" data-pose-view="${x.id}">查看</button><button class="button compact" data-pose-load="${x.id}" ${!['sandbox','original_preview'].includes(x.profile)?'disabled':''}>载入沙盒</button></div></div>`).join('')||empty('收藏一个熟悉的姿态','只保存当前快照，实机不会因此动作。');
    }
    if(route==='learning')$('experiments-list').innerHTML=library.experiments.map(x=>`<div class="library-item"><span class="item-icon">${esc(x.policy.toUpperCase())}</span><div><strong>${esc(x.name)}</strong><p>${esc(x.dataset)} · ${x.steps}步 · ${date(x.created_at)}</p></div><div class="actions"><span class="tag">配置已保存</span><button class="button compact" data-experiment="${x.id}">查看命令</button></div></div>`).join('')||empty('保存第一个实验配置','配置存储在本机，命令由你确认后执行。');
  }
  function viewer(state){return `<div class="viewer">${StudioVisuals.robot(state)}<div class="viewer-meta"><span>计划姿态 / 二连杆平面</span><span>底盘 ${(state?.base_pose||[0,0,0]).map(x=>fmt(x)).join(' / ')} · 轮速 ${(state?.wheel_raw||[0,0]).join(' / ')}</span></div></div>`;}
  function renderFrame(){
    if(!player?.frames?.length||route!=='data')return;playerFrame=Math.max(0,Math.min(playerFrame,player.frames.length-1));const f=player.frames[playerFrame];
    $('trajectory-viewer').innerHTML=viewer(f.original);$('timeline').value=playerFrame;$('player-time').textContent=`${fmt(f.t)} / ${fmt(player.meta.duration_s)}s · 第${playerFrame+1} / ${player.frames.length}帧`;
    const readbacks=(f.ports||[]).flatMap(p=>p.motors||[]);$('frame-metrics').innerHTML=`<span>来源<strong>${esc(f.profile)}</strong></span><span>实测电机<strong>${readbacks.length}</strong></span><span>控制状态<strong>${f.controller?.paused?'暂停':f.controller?.controller||'演示'}</strong></span><span>输入延迟<strong>${esc(f.input_age_ms??'—')}ms</strong></span>`;
  }
  function animate(now){if(!playing||route!=='data'||!player)return;const t=playOrigin+(now-playStart)/1000;while(playerFrame<player.frames.length-1&&player.frames[playerFrame+1].t<=t)playerFrame++;renderFrame();if(playerFrame>=player.frames.length-1){playing=false;$('play-toggle').textContent='▶ 重新回看';}else requestAnimationFrame(animate);}
  function renderSandbox(){if(route!=='simulation'||!sandbox)return;$('sandbox-view').innerHTML=StudioVisuals.robot(sandbox);$('sandbox-metrics').innerHTML=`<span>头部<strong>${sandbox.head_deg.map(fmt).join(' / ')}°</strong></span><span>轮速 raw<strong>${sandbox.wheel_raw.join(' / ')}</strong></span><span>底盘 x / y / θ<strong>${sandbox.base_pose.map(fmt).join(' / ')}</strong></span>`;$('sandbox-blockers').textContent=sandbox.blockers.join('；')||'沙盒就绪；不会向真实控制器发送输入。';}
  function stopHold(){hold=null;document.querySelectorAll('[data-hold]').forEach(b=>b.classList.remove('pressed'));}
  function sandboxInput(key){
    const axes=[0,0,0,0,0,0],buttons=[],sides=$('sandbox-side').value==='both'?['left','right']:[$('sandbox-side').value],mode=$('sandbox-mode').value;
    for(const s of sides){const o=s==='left'?0:2;if(key==='extend')axes[o+1]=-.8;if(key==='retract')axes[o+1]=.8;if(key==='up')axes[o]=.8;if(key==='down')axes[o]=-.8;if(key==='grip')axes[s==='left'?4:5]=1;if(mode==='wrist')buttons.push(s==='left'?4:5);if(mode==='pan')buttons.push(s==='left'?8:9);}
    return {axes,buttons,hat:[0,0],dt:.08};
  }
  async function holdTick(){if(!hold||holdBusy||route!=='simulation')return;holdBusy=true;try{const next=await api('/api/platform/sandbox/step',sandboxInput(hold));if(route==='simulation'){sandbox=next;renderSandbox();}}catch(e){stopHold();toast(e.message,true);}finally{holdBusy=false;}}
  let demoToken=0;
  function cancelDemo(){demoToken++;if($('sandbox-save'))$('sandbox-save').disabled=false;}
  async function demo(id){
    stopHold();const token=++demoToken,version=routeVersion;let pack;
    try{pack=await api('/api/platform/sandbox/demo/'+id);}catch(e){cancelDemo();throw e;}
    if(token!==demoToken||version!==routeVersion)return;$('sandbox-save').disabled=true;
    let i=0;const start=performance.now();
    async function frame(now){
      if(token!==demoToken||version!==routeVersion||route!=='simulation')return;
      while(i<pack.frames.length-1&&pack.frames[i+1].t<=(now-start)/1000)i++;
      sandbox=pack.frames[i].original;renderSandbox();$('demo-progress').textContent=`${pack.meta.name} · ${fmt(pack.frames[i].t)} / ${fmt(pack.meta.duration_s)}s · 演示只在页面播放，未修改沙盒或接管状态`;
      if(i<pack.frames.length-1)requestAnimationFrame(frame);
      else{try{const original=await api('/api/platform/sandbox');if(token!==demoToken||version!==routeVersion)return;sandbox=original;renderSandbox();$('demo-progress').textContent='演示完成，已返回你原来的沙盒姿态。';}catch(e){toast(e.message,true);}finally{if(token===demoToken)cancelDemo();}}
    }
    requestAnimationFrame(frame);
  }
  function markdown(raw){
    // Escape first. Raw HTML and executable URLs are never rendered.
    let inCode=false,out='';
    const inline=s=>esc(s).replace(/`([^`]+)`/g,'<code>$1</code>').replace(/\*\*([^*]+)\*\*/g,'<strong>$1</strong>').replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g,'<a href="$2" target="_blank" rel="noopener noreferrer">$1 ↗</a>');
    for(const row of raw.split('\n')){if(/^\s*```/.test(row)){out+=inCode?'</code></pre>':'<pre><code>';inCode=!inCode;continue;}if(inCode){out+=esc(row)+'\n';continue;}const h=row.match(/^(#{1,5})\s+(.+)/);if(h){out+=`<h${h[1].length}>${inline(h[2])}</h${h[1].length}>`;continue;}if(/^\s*[-*]\s+/.test(row)){out+=`<p>• ${inline(row.replace(/^\s*[-*]\s+/,''))}</p>`;continue;}if(row.trim())out+=`<p>${inline(row)}</p>`;}return out+(inCode?'</code></pre>':'');
  }
  async function openSource(id){$('source-dialog').showModal();$('source-title').textContent='正在读取…';$('source-content').textContent='';$('source-path').textContent='';try{const s=await api('/api/platform/sources/'+id);sourceRaw=s.content;$('source-title').textContent=s.path.split('/').pop();$('source-path').textContent=s.path;$('source-kind').textContent=s.kind==='md'?'REPOSITORY MANUAL':'SOURCE CODE / READ ONLY';$('source-content').innerHTML=s.kind==='md'?markdown(s.content):`<pre><code>${esc(s.content)}</code></pre>`;}catch(e){$('source-content').textContent=e.message;}}
  function search(q){q=q.trim().toLowerCase();const results=[...catalog.capabilities.filter(c=>(c.title+c.summary).toLowerCase().includes(q)).map(c=>({title:c.title,desc:labels[c.state]+' · '+c.summary,href:c.route==='manual'?'manual/'+c.guide:c.route})),...catalog.guides.filter(g=>(g.title+g.summary).toLowerCase().includes(q)).map(g=>({title:g.title,desc:'图文手册 · '+g.kicker,href:'manual/'+g.id})),...catalog.sources.filter(s=>s.path.toLowerCase().includes(q)).map(s=>({title:s.name,desc:s.path,source:s.id}))].slice(0,40);$('search-results').innerHTML=results.map(x=>`<button class="search-result" ${x.source?`data-search-source="${x.source}"`:`data-search-route="${x.href}"`}><strong>${esc(x.title)}</strong><small>${esc(x.desc)}</small></button>`).join('')||empty('未找到匹配内容','试试手柄、数据、训练、PICO或源文件名。');}
  async function refresh(){if(pollBusy)return;pollBusy=true;try{status=await api('/api/platform/status');lastControlStatus=Date.now();updateStatusUI();}catch(e){if(status)status.upstream_connected=false;$('connection-text').textContent='平台连接中断';$('session-strip').textContent='平台暂未响应，控制器状态未知；不会自动取消你的接管。';$('global-pause').disabled=true;}finally{pollBusy=false;}}
  function experimentOutput(x){return `<div class="notice">${esc(x.note)}</div><h3>${esc(x.name)} <span class="tag">未执行</span></h3><pre class="code">${esc(x.command)}</pre><button class="button compact" data-copy-experiment="${x.id}">复制命令</button>`;}
  async function setQuality(blob){if(blob.size>8000000)throw new Error('图片需小于8MB');qualityBlob=blob;const img=$('quality-preview');if(img.dataset.objectUrl)URL.revokeObjectURL(img.dataset.objectUrl);const url=URL.createObjectURL(blob);img.dataset.objectUrl=url;img.src=url;img.hidden=false;$('quality-analyze').disabled=false;$('quality-result').innerHTML='<p class="subtle">画面已就绪，点击检查画面质量。</p>';}
  async function clicked(e){
    const b=e.target.closest('button,a,[data-zoom]');if(!b)return;
    if(b.dataset.guide){location.hash='manual/'+b.dataset.guide;return;}
    if(b.dataset.source){await openSource(b.dataset.source);return;}
    if(b.dataset.close){$(b.dataset.close).close();return;}
    if(b.dataset.runtime){mountRuntime(b.dataset.runtime);return;}
    if(b.dataset.zoom){$('large-image').src=catalog.media[b.dataset.zoom];$('image-dialog').showModal();return;}
    if(b.dataset.stateFilter){capState=b.dataset.stateFilter;$('capability-grid').innerHTML=capCards();$('state-filters').querySelectorAll('button').forEach(x=>x.classList.toggle('active',x===b));return;}
    if(b.dataset.groupFilter){capGroup=b.dataset.groupFilter;$('capability-grid').innerHTML=capCards();$('group-filters').querySelectorAll('button').forEach(x=>x.classList.toggle('active',x===b));return;}
    if(b.dataset.searchRoute){$('search-dialog').close();location.hash=b.dataset.searchRoute;return;}
    if(b.dataset.searchSource){$('search-dialog').close();await openSource(b.dataset.searchSource);return;}
    if(b.dataset.camera!==undefined){const img=$('camera-'+b.dataset.camera);if(img.hidden){img.src='/camera/'+b.dataset.camera+'.mjpg';img.hidden=false;b.textContent='关闭画面';img.onerror=()=>{img.hidden=true;img.removeAttribute('src');b.textContent='重新连接';toast('相机流不可用，请检查设备连接',true);};}else{img.hidden=true;img.removeAttribute('src');b.textContent='打开画面';}return;}
    if(b.dataset.recording)await action(b,async()=>{const version=routeVersion,pack=await api('/api/platform/recordings/'+b.dataset.recording);if(version!==routeVersion)return;playing=false;player=pack;playerFrame=0;if(!pack.frames.length){$('trajectory-viewer').innerHTML=empty('这份记录暂无有效帧','可能在第一次读取前已停止，或控制服务始终离线。');$('player-controls').hidden=true;return;}$('player-controls').hidden=false;$('play-toggle').textContent='▶ 播放回看';$('timeline').max=pack.frames.length-1;renderFrame();$('trajectory-viewer').scrollIntoView({block:'center',behavior:'smooth'});});
    if(b.dataset.poseLoad)await action(b,async()=>{await api('/api/platform/sandbox/pose/'+b.dataset.poseLoad,{});location.hash='simulation';toast('姿态已载入独立沙盒');});
    if(b.dataset.poseView){const x=library.poses.find(x=>x.id===b.dataset.poseView);$('source-title').textContent=x.name;$('source-path').textContent='本地姿态快照 · 不执行实机动作';sourceRaw=JSON.stringify(x,null,2);$('source-content').innerHTML=`<pre>${esc(sourceRaw)}</pre>`;$('source-dialog').showModal();}
    if(b.dataset.experiment){const x=library.experiments.find(x=>x.id===b.dataset.experiment);$('experiment-output').innerHTML=experimentOutput(x);$('experiment-output').scrollIntoView({block:'center',behavior:'smooth'});}
    if(b.dataset.copyExperiment){await navigator.clipboard.writeText(library.experiments.find(x=>x.id===b.dataset.copyExperiment).command);toast('命令已复制；尚未执行');}
    if(b.dataset.demo)await action(b,()=>demo(b.dataset.demo));
    const id=b.id;
    if(b.classList.contains('skip')){e.preventDefault();$('workspace').focus();return;}
    if(id==='search-open'){$('search-dialog').showModal();$('search-input').focus();search('');}
    if(id==='menu-toggle')$('sidebar').classList.toggle('open');
    if(id==='source-copy')await action(b,async()=>{await navigator.clipboard.writeText(sourceRaw);toast('已复制原文');});
    if(id==='global-pause')await action(b,async()=>{await api('/api/original/pause',{});await refresh();toast('已暂停动作，接管保留');});
    if(id==='read-limits')await action(b,async()=>{await api('/api/calibration/read',{});await refresh();toast('设备寄存器快照已读取；未扩大行程');});
    if(id==='torque-on'||id==='torque-off')await action(b,async()=>{await api('/api/torque',{enabled:id==='torque-on',scope:$('torque-scope').value,port:$('torque-port').value,id:Number($('torque-id').value)});await refresh();toast('扭矩指令已完成，请核对实测回读');});
    if(id==='record-stop')await action(b,async()=>{await api('/api/platform/recordings/stop',{});library=await api('/api/platform/library');renderLibrary();await refresh();toast('状态轨迹已保存');});
    if(id==='library-refresh')await action(b,async()=>{library=await api('/api/platform/library');renderLibrary();});
    if(id==='play-toggle'){if(!player)return;playing=!playing;if(playing){if(playerFrame>=player.frames.length-1)playerFrame=0;playOrigin=player.frames[playerFrame].t;playStart=performance.now();requestAnimationFrame(animate);}b.textContent=playing?'Ⅱ 暂停回看':'▶ 播放回看';}
    if(id==='sandbox-reset')await action(b,async()=>{cancelDemo();stopHold();sandbox=await api('/api/platform/sandbox/reset',{});renderSandbox();$('demo-progress').textContent='已重置独立沙盒，控制服务状态保持。';});
    if(id==='sandbox-save')await action(b,async()=>{await api('/api/platform/poses',{name:'沙盒姿态 '+new Date().toLocaleTimeString('zh-CN'),source:'sandbox'});toast('已保存，可在数据工作室的姿态收藏中查看');await refresh();});
    if(id==='quality-capture')await action(b,async()=>{const v=routeVersion,res=await fetch('/api/platform/camera/'+$('quality-camera').value+'/frame');if(!res.ok){const x=await res.json();throw new Error(x.detail);}const blob=await res.blob();if(v===routeVersion)await setQuality(blob);});
    if(id==='quality-analyze')await action(b,async()=>{if(!qualityBlob)return;const v=routeVersion,q=await api('/api/platform/vision/quality',qualityBlob,true);if(v!==routeVersion)return;$('quality-result').innerHTML=`<div class="quality-results"><div>平均亮度<strong>${q.brightness}</strong>0–255</div><div>高频细节<strong>${q.laplacian_variance}</strong>Laplacian方差</div><div>图像尺寸<strong>${q.width}</strong>× ${q.height}</div></div><div class="notice">${q.notes.map(esc).join('<br>')}</div><p class="subtle">暗部 ${fmt(q.dark_fraction*100)}% · 高亮 ${fmt(q.bright_fraction*100)}%<br>${esc(q.method)}</p>`;});
  }
  async function submitted(e){
    const form=e.target;if(!['record-form','pose-form','experiment-form'].includes(form.id))return;e.preventDefault();const data=Object.fromEntries(new FormData(form)),button=form.querySelector('button');
    await action(button,async()=>{
      if(form.id==='record-form'){data.fps=Number(data.fps);await api('/api/platform/recordings/start',data);toast('状态录制已开始，切换页面仍会继续');}
      if(form.id==='pose-form'){await api('/api/platform/poses',data);toast('姿态快照已保存，未执行运动');}
      if(form.id==='experiment-form'){data.steps=Number(data.steps);data.batch_size=Number(data.batch_size);const x=await api('/api/platform/experiments',data);if(route==='learning')$('experiment-output').innerHTML=experimentOutput(x);toast('配置已保存，未启动训练');}
      library=await api('/api/platform/library');renderLibrary();await refresh();
    });
  }
  function wire(){
    document.addEventListener('click',e=>clicked(e).catch(err=>toast(err.message,true)));document.addEventListener('submit',submitted);
    document.addEventListener('input',e=>{if(e.target.id==='search-input')search(e.target.value);if(e.target.id==='source-filter')$('source-index').innerHTML=sourceIndex(e.target.value);if(e.target.id==='timeline'){playing=false;playerFrame=Number(e.target.value);renderFrame();$('play-toggle').textContent='▶ 播放回看';}});
    document.addEventListener('change',e=>{if(e.target.id==='torque-confirm')updateStatusUI();if(e.target.id==='quality-file'&&e.target.files[0])setQuality(e.target.files[0]).catch(e=>toast(e.message,true));});
    document.addEventListener('keydown',e=>{if(e.key==='/'&&!['INPUT','TEXTAREA','SELECT'].includes(e.target.tagName)&&!document.querySelector('dialog[open]')){e.preventDefault();$('search-open').click();}});
    document.addEventListener('pointerdown',e=>{const b=e.target.closest('[data-hold]');if(!b)return;e.preventDefault();cancelDemo();hold=b.dataset.hold;b.classList.add('pressed');b.setPointerCapture?.(e.pointerId);holdTick();});
    document.addEventListener('pointerup',stopHold);document.addEventListener('pointercancel',stopHold);window.addEventListener('blur',stopHold);document.addEventListener('visibilitychange',()=>{if(document.hidden)stopHold();});
    document.addEventListener('keydown',e=>{const b=e.target.closest('[data-hold]');if(!b||![' ','Enter'].includes(e.key)||e.repeat)return;e.preventDefault();cancelDemo();hold=b.dataset.hold;b.classList.add('pressed');holdTick();});document.addEventListener('keyup',e=>{if([' ','Enter'].includes(e.key))stopHold();});
    window.addEventListener('hashchange',navigate);setInterval(holdTick,90);setInterval(refresh,1500);setInterval(controlHeartbeat,600);
  }
  async function init(){
    $('navigation').innerHTML=pages.map(([id,t,n])=>`<a href="#${id}">${StudioVisuals.icon(id)}<span>${t}</span><small>${n}</small></a>`).join('');
    try{catalog=await api('/api/platform/catalog');wire();await refresh();await navigate();}catch(e){$('page').innerHTML=`<div class="error-box">工作台加载失败：${esc(e.message)}<p>请确认8770平台服务已启动，然后刷新页面。</p></div>`;}
  }
  return {init,esc,markdown,sessionInfo,sandboxInput,deviceConnections,heartbeatAllowed};
})();
if(typeof document!=='undefined')Studio.init();
if(typeof module!=='undefined')module.exports=Studio;
