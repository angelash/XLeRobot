export class SpeechPlayer {
  constructor({audio,request=(...args)=>fetch(...args),status=()=>{},error=()=>{},blocked=()=>{}}) {
    this.audio=audio;this.request=request;this.status=status;this.error=error;this.blocked=blocked;
    this.settings={enabled:false,volume:.8};this.queue=[];this.seen=new Set();this.generation=0;this.armed=false;
    this.streamedRequests=new Set();this.nextParts=new Map();
    this.mutedRequests=new Set();
    this.fallbackRequests=new Set();
  }
  configure(settings) {
    if(JSON.stringify(this.settings)!==JSON.stringify(settings))this.stop();
    this.settings={...settings};this.audio.volume=settings.volume??.8;
    this.status(!settings.enabled?'语音关闭 · 文字正常':this.armed?'语音待机':'语音待启用 · 点击上方语音按钮');
  }
  async arm() {
    // A short silent WAV unlocks this audio element during the user's gesture.
    this.audio.src='data:audio/wav;base64,UklGRiYAAABXQVZFZm10IBAAAAABAAEAgD4AAAB9AAACABAAZGF0YQIAAAAAAA==';
    try {await this.audio.play();this.audio.pause();this.armed=true;}
    catch {this.armed=true;} // The real play() still reports any autoplay block.
  }
  observe(events,{epoch,baseline=false,demo=false}={}) {
    if(this.epoch!==epoch){this.stop();this.seen.clear();baseline=true;this.epoch=epoch;}
    for(const event of events) {
      if(this.seen.has(event.event_id))continue;
      this.seen.add(event.event_id);
      if(baseline||demo||event.evidence==='demo'||!this.settings.enabled||!this.armed)continue;
      if(event.kind!=='message'||event.phase!=='completed'||!event.text?.trim())continue;
      if(this.streamedRequests.has(event.task_id))continue;
      if(!event.received_at||Date.now()-Date.parse(event.received_at)>20000)continue;
      if(event.task_id)this.fallbackRequests.add(event.task_id);
      this.queue.push({epoch,event_id:event.event_id,task_id:event.task_id});
    }
    while(this.seen.size>1000)this.seen.delete(this.seen.values().next().value);
    while(this.fallbackRequests.size>128)this.fallbackRequests.delete(this.fallbackRequests.values().next().value);
    if(!this.busy&&this.queue.length)void this.drain();
  }
  observeLive(value,{baseline=false}={}) {
    if(!value.kind?.startsWith('reply.'))return;
    const id=value.request_id;
    if(['reply.cancelled','reply.failed','reply.interrupted'].includes(value.kind)){
      if(this.currentRequest===id)this.stop();
      this.queue=this.queue.filter(x=>x.request_id!==id&&x.task_id!==id);return;
    }
    // A receipt poll can beat SSE delivery. The first observed path owns this
    // request's audio; never start sentence playback after whole-reply fallback.
    if(this.fallbackRequests.has(id))return;
    const key=id+':'+value.generation_id,start=this.nextParts.get(key)||0;
    this.nextParts.set(key,value.part_count||0);this.streamedRequests.add(id);
    if(!baseline&&!this.mutedRequests.has(id)&&this.settings.enabled&&this.armed&&Date.now()/1000-value.updated_at<20){
      for(let part=start;part<(value.part_count||0);part++)this.queue.push({request_id:id,generation_id:value.generation_id,part});
      if(!this.busy&&this.queue.length)void this.drain();
      else if(this.playing&&!this.prefetched&&this.queue[0]?.request_id){
        const item=this.queue[0],promise=this.fetchAudio('/api/presence/speech/reply',item,this.generation);promise.catch(()=>{});this.prefetched={item,promise};
      }
    }
    while(this.nextParts.size>128)this.nextParts.delete(this.nextParts.keys().next().value);
    while(this.streamedRequests.size>128)this.streamedRequests.delete(this.streamedRequests.values().next().value);
  }
  stop() {
    for(const item of this.queue)if(item.request_id)this.mutedRequests.add(item.request_id);
    if(this.currentRequest)this.mutedRequests.add(this.currentRequest);
    while(this.mutedRequests.size>128)this.mutedRequests.delete(this.mutedRequests.values().next().value);
    this.generation++;this.queue=[];this.busy=false;this.controller?.abort();this.controller=null;this.prefetched=null;this.currentRequest=null;
    this.audio.pause();this.audio.removeAttribute('src');this.audio.load();
    if(this.url)URL.revokeObjectURL(this.url);this.url=null;
    this.finish?.();this.finish=null;
  }
  async fetchAudio(path,body,generation) {
    const controller=new AbortController();this.controller=controller;
    const response=await this.request(path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body),signal:controller.signal});
    if(generation!==this.generation)return null;
    if(!response.ok){let data;try{data=await response.json();}catch{}throw new Error(data?.detail||'语音暂不可用，文字已保留');}
    if(response.status===204)return {parts:0};
    const blob=await response.blob();if(generation!==this.generation)return null;
    return {blob,parts:Number(response.headers.get('X-Speech-Parts')||1)};
  }
  async play(blob,generation) {
    if(this.url)URL.revokeObjectURL(this.url);
    this.url=URL.createObjectURL(blob);this.audio.src=this.url;
    return new Promise((resolve,reject)=>{
      let settled=false;
      const clean=()=>{this.audio.onended=null;this.audio.onerror=null;if(this.finish===finish)this.finish=null;};
      const finish=()=>{if(settled)return;settled=true;clean();resolve();};
      const fail=error=>{if(settled)return;settled=true;clean();reject(error);};
      this.finish=finish;this.audio.onended=finish;this.audio.onerror=()=>fail(new Error('语音无法播放，文字已保留'));
      this.audio.play().catch(error=>{if(generation!==this.generation)finish();else fail(error);});
    });
  }
  async drain() {
    const generation=this.generation;this.busy=true;
    try {
      while(this.queue.length&&generation===this.generation&&this.settings.enabled) {
        const item=this.queue.shift();this.currentRequest=item.request_id||item.task_id;let parts=1;
        for(let part=0;part<parts;part++) {
          this.status(part?'继续生成语音…':'正在生成语音…');
          const path=item.request_id?'/api/presence/speech/reply':'/api/presence/speech';
          const body=item.request_id?item:{...item,part};
          const cached=this.prefetched?.item===item?this.prefetched.promise:null;this.prefetched=null;
          const result=await (cached||this.fetchAudio(path,body,generation));
          if(!result||generation!==this.generation)return;
          parts=result.parts;if(!result.blob)break;
          this.status(`正在播报${parts>1?` · ${part+1}/${parts}`:''}`);
          const next=this.queue[0];
          if(next?.request_id){const promise=this.fetchAudio('/api/presence/speech/reply',next,generation);promise.catch(()=>{});this.prefetched={item:next,promise};}
          this.playing=true;
          try{await this.play(result.blob,generation);}finally{this.playing=false;}
          if(generation!==this.generation)return;
        }
      }
      if(generation===this.generation)this.status('语音待机');
    } catch(error) {
      if(generation===this.generation&&error.name!=='AbortError') {
        this.queue=[];
        if(error.name==='NotAllowedError'){this.armed=false;this.blocked();this.status('点击语音按钮启用声音 · 文字已保留');}
        else {this.error(error.message);this.status('语音暂不可用 · 文字正常');}
      }
    } finally {if(generation===this.generation)this.busy=false;}
  }
  async acknowledgement(shouldPlay=()=>true) {
    if(!this.settings.enabled||!this.armed||this.busy||!shouldPlay())return;
    const generation=this.generation;
    try{
      const response=await this.request('/api/presence/voice-input/ack');
      if(!response.ok||response.status===204)return;
      const blob=await response.blob();
      if(generation!==this.generation||this.busy||!shouldPlay())return;
      this.busy=true;this.acknowledging=true;await this.play(blob,generation);
    }catch{/* Cache misses stay visual. */}
    finally{this.acknowledging=false;if(generation===this.generation){this.busy=false;if(this.queue.length)void this.drain();}}
  }
  async preview(body) {
    this.stop();await this.arm();const generation=this.generation;
    try {
      this.status('正在准备试听…');
      const result=await this.fetchAudio('/api/presence/speech/preview',body,generation);
      if(!result||generation!==this.generation)return;
      this.status('正在试听');await this.play(result.blob,generation);
      if(generation===this.generation)this.status(this.settings.enabled?'语音待机':'试听结束 · 自动语音仍关闭');
    } catch(error) {if(generation===this.generation&&error.name!=='AbortError'){this.status('试听未完成');throw error;}}
  }
}
