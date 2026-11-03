'use strict';
const $=id=>document.getElementById(id);
const API='/api/platform/grasp';
const CONFIG=[['desk_z','桌面高度',0.5],['approach_clearance','推进离桌',0.1],['in_top','进入两指上沿',5],['ref_x_far','远处横向参考',5],['ref_x_near','近处横向参考',5],['pan_gain','横向增益',0.1],['lift_raw','提起抬肩',10],['hold_s','保持秒数',0.5]];
let busy=false,streaming=false,state=null,proposal=null,drag=null,configShown=false;
function esc(v){return String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'})[c]);}
function notice(t){$('notice').textContent=t||'';$('notice').hidden=!t;}
async function request(path,body,timeout){
  const controller=new AbortController();const timer=setTimeout(()=>controller.abort(),timeout||(body?20000:5000));
  try{const r=await fetch(API+path,{cache:'no-store',signal:controller.signal,...(body?{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}:{})});
    let data;try{data=await r.json();}catch{throw Error('服务返回了无法读取的数据');}if(!r.ok)throw Error(data.detail||('请求失败 '+r.status));return data;}
  finally{clearTimeout(timer);}
}
function fact(name,value,cls=''){return `<dt>${esc(name)}</dt><dd class="${cls}">${value}</dd>`;}
const PHASES={idle:'空闲',survey:'到观察位',ready:'观察位就绪',align:'对准',lower:'下降',advance:'推进',close:'闭合',squeeze:'加压',lift:'提起',retry:'重试',hold:'保持',transport:'搬运',place:'放下',release:'松开',retreat:'撤离',home:'收回',rehearsal:'演练',done:'完成',failed:'失败',stopped:'已停止'};
function render(s){
  state=s;const b=s.brain||{},t=s.track||{},ex=s.executor||{},phase=b.phase||'idle';
  $('phase').textContent=PHASES[phase]||phase;$('note').textContent=b.note||'';
  const running=!!s.operation;
  $('state').textContent=running?'运行中':!s.cameras_on?'相机未开':t.active?(t.ok?'跟踪正常':'跟踪丢失'):'未选目标';
  $('state').className='pill '+(running?'warn':t.active&&t.ok?'ok':t.active?'bad':'');
  const cams=(s.cameras||[]).map(c=>`${c.camera}号 ${c.mode==='stream'?'独占':c.mode==='shared'?'共享':'关'}${c.age_ms!=null?' '+c.age_ms+'ms':''}${c.error?' · '+esc(c.error):''}`).join('；');
  const r=s.last_result;
  $('facts').innerHTML=fact('相机',cams||'—')+
    fact('跟踪',t.active?`得分 ${t.score} · 框 ${esc(JSON.stringify(t.bbox))}`:'未选目标',t.active&&t.ok?'ok':t.active?'bad':'')+
    fact('跟踪耗时',s.tracking_ms_median!=null?`中位 ${s.tracking_ms_median}ms`:'—')+
    fact('控制会话',ex.active?`进行中 · ${ex.steps}步`:ex.reachable===false?'控制服务无响应':`空闲${ex.ended?' · 上次：'+esc(ex.ended):''}`,ex.reachable===false?'bad':'')+
    fact('尝试',b.attempt||'—')+
    fact('上次结果',r?(r.ok?`成功 · ${r.seconds??'—'}秒 · ${r.steps??'—'}步`:'未完成：'+esc(r.error)):'—',r?(r.ok?'ok':'bad'):'');
  $('events').innerHTML=(b.events||[]).slice(-12).reverse().map(e=>`<p><b>${esc(e.t)}s</b> ${esc(e.kind==='phase'?(PHASES[e.phase]||e.phase)+'：'+(e.note||''):e.kind==='step'?(e.label||'动作')+(e.contact?'（受阻）':''):e.kind==='verify'?'提起核对：'+(e.why||''):e.kind==='contact'?'夹爪接触 '+e.grip:e.kind==='lateral'?'横向误差 '+e.err+'px → 肩水平'+e.dpan:e.kind)}</p>`).join('');
  $('target').textContent=t.active?`已选目标，跟踪${t.ok?'正常':'丢失'}`:'尚未选目标';
  if(!configShown&&s.config){$('config').innerHTML=CONFIG.map(([k,n,st])=>`<label>${esc(n)} <input data-key="${k}" type="number" step="${st}" value="${s.config[k]}"></label>`).join('');configShown=true;}
  $('cam-start').disabled=busy||s.cameras_on;$('cam-stop').disabled=busy||!s.cameras_on||running;
  $('survey').disabled=busy||running||!s.cameras_on;$('run').disabled=busy||running||!(t.active&&t.ok);
  $('propose').disabled=busy||running||!s.cameras_on;$('use-proposal').disabled=busy||running||!proposal;$('clear').disabled=busy||running;
  if(s.cameras_on&&!streaming){$('live').src='/grasp/stream.mjpg';streaming=true;}
  if(!s.cameras_on&&streaming){$('live').removeAttribute('src');streaming=false;}
}
async function refresh(){try{render(await request('/status'));}catch(e){$('state').textContent='连接失败';$('state').className='pill bad';}}
async function loadRuns(){try{const runs=await request('/runs');$('runs').innerHTML=runs.slice(0,3).map(r=>{const res=r.result||{};
  const shots=(r.keyframes||[]).map(k=>(k.files||[]).find(f=>f.includes('overlay'))||(k.files||[])[0]).filter(Boolean).slice(0,12);
  return `<div class="run"><p><b>${esc(r.id)}</b> ${res.ok?'<span class="ok">成功</span>':'<span class="bad">'+esc(res.error||'未完成')+'</span>'}</p><div class="thumbs">${shots.map(f=>`<a href="${API}/runs/${encodeURIComponent(r.id)}/${encodeURIComponent(f)}" target="_blank"><img loading="lazy" src="${API}/runs/${encodeURIComponent(r.id)}/${encodeURIComponent(f)}" alt="${esc(f)}"></a>`).join('')}</div></div>`;}).join('')||'<p class="hint">暂无记录</p>';}catch(e){}}
async function act(button,path,body,timeout){if(busy)return null;busy=true;if(button)button.disabled=true;notice('');let out=null;
  try{out=await request(path,body||{},timeout);}catch(e){notice(e.name==='AbortError'?'请求超时，请刷新确认状态':e.message);}finally{busy=false;refresh();}return out;}
function drawBox(box,dashed){const c=$('pick'),g=c.getContext&&c.getContext('2d');if(!g)return;g.clearRect(0,0,640,480);if(!box)return;
  g.setLineDash(dashed?[8,6]:[]);g.lineWidth=2;g.strokeStyle=dashed?'#f0c674':'#d6f281';g.strokeRect(box[0],box[1],box[2],box[3]);}
function point(e){const c=$('pick'),r=c.getBoundingClientRect();return [Math.max(0,Math.min(640,(e.clientX-r.left)*640/r.width)),Math.max(0,Math.min(480,(e.clientY-r.top)*480/r.height))];}
$('pick').addEventListener('pointerdown',e=>{if(!state||!state.cameras_on||state.operation)return;drag=point(e);});
$('pick').addEventListener('pointermove',e=>{if(!drag)return;const p=point(e);drawBox([Math.min(drag[0],p[0]),Math.min(drag[1],p[1]),Math.abs(p[0]-drag[0]),Math.abs(p[1]-drag[1])]);});
$('pick').addEventListener('pointerup',async e=>{if(!drag)return;const p=point(e),box=[Math.min(drag[0],p[0]),Math.min(drag[1],p[1]),Math.abs(p[0]-drag[0]),Math.abs(p[1]-drag[1])].map(Math.round);drag=null;
  if(box[2]<16||box[3]<16){drawBox(null);return;}const out=await act(null,'/select',{bbox:box});if(out)drawBox(null);});
$('cam-start').addEventListener('click',e=>act(e.currentTarget,'/cameras/start'));
$('cam-stop').addEventListener('click',e=>act(e.currentTarget,'/cameras/stop'));
$('propose').addEventListener('click',async e=>{const out=await act(e.currentTarget,'/propose');proposal=out?out.proposal:null;drawBox(proposal,true);});
$('use-proposal').addEventListener('click',async e=>{if(!proposal)return;const out=await act(e.currentTarget,'/select',{bbox:proposal});if(out){proposal=null;drawBox(null);}});
$('clear').addEventListener('click',e=>act(e.currentTarget,'/clear'));
$('survey').addEventListener('click',e=>act(e.currentTarget,'/survey',{confirmed:$('confirm').checked}));
$('run').addEventListener('click',e=>act(e.currentTarget,'/run',{confirmed:$('confirm').checked,options:{place_lateral_cm:Number($('lateral').value)||0,place_forward_cm:Number($('forward').value)||0,end_pose:$('end-pose').value,stop_after:$('mode').value==='inserted'?'inserted':null}}));
$('stop').addEventListener('click',async()=>{try{await request('/stop',{},4000);notice('已发送停止');}catch(e){notice('停止请求失败：'+e.message+'；请按手柄任意键');}refresh();});
$('save-config').addEventListener('click',e=>{const changes={};for(const input of document.querySelectorAll('#config input'))changes[input.dataset.key]=Number(input.value);act(e.currentTarget,'/config',{changes});});
refresh();loadRuns();setInterval(refresh,500);setInterval(loadRuns,5000);
