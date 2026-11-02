export class VoicePicker {
  constructor({player,api,selected,choose,notify}) {
    this.player=player;this.api=api;this.selected=selected;this.choose=choose;this.notify=notify;
    this.rows=[];this.recommended=false;this.chosen=null;
    const $=id=>document.getElementById(id);this.$=$;
    $('voices-close').onclick=()=>$('voices-dialog').close();
    $('voices-dialog').addEventListener('close',()=>player.stop());
    $('voice-search').oninput=()=>this.render();$('voice-language').onchange=()=>this.render();
    $('voice-recommended').onclick=()=>{this.recommended=!this.recommended;$('voice-recommended').setAttribute('aria-pressed',String(this.recommended));this.render();};
    $('voices-refresh').onclick=()=>this.load(true);
    $('voice-stop').onclick=()=>{player.stop();$('voice-preview-status').textContent='已停止试听';};
    $('voice-use').onclick=async()=>{
      if(!this.chosen)return;
      try {await choose(this.chosen);this.render();$('voice-preview-status').textContent='已选择此声线，下次播报生效';}
      catch(error){notify(error.message,true);}
    };
    $('voice-preview').onclick=()=>this.preview();
    $('voice-export').onclick=()=>{
      const url=URL.createObjectURL(new Blob([JSON.stringify({voices:this.rows},null,2)],{type:'application/json'}));
      const link=document.createElement('a');link.href=url;link.download='minimax-voices.json';link.click();setTimeout(()=>URL.revokeObjectURL(url),1000);
    };
  }
  async open() {
    const value=this.selected();this.chosen=value.voice_id;
    this.$('voice-speed').value=value.speed;this.$('voice-pitch').value=value.pitch;this.$('voice-emotion').value=value.emotion;
    this.$('voices-dialog').showModal();await this.load();
  }
  async load(refresh=false) {
    this.$('voice-catalogue-status').textContent='正在读取声线目录…';
    try {
      const value=await this.api(refresh?'/voices/refresh':'/voices',refresh?{}:undefined);
      this.rows=value.voices;this.$('voice-catalogue-status').textContent=`${value.total} 个声线 · ${value.source==='provider'?'当前账号目录':'目录快照'} · ${value.note}`;
      const select=this.$('voice-language'),previous=select.value;select.replaceChildren(new Option('全部语言',''));
      for(const language of [...new Set(this.rows.map(v=>v.language))].sort())select.add(new Option(language,language));
      select.value=previous;this.$('voice-preview').disabled=!value.available;
      this.render();
    }catch(error){this.$('voice-catalogue-status').textContent=error.message;}
  }
  render() {
    const query=this.$('voice-search').value.trim().toLowerCase(),language=this.$('voice-language').value;
    const rows=this.rows.filter(v=>(!this.recommended||v.recommended)&&(!language||v.language===language)&&
      (!query||[v.id,v.name,v.provider_name,v.description].join(' ').toLowerCase().includes(query)));
    rows.sort((a,b)=>Number(b.recommended)-Number(a.recommended));
    this.$('voice-result-count').textContent=`显示 ${rows.length} / ${this.rows.length}`;
    const fragment=document.createDocumentFragment();
    for(const voice of rows) {
      const row=document.createElement('button');row.type='button';row.className='voice-row';row.dataset.voice=voice.id;
      row.setAttribute('aria-pressed',String(voice.id===this.chosen));
      const title=document.createElement('strong');title.textContent=voice.name+(voice.id===this.selected().voice_id?' · 当前':'');
      const tag=document.createElement('span');tag.className='voice-tag';tag.textContent=[voice.language,voice.recommended?'MOSS 方向候选':'',voice.gender==='male'?'男声':voice.gender==='female'?'女声':''].filter(Boolean).join(' · ');
      const description=document.createElement('span');description.className='voice-description';description.textContent=voice.hint||voice.description;
      const id=document.createElement('code');id.textContent=voice.id;
      row.append(title,tag,description,id);row.onclick=()=>{this.chosen=voice.id;this.render();};fragment.append(row);
    }
    if(!rows.length){const empty=document.createElement('p');empty.textContent='没有匹配的声线';fragment.append(empty);}
    this.$('voice-list').replaceChildren(fragment);
    const selected=this.rows.find(v=>v.id===this.chosen);
    this.$('voice-selected-name').textContent=selected?.name||'请选择声线';
    this.$('voice-use').disabled=!selected;
  }
  async preview() {
    if(!this.chosen)return;
    this.$('voice-preview-status').textContent='正在生成试听…';
    try {
      await this.player.preview({voice_id:this.chosen,text:this.$('voice-sample-text').value,
        speed:Number(this.$('voice-speed').value),pitch:Number(this.$('voice-pitch').value),emotion:this.$('voice-emotion').value});
      this.$('voice-preview-status').textContent='试听结束，可选择下一声线';
    }catch(error){this.$('voice-preview-status').textContent=error.message;}
  }
  settings() {return {voice_id:this.chosen,speed:Number(this.$('voice-speed').value),pitch:Number(this.$('voice-pitch').value),emotion:this.$('voice-emotion').value};}
}
