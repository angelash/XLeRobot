/* This view observes the onboard job; closing it is not a motion command. */
(() => {
  const api='/api/platform/fieldmap/local-scan';
  const phases={idle:'等待本地任务',starting:'准备扫描',observing:'检查新图',moving:'正在采集',waiting_source:'等待画面恢复',saving:'保存观察',stopping:'正在停止',completed:'本批采集完成',needs_review:'等待判断与调整',cancelled:'已停止',interrupted:'任务被中断'};
  const host=document.createElement('section');host.className='spatial-memory local-scan';
  host.innerHTML='<div><p class="eyebrow">LOCAL SCAN / 车载执行</p><h2>连续采集，按批复核。</h2><p id="local-scan-phase" role="status">正在读取本地任务…</p></div><div class="memory-actions"><strong id="local-scan-progress">—</strong><button id="local-scan-stop" disabled>停止本地批次</button><a id="local-scan-record" target="_blank" rel="noopener" hidden>查看本批证据 ↗</a></div><p class="memory-boundary" id="local-scan-note">本地完成动作、取帧与检查，批次结束或异常时再反馈。照片覆盖和可靠空间测量分别验收。</p>';
  document.querySelector('.spatial-memory')?.after(host);
  if(!host.isConnected)document.querySelector('.intro')?.after(host);
  const find=id=>host.querySelector('#'+id);let version=0,busy=false;
  async function poll(){
    if(document.hidden)return;
    const current=++version;
    try{
      const response=await fetch(api+'/status',{signal:AbortSignal.timeout(5000)});
      if(!response.ok)throw Error('本地任务暂不可读');
      const s=await response.json();if(current!==version)return;
      find('local-scan-phase').textContent=phases[s.phase]||s.phase;
      const views=s.planned_views?`${s.completed_views||0} / ${s.planned_views} 视角`:'尚无采集批次';
      find('local-scan-progress').textContent=s.planned_pulses?`${s.passed_pulses||0} / ${s.planned_pulses} 段` : views;
      find('local-scan-stop').disabled=!s.running||busy;
      find('local-scan-note').textContent=[s.note,s.error,s.elapsed_s?`本批 ${s.elapsed_s.toFixed(1)} 秒`:'',s.phone_epochs?.length?`手机分卷 ${s.phone_epochs.length} 份`:'',s.parked_verified?'停车核验通过':''].filter(Boolean).join(' · ')||'本地执行有限步骤，结果就绪后集中复核；未知空间不自动通行。';
      find('local-scan-record').hidden=!s.id;
      if(s.id)find('local-scan-record').href=api+'/'+encodeURIComponent(s.id);
    }catch(error){if(current===version)find('local-scan-phase').textContent=error.message;}
  }
  find('local-scan-stop').addEventListener('click',async()=>{
    busy=true;find('local-scan-stop').disabled=true;++version;
    try{
      const response=await fetch(api+'/stop',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}',signal:AbortSignal.timeout(8000)});
      if(!response.ok)throw Error('停止请求尚未确认，请使用顶部停止动作');
    }catch(error){find('local-scan-phase').textContent=error.message;}
    finally{busy=false;poll();}
  });
  poll();setInterval(poll,1500);document.addEventListener('visibilitychange',()=>{if(!document.hidden)poll();});
})();
