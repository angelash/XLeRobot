/* This view observes the onboard job; closing it is not a motion command. */
(() => {
  const api='/api/platform/fieldmap/local-scan';
  const phases={idle:'等待本地任务',starting:'准备扫描',observing:'检查新图',moving:'正在采集',waiting_source:'等待画面恢复',saving:'保存观察',stopping:'正在停止',completed:'本批采集完成',needs_review:'等待判断与调整',cancelled:'已停止',interrupted:'任务被中断'};
  const autonomousPhases={starting:'自主任务准备中',survey_station:'自行补扫当前驻点',waiting_source:'等待新图，恢复后自行继续',waiting_hardware:'等待硬件读回与停车窗口',waiting_resources:'等待供电或归档空间恢复',waiting_navigation:'等待可靠测距与定位接入',latched_stop:'已锁定停止，需处理具体异常',stopping:'正在停止自主任务',stopped:'自主任务已停止',budget_reached:'本地任务时间预算已到',interrupted:'重启中断，旧动作未重放'};
  const reasons={metric_ground_clearance_unvalidated:'地面测距未通过',continuous_localization_not_connected:'连续定位未接通',sensor_to_base_extrinsics_unverified:'相机与底盘外参未验证',body_footprint_not_measured:'整车包络待实测',live_navigation_backend_not_connected:'实时导航后端未接通',head_source_recovery:'相机临时中断，恢复后从缺失视角继续'};
  const host=document.createElement('section');host.className='spatial-memory local-scan';
  host.innerHTML='<div><p class="eyebrow">LOCAL SCAN / 车载执行</p><h2>连续采集，按批复核。</h2><p id="local-scan-phase" role="status">正在读取本地任务…</p></div><div class="memory-actions"><strong id="local-scan-progress">—</strong><button id="local-scan-stop" disabled>停止本地批次</button><a id="local-scan-record" target="_blank" rel="noopener" hidden>查看本批证据 ↗</a></div><p class="memory-boundary" id="local-scan-note">本地完成动作、取帧与检查，批次结束或异常时再反馈。照片覆盖和可靠空间测量分别验收。</p>';
  document.querySelector('.spatial-memory')?.after(host);
  if(!host.isConnected)document.querySelector('.intro')?.after(host);
  const find=id=>host.querySelector('#'+id);let version=0,busy=false;
  async function poll(){
    if(document.hidden)return;
    const current=++version;
    try{
      const [response,autoResponse,calResponse]=await Promise.all([fetch(api+'/status',{signal:AbortSignal.timeout(5000)}),fetch('/api/platform/fieldmap/autonomy/status',{signal:AbortSignal.timeout(5000)}),fetch('/api/platform/fieldmap/turn-calibration/status',{signal:AbortSignal.timeout(5000)})]);
      if(!response.ok)throw Error('本地任务暂不可读');
      const s=await response.json();const a=autoResponse.ok?await autoResponse.json():null,c=calResponse.ok?await calResponse.json():null;if(current!==version)return;
      if(c?.id&&(c.running||(c.finished_at||c.started_at)>=(a?.finished_at||a?.started_at||''))&&(c.running||(c.finished_at||c.started_at)>=(s.started_at||''))){
        const labels={calibrating:'正反向转角采集',waiting_visibility:'等待关键视图恢复，保留校对任务',checking_rgbd:'停车核对RGB-D',checking_model:'检验未参与拟合的试验',completed:'本场转角校对通过',not_adopted:'校对未采纳，证据保留'};
        host.querySelector('h2').textContent='本地试验，自行校对。';
        find('local-scan-phase').textContent=labels[c.phase]||autonomousPhases[c.phase]||c.phase;
        find('local-scan-progress').textContent=`${c.completed_trials||0} / ${c.planned_trials||8} 组`;
        find('local-scan-stop').disabled=!c.running||busy;find('local-scan-stop').textContent='停止转角校对';
        find('local-scan-note').textContent=[c.phone_device==='android'?'红米 / 安卓独立参考':'iPhone独立参考',c.note,c.model?.gain?`本场转角系数 ${c.model.gain.toFixed(3)}`:'',c.model?.maximum_validation_error_deg!==undefined?`独立验证最大差 ${c.model.maximum_validation_error_deg.toFixed(2)}°`:'',c.error,'只校对转角估计，空间净空与自主导航另行验证'].filter(Boolean).join(' · ');
        find('local-scan-record').hidden=false;find('local-scan-record').href='/api/platform/fieldmap/turn-calibration/'+encodeURIComponent(c.id);return;
      }
      host.querySelector('h2').textContent=a?.id?'车载自行决策，按条件回报。':'连续采集，按批复核。';
      if(a?.id&&(a.running||(a.finished_at||a.started_at)>=(s.started_at||''))){
        find('local-scan-phase').textContent=autonomousPhases[a.phase]||a.phase;
        find('local-scan-progress').textContent=`${a.continuous?'持续任务 · 第'+(a.round||1)+'轮 · ':''}${a.progress?.station_scans||0} 驻点 / ${a.progress?.views||0} 视角`;
        find('local-scan-stop').disabled=!a.running||busy;
        find('local-scan-note').textContent=[a.note,...(a.blocked_by||[]).map(v=>reasons[v]||v),a.error,a.continuous?'任务持续至停止或保护触发，每轮核验停车后续接':'',a.policy?`每${a.policy.report_interval_s}秒汇总 / ${a.policy.no_progress_s}秒无进展上报`:'',a.last_summary?`最近汇总：${a.last_summary.since_progress_s.toFixed(0)}秒无新增覆盖`:'','自主行驶尚未启用'].filter(Boolean).join(' · ');
        find('local-scan-record').hidden=false;find('local-scan-record').href='/api/platform/fieldmap/autonomy/'+encodeURIComponent(a.id);
        find('local-scan-stop').textContent='停止自主任务';return;
      }
      find('local-scan-stop').textContent='停止本地批次';
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
