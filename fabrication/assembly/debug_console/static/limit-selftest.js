/* Limit self-test panel: shares the original console poll and api(). */
let limitOpen=false,limitView='',limitSelected=null,limitPending=false;
const limitKindNames={tilt:'头部俯仰',pan:'水平旋转',grip:'夹爪',roll:'腕旋转',pitch:'腕俯仰',elbow:'肘',lift:'肩抬升',fold:'收起',upright:'竖直'};
function limitDiagram(side,kind){
 const hl='var(--amber)',arm='var(--green)',ink='var(--ink)';
 const arc=(x,y,r,a0,a1)=>{const p=a=>[x+r*Math.cos(a*Math.PI/180),y-r*Math.sin(a*Math.PI/180)];const [x0,y0]=p(a0),[x1,y1]=p(a1);return `<path d="M${x0} ${y0}A${r} ${r} 0 0 0 ${x1} ${y1}" fill="none" stroke="${hl}" stroke-width="3" marker-end="url(#la)" marker-start="url(#lb)"/>`;};
 const defs='<defs><marker id="la" viewBox="0 0 8 8" refX="4" refY="4" markerWidth="5" markerHeight="5" orient="auto"><path d="M0 0L8 4L0 8Z" fill="var(--amber)"/></marker><marker id="lb" viewBox="0 0 8 8" refX="4" refY="4" markerWidth="5" markerHeight="5" orient="auto-start-reverse"><path d="M0 0L8 4L0 8Z" fill="var(--amber)"/></marker></defs>';
 const seg=(pts,color,w=8)=>`<path d="M${pts.map(p=>p.join(' ')).join('L')}" fill="none" stroke="${color}" stroke-width="${w}" stroke-linecap="round" stroke-linejoin="round"/>`;
 const dot=(x,y)=>`<circle cx="${x}" cy="${y}" r="5" fill="var(--paper)" stroke="${ink}" stroke-width="2"/>`;
 let body='';
 if(side==='head'){
  body=`<rect x="58" y="34" width="44" height="30" rx="6" fill="${ink}"/><circle cx="80" cy="49" r="9" fill="var(--paper)"/><circle cx="80" cy="49" r="4" fill="${ink}"/><path d="M80 64V86M62 86H98" stroke="${arm}" stroke-width="6"/>`;
  body+=kind==='tilt'?arc(80,49,34,40,-40)+'<text x="122" y="20">上/下</text>':arc(80,49,36,160,20)+'<text x="4" y="20">左/右</text>';
 }else if(kind==='pan'){
  body=`<rect x="50" y="56" width="60" height="34" rx="6" fill="none" stroke="${ink}" stroke-width="2"/>${seg([[80,62],[80,20]],arm)}${dot(80,62)}${arc(80,62,30,150,30)}<text x="4" y="96">俯视</text>`;
 }else if(kind==='upright'){
  body=`<rect x="10" y="82" width="44" height="12" fill="${ink}"/>${seg([[32,82],[32,50]],hl)}${seg([[32,50],[32,22]],hl)}${seg([[32,22],[32,8]],hl,6)}${dot(32,82)}${dot(32,50)}${dot(32,22)}<text x="58" y="44">竖直向上</text><text x="58" y="62">测时向两侧下摆</text>`;
 }else if(kind==='roll'){
  body=`<circle cx="80" cy="52" r="16" fill="${arm}"/><rect x="74" y="32" width="12" height="40" rx="3" fill="${ink}"/>${arc(80,52,30,200,-20)}<text x="4" y="96">端视</text>`;
 }else if(kind==='grip'){
  body=`${seg([[20,52],[70,52]],arm)}<path d="M70 52L110 34M70 52L110 70" stroke="${hl}" stroke-width="7" stroke-linecap="round"/><path d="M128 30L128 46M128 74L128 58" stroke="${hl}" stroke-width="3" marker-end="url(#la)"/>`;
 }else{
  const S=[30,82],E=kind==='fold'?[52,40]:[56,30],W=kind==='fold'?[86,62]:[110,30],T=kind==='fold'?[104,72]:[142,30];
  body=`<rect x="10" y="82" width="44" height="12" fill="${ink}"/>${seg([S,E],kind==='lift'?hl:arm)}${seg([E,W],kind==='elbow'?hl:arm)}${seg([W,T],kind==='pitch'?hl:arm,6)}${dot(...S)}${dot(...E)}${dot(...W)}`;
  if(kind==='lift')body+=arc(30,82,26,100,20);if(kind==='elbow')body+=arc(56,30,22,40,-60);if(kind==='pitch')body+=arc(110,30,22,60,-60);
  if(kind==='fold')body+='<path d="M130 30L112 52" stroke="var(--amber)" stroke-width="3" marker-end="url(#la)"/><text x="104" y="20">收起</text>';
 }
 return `<svg class="limit-diagram" viewBox="0 0 160 100" role="img" aria-label="${limitKindNames[kind]||kind}示意">${defs}${body}</svg>`;
}
const limitEnd={stall:'止挡',bound:'探测上限',inside:'行程内受阻·保留原限位'};
function limitStatus(res){
 if(!res)return '未测';
 if(res.status==='done'&&res.min!==undefined)return `完成：${res.range_deg}°（原${res.old_range_deg}°）· 下端${limitEnd[res.min_kind]||res.min_kind} · 上端${limitEnd[res.max_kind]||res.max_kind}${res.applied?' · 已写入':' · 未写入'}`;
 return {queued:'排队中',running:'测试中…',done:res.message||'完成',failed:'失败：'+(res.message||''),skipped:'未执行（已停止）',aborted:'已停止'}[res.status]||res.status;
}
function limitBody(t){
 const running=!!t.running,sel=limitSelected,groups=[['head','头部（主镜头）'],['left','左臂'],['right','右臂']];
 const cards=groups.map(([side,title])=>`<div class="limit-group"><h3>${title}</h3><div class="limit-cards">${(t.items||[]).filter(x=>x.side===side).map(x=>{const res=x.result,cls=res?res.status:'';
  const range=x.range_deg!==undefined?`当前限位 ${x.range_deg}°（${x.min}–${x.max}）· 位置 ${x.position}`:'';
  const fold=x.folded===true?'<b class="ok">已检测到全收起</b>':x.folded===false?(x.kind==='pan'?'<b class="warn">需先收起：会自动执行“收起此臂”</b>':''):x.kind==='fold'?'<b class="warn">没有折叠姿态记录</b>':'';
  const pre=x.from_upright?(x.kind==='lift'?' 上臂先摆到竖直、肘放中位（收短手臂、减小力矩），向前后两侧测，测完回到原姿态。':' 先摆到记录的竖直姿态，从竖直向两侧测，测完回到原姿态。'):x.prepose&&x.prepose.length?' 预置：'+esc(x.prepose.join('、'))+'先摆到中位，测完放回。':'';
  const up=x.kind!=='upright'?'':(x.upright?`<b class="ok">已记录：肩抬升${x.upright['2']} · 肘${x.upright['3']} · 腕俯仰${x.upright['4']}</b>`:'<b class="warn">未记录：用“手动摆位”卸力，把这条手臂扶到竖直向上并锁定，再点“记录当前姿态为竖直”</b>')+`<span class="cal-buttons"><button type="button" data-upright="record" data-side="${x.side}" ${running?'disabled':''}>记录当前姿态为竖直</button>${x.upright?`<button type="button" class="outline" data-upright="clear" data-side="${x.side}" ${running?'disabled':''}>清除</button>`:''}</span>`;
  return `<label class="limit-card ${cls} ${t.current===x.id?'active':''}"><span class="limit-check"><input type="checkbox" data-limit-item="${x.id}" ${sel.has(x.id)?'checked':''} ${running||(x.kind==='upright'&&!x.upright)?'disabled':''}><strong>${esc(x.name)}</strong></span>${limitDiagram(x.side,x.kind)}<small>${esc(range)}</small><small>${esc(x.note)}${pre}</small>${fold}${up}<em>${esc(limitStatus(res))}</em></label>`;}).join('')}</div></div>`).join('');
 const events=(t.events||[]).slice(-8).reverse().map(e=>`<li><time>${new Date(e.time).toLocaleTimeString('zh-CN')}</time>${esc(e.message)}</li>`).join('');
 return `<div class="limit-toolbar"><div class="cal-buttons"><button type="button" data-limit="recommend" ${running?'disabled':''}>一条龙推荐方案（全选并按顺序）</button><button type="button" class="outline" data-limit="none" ${running?'disabled':''}>清空勾选</button></div>
<p class="fineprint">每个关节低速推进，扭矩上限降到35%–75%（低于舵机80%的过载保护线；预置与回位用全扭矩）；编码器不再跟随即判为顶到止挡，立即退回约3°并确认真的动了，退不动先等保护恢复再重试。新限位=止挡位置内缩约2°。腕旋转等没有硬止挡的关节最多在已存行程外再探30°–60°。记录了竖直姿态的手臂，腕俯仰、肘、肩抬升都从竖直开始测；没记录时先把可能干涉的关节摆到中位。测完回到原姿态。</p>
<label class="cal-check"><input type="checkbox" id="limit-apply" ${t.apply!==false?'checked':''} ${running?'disabled':''}><span>测完自动写入新限位（同时更新已保存行程）</span></label>
<label class="cal-check"><input type="checkbox" id="limit-confirm" ${running?'disabled':''}><span>周围空间足够、无人靠近；手柄放下。自检中按手柄任意键或点“立即停止”立即停下</span></label>
<div class="cal-buttons"><button type="button" class="primary" data-limit="start" id="limit-start">开始自检</button><button type="button" class="danger" data-limit="stop" id="limit-stop">立即停止</button></div>
<p id="limit-live" class="limit-live"></p></div>${cards}<div class="limit-log"><h3>自检记录</h3><ol>${events||'<li>尚无记录</li>'}</ol></div>`;
}
function renderLimitTest(s){
 const t=s.limit_selftest;if(!t)return;
 if(limitSelected===null)limitSelected=new Set(t.recommended||[]);
 if(t.running)limitOpen=true;
 const button=$('open-limit-test');if(button){button.disabled=s.preview_only||s.ports.length!==2;button.textContent=t.running?'限位自检进行中…':'限位自检';}
 $('limit-panel').hidden=!limitOpen;if(!limitOpen)return;
 const sig=JSON.stringify([t.running,t.current,t.results,t.apply,(t.items||[]).map(x=>[x.id,x.min,x.max,x.folded,x.upright,x.from_upright]),[...limitSelected],(t.events||[]).length]);
 if(sig!==limitView){limitView=sig;const confirm=$('limit-confirm')?.checked;$('limit-body').innerHTML=limitBody(t);if(confirm&&$('limit-confirm'))$('limit-confirm').checked=true;}
 const live=t.live||{};$('limit-live').textContent=t.running?`正在测试 ${live.joint||t.current||''} · ${live.direction<0?'负向':live.direction>0?'正向':''} · 位置 ${live.position??'—'} → 目标 ${live.goal??'—'} · 负载 ${live.load!==undefined?Math.round(live.load/10)+'%':'—'}`:t.error?'上次自检：'+t.error:t.finished_at?'上次自检已完成':'未运行';
 const n=[...limitSelected].length,offline=s.controller.controller!=='online';
 $('limit-start').textContent=t.running?'自检进行中…':`开始自检（${n}项）`;
 $('limit-start').disabled=limitPending||t.running||!n||!offline||!!s.manual_calibration?.active;
 $('limit-stop').disabled=!t.running;
}
async function limitCommand(path,body){
 limitPending=true;$('limit-message').textContent='正在处理…';
 try{await api(path,body);$('limit-message').textContent=path.endsWith('stop')?'已请求停止':'自检已开始；按手柄任意键可立即停止';}
 catch(e){$('limit-message').textContent='未执行：'+e.message;}
 finally{limitPending=false;await refresh();}
}
$('open-limit-test').onclick=()=>{limitOpen=true;limitView='';if(latest)renderLimitTest(latest);$('limit-panel').scrollIntoView({behavior:'smooth',block:'start'});};
$('limit-close').onclick=()=>{limitOpen=false;$('limit-panel').hidden=true;};
$('limit-body').onchange=e=>{const id=e.target.dataset?.limitItem;if(!id)return;if(e.target.checked)limitSelected.add(id);else limitSelected.delete(id);if(latest)renderLimitTest(latest);};
$('limit-body').onclick=e=>{
 const up=e.target.closest('[data-upright]');
 if(up){e.preventDefault();const action=up.dataset.upright;
  limitPending=true;$('limit-message').textContent='正在处理…';
  api('/api/limit-test/upright',{side:up.dataset.side,action}).then(()=>{$('limit-message').textContent=action==='clear'?'竖直姿态已清除':'已记录当前姿态为竖直起点';limitView='';})
   .catch(err=>{$('limit-message').textContent='未执行：'+err.message;}).finally(()=>{limitPending=false;refresh();});
  return;}
 const action=e.target.closest('[data-limit]')?.dataset.limit;if(!action)return;
 const t=latest?.limit_selftest;
 if(action==='recommend'){limitSelected=new Set(t?.recommended||[]);limitView='';return renderLimitTest(latest);}
 if(action==='none'){limitSelected=new Set();limitView='';return renderLimitTest(latest);}
 if(action==='stop')return limitCommand('/api/limit-test/stop',{});
 if(action==='start'){
  if(!$('limit-confirm').checked){$('limit-message').textContent='请先勾选现场确认';return;}
  const order=t?.recommended||[];return limitCommand('/api/limit-test/start',{items:order.filter(x=>limitSelected.has(x)),confirmed:true,apply:$('limit-apply').checked});
 }
};
