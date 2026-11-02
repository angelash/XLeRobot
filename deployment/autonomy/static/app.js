'use strict';
const $=id=>document.getElementById(id);
const API='/api/platform/autonomy';
let status=null,learning=null,group='wheel',busy=false;
const GROUPS={wheel:'轮组与底盘',arm:'手臂',camera:'相机与双目',grasp:'测距与抓取'};
const MOTION={none:'不动',none_then_base:'先静止后底盘',wheels_lifted:'轮离地空转',base:'底盘分段',arm:'手臂',head:'头部',head_or_arm:'头部或手臂'};
const STATUS={done:['完成','ok'],failed:['失败','bad'],expired:['过期','warn'],queued:['排队(工作端)','warn'],queued_local:['排队(笔记本)','warn'],assigned:['工作端执行中','warn'],running:['笔记本执行中','warn'],cancelled:['取消','warn']};
const MODEL={candidate:['候选','warn'],validated:['已验证','ok'],adopted:['已采用','ok'],retired:['已退役','bad']};
function esc(v){return String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'})[c]);}
function time(v){if(!v)return '—';const d=new Date(v);return Number.isNaN(d.valueOf())?esc(v):d.toLocaleTimeString('zh-CN',{hour12:false});}
function ms(v){return v===null||v===undefined?'—':v>=1000?(v/1000).toFixed(2)+' s':Math.round(v)+' ms';}
function notice(text){$('notice').textContent=text||'';$('notice').hidden=!text;}
async function request(path,body,timeout=15000){
  const controller=new AbortController();const timer=setTimeout(()=>controller.abort(),timeout);
  try{const r=await fetch(API+path,{cache:'no-store',signal:controller.signal,...(body?{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}:{})});
    let data;try{data=await r.json();}catch{throw Error('服务返回了无法读取的数据');}
    if(!r.ok)throw Error(data.detail||data.error||('请求失败 '+r.status));return data;}
  finally{clearTimeout(timer);}
}
function meter(percent){const p=Math.max(0,Math.min(100,Number(percent)||0));return `<meter class="meter" min="0" max="100" low="70" high="88" optimum="20" value="${p}" title="内存占用 ${p}%"></meter>`;}
function nodeCard(title,state,res,caps,extra=''){
  const mem=res?.memory||{};const gpu=(res?.gpu||[])[0];
  return `<article class="node"><h3>${esc(title)}<span class="state ${state[1]}">${esc(state[0])}</span></h3>
  <div class="kv"><span>主机</span><b>${esc(res?.host||'—')}</b><span>处理器</span><b>${esc(res?.logical_cpus??'—')} 线程 · 占用 ${res?.cpu_percent??'—'}%</b>
  <span>内存</span><b>可用 ${mem.available_mb??'—'} / ${mem.total_mb??'—'} MB</b>${gpu?`<span>显卡</span><b>${esc(gpu.name)} · 可用 ${gpu.memory_free_mb} / ${gpu.memory_total_mb} MB</b>`:''}${extra}</div>
  ${mem.load_percent!==undefined?meter(mem.load_percent):''}<div class="chips">${(caps||[]).map(c=>`<span class="chip">${esc(c)}</span>`).join('')}</div></article>`;
}
function renderStatus(){
  const s=status;const local=s.local.resources;
  const localCaps=s.capabilities.filter(c=>c.available_on.includes('local')).map(c=>c.name);
  let html=nodeCard('车载笔记本（实时层）',['在线','on'],local,localCaps,`<span>本进程</span><b>${local?.memory?.process_mb??'—'} MB · 计算线程 ${s.local.executor_threads}</b>`);
  if(!s.nodes.length)html+=`<article class="node"><h3>工作端（加速层）<span class="state off">未连接</span></h3><p class="hint">在工作端运行 python -m deployment.autonomy.worker 后出现。离线时可回退的任务自动在笔记本执行。</p></article>`;
  for(const n of s.nodes){const details=n.details?.providers||{};
    const extra=`<span>心跳</span><b>${n.heartbeat_age_s}s 前 · 执行中 ${n.inflight}/${n.max_parallel}</b><span>地址</span><b>${esc(n.address||'—')}</b>`+
      Object.entries(details).filter(([,v])=>v&&v.available===false).map(([k,v])=>`<span>${esc(k)}</span><b class="warn">不可用：${esc(v.reason)}</b>`).join('');
    html+=nodeCard(n.name||n.node_id,n.online?['在线','on']:['离线','off'],n.resources,n.capabilities,extra);}
  $('nodes').innerHTML=html;
  $('routes').textContent=Object.entries(s.routes).map(([k,v])=>k+'：'+v).join('；');
  $('capabilities').innerHTML=s.capabilities.map(c=>`<tr><td><code>${esc(c.name)}</code></td><td>${esc(c.title)}${c.note?`<br><span class="hint">${esc(c.note)}</span>`:''}</td><td>${esc(s.tiers[c.tier]||c.tier)}</td><td>${c.available_on.length?c.available_on.map(x=>esc(x==='local'?'笔记本':x)).join('、'):'<span class="bad">暂无</span>'}</td></tr>`).join('');
}
async function refreshJobs(){
  const data=await request('/jobs');
  $('jobs').innerHTML=data.jobs.length?data.jobs.map(j=>{const st=STATUS[j.status]||[j.status,''];return `<tr><td>${time(j.created_at)}</td><td><code>${esc(j.capability)}</code></td><td>${esc(j.route)}${j.fallback?`<br><span class="hint">回退：${esc(j.fallback)}</span>`:''}</td><td>${esc(j.node==='local'?'笔记本':j.node||'—')}</td><td class="${st[1]}">${esc(st[0])}${j.error?`<br><span class="hint">${esc(j.error)}</span>`:''}</td><td>${ms(j.run_ms)} / ${ms(j.total_ms)}</td></tr>`;}).join(''):'<tr><td colspan="6" class="empty">还没有计算任务。</td></tr>';
}
async function refresh(){
  try{status=await request('/status');renderStatus();await refreshJobs();$('live').textContent='平台已连接';$('live').className='connection ok';}
  catch(e){$('live').textContent='连接失败';$('live').className='connection error';notice('状态读取失败：'+(e.name==='AbortError'?'超时':e.message));}
}
function renderLearning(){
  const l=learning;
  $('groups').innerHTML=Object.entries(GROUPS).map(([k,v])=>`<button role="tab" aria-selected="${k===group}" data-group="${k}">${v}</button>`).join('');
  $('protocols').innerHTML=l.protocols.filter(p=>p.group===group).map(p=>`<article class="protocol"><h3>${esc(p.id)} · ${esc(p.title)}</h3>
    <p><strong>目的</strong> ${esc(p.purpose)}</p><p><strong>方法</strong> ${esc(p.method)}</p>
    <p><strong>动作</strong> ${esc(MOTION[p.motion]||p.motion)} · <strong>真值</strong> ${esc(p.truth.join('、'))}</p>
    <p><strong>样本</strong> ${esc(p.samples)} · <strong>门槛</strong> ${esc(p.threshold)}</p>
    ${p.prerequisites.length?`<p><strong>前提</strong> ${esc(p.prerequisites.join('；'))}</p>`:''}${p.model_kind?`<p><strong>产出模型</strong> <code>${esc(p.model_kind)}</code></p>`:''}</article>`).join('');
  $('sessions').innerHTML=l.sessions.length?l.sessions.map(s=>`<div class="item"><header><b>${esc(s.protocol)} · ${esc(s.title)}</b><span class="state ${s.status==='open'?'warn':'on'}">${esc(s.status==='open'?'进行中':s.status==='closed'?'已结束':'中断')}</span></header>
    <p>${time(s.created_at)} · 样本 ${s.samples}（含真值 ${s.with_truth}） · 运行 ${s.runs} · 照片组 ${s.photos}${s.note?' · '+esc(s.note):''}</p></div>`).join(''):'<p class="empty">明日实验开始后在这里出现。</p>';
  const models=l.models.map(m=>({...m,label:'模型 '+m.kind,metrics:Object.entries(m.metrics||{}).filter(([,v])=>typeof v==='number').slice(0,3).map(([k,v])=>k+'='+(Math.abs(v)<0.1?v.toExponential(2):v.toFixed(3))).join('，'),type:'model'}));
  const cals=l.calibrations.map(c=>({...c,label:'标定 '+c.kind+' · '+c.camera,metrics:c.metric?`${c.metric}=${c.metric_value} / 门槛 ${c.threshold}`:'',type:'calibration'}));
  const items=[...models,...cals];
  $('models').innerHTML=items.length?items.map(m=>{const st=MODEL[m.status]||[m.status,''];return `<div class="item"><header><b>${esc(m.label)}</b><span class="state ${st[1]==='ok'?'on':st[1]==='bad'?'off':'warn'}">${esc(st[0])}</span></header>
    <p>${time(m.created_at)} · ${esc(m.metrics)}</p><div class="actions">${m.status==='validated'||(m.type==='calibration'&&m.status==='candidate'&&m.meets_threshold)?`<button class="small" data-adopt="${m.type}:${m.id}">由我采用</button>`:''}${m.status!=='retired'?`<button class="small" data-retire="${m.type}:${m.id}">退役</button>`:''}</div></div>`;}).join(''):'<p class="empty">拟合或标定后在这里出现；只有验证通过、你确认采用的才会成为默认值。</p>';
  $('assets').innerHTML=l.print_assets.length?l.print_assets.map(a=>`<a href="/autonomy/print/${encodeURIComponent(a)}" target="_blank" rel="noopener">${esc(a)} ↗</a>`).join(''):'<p class="empty">尚未生成打印资产。</p>';
}
async function loadLearning(){try{learning=await request('/learning');renderLearning();}catch(e){notice('学习台账读取失败：'+e.message);}}
async function job(capability,route,payload={},deadline=60){return request('/jobs',{capability,route,payload,deadline_s:deadline,wait_s:Math.min(deadline,55),source:'autonomy-page'},(Math.min(deadline,55)+10)*1000);}
async function action(button,work){if(busy)return;busy=true;button.disabled=true;try{await work();}catch(e){notice(e.name==='AbortError'?'请求超时':e.message);}finally{busy=false;button.disabled=false;refresh();}}
$('echo').addEventListener('click',e=>action(e.currentTarget,async()=>{
  const sent=performance.now();const r=await job('system.echo','prefer_worker',{message:'ping'},10);const rtt=performance.now()-sent;
  $('action-result').hidden=false;$('action-result').innerHTML=`连通测试：${r.status==='done'?'<span class="ok">完成</span>':'<span class="bad">'+esc(r.status)+'</span>'}，执行于 <b>${esc(r.node==='local'?'笔记本':r.node)}</b>${r.fallback?'（回退：'+esc(r.fallback)+'）':''}，页面往返 ${ms(rtt)}，服务端总耗时 ${ms(r.total_ms)}。`;
}));
$('bench').addEventListener('click',e=>action(e.currentTarget,async()=>{
  $('action-result').hidden=false;$('action-result').textContent='正在两节点运行标准视觉基准……';
  const local=await job('system.benchmark','local',{repeats:3},120);
  let worker=null;try{worker=await job('system.benchmark','worker_only',{repeats:3},120);}catch(err){worker={status:'failed',error:err.message};}
  const names=Object.keys(local.result?.timings||worker.result?.timings||{});
  $('action-result').innerHTML=`<div class="table-wrap"><table><thead><tr><th>基准</th><th>笔记本</th><th>工作端</th></tr></thead><tbody>${names.map(n=>`<tr><td>${esc(n)}</td><td>${ms(local.result?.timings?.[n]?.median_ms)}</td><td>${worker.status==='done'?ms(worker.result.timings[n]?.median_ms):'<span class="bad">'+esc(worker.error||worker.status)+'</span>'}</td></tr>`).join('')}</tbody></table></div>`;
}));
document.addEventListener('click',e=>{
  const tab=e.target.closest('[data-group]');if(tab){group=tab.dataset.group;renderLearning();return;}
  const adopt=e.target.closest('[data-adopt]'),retire=e.target.closest('[data-retire]');const b=adopt||retire;if(!b)return;
  const [type,id]=(b.dataset.adopt||b.dataset.retire).split(':');const act=adopt?'adopt':'retire';
  if(!confirm(adopt?'确认由你采用这项结果作为默认值？旧的同类结果会退役。':'确认退役这项结果？'))return;
  action(b,async()=>{await request((type==='model'?'/learning/models/':'/calibrations/')+id+'/review',{action:act,reviewer:'user',note:'页面审核'});await loadLearning();});
});
refresh();loadLearning();setInterval(refresh,4000);
