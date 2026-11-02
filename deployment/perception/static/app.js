'use strict';
const $=id=>document.getElementById(id);
const API='/api/platform/perception';
let busy=false,streaming=false,state=null;
function esc(v){return String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'})[c]);}
function notice(t){$('notice').textContent=t||'';$('notice').hidden=!t;}
async function request(path,body){
  const controller=new AbortController();const timer=setTimeout(()=>controller.abort(),body?12000:4000);
  try{const r=await fetch(API+path,{cache:'no-store',signal:controller.signal,...(body?{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}:{})});
    let data;try{data=await r.json();}catch{throw Error('服务返回了无法读取的数据');}if(!r.ok)throw Error(data.detail||('请求失败 '+r.status));return data;}
  finally{clearTimeout(timer);}
}
function fact(name,value,cls=''){return `<dt>${esc(name)}</dt><dd class="${cls}">${value}</dd>`;}
function render(s){
  state=s;const l=s.latest||{};
  $('state').textContent=!s.running?'未启动':l.valid?'放行':'阻断';$('state').className='pill '+(!s.running?'':l.valid?'ok':'bad');
  $('clear').textContent=l.clear_m===null||l.clear_m===undefined?'—':Number(l.clear_m).toFixed(2);
  $('clear-note').textContent=l.metric?'米（车宽走廊内）':'未标定，无距离';
  $('facts').innerHTML=fact('输入帧',s.running?`${s.frames_in} · 帧龄 ${s.frame_age_ms??'—'}ms`:'—')+
    fact('处理耗时',s.processing_ms_median!==null&&s.processing_ms_median!==undefined?`中位 ${s.processing_ms_median}ms · P90 ${s.processing_ms_p90}ms`:'—')+
    fact('盲区',l.blind_m!==undefined&&l.blind_m!==null?`车头前 ${l.blind_m}m 内看不到`:'—')+
    fact('走廊遮挡',l.occluded_fraction!==undefined?Math.round(l.occluded_fraction*100)+'%':'—')+
    fact('障碍点',(l.obstacles||[]).length?(l.obstacles||[]).slice(0,4).map(o=>`${o.kind==='person'?'人':'物'} ${o.x_m}m/${o.y_m}m`).join('，'):'无')+
    fact('已发送闸门',`${s.published} 条${s.publish_errors?' · 失败 '+s.publish_errors:''}`,s.publish_errors?'warn':'');
  $('reasons').innerHTML=(l.reasons||[]).map(r=>`<p>· ${esc(r)}</p>`).join('')+(s.reader_error?`<p>· 相机：${esc(s.reader_error)}</p>`:'');
  const c=s.calibration||{},h=s.head||{};
  $('calibration').innerHTML=`<dl>${fact('地面单应',c.status==='adopted'?`已采用 · 误差 ${c.rms_m!=null?(c.rms_m*100).toFixed(1)+'cm':'—'}`:'未采用（请先完成C2并在“自主学习”页采用）',c.status==='adopted'?'ok':'warn')}
    ${fact('导航姿态',c.head_pose?esc(JSON.stringify(c.head_pose)):'—')}${fact('当前头部',h.positions?esc(JSON.stringify(h.positions)):'—')}${fact('姿态检查',h.ok?'在容差内':esc(h.reason||'未知'),h.ok?'ok':'warn')}</dl>`;
  const g=s.gate||{};
  $('gate').innerHTML=`<dl>${fact('收到数据',g.present?'是':'否',g.present?'ok':'warn')}${fact('数据年龄',g.present?`${g.age_ms}ms / 上限 ${g.max_age_ms}ms`:'—',g.present&&g.age_ms<=g.max_age_ms?'ok':'warn')}
    ${fact('闸门状态',g.present&&g.valid&&g.metric&&g.head_pose_ok&&g.age_ms<=g.max_age_ms?'可放行（仍需净空足够）':'阻断',g.present&&g.valid&&g.age_ms<=g.max_age_ms?'ok':'bad')}${fact('累计拦截',g.blocks??0)}</dl>`;
  $('floor').textContent=s.floor_model?.ready?`地面模型已就绪（${s.floor_model.samples}个像素样本）`:'尚未学习地面外观';
  $('start').disabled=busy||s.running;$('stop').disabled=busy||!s.running;$('learn').disabled=busy||!s.running;
  if(s.running&&!streaming){$('live').src='/perception/stream.mjpg';streaming=true;$('caption').textContent='绿色为判定地面，红线为障碍接地点，黄线为车宽走廊，橙线为当前净空。';}
  if(!s.running&&streaming){$('live').removeAttribute('src');streaming=false;}
}
async function refresh(){try{render(await request('/status'));}catch(e){$('state').textContent='连接失败';$('state').className='pill bad';}}
async function act(button,path,body){if(busy)return;busy=true;button.disabled=true;notice('');
  try{await request(path,body||{});}catch(e){notice(e.name==='AbortError'?'请求超时，请刷新确认状态':e.message);}finally{busy=false;refresh();}}
$('start').addEventListener('click',e=>act(e.currentTarget,'/start'));
$('stop').addEventListener('click',e=>act(e.currentTarget,'/stop'));
$('learn').addEventListener('click',e=>act(e.currentTarget,'/learn-floor',{confirmed:$('confirm').checked}));
$('reset').addEventListener('click',e=>{if(confirm('确认清空已学习的地面外观？'))act(e.currentTarget,'/reset-floor',{confirmed:true});});
refresh();setInterval(refresh,600);
