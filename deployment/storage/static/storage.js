'use strict';
const StorageView = (() => {
  const $ = id => document.getElementById(id);
  const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const bytes = n => n == null ? '—' : Math.abs(n) >= 1024 ** 3 ? (n / 1024 ** 3).toFixed(2) + ' GiB' : Math.abs(n) >= 1024 ** 2 ? (n / 1024 ** 2).toFixed(1) + ' MiB' : Math.abs(n) >= 1024 ? (n / 1024).toFixed(1) + ' KiB' : n + ' B';
  const count = n => Number(n || 0).toLocaleString('zh-CN');
  const date = value => value ? new Date(value).toLocaleString('zh-CN', {hour12:false}) : '尚无记录';
  const levels = {ok:'余量正常', warning:'空间偏低', critical:'空间紧张', unknown:'等待检查'};
  const purposeNames = {environments:'运行环境与依赖', deployment:'部署包与恢复备份', evidence:'实物测试与证据', maps:'地图与空间观察', studio:'Studio 运行资料', project:'工程与其他资料'};
  const kindNames = {images:'照片与图片', videos:'视频', documents:'文档与说明', sensor_data:'深度与点云数据', records:'日志、记录与数据库', other:'程序、安装包与其他'};
  let busy = false, lastSuccess = 0, scanPending = false, lastScan = null;

  function spark(history, path) {
    const points = history.slice(-120).map(s => ({t:new Date(s.at).getTime(), v:s.volumes.find(v => v.path === path)?.free_bytes})).filter(p => Number.isFinite(p.v) && Number.isFinite(p.t));
    if (points.length < 2 || points.at(-1).t - points[0].t < 60000) return '<p class="meta">累积检查记录后显示余量变化</p>';
    const min = Math.min(...points.map(p => p.v)), max = Math.max(...points.map(p => p.v)), start = points[0].t, duration = points.at(-1).t - start;
    const xy = points.map(p => `${((p.t - start) / duration * 300).toFixed(1)},${(33 - (p.v - min) / Math.max(max - min, 1) * 28).toFixed(1)}`).join(' ');
    return `<svg class="spark" viewBox="0 0 300 38" preserveAspectRatio="none" role="img" aria-label="最近${Math.round(duration/60000)}分钟剩余空间，最低${bytes(min)}，最高${bytes(max)}"><polyline points="${xy}"></polyline></svg><p>近 ${Math.round(duration/60000)} 分钟 · ${bytes(points.at(-1).v - points[0].v)} 变化</p>`;
  }

  function usage(rows, names, total) {
    return rows.map(r => `<div class="usage"><div class="usage-title"><span>${esc(names[r.id] || r.id)}</span><span>${bytes(r.bytes)}</span></div><meter min="0" max="${Math.max(total,1)}" value="${r.bytes}" aria-label="${esc(names[r.id] || r.id)}占用"></meter><small>${count(r.files)} 个文件${r.images ? ' · ' + count(r.images) + ' 张图片 / ' + bytes(r.image_bytes) : ''}</small></div>`).join('') || '<p class="meta">尚未统计到文件</p>';
  }

  function render(s) {
    const notice = $('notice'), level = s.stale ? 'unknown' : s.level;
    notice.className = 'notice ' + level;
    const affected = (s.alerts || []).map(a => a.volume || '磁盘').join('、');
    notice.textContent = s.stale ? '空间检查尚未完成或已过期，请稍后刷新；当前余量不可确认。' : level === 'critical' ? `${affected} 剩余空间紧张。请尽快备份并整理资料，为持续采集留出余量。` : level === 'warning' ? `${affected} 余量已触及预警线，建议安排备份与空间整理。` : level === 'unknown' ? `${affected} 容量读取失败，请检查磁盘连接和访问状态。` : '各盘余量正常。空间检测在 Studio 后台持续运行。';
    $('volumes').innerHTML = s.volumes.map(v => `<article class="volume ${v.level}"><div class="volume-head"><strong class="volume-name">${esc(v.path)}</strong><span class="pill">${s.stale ? '记录已过期' : levels[v.level]}</span></div><div class="capacity">${v.free_bytes == null ? '—' : (v.free_bytes/1024**3).toFixed(1)}<small>GiB 可用</small></div><p>${v.free_percent == null ? '容量暂不可读' : '剩余 ' + v.free_percent.toFixed(1) + '% · 总容量 ' + bytes(v.total_bytes)}</p>${v.total_bytes ? `<progress max="${v.total_bytes}" value="${v.used_bytes}" aria-label="${esc(v.path)}已用空间"></progress><p>已用 ${bytes(v.used_bytes)}</p>` : ''}${spark(s.history || [], v.path)}</article>`).join('');
    $('check-time').textContent = `采集电脑：${s.host} · 容量检查：${date(s.checked_at)} · 每 ${s.capacity_interval_s} 秒检查`;
    $('hardware').textContent = (s.hardware?.disks || []).map(d => `磁盘 ${d.number}：${d.model} · ${d.bus} · ${bytes(d.size_bytes)} · ${d.health === 'Healthy' ? '系统报告健康' : d.health} · 分区 ${d.volumes.join(' / ')}`).join('；') || (s.hardware?.error ? '物理磁盘信息读取失败：' + s.hardware.error : '正在读取物理磁盘与分区信息…');
    const t = s.thresholds;
    $('thresholds').textContent = `提醒：剩余 ≤ ${t.warning_free_gib} GiB 或 ≤ ${t.warning_free_percent}%；紧急：剩余 ≤ ${t.critical_free_gib} GiB 或 ≤ ${t.critical_free_percent}%。任一条件触发即预警，阈值恢复后自动解除。`;
    const inv = s.inventory;
    if (scanPending && inv?.completed_at !== lastScan) scanPending = false;
    const scanning = s.scanning || scanPending;
    $('inventory-state').textContent = scanning ? '正在统计…' : s.inventory_error ? '统计失败' : inv ? inv.complete ? '统计完成' : '部分统计' : '等待首次统计';
    $('refresh').disabled = scanning;
    $('inventory-time').textContent = (inv ? `最近统计：${date(inv.completed_at)} · 用时 ${inv.duration_s} 秒${inv.from_previous_run ? ' · 来自上次运行，等待新统计' : ''}${!inv.complete ? ' · 存在未覆盖内容，下面数值为已统计部分' : ''}` : '后台正在统计目录，容量预警可独立运行。') + (s.inventory_error ? ' · 本次扫描失败：' + s.inventory_error : '') + (s.persistence_error ? ' · 检查记录保存失败：' + s.persistence_error : '') + ` · 每 ${s.inventory_interval_s/60} 分钟更新`;
    $('totals').innerHTML = [['机器人目录总量',inv ? bytes(inv.bytes) : '—',inv ? count(inv.files) + ' 个文件' : '等待统计'],['照片与图片',inv ? count(inv.images) + ' 张' : '—',inv ? bytes(inv.image_bytes) : '等待统计'],['视频',inv ? bytes(inv.kinds.find(r=>r.id==='videos')?.bytes || 0) : '—',inv ? count(inv.kinds.find(r=>r.id==='videos')?.files || 0) + ' 个文件' : '等待统计']].map(r => `<div class="total"><span>${r[0]}</span><strong>${r[1]}</strong><small>${r[2]}</small></div>`).join('');
    if (!inv) return;
    $('purposes').innerHTML = usage(inv.purposes,purposeNames,inv.bytes);
    $('kinds').innerHTML = usage(inv.kinds,kindNames,inv.bytes);
    $('directories').innerHTML = inv.directories.map(r => `<tr><td>${esc(r.id)}</td><td>${bytes(r.bytes)}</td><td>${count(r.files)}</td><td>${count(r.images)} / ${bytes(r.image_bytes)}</td></tr>`).join('');
    $('largest').innerHTML = inv.largest_files.map(r => `<div><span>${esc(r.path)}</span><b>${bytes(r.bytes)}</b></div>`).join('');
    $('coverage').innerHTML = inv.roots.map(r => `<p><code>${esc(r.path)}</code> · ${esc({complete:'已统计',partial:'部分统计',pending:'未扫描',unavailable:'无法读取',link_skipped:'链接已跳过'}[r.state] || r.state)} · ${bytes(r.bytes)}</p>`).join('') + `<p>跳过链接 ${count(inv.skipped_links)} 项；读取失败 ${count(inv.error_count)} 项${inv.limit ? '；达到扫描上限：' + esc(inv.limit) : ''}。每次扫描最多 ${s.scan_budget_s} 秒，目录变化可能使统计与磁盘已用量存在差异。</p>` + inv.errors.map(r => `<p>${esc(r.path)} · ${esc(r.error)}</p>`).join('');
  }

  async function poll() {
    if (busy || document.hidden) return;
    busy = true;
    try {
      const response = await fetch('/api/platform/storage/status', {cache:'no-store', signal:AbortSignal.timeout(8000)});
      if (!response.ok) throw new Error('HTTP ' + response.status);
      const state = await response.json();
      render(state); lastSuccess = Date.now();
    } catch (error) {
      $('notice').className = 'notice unknown';
      $('notice').textContent = '无法获取最新空间状态。' + (lastSuccess ? '上次连接：' + date(lastSuccess) + '，以下数据为旧记录。' : '') + ' ' + error.message;
    } finally { busy = false; }
  }

  async function refresh() {
    $('refresh').disabled = true;
    try {
      const before = await fetch('/api/platform/storage/status').then(r=>r.json());
      lastScan = before.inventory?.completed_at;
      const response = await fetch('/api/platform/storage/refresh', {method:'POST',headers:{'Content-Type':'application/json'},body:'{}',signal:AbortSignal.timeout(8000)});
      if (!response.ok) throw new Error('HTTP ' + response.status);
      scanPending = true;
      render(await response.json());
    } catch (error) {
      $('notice').className = 'notice unknown';
      $('notice').textContent = '重新统计未成功：' + error.message;
      $('refresh').disabled = false;
    }
  }
  if (typeof document !== 'undefined') {
    $('refresh').addEventListener('click',refresh);
    document.addEventListener('visibilitychange',()=>{if(!document.hidden)poll();});
    poll();setInterval(poll,5000);
  }
  return {bytes, esc, spark};
})();
if (typeof module !== 'undefined') module.exports = StorageView;
