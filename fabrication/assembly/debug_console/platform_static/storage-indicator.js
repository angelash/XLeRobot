'use strict';
(() => {
  const badge=document.getElementById('storage-badge'),banner=document.getElementById('storage-alert');
  let busy=false;
  async function refreshStorage(){
    if(busy||document.hidden)return;
    busy=true;
    try{
      const response=await fetch('/api/platform/storage/summary',{cache:'no-store',signal:AbortSignal.timeout(7000)});
      if(!response.ok)throw new Error('空间检查不可用');
      const s=await response.json(),level=s.stale?'unknown':s.level;
      badge.className='storage-badge '+level;
      badge.textContent=level==='ok'?'空间正常 ↗':level==='critical'?'空间紧张 ↗':level==='warning'?'空间预警 ↗':'空间待检查 ↗';
      badge.title=`${s.host} · ${s.checked_at?new Date(s.checked_at).toLocaleString('zh-CN'):'尚无检查'}`;
      banner.hidden=level==='ok';
      banner.className='storage-alert '+level;
      banner.textContent=level==='critical'?'磁盘剩余空间紧张，请尽快备份并整理资料。查看空间 →':level==='warning'?'磁盘余量已触及预警线。查看占用与剩余空间 →':'空间检测暂不可确认，点击查看检查状态 →';
    }catch{
      badge.textContent='空间检测离线 ↗';badge.className='storage-badge unknown';
      banner.hidden=false;banner.className='storage-alert unknown';banner.textContent='无法获取最新磁盘状态。查看存储空间 →';
    }finally{busy=false;}
  }
  refreshStorage();setInterval(refreshStorage,30000);
  document.addEventListener('visibilitychange',()=>{if(!document.hidden)refreshStorage();});
})();
