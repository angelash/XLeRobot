'use strict';
// Shared Canvas projection. Normal page updates run in an OffscreenCanvas
// worker so detailed model painting cannot stall XR tracking or input leases.
if (typeof importScripts === 'function' && typeof document === 'undefined')
  importScripts('vr-mapping.js', 'vr-model.js');
(function(root) {
  const M = typeof module !== 'undefined' && module.exports ? require('./vr-mapping.js') : root.VRMapping;
  const Model = typeof module !== 'undefined' && module.exports ? require('./vr-model.js') : root.VRModel;
  function draw(canvas, sim, options = {}) {
    const g = canvas.getContext('2d'), W = canvas.width, H = canvas.height;
    g.fillStyle = '#11161c'; g.fillRect(0, 0, W, H);
    g.font = 'bold 30px "Microsoft YaHei", "Segoe UI", sans-serif'; g.fillStyle = '#e8edf2';
    g.fillText(options.live ? '双臂三视图 · 实机目标与反馈' : '双臂三视图 · 预演', 24, 42);
    g.font = '24px "Microsoft YaHei", "Segoe UI", sans-serif';
    g.fillStyle = '#60a5fa'; g.fillText('左臂', W - 280, 42);
    g.fillStyle = '#fb923c'; g.fillText('右臂', W - 180, 42);
    const views = [['front', '正视 · 左右／上下'], ['side', '侧视 · 前后／上下'], ['top', '俯视 · 左右／前后']];
    const models = Object.fromEntries(['left', 'right'].map(side => [side, sim[side] ? Model.arm(side, sim[side]) : null]));
    const gap = 16, boxW = (W - 4 * gap) / 3, boxH = H - 170;
    const depth = (p,view) => view === 'front' ? p[0] : view === 'side' ? -p[1] : p[2];
    function solid(faces, pixel, view, overlay = false) {
      // Orthographic projection only needs the camera-facing surfaces. Cache
      // each depth once instead of recomputing it during every sort comparison.
      const sorted = faces.filter(f => depth(f.normal,view)>1e-7)
        .map(f => ({face:f,z:f.points.reduce((s,p)=>s+depth(p,view),0)/f.points.length}))
        .sort((a,b)=>a.z-b.z);
      for (const {face} of sorted) {
        const pts = face.points.map(pixel);
        const rgb = Model.shade(face.color, face.normal);
        g.globalAlpha = overlay ? .66 : 1;
        g.fillStyle = `rgb(${rgb.join(',')})`; g.strokeStyle = '#111923'; g.lineWidth = .8;
        g.beginPath(); pts.forEach((p,n) => n ? g.lineTo(...p) : g.moveTo(...p)); g.closePath(); g.fill(); g.stroke();
      }
      g.globalAlpha = 1;
    }
    for (const [i, [view, title]] of views.entries()) {
      const x = gap + i * (boxW + gap), y = 64;
      g.strokeStyle = '#475569'; g.lineWidth = 2; g.strokeRect(x, y, boxW, boxH);
      g.fillStyle = '#e8edf2'; g.fillText(title, x + 16, y + 36);
      const area = {x: x + 12, y: y + 56, w: boxW - 24, h: boxH - 350};
      const ox = view === 'side' ? area.x + area.w * .25 : area.x + area.w / 2;
      const oy = view === 'top' ? area.y + area.h * .82 : area.y + area.h * .65;
      const scale = Math.min(area.w / .95, area.h / .65), grid = .05 * scale;
      g.save(); g.beginPath(); g.rect(area.x, area.y, area.w, area.h); g.clip();
      g.lineWidth = 1; g.strokeStyle = '#273444'; g.beginPath();
      for (let a = ox - Math.ceil((ox - area.x) / grid) * grid; a < area.x + area.w; a += grid) {
        if (a < area.x) continue; g.moveTo(a, area.y); g.lineTo(a, area.y + area.h);
      }
      for (let b = oy - Math.ceil((oy - area.y) / grid) * grid; b < area.y + area.h; b += grid) {
        if (b < area.y) continue; g.moveTo(area.x, b); g.lineTo(area.x + area.w, b);
      }
      g.stroke();
      const pixel = p => { const q = M.project(p, view); return [ox + q[0] * scale, oy + q[1] * scale]; };
      for (const side of ['left', 'right']) {
        if (!models[side]) continue;
        solid(models[side].faces, pixel, view, view === 'side' && side === 'right');
        if (view === 'side' && side === 'right') {
          const pts = models[side].points.slice(0,4).map(pixel);
          g.strokeStyle = '#fb923c'; g.lineWidth = 2; g.setLineDash([8,5]); g.beginPath();
          pts.forEach((p,n) => n ? g.lineTo(...p) : g.moveTo(...p)); g.stroke(); g.setLineDash([]);
        }
      }
      if (options.live) for (const side of ['left', 'right']) {
        const measured = sim.measured?.[side];
        if (!measured) continue;
        const points = M.armPoints(side, measured).map(pixel);
        g.strokeStyle = options.feedbackFresh ? '#f1f5f9' : '#94a3b8'; g.lineWidth = 3;
        g.setLineDash([5, 4]); g.beginPath();
        points.forEach((p, n) => n ? g.lineTo(...p) : g.moveTo(...p)); g.stroke(); g.setLineDash([]);
        for (const p of points.slice(1)) { g.beginPath(); g.arc(...p, 4, 0, Math.PI * 2); g.stroke(); }
      }
      g.restore(); g.fillStyle = '#aebccd';
      g.font = '20px "Microsoft YaHei", "Segoe UI", sans-serif';
      g.fillText(view === 'side' ? '网格 5 cm · 右臂虚线叠加' : '网格 5 cm', x + 16, y + boxH - 296);
      g.fillStyle = '#1a242f'; g.fillRect(x + 12, y + boxH - 282, boxW - 24, 210);
      g.fillStyle = '#cbd5e1'; g.fillText('夹爪放大 · 黄面是顶部', x + 20, y + boxH - 256);
      for (const [n,side] of ['left','right'].entries()) {
        const model = models[side];
        if (!model) continue;
        const center = model.points[3].map((v,k) => (v+model.points[4][k])/2);
        const projected = M.project(center,view), zoom = 1020;
        const cx = x + boxW * (n ? .74 : .26), cy = y + boxH - 160;
        const zoomPixel = p => { const q = M.project(p,view); return [cx+(q[0]-projected[0])*zoom,cy+(q[1]-projected[1])*zoom]; };
        g.save(); g.beginPath(); g.rect(x + 12, y + boxH - 244, boxW - 24, 170); g.clip();
        solid(model.gripper,zoomPixel,view);
        g.restore();
      }
      g.font = '20px "Microsoft YaHei", "Segoe UI", sans-serif';
      for (const [n,side] of ['left','right'].entries()) {
        const model=models[side], a=sim[side];
        g.fillStyle=side === 'left' ? '#60a5fa' : '#fb923c';
        if (!model) {
          g.fillText(`${n ? '右' : '左'}：等待有效标定／编码器反馈`, x + 16, y + boxH - 46 + n*28);
          continue;
        }
        const angle = v => (v > 0 ? '+' : '') + v.toFixed(0) + '°';
        g.fillText(`${n ? '右' : '左'}：张开 ${Math.round((1-a.grip)*100)}%　俯仰 ${angle(model.pitch)}　旋转 ${angle(model.roll)}`, x + 16, y + boxH - 46 + n*28);
      }
      g.font = '24px "Microsoft YaHei", "Segoe UI", sans-serif';
    }
    g.fillStyle = '#cbd5e1'; g.font = '23px "Microsoft YaHei", "Segoe UI", sans-serif';
    g.fillText(options.live ? `实色：已发送目标　白线：编码器反馈　${options.feedbackFresh ? '反馈正常' : '反馈过期／未知，等待更新'}` :
      `头部 水平 ${sim.head[0].toFixed(0)}° 俯仰 ${sim.head[1].toFixed(0)}°　底盘 ${sim.base[0].toFixed(2)} m / ${sim.base[1].toFixed(2)} m`, 24, H - 64);
    g.fillStyle = '#aebccd'; g.fillText(options.live ? '肩转／腕旋转相对本次参考；几何尺寸为示意，请与视频核对' :
      options.jogMode ? `左臂逐关节 ${options.jointLabel} · 模型示意，非实机位置／碰撞判定` :
      '握把跟随 · 松开重置　模型示意，非实机位置／碰撞判定', 24, H - 24);
  }

  const api = {draw};
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  else root.VRPreview = api;
  if (typeof importScripts === 'function' && typeof document === 'undefined') {
    let canvas = null;
    root.onmessage = event => {
      const {sim, options, width, height} = event.data;
      if (!canvas) canvas = new OffscreenCanvas(width, height);
      const started = performance.now();
      draw(canvas, sim, options);
      const bitmap = canvas.transferToImageBitmap();
      root.postMessage({bitmap, drawMs: performance.now() - started}, [bitmap]);
    };
  }
})(typeof self !== 'undefined' ? self : this);
