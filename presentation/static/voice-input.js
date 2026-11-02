import {PcmResampler} from './audio-resampler.js';

const LOCAL_NAMES=new Set(['127.0.0.1','localhost','[::1]','::1']);
const VIRTUAL=/stereo mix|立体声混音|混音|pico|virtual|voicemeeter|cable|loopback/i;
const STATES={off:'麦克风关闭',preparing:'正在准备本地识别…',armed:'等你叫我',listening:'我在听',
  'playback-paused':'播报中 · 唤醒暂停',fault:'需要重新启用'};

export class VoiceInputController {
  constructor({api,profile,saveEnabled,notify,onWake=()=>{},onCommand=()=>{},onSubmitted=()=>{}}){
    Object.assign(this,{api,profile,saveEnabled,notify,onWake,onCommand,onSubmitted});
    this.$=id=>document.getElementById(id);this.generation=0;this.seq=0;this.owned=false;
    this.local=LOCAL_NAMES.has(location.hostname)&&isSecureContext;
    this.owner=crypto.randomUUID?.().replaceAll('-','')||`browser-${Date.now()}-${Math.random().toString(36).slice(2)}`;
    this.$('mic-toggle').addEventListener('click',()=>void this.toggle());
    this.$('mic-close').addEventListener('click',()=>void this.disable());
    this.$('mic-settings-close').addEventListener('click',()=>this.$('mic-settings').close());
    this.$('mic-permission').addEventListener('click',()=>void this.enumerate());
    this.$('mic-start').addEventListener('click',()=>void this.start());
    this.$('mic-confirm').addEventListener('click',()=>void this.confirm());
    this.$('mic-discard').addEventListener('click',()=>this.cancel());
    const ptt=this.$('mic-ptt');
    ptt.addEventListener('pointerdown',event=>{event.preventDefault();if(!this.owned||this.paused)return;ptt.setPointerCapture(event.pointerId);this.ptt=true;this.send('begin');});
    const end=()=>{if(this.ptt){this.ptt=false;this.send('end');}};
    for(const name of ['pointerup','pointercancel','lostpointercapture'])ptt.addEventListener(name,end);
    ptt.addEventListener('keydown',event=>{if([' ','Enter'].includes(event.key)&&!event.repeat){event.preventDefault();this.ptt=true;this.send('begin');}});
    ptt.addEventListener('keyup',event=>{if([' ','Enter'].includes(event.key)){event.preventDefault();end();}});
    ptt.addEventListener('blur',end);
    navigator.mediaDevices?.addEventListener('devicechange',()=>void this.deviceChanged());
  }
  configure(profile){
    const fingerprint=JSON.stringify({name:profile.name,persona:profile.persona,input:profile.voice_input});
    if(this.fingerprint&&fingerprint!==this.fingerprint&&this.owned)void this.stop('设置已变化，请重新启用');
    this.fingerprint=fingerprint;this.render();
  }
  render(status=this.serverStatus){
    if(status)this.serverStatus=status;
    const enabled=Boolean(this.profile()?.voice_input?.enabled);
    const active=this.owned||status?.active;
    this.$('mic-toggle').setAttribute('aria-pressed',String(Boolean(enabled&&active)));
    this.$('mic-toggle').textContent=this.owned?'语音输入 开启':active?'语音输入 本机监听':enabled?'启用麦克风':'语音输入 关闭';
    this.$('mic-ptt').disabled=!this.owned||this.paused;
    this.$('mic-strip').hidden=!enabled&&!active&&!this.final;
    this.$('mic-strip').dataset.state=this.paused?'paused':this.owned?'active':'off';
    if(!this.owned)this.$('mic-state').textContent=active?'笔记本正在监听':enabled?'等待在本机启用':'麦克风关闭';
    this.$('mic-close').hidden=!enabled;
  }
  async toggle(){
    if(this.owned){await this.disable();return;}
    this.$('mic-settings').showModal();
    this.$('mic-start').disabled=true;
    this.$('mic-detail').textContent=this.local?'请选择真实麦克风，授权只用于本机语音识别。':'请在笔记本打开 http://127.0.0.1:8770/presence/ 使用麦克风。';
    this.$('mic-permission').disabled=!this.local;
    if(this.local)await this.enumerate(false);
  }
  async enumerate(authorize=true){
    let permission;
    try{
      if(!this.local||!navigator.mediaDevices?.getUserMedia)throw new Error('当前页面无法采集，请使用笔记本本机入口');
      if(authorize){permission=await navigator.mediaDevices.getUserMedia({audio:{channelCount:1,echoCancellation:true,noiseSuppression:true,autoGainControl:true}});permission.getTracks().forEach(track=>track.stop());permission=null;}
      const devices=(await navigator.mediaDevices.enumerateDevices()).filter(device=>device.kind==='audioinput'&&device.deviceId!=='default'&&device.deviceId!=='communications');
      const select=this.$('mic-device');select.replaceChildren(new Option('选择真实麦克风…',''));
      for(const device of devices){
        const virtual=VIRTUAL.test(device.label);const option=new Option((device.label||'请先授权以显示设备名')+(virtual?' · 回放/虚拟输入不可选':''),device.deviceId);
        option.disabled=virtual||!device.label;select.add(option);
      }
      let saved;try{saved=localStorage.getItem('presence-microphone');}catch{}
      if(saved&&[...select.options].some(x=>x.value===saved&&!x.disabled))select.value=saved;
      const update=()=>this.$('mic-start').disabled=!select.value;
      select.onchange=update;update();
      if(authorize)this.$('mic-detail').textContent='授权完成。请选择笔记本的麦克风阵列，再点击开始监听。';
    }catch(error){this.$('mic-detail').textContent=error.name==='NotAllowedError'?'麦克风许可被拒绝；可在浏览器地址栏重新授权。':error.message;}
    finally{permission?.getTracks().forEach(track=>track.stop());}
  }
  send(op){if(this.socket?.readyState===WebSocket.OPEN)this.socket.send(JSON.stringify({op}));}
  async start(){
    if(this.starting)return;this.starting=true;const generation=++this.generation;
    const id=this.$('mic-device').value;
    if(!id){this.starting=false;return;}
    this.$('mic-start').disabled=true;this.$('mic-detail').textContent='正在准备本地识别与音频连接…';
    let media;
    try{
      // Unlock the capture graph in the original click, before permission,
      // model loading or network awaits consume transient user activation.
      this.context=new AudioContext({latencyHint:'interactive'});
      const contextReady=this.context.resume();contextReady.catch(()=>{});
      media=await navigator.mediaDevices.getUserMedia({audio:{deviceId:{exact:id},channelCount:1,echoCancellation:true,noiseSuppression:true,autoGainControl:true},video:false});
      if(generation!==this.generation){media.getTracks().forEach(t=>t.stop());return;}
      this.media=media;
      if(!this.profile().voice_input.enabled)await this.saveEnabled(true);
      const claim=await this.api('/voice-input/claim',{owner_id:this.owner,takeover:this.$('mic-takeover').checked});
      if(generation!==this.generation){await this.api('/voice-input/release',{owner_id:claim.owner_id,capture_epoch:claim.capture_epoch,token:claim.token});return;}
      this.lease={owner_id:claim.owner_id,capture_epoch:claim.capture_epoch,token:claim.token};
      this.socket=new WebSocket(`${location.protocol==='https:'?'wss':'ws'}://${location.host}/api/presence/voice-input/stream`);
      const socket=this.socket;
      await new Promise((resolve,reject)=>{
        const timeout=setTimeout(()=>reject(new Error('音频连接超时')),3000);
        socket.onopen=()=>socket.send(JSON.stringify(this.lease));
        socket.onerror=()=>{clearTimeout(timeout);reject(new Error('音频连接失败'));};
        socket.onclose=()=>{clearTimeout(timeout);reject(new Error('采集租约已失效'));};
        socket.onmessage=event=>{const value=JSON.parse(event.data);if(value.kind==='voice.ready'){clearTimeout(timeout);resolve();}};
      });
      this.heartbeat=setInterval(()=>this.send('heartbeat'),900);
      this.lastHeartbeat=performance.now();
      socket.onmessage=event=>{if(JSON.parse(event.data).kind==='heartbeat')this.lastHeartbeat=performance.now();};
      socket.onclose=()=>{if(this.socket===socket)void this.stop('连接已关闭，需显式重新启用');};
      socket.onerror=()=>{if(this.socket===socket)void this.stop('音频连接中断');};
      await Promise.race([contextReady,new Promise((_,reject)=>setTimeout(()=>reject(new Error('音频采样未启动，请重新点击启用麦克风')),5000))]);
      let worklet=true;
      try{
        await Promise.race([this.context.audioWorklet.addModule(new URL('./mic-worklet.js',import.meta.url).href),
          new Promise((_,reject)=>setTimeout(()=>reject(new Error('音频模块加载超时')),3000))]);
      }catch{
        // Some Windows embedded/headless browsers expose AudioWorklet but never
        // load its module. A bounded compatibility graph uses the same filter;
        // transport backpressure still invalidates delayed/partial utterances.
        worklet=false;
      }
      if(generation!==this.generation)return;
      if(worklet){
        this.node=new AudioWorkletNode(this.context,'presence-microphone',{numberOfInputs:1,numberOfOutputs:1,outputChannelCount:[1],channelCount:1,channelCountMode:'explicit'});
      }else{
        this.node=this.context.createScriptProcessor(1024,1,1);
        const port={enabled:true,postMessage:value=>{port.enabled=value.enabled;resampler.reset();}};
        const resampler=new PcmResampler(this.context.sampleRate);
        this.node.port=port;
        this.node.onaudioprocess=event=>{
          event.outputBuffer.getChannelData(0).fill(0);
          if(port.enabled)resampler.push(event.inputBuffer.getChannelData(0),(pcm,level)=>port.onmessage?.({data:{pcm,level}}));
        };
      }
      this.source=this.context.createMediaStreamSource(media);this.silent=this.context.createGain();this.silent.gain.value=0;
      this.source.connect(this.node);this.node.connect(this.silent);this.silent.connect(this.context.destination);
      this.seq=0;this.owned=true;this.paused=false;
      this.node.port.onmessage=event=>{
        if(!this.owned||this.paused||generation!==this.generation)return;
        if(this.socket?.readyState!==WebSocket.OPEN||this.socket.bufferedAmount>16000||performance.now()-this.lastHeartbeat>2800){void this.stop('音频传输中断，请重新启用');return;}
        const frame=new Uint8Array(644);new DataView(frame.buffer).setUint32(0,this.seq++,true);frame.set(new Uint8Array(event.data.pcm),4);this.socket.send(frame);
        this.level=event.data.level;this.$('mic-meter').value=Math.min(1,this.level*8);
      };
      media.getTracks().forEach(track=>track.onended=()=>void this.stop('麦克风已断开，请重新选择'));
      try{localStorage.setItem('presence-microphone',id);}catch{}
      const settings=media.getAudioTracks()[0].getSettings();
      this.$('mic-device-note').textContent=`${media.getAudioTracks()[0].label} · ${settings.sampleRate||this.context.sampleRate} Hz → 16 kHz${worklet?'':' · 兼容采样模式'} · 回声消除${settings.echoCancellation?'已请求并启用':'由设备决定'}`;
      this.$('mic-state').textContent=claim.status.wake_ready?'等你叫我：'+claim.status.wake_names.join(' / '):'按住说话可用 · 请设置可发音别名';
      if(claim.status.unsupported_names.length)this.notify('未支持的唤醒名：'+claim.status.unsupported_names.join('、')+'；可在设置中添加发音别名');
      this.$('mic-settings').close();this.render();
    }catch(error){this.$('mic-detail').textContent=error.message;await this.stop(error.message);}
    finally{this.starting=false;this.$('mic-start').disabled=!this.$('mic-device').value;}
  }
  async stop(reason='麦克风关闭'){
    ++this.generation;this.owned=false;this.ptt=false;clearInterval(this.heartbeat);clearTimeout(this.echoTimer);
    this.media?.getTracks().forEach(track=>{track.onended=null;track.stop();});this.media=null;
    if(this.node?.onaudioprocess)this.node.onaudioprocess=null;
    this.node?.disconnect();this.source?.disconnect();this.silent?.disconnect();this.node=null;this.source=null;this.silent=null;
    const context=this.context;this.context=null;if(context&&context.state!=='closed')void context.close();
    const socket=this.socket;this.socket=null;if(socket){socket.onclose=null;socket.onerror=null;socket.close();}
    const lease=this.lease;this.lease=null;this.final=null;this.paused=false;
    this.$('mic-transcript').textContent='';this.$('mic-confirmation').hidden=true;this.$('mic-meter').value=0;
    if(lease)try{await this.api('/voice-input/release',lease);}catch{}
    this.render();this.$('mic-state').textContent=reason;
  }
  async disable(){await this.stop();try{await this.saveEnabled(false);}catch(error){this.notify(error.message,true);}this.render();}
  cancel(){this.send('cancel');this.final=null;this.$('mic-confirmation').hidden=true;this.$('mic-transcript').textContent='已取消本次采集';}
  async confirm(){
    if(!this.final||!this.lease)return;
    this.$('mic-confirm').disabled=true;
    try{
      const result=await this.api('/voice-input/confirm',{...this.lease,request_id:this.final.request_id,text:this.$('mic-edit').value});
      if(result.status==='waiting-confirmation')return;
      this.$('mic-confirmation').hidden=true;this.final=null;
      if(!['local','accepted'].includes(result.status))this.onSubmitted(result.id,result.status);
    }catch(error){this.notify(error.message,true);}
    finally{this.$('mic-confirm').disabled=false;}
  }
  event(value,baseline=false){
    if(value.kind==='voice.status'){this.render(value);if(this.owned&&value.capture_epoch!==this.lease?.capture_epoch)void this.stop('角色或采集窗口已变化，请重新启用');if(this.owned)this.$('mic-state').textContent=STATES[value.state]||value.state;return;}
    if(baseline)return;
    if(value.capture_epoch&&value.capture_epoch!==this.lease?.capture_epoch)return;
    if(value.kind==='voice.wake'){this.onWake(value);this.$('mic-state').textContent='我在听';this.$('mic-transcript').textContent='';}
    if(value.kind==='asr.partial')this.$('mic-transcript').textContent=value.text||'识别中…';
    if(value.kind==='asr.final'){
      this.$('mic-transcript').textContent=value.text;this.final=value;
      this.$('mic-edit').value=value.text;this.$('mic-confirmation').hidden=false;
    }
    if(value.kind==='voice.submitted'){
      this.$('mic-confirmation').hidden=true;this.final=null;this.onSubmitted(value.request_id,value.status);
    }
    if(value.kind==='voice.empty'||value.kind==='voice.error'){this.$('mic-transcript').textContent=value.text;if(value.uncertain)this.onSubmitted(value.request_id,'uncertain');}
    if(value.kind==='voice.command'){this.final=null;this.$('mic-confirmation').hidden=true;this.onCommand(value.action);}
  }
  playback(active,ack=false){
    if(!this.owned)return;clearTimeout(this.echoTimer);
    if(!active&&!this.paused)return;
    if(active){this.paused=true;this.node?.port.postMessage({enabled:false});this.send(ack?'ack-pause':'pause');this.$('mic-state').textContent='播报中 · 唤醒暂停';this.render();}
    else this.echoTimer=setTimeout(()=>{if(!this.owned)return;this.send(ack?'ack-resume':'resume');this.node?.port.postMessage({enabled:true});this.paused=false;this.render();},300);
  }
  async deviceChanged(){
    if(!this.owned)return;
    try{const id=this.media?.getAudioTracks()[0]?.getSettings().deviceId;const devices=await navigator.mediaDevices.enumerateDevices();if(!devices.some(x=>x.deviceId===id))await this.stop('选定麦克风已离线，请重新选择');}
    catch{await this.stop('麦克风许可已变化，请重新启用');}
  }
}
