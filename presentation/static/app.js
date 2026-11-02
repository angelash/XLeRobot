import {AvatarView} from './avatar.js';
import {createRequestId} from './request-id.js';
import {Demo} from './demo.js';
import {mergeEvents, filterEvents, visualFrame, viewState} from './model.js';

const $ = id => document.getElementById(id);
const apiRoot = '/api/presence';
let profile, draft, avatarDraft, source, demo, frame, stream;
let live = {epoch: null, seq: 0, events: []}, sample = {epoch: 'demo', seq: 0, events: []};
let filter = 'all', follow = true, showOverlays = true, cameraOn = false, visionDismissed = false;
let connected = false, bridge = 'unconfigured', pending = null, pollTimer, toastTimer, responseTimer;
let layoutWrite = Promise.resolve(), disposed = false;

function notify(message, error = false) {
  $('toast').textContent = message; $('toast').classList.toggle('error', error); $('toast').hidden = false;
  clearTimeout(toastTimer); toastTimer = setTimeout(() => $('toast').hidden = true, error ? 7000 : 3500);
}

async function api(path, value, method = 'POST') {
  const options = value === undefined ? {} : {method, headers: {'Content-Type': 'application/json'}, body: JSON.stringify(value)};
  const response = await fetch(apiRoot + path, options);
  let data;
  try {data = await response.json();} catch {throw new Error('展示服务没有返回有效数据');}
  if (!response.ok) {
    const detail = Array.isArray(data.detail) ? data.detail.map(x => x.msg).join('；') : data.detail;
    const error = new Error(detail || '请求未完成'); error.status = response.status; throw error;
  }
  return data;
}

async function upload(path, file, limit) {
  if (file.size > limit) throw new Error(`文件超过 ${limit / 1024**2}MiB 上限`);
  const response = await fetch(apiRoot + path + '?name=' + encodeURIComponent(file.name), {
    method: 'POST', headers: {'Content-Type': 'application/octet-stream'}, body: file,
  });
  const data = await response.json();
  if (!response.ok) throw new Error(data.detail || '导入失败');
  return data;
}

const avatar = new AvatarView($('avatar-host'), (message, type) => {
  $('renderer-note').textContent = message; $('model-type').textContent = type;
});

function applyProfile(value) {
  profile = structuredClone(value);
  document.documentElement.style.setProperty('--accent', profile.accent);
  document.documentElement.style.setProperty('--split', `${profile.layout.split * 100}%`);
  $('robot-name').textContent = profile.name; $('command-name').textContent = profile.name;
  $('robot-name').title = profile.name;
  $('robot-subtitle').textContent = profile.subtitle;
  document.title = `${profile.name} · XLeRobot Presence`;
  $('avatar-host').setAttribute('aria-label', `${profile.name} 机器人形象`);
  if (!current().events.length) $('robot-response').textContent = profile.greeting;
  setProcess(profile.layout.log_open, false);
  avatar.setProfile(profile);
}

function current() {return demo ? sample : live;}

function saveLayout() {
  const snapshot = structuredClone(profile);
  layoutWrite = layoutWrite.catch(() => {}).then(() => api('/config', snapshot, 'PUT')).catch(error => notify(`布局未保存：${error.message}`, true));
}

function setProcess(open, save = true) {
  $('workspace').classList.toggle('process-hidden', !open);
  $('process-panel').hidden = !open;
  $('process-toggle').setAttribute('aria-expanded', String(open));
  $('process-toggle').textContent = open ? '收起过程 ↗' : '展开过程 ↙';
  if (profile) {profile.layout.log_open = open; if (save) saveLayout();}
  requestAnimationFrame(() => {avatar.resize(); layoutOverlays();});
}

function setVision(open, manual = true) {
  $('vision-workspace').hidden = !open;
  $('vision-toggle').setAttribute('aria-expanded', String(open));
  if (manual) visionDismissed = !open;
  if (!open) stopCamera();
  requestAnimationFrame(layoutOverlays);
}

function renderConnection() {
  $('connection').dataset.state = demo ? 'demo' : connected ? bridge : 'offline';
  $('connection-label').textContent = demo ? '合成演示 · 不连接机器人' : !connected ? '展示服务断开' : {
    unconfigured: '本地展示 · 智能服务未配置', online: '智能服务在线', offline: '智能服务未连接',
    fault: '智能服务异常', legacy: '会话桥在线 · 待升级回执',
  }[bridge] || '本地展示已连接';
  $('source-badge').textContent = demo ? '演示数据' : '真实来源';
  $('source-badge').classList.toggle('demo', Boolean(demo));
  $('send-message').disabled = Boolean(demo) || !connected || !['online', 'legacy'].includes(bridge) || Boolean(pending && !pending.uncertain);
  $('send-message').textContent = pending?.uncertain ? '查询回执' : pending ? '处理中…' : '发送 ↵';
  $('command-status').textContent = demo ? '合成演示' : pending ? '请求 ' + pending.id.slice(-8) : '观察与规划';
}

function showState() {
  const state = connected || demo ? viewState(current().events) : 'offline';
  $('avatar-state').dataset.state = state;
  $('avatar-state').textContent = {idle: '待机', working: '处理中', responding: '回应', error: '需要关注', offline: '展示断开'}[state];
  avatar.setState(state);
  const latest = current().events.at(-1);
  $('task-label').textContent = latest?.title || '等待一个新的任务';
  if (latest?.kind === 'message' && latest.phase === 'completed') {
    $('robot-response').textContent = latest.text.slice(0, 500);
    clearTimeout(responseTimer);
    responseTimer = setTimeout(() => {
      if (current().events.at(-1)?.event_id === latest.event_id) {
        $('avatar-state').textContent = '待机'; $('avatar-state').dataset.state = 'idle'; avatar.setState('idle');
      }
    }, 4500);
  }
}

const kindLabels = {system: '系统', task: '任务', summary: '摘要', tool: '工具', vision: '视觉', message: '回应'};
const phaseLabels = {started: '开始', progress: '进行中', completed: '完成', failed: '失败', cancelled: '取消', interrupted: '中断'};

function textNode(tag, cls, text) {
  const node = document.createElement(tag); node.className = cls; node.textContent = text; return node;
}

function renderEvents() {
  const events = current().events;
  $('event-count').textContent = String(events.length).padStart(3, '0');
  const visible = filterEvents(events, filter).slice(-160);
  const scrollTop = $('event-scroll').scrollTop;
  const fragment = document.createDocumentFragment();
  for (const event of visible) {
    const article = document.createElement('article'); article.className = 'event';
    article.dataset.kind = event.kind; article.dataset.phase = event.phase;
    const meta = document.createElement('div'); meta.className = 'event-meta';
    const timestamp = new Date(event.received_at).toLocaleTimeString('zh-CN', {hour12: false});
    meta.append(textNode('time', '', timestamp), textNode('span', 'event-type', kindLabels[event.kind]),
      textNode('span', '', phaseLabels[event.phase]), textNode('span', 'event-evidence' + (event.evidence === 'demo' ? ' demo' : ''),
        {reported: '来源报告', computed: '本机计算', demo: '演示数据'}[event.evidence]));
    article.append(meta, textNode('h3', 'event-title', event.title));
    if (event.text) {
      const content = textNode('p', 'event-text', event.text);
      if (event.text.length > 450) {
        const details = document.createElement('details'); details.append(textNode('summary', '', '展开输出'), content); article.append(details);
      } else article.append(content);
    }
    if (visualFrame(event)) {
      const button = textNode('button', 'event-media-link', '查看对应分析帧 ↗'); button.type = 'button';
      button.addEventListener('click', () => {setVision(true); showFrame(event);}); article.append(button);
    }
    fragment.append(article);
  }
  $('event-list').replaceChildren(fragment);
  $('empty-stream').hidden = Boolean(events.length);
  if (events.length && !visible.length) $('event-list').append(textNode('p', 'field-note', '此筛选下暂无事件。'));
  $('stream-note').textContent = events.length > 160 ? '保留最近 500 条 · 当前显示最多 160 条' : '过程摘要与公开结果';
  if (follow) $('event-scroll').scrollTop = $('event-scroll').scrollHeight;
  else $('event-scroll').scrollTop = scrollTop;
}

function ingest(batch, isDemo = false) {
  const previous = isDemo ? sample : live;
  const reset = batch.reset || (previous.epoch && previous.epoch !== batch.epoch);
  const known = new Set(reset ? [] : previous.events.map(event => event.event_id));
  const before = previous.events.at(-1)?.event_id;
  const next = mergeEvents(previous, batch);
  if (isDemo) sample = next; else live = next;
  if (Boolean(demo) !== isDemo) return;
  const additions = (batch.events || []).filter(event => !known.has(event.event_id));
  if (before === next.events.at(-1)?.event_id && !batch.reset) return;
  if (reset) {resetFrame(); $('robot-response').textContent = profile.greeting;}
  for (const event of additions) {
    if (event.media_id || event.demo_url) showFrame(event, false);
    if (pending && event.task_id === pending.id && ['completed', 'failed', 'cancelled', 'interrupted'].includes(event.phase)) pending = null;
  }
  renderEvents(); showState(); renderConnection();
}

function showFrame(event, forceOpen = true) {
  const next = visualFrame(event); if (!next) return;
  frame = next;
  $('frame-zoom-open').disabled = false;
  $('analysis-image').hidden = false; $('frame-empty').hidden = true;
  $('analysis-image').src = frame.url;
  $('analysis-image').alt = `${frame.title}，输入帧 ${frame.id}`;
  $('frame-label').textContent = `分析帧 / ${frame.id.slice(-23)}`;
  const age = frame.at ? (Date.now() - new Date(frame.at).getTime()) / 1000 : null;
  const timestamp = frame.at ? new Date(frame.at).toLocaleTimeString('zh-CN', {hour12: false}) : '采集时间未知';
  $('frame-caption').textContent = `${frame.evidence === 'demo' ? '演示数据 · ' : ''}${timestamp}${age > 3 ? ' · 历史输入帧' : ''}\n${frame.text || '结果绑定此输入帧。'}`;
  if (forceOpen || !visionDismissed) setVision(true, false);
  layoutOverlays();
}

function layoutOverlays() {
  const image = $('analysis-image'), host = $('analysis-frame'), layer = $('analysis-overlays');
  layer.replaceChildren();
  if (!frame || !image.naturalWidth || !host.clientWidth || !showOverlays) return;
  const scale = Math.min(host.clientWidth / image.naturalWidth, host.clientHeight / image.naturalHeight);
  const width = image.naturalWidth * scale, height = image.naturalHeight * scale;
  Object.assign(layer.style, {width: `${width}px`, height: `${height}px`, left: `${(host.clientWidth-width)/2}px`, top: `${(host.clientHeight-height)/2}px`});
  for (const overlay of frame.overlays) {
    const box = document.createElement('div'); box.className = 'overlay-box';
    Object.assign(box.style, {left: `${overlay.x*100}%`, top: `${overlay.y*100}%`, width: `${overlay.w*100}%`, height: `${overlay.h*100}%`});
    box.append(textNode('span', '', overlay.label + (overlay.confidence == null ? '' : ` ${Math.round(overlay.confidence*100)}%`)));
    layer.append(box);
  }
}

function resetFrame() {
  frame = null; $('analysis-image').removeAttribute('src'); $('analysis-image').hidden = true;
  $('frame-zoom-open').disabled = true;
  $('analysis-overlays').replaceChildren(); $('frame-empty').hidden = false;
  $('frame-label').textContent = '分析帧 · 尚未选择'; $('frame-caption').textContent = '实时视频与分析输入帧分别展示，结果保留在对应画面上。';
}

function stopCamera() {
  cameraOn = false; $('live-image').removeAttribute('src'); $('live-image').hidden = true;
  $('camera-toggle').textContent = '打开相机'; $('camera-toggle').setAttribute('aria-pressed', 'false');
  if (!demo) $('live-view').hidden = true;
}

function startCamera() {
  if (demo) {notify('当前是合成演示，退出演示后可打开真实相机'); return;}
  stopCamera(); setVision(true); cameraOn = true;
  $('live-view').hidden = false; $('live-image').hidden = false; $('demo-video').hidden = true;
  $('live-label').textContent = '实时画面 · 源时间未知 · 非逐帧分析';
  $('live-image').src = apiRoot + '/camera/' + $('camera-select').value;
  $('camera-toggle').textContent = '关闭相机'; $('camera-toggle').setAttribute('aria-pressed', 'true');
}

function stopDemo() {
  if (!demo) return;
  demo.stop(); demo = null; sample = {epoch:'demo', seq:0, events:[]};
  $('demo-toggle').textContent = '播放演示'; $('demo-video').hidden = true; $('live-view').hidden = true;
  resetFrame();
  const lastVisual = live.events.findLast(event => visualFrame(event));
  if (lastVisual) showFrame(lastVisual, false);
  $('robot-response').textContent = profile.greeting;
  renderEvents(); showState(); renderConnection();
}

function startDemo() {
  if (demo) {stopDemo(); return;}
  stopCamera(); visionDismissed = false; setVision(true);
  sample = {epoch:'demo', seq:0, events:[]};
  demo = new Demo($('demo-video'), event => ingest({epoch:'demo', seq:event.seq, events:[event]}, true));
  $('demo-toggle').textContent = '退出演示'; $('live-view').hidden = false; $('demo-video').hidden = false;
  $('live-image').hidden = true; $('live-label').textContent = '合成实时画面 · 演示数据';
  demo.start(); renderConnection();
}

function connectStream() {
  stream?.close();
  if (document.hidden || disposed) return;
  const query = new URLSearchParams({after: live.seq, ...(live.epoch ? {epoch:live.epoch} : {})});
  stream = new EventSource(apiRoot + '/stream?' + query);
  stream.addEventListener('progress', event => {
    const value = JSON.parse(event.data); ingest({epoch:value.epoch, seq:value.seq, events:[value]});
  });
  stream.addEventListener('reset', event => ingest(JSON.parse(event.data)));
  stream.onopen = () => $('stream-label').textContent = 'LIVE';
  stream.onerror = () => $('stream-label').textContent = '重连中';
}

async function poll() {
  if (disposed) return;
  try {
    const state = await api('/state');
    connected = true; bridge = state.connection.state;
    if (!profile) applyProfile(state.profile);
    ingest(state);
    if (pending) {
      try {
        const receipt = await api('/messages/' + pending.id);
        if (['completed','failed','cancelled','interrupted'].includes(receipt.status)) pending = null;
      } catch { /* Keep the original ID and never silently resubmit. */ }
    }
  } catch (error) {
    connected = false;
    if (!profile) notify(error.message, true);
  }
  renderConnection();
  pollTimer = setTimeout(poll, document.hidden ? 5000 : 1500);
}

function fillSettings(value) {
  draft = structuredClone(value); avatarDraft = structuredClone(value.avatar);
  for (const key of ['name','subtitle','greeting','persona','accent']) $('setting-' + key).value = value[key];
  $('setting-avatar').value = value.avatar.kind; $('setting-credit').value = value.avatar.credit;
  $('setting-detail').value = value.avatar.detail || 'detailed';
  $('detail-setting').hidden = value.avatar.kind !== 'procedural-moss';
  $('setting-split').value = Math.round(value.layout.split * 100); $('split-value').textContent = $('setting-split').value + '%';
  $('setting-animate').checked = value.layout.animate;
  $('avatar-file-note').textContent = value.avatar.asset_id ? '已保存资源：' + value.avatar.asset_id.slice(0,8) : '自包含 GLB ≤20MiB · 图片 ≤10MiB';
  $('settings-status').textContent = ''; $('settings-status').classList.remove('error');
}

function readSettings() {
  const value = structuredClone(draft);
  for (const key of ['name','subtitle','greeting','persona','accent']) value[key] = $('setting-' + key).value;
  value.name = value.name.trim();
  const kind = $('setting-avatar').value;
  const imported = kind === 'glb' || kind === 'image';
  if (imported && (!avatarDraft.asset_id || avatarDraft.kind !== kind)) throw new Error('请先导入与所选类型匹配的形象文件');
  value.avatar = {kind, detail: $('setting-detail').value, asset_id: imported ? avatarDraft.asset_id : null, credit: $('setting-credit').value};
  value.layout.split = Number($('setting-split').value) / 100;
  value.layout.animate = $('setting-animate').checked;
  value.layout.log_open = profile.layout.log_open;
  return value;
}

function settingsError(error) {$('settings-status').textContent = error.message; $('settings-status').classList.add('error');}

$('process-toggle').addEventListener('click', () => setProcess($('process-panel').hidden));
$('vision-toggle').addEventListener('click', () => setVision($('vision-workspace').hidden));
$('fullscreen').addEventListener('click', async () => {
  try {if (document.fullscreenElement) await document.exitFullscreen(); else await document.documentElement.requestFullscreen();}
  catch {notify('当前环境未开启全屏支持，可使用浏览器 F11');}
});
$('demo-toggle').addEventListener('click', startDemo); $('empty-demo').addEventListener('click', startDemo);
$('settings-open').addEventListener('click', () => {if (!profile) return; fillSettings(profile); $('settings').showModal();});
$('settings-close').addEventListener('click', () => $('settings').close());
$('setting-split').addEventListener('input', () => $('split-value').textContent = $('setting-split').value + '%');
$('settings-form').addEventListener('submit', async event => {
  event.preventDefault(); $('save-settings').disabled = true;
  try {
    await layoutWrite; const value = readSettings();
    applyProfile(await api('/config', value, 'PUT')); $('settings').close(); notify('角色设置已保存');
  } catch (error) {settingsError(error);} finally {$('save-settings').disabled = false;}
});
$('avatar-upload').addEventListener('click', () => $('avatar-file').click());
$('setting-avatar').addEventListener('change', () => {$('detail-setting').hidden = $('setting-avatar').value !== 'procedural-moss';});
$('avatar-file').addEventListener('change', async event => {
  const file = event.target.files[0]; if (!file) return;
  $('save-settings').disabled = true;
  try {
    const asset = await upload('/avatar', file, file.name.toLowerCase().endsWith('.glb') ? 20*1024**2 : 10*1024**2);
    avatarDraft = {kind:asset.kind, asset_id:asset.id, credit:$('setting-credit').value};
    $('setting-avatar').value = asset.kind; $('avatar-file-note').textContent = `${file.name} · 已导入，保存后生效`;
    $('detail-setting').hidden = true;
  } catch (error) {settingsError(error);} finally {$('save-settings').disabled = false; event.target.value = '';}
});
$('export-config').addEventListener('click', () => {
  try {
    const url = URL.createObjectURL(new Blob([JSON.stringify(readSettings(), null, 2)], {type:'application/json'}));
    const link = document.createElement('a'); link.href = url; link.download = 'robot-presence.json'; link.click(); setTimeout(() => URL.revokeObjectURL(url), 1000);
  } catch (error) {settingsError(error);}
});
$('import-config').addEventListener('click', () => $('config-file').click());
$('config-file').addEventListener('change', async event => {
  const file = event.target.files[0]; if (!file) return;
  try {if (file.size > 64000) throw new Error('配置文件过大'); fillSettings(JSON.parse(await file.text())); $('settings-status').textContent = '已读入，保存后生效';}
  catch {settingsError(new Error('配置格式无效，请使用本系统导出的 JSON'));} finally {event.target.value = '';}
});

for (const button of document.querySelectorAll('[data-filter]')) button.addEventListener('click', () => {
  filter = button.dataset.filter;
  document.querySelectorAll('[data-filter]').forEach(item => item.setAttribute('aria-pressed', String(item === button)));
  renderEvents();
});
$('follow-toggle').addEventListener('click', () => {
  follow = !follow; $('follow-toggle').setAttribute('aria-pressed', String(follow));
  $('follow-toggle').textContent = follow ? '跟随最新 ↓' : '回到最新 ↓';
  if (follow) $('event-scroll').scrollTop = $('event-scroll').scrollHeight;
});
$('event-scroll').addEventListener('wheel', event => {
  if (event.deltaY < 0) {follow = false; $('follow-toggle').setAttribute('aria-pressed','false'); $('follow-toggle').textContent = '回到最新 ↓';}
}, {passive:true});
$('event-scroll').addEventListener('touchmove', () => {follow = false; $('follow-toggle').setAttribute('aria-pressed','false'); $('follow-toggle').textContent = '回到最新 ↓';}, {passive:true});
$('event-scroll').addEventListener('scroll', () => {
  const element = $('event-scroll');
  if (follow && element.scrollHeight - element.scrollTop - element.clientHeight > 30) {
    follow = false; $('follow-toggle').setAttribute('aria-pressed','false'); $('follow-toggle').textContent = '回到最新 ↓';
  }
}, {passive:true});
$('analysis-image').addEventListener('load', layoutOverlays);
$('analysis-image').addEventListener('error', () => {if (frame) $('frame-caption').textContent = '输入图片已无法读取，保留事件文本。';});
new ResizeObserver(layoutOverlays).observe($('analysis-frame'));
$('overlay-toggle').addEventListener('click', () => {
  showOverlays = !showOverlays; $('overlay-toggle').setAttribute('aria-pressed', String(showOverlays));
  $('overlay-toggle').textContent = showOverlays ? '隐藏标注' : '显示标注'; layoutOverlays();
});
$('frame-zoom-open').addEventListener('click', () => {if (frame) {$('frame-zoom-image').src = frame.url; $('frame-zoom-title').textContent = '原始输入帧 / ' + frame.id; $('frame-zoom').showModal();}});
$('frame-zoom-close').addEventListener('click', () => $('frame-zoom').close());
$('camera-toggle').addEventListener('click', () => cameraOn ? stopCamera() : startCamera());
$('close-camera').addEventListener('click', () => {if (demo) stopDemo(); else stopCamera();});
$('camera-select').addEventListener('change', () => {if (cameraOn) startCamera();});
$('live-image').addEventListener('error', () => {if (cameraOn) {stopCamera(); notify('相机未连接或不可用；已关闭观看连接', true);}});
$('upload-image').addEventListener('click', () => $('image-file').click());
$('image-file').addEventListener('change', async event => {
  const file = event.target.files[0]; if (!file) return;
  stopDemo(); $('upload-image').disabled = true;
  try {
    const asset = await upload('/media', file, 10*1024**2);
    const result = await api('/analyze', {media_id:asset.id});
    // The stream/poll owns ordering. Display the result frame immediately only.
    showFrame(result.event); await refreshNow();
  } catch (error) {notify(error.message, true);} finally {$('upload-image').disabled = false; event.target.value = '';}
});

async function refreshNow() {const state = await api('/state'); ingest(state);}
$('message-form').addEventListener('submit', async event => {
  event.preventDefault();
  if (pending?.uncertain) {
    try {const value = await api('/messages/' + pending.id); notify('原请求状态：' + value.status); await refreshNow();}
    catch (error) {notify(error.message, true);} return;
  }
  const message = $('message-input').value.trim();
  if (!message || pending || demo) return;
  try {pending = {id:createRequestId(), message, uncertain:false};}
  catch (error) {notify(error.message, true); return;}
  renderConnection();
  try {await api('/messages', {id:pending.id, message}); $('message-input').value = ''; await refreshNow();}
  catch (error) {
    if (pending && (!error.status || error.status >= 500)) {
      pending.uncertain = true; notify(error.message + ' · 原请求 ID：' + pending.id, true);
    } else {pending = null; notify(error.message, true);}
  }
  renderConnection();
});

document.addEventListener('keydown', event => {
  if (event.ctrlKey || event.metaKey || event.altKey || $('settings').open || ['INPUT','TEXTAREA','SELECT'].includes(event.target.tagName)) return;
  if (event.key.toLowerCase() === 'f') {event.preventDefault(); $('fullscreen').click();}
  if (event.key.toLowerCase() === 'i') {event.preventDefault(); $('process-toggle').click();}
});
document.addEventListener('visibilitychange', () => {
  if (document.hidden) {stream?.close(); stopCamera(); stopDemo();}
  else {connectStream(); clearTimeout(pollTimer); poll();}
});
window.addEventListener('pagehide', () => {disposed = true; clearTimeout(pollTimer); stream?.close(); stopCamera(); demo?.stop(); avatar.dispose();});
window.addEventListener('pageshow', event => {if (event.persisted) location.reload();});

await poll(); connectStream();
