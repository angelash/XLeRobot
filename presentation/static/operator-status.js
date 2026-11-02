// This view never sends commands and never reads the workstation's Gamepad API.
export function describeOperator(value) {
  const unknown = !value || value.connected == null || value.phase === 'unknown';
  if (unknown) return {state:'unknown',device:'手柄状态未知',session:'接管未知',mode:'',hint:'等待状态',reason:value?.reason||'连接状态流…'};
  const connected = value.connected && value.input_fresh;
  const sessions = {offline:'未接管','waiting-release':'等待松开',starting:'准备中',stopping:'结束中',
    active:'控制中',paused:'已暂停',blocked:'启动受阻'};
  const modes = {original_hardware:'整机联动',original_preset:'预设直控',original_joint:'关节试控',
    original_preview:'运动学预览',diagnostic:'单关节调试'};
  let mode = value.active ? modes[value.mode]||'控制模式未知' : '整机联动优先';
  if (value.mode === 'original_preset' && value.fallback_reason) mode += ' · 联动回退';
  const reason = value.reason || value.fallback_reason || (!value.whole_ready ? value.setup_reason : '') || '';
  let hint = !connected ? '等待连接' : value.active ? 'Menu 取消' : 'Menu 开始';
  if (value.phase === 'waiting-release') hint = '松开 Menu';
  if (value.phase === 'starting') hint = 'Menu 取消准备';
  if (value.phase === 'blocked') hint = '回中后再按 Menu';
  return {state:value.phase==='blocked'||value.phase==='paused'||(value.mode==='original_preset'&&value.fallback_reason)?'warning':value.active?'active':connected?'connected':'offline',
    device:connected?'手柄已连接':value.connected?'手柄输入超时':'手柄未连接',
    session:sessions[value.phase]||'状态未知',mode,hint,reason};
}

export class OperatorStatus {
  constructor(host, {createSource=url=>new EventSource(url), timeout=7500}={}) {
    this.host=host;this.createSource=createSource;this.timeout=timeout;this.value=null;
    this.render(null);
  }
  render(value) {
    const state=describeOperator(value);this.value=value;
    this.host.dataset.state=state.state;
    for(const key of ['device','session','mode','hint'])this.host.querySelector('[data-operator='+key+']').textContent=state[key];
    this.host.title=[state.device,state.session,state.mode,state.reason,state.hint].filter(Boolean).join(' · ');
  }
  start() {
    if(this.source)return;
    this.render(null);
    const source=this.createSource('/api/presence/operator-stream');this.source=source;
    const receive=event=>{
      if(this.source!==source)return;
      try {
        const value=JSON.parse(event.data);
        if(value.version!==1)throw new Error('Unsupported operator state');
        clearTimeout(this.timer);
        if(JSON.stringify(value)!==JSON.stringify(this.value))this.render(value);
        this.timer=setTimeout(()=>this.render({connected:null,phase:'unknown',reason:'状态流超过时限未更新'}),this.timeout);
      } catch {this.render({connected:null,phase:'unknown',reason:'控制状态无法读取'});}
    };
    source.addEventListener('operator',receive);source.addEventListener('heartbeat',receive);
    source.onerror=()=>{if(this.source===source)this.render({connected:null,phase:'unknown',reason:'控制状态流断开；实机接管状态未知'});};
  }
  stop() {clearTimeout(this.timer);this.source?.close();this.source=null;this.render(null);}
}
