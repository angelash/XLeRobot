// Entirely synthetic, local geometry. No camera, model, or robot calls.
export class Demo {
  constructor(canvas, publish) {this.canvas = canvas; this.publish = publish; this.seq = 0;}
  event(kind, phase, title, text, extra = {}) {
    this.publish({version: 1, event_id: `demo-${++this.seq}`, seq: this.seq, epoch: 'demo',
      received_at: new Date().toISOString(), kind, phase, title, text,
      evidence: 'demo', source: 'synthetic-demo', task_id: 'demo-observe', ...extra});
  }
  draw(time) {
    const ctx = this.canvas.getContext('2d');
    const x = 178 + Math.sin(time / 2600) * 75;
    ctx.fillStyle = '#27363e'; ctx.fillRect(0, 0, 640, 360);
    ctx.fillStyle = '#3c515b'; ctx.fillRect(0, 0, 640, 125);
    ctx.strokeStyle = '#465a64'; ctx.lineWidth = 1;
    for (let i = 0; i < 7; i++) {ctx.beginPath(); ctx.moveTo(i * 110 - 140, 360); ctx.lineTo(i * 70 + 70, 125); ctx.stroke();}
    for (let i = 0; i < 5; i++) {ctx.beginPath(); ctx.moveTo(0, 160 + i * 46); ctx.lineTo(640, 160 + i * 46); ctx.stroke();}
    ctx.fillStyle = '#c6b691'; ctx.fillRect(x, 185, 95, 85);
    ctx.fillStyle = '#e1d1b0'; ctx.beginPath(); ctx.moveTo(x,185); ctx.lineTo(x+23,167); ctx.lineTo(x+118,167); ctx.lineTo(x+95,185); ctx.closePath();ctx.fill();
    ctx.fillStyle = '#8f826b';ctx.beginPath();ctx.moveTo(x+95,185);ctx.lineTo(x+118,167);ctx.lineTo(x+118,250);ctx.lineTo(x+95,270);ctx.closePath();ctx.fill();
    ctx.fillStyle = '#7d9d9d';ctx.fillRect(422,203,100,51);ctx.fillStyle = '#a4bdb8';ctx.fillRect(432,193,80,16);
    ctx.fillStyle = '#182a32';ctx.fillRect(34,22,224,29);ctx.fillStyle = '#e6d3a4';ctx.font = '12px Consolas, monospace';ctx.fillText('SYNTHETIC CAMERA / DEMO',46,41);
    ctx.fillStyle = '#c1d0d6';ctx.font = '11px Consolas, monospace';ctx.fillText(`FRAME ${String(this.frame || 0).padStart(5,'0')}`,34,335);
    return [{label: '合成方块 · 演示标注', x: x/640, y: 167/360, w: 118/640, h: 103/360},
      {label: '合成工具盒 · 演示标注', x: 422/640, y: 193/360, w: 100/640, h: 61/360}];
  }
  start() {
    this.started = performance.now(); this.frame = 0;
    this.event('task','started','开始观察桌面','演示任务：展示一次取帧、分析和回应的过程。');
    this.event('summary','progress','先确认输入画面','使用本地生成的合成场景，不打开真实相机。');
    this.videoTimer = setInterval(() => {this.frame++; this.draw(performance.now() - this.started);},125);
    this.draw(0);
    let step = 0;
    this.eventTimer = setInterval(() => {
      step++;
      if (step === 1) this.event('tool','started','取得一帧演示画面','输入来源：本地合成视频。');
      if (step >= 2) {
        const overlays = this.draw(performance.now() - this.started);
        this.event('vision','completed','已更新分析输入帧','框来自合成场景的已知几何位置，用于演示帧与结果的对应关系；并非视觉模型识别。',
          {frame_id: `demo-frame-${this.frame}`, demo_url: this.canvas.toDataURL('image/png'),
            captured_at: new Date().toISOString(), overlays});
      }
      if (step === 3) this.event('message','completed','观察结果','演示画面中有一个方块和一个工具盒。输入帧与标注已保留在右侧。真实任务会在接入的服务中执行。');
    },2000);
  }
  stop() {clearInterval(this.videoTimer);clearInterval(this.eventTimer);}
}
