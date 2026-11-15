// Local replay only. No camera, robot-control or model requests.
const root=document.getElementById('metricWorkspace');
if(root){
  root.innerHTML=`<header class="metric-heading"><div><p class="eyebrow">LOCAL MAPPING / REPLAY BENCH</p><h2>本地建图<span>几何先行，逐格可查。</span></h2><p>用测距与位姿生成地图，沿已知自由区寻找下一个观察位。</p></div><div class="metric-mode">算法回放<span>真实传感器与连续定位待接入</span></div></header>
  <div class="metric-toolbar"><label>地图记录<select id="metricSession"><option value="">正在读取…</option></select></label><label>显示图层<select id="metricLayer"><option value="robot">本机回放栅格</option></select></label><button id="metricDemo">生成合成环境回放</button><button id="metricRefresh">刷新记录</button></div>
  <p id="metricMessage" class="metric-message" role="status">读取已保存的建图记录…</p>
  <div class="metric-bench"><div class="metric-map-wrap"><canvas id="metricCanvas" width="960" height="650" aria-label="占据栅格地图，横轴X米，纵轴Y米。点击已知区域计算路径预览。"></canvas><div id="metricMapEmpty" class="metric-map-empty"><strong>从可复验的观测开始</strong><span>生成合成办公室回放，检查墙体、未知区域和规划路径。</span></div><p class="metric-map-caption" id="metricMapCaption">横轴 X / 米 · 纵轴 Y / 米</p></div><aside class="metric-inspector"><p class="eyebrow">MAP EVIDENCE</p><h3 id="metricBasis">等待选择记录</h3><p id="metricLabel">回放数据单独保存，历史照片和手机模型仍在原处。</p><dl><div><dt>有效扫描</dt><dd id="metricFrames">—</dd></div><div><dt>自由格面积</dt><dd id="metricArea">—</dd></div><div><dt>未知栅格</dt><dd id="metricUnknown">—</dd></div><div><dt>核心循环大模型调用</dt><dd id="metricLlm">0</dd></div></dl><label class="metric-radius">整车预览半径 / 米<select id="metricRadius"><option value="0.6">0.60 · 粗略包络</option><option value="0.75">0.75 · 增加余量</option><option value="1.0">1.00 · 增加余量</option></select></label><button id="metricPlan" class="metric-primary" disabled>寻找下一观察位</button><p id="metricPlanResult" class="metric-plan-result">路径仅作几何预览。点击地图也可指定目标。</p><ol id="metricCandidates" class="metric-candidates"></ol><div class="metric-export"><a id="metricExport" hidden>保存可重放记录 ↗</a><a id="metricPgm" hidden>PGM栅格 ↗</a><a id="metricYaml" hidden>YAML参数 ↗</a></div></aside></div>
  <div class="metric-legend"><span><i class="metric-free"></i>已知自由</span><span><i class="metric-wall"></i>占据</span><span><i class="metric-unknown"></i>未知</span><span><i class="metric-uncertain"></i>不确定</span><span><i class="metric-eroded"></i>包络后不可走中心</span><span><i class="metric-path"></i>路径预览</span></div>
  <p class="metric-boundary">回放使用已提供的位姿，尚未完成连续SLAM。扫地机地图在独立先验层显示；本页不开放实机行驶。</p>`;
  const $=id=>document.getElementById(id),api='/api/platform/fieldmap/metric';
  const canvas=$('metricCanvas'),ctx=canvas.getContext('2d');
  let data=null,display=null,preview=null,generation=0,busy=false,transform=null;
  const messages={no_pose:'还没有合格的位姿与测距输入。',start_not_in_eroded_free_space:'当前位置周围已知自由区不足以容纳整车包络，需要补充观测。',goal_unreachable:'目标无法从当前已知自由区到达。',no_reachable_frontier:'当前没有符合条件的可达前沿。',frontier_preview:'已选出可达观察位，路径等待真实定位和现场保护验证。',path_preview:'已计算目标路径预览。'};
  async function request(path,body){
    const controller=new AbortController(),timer=setTimeout(()=>controller.abort(),25000);
    try{
      const response=await fetch(api+path,{signal:controller.signal,...(body===undefined?{}:{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)})});
      const result=await response.json();
      if(!response.ok)throw new Error(typeof result.detail==='string'?result.detail:'建图请求失败');
      return result;
    }finally{clearTimeout(timer);}
  }
  function notice(text,error=false){$('metricMessage').textContent=text;$('metricMessage').classList.toggle('error',error);}
  function controls(){
    $('metricDemo').disabled=busy;$('metricRefresh').disabled=busy;
    $('metricSession').disabled=busy;$('metricLayer').disabled=busy||!data;
    $('metricRadius').disabled=busy||!data||$('metricLayer').value!=='robot';
    $('metricPlan').disabled=busy||!data||$('metricLayer').value!=='robot';
  }
  async function action(fn){if(busy)return;busy=true;controls();try{await fn();}catch(error){if(data){$('metricSession').value=data.id;$('metricLayer').value=display===data?'robot':display.id;}notice(error.name==='AbortError'?'读取超时，已保留当前回放。':error.message,true);}finally{busy=false;controls();}}
  function option(value,text){const node=document.createElement('option');node.value=value;node.textContent=text;return node;}
  async function listing(prefer){
    const result=await request('/sessions');
    const select=$('metricSession');select.replaceChildren();
    if(!result.items.length){select.append(option('','还没有建图回放'));notice('可生成合成办公室回放，或通过已标定的数据接口导入扫描。');return;}
    for(const item of result.items)select.append(option(item.id,`${item.mode==='synthetic_demo'?'合成':'传感器回放'} · ${item.label} · ${item.frames}帧`));
    select.value=result.items.some(item=>item.id===prefer)?prefer:result.items[0].id;
    await load(select.value);
  }
  async function load(id){
    const version=++generation;
    const next=await request('/sessions/'+encodeURIComponent(id));
    if(version!==generation)return;
    data=next;display=next;preview=null;
    const layers=$('metricLayer');layers.replaceChildren(option('robot','本机回放栅格'));
    for(const prior of next.priors)layers.append(option(prior.id,`外部先验 ${prior.id.slice(0,6)} · 未配准`));
    $('metricFrames').textContent=String(data.scan_count);
    $('metricArea').textContent=(data.counts.free*data.grid.resolution_m**2).toFixed(2)+' m²';
    $('metricUnknown').textContent=data.counts.unknown.toLocaleString();
    $('metricLlm').textContent=String(data.llm_calls);
    $('metricBasis').textContent=data.config.mode==='synthetic_demo'?'合成环境 · 已知位姿':'传感器数据 · 已知位姿回放';
    $('metricLabel').textContent=data.config.label;
    $('metricPlanResult').textContent='点击“寻找下一观察位”，或在地图上指定目标。';
    $('metricCandidates').replaceChildren();
    for(const [id,suffix] of [['metricExport','export'],['metricPgm','occupancy.pgm'],['metricYaml','occupancy.yaml']]){$(id).href=api+'/sessions/'+data.id+'/'+suffix;$(id).hidden=false;}
    notice(`${data.scan_count}帧已载入 · 定位时期 ${data.config.epoch} · 记录版本 ${data.revision}。`);
    draw();controls();
  }
  function draw(){
    const width=Math.max(280,Math.round(canvas.getBoundingClientRect().width)),height=Math.round(width*650/960),ratio=window.devicePixelRatio||1;
    canvas.width=Math.round(width*ratio);canvas.height=Math.round(height*ratio);ctx.setTransform(ratio,0,0,ratio,0,0);
    $('metricMapEmpty').hidden=Boolean(display);
    if(!display){ctx.clearRect(0,0,width,height);return;}
    const external=display!==data;
    $('metricBasis').textContent=external?'外部二维先验 · 未配准':data.config.mode==='synthetic_demo'?'合成环境 · 已知位姿':'传感器数据 · 已知位姿回放';
    $('metricLabel').textContent=external?'外部图中的自由格尚未由本机验证。房间与实体几何等待独立适配。':data.config.label;
    $('metricFrames').textContent=external?'外部图':String(data.scan_count);
    $('metricArea').textContent=((external?display.cells.filter(n=>n===0).length:data.counts.free)*display.grid.resolution_m**2).toFixed(2)+' m²';
    $('metricUnknown').textContent=(external?display.cells.filter(n=>n===-1).length:data.counts.unknown).toLocaleString();
    const grid=display.grid,w=grid.width,h=grid.height,pad=width<500?30:48;
    const size=Math.min((width-pad*2)/w,(height-pad*2)/h),left=(width-w*size)/2,top=(height-h*size)/2;
    transform={left,top,size,grid,width,height};ctx.clearRect(0,0,width,height);
    const raster=document.createElement('canvas');raster.width=w;raster.height=h;
    const rc=raster.getContext('2d'),pixels=rc.createImageData(w,h);
    for(let y=0;y<h;y++)for(let x=0;x<w;x++){
      const source=y*w+x,n=display.cells[source],target=((h-1-y)*w+x)*4;
      let color=n===0?[198,210,183]:n===100?[241,223,165]:n===50?[169,128,81]:((x+y)%2?[34,46,39]:[31,43,37]);
      if(preview&&display===data&&n===0&&!preview.safe_centres[source])color=[99,109,94];
      pixels.data.set([...color,255],target);
    }
    rc.putImageData(pixels,0,0);ctx.imageSmoothingEnabled=false;ctx.drawImage(raster,left,top,w*size,h*size);
    function point(x,y){return [left+(x-grid.origin_x_m)/grid.resolution_m*size,top+(h-(y-grid.origin_y_m)/grid.resolution_m)*size];}
    ctx.font=`${width<500?10:13}px Consolas, monospace`;ctx.fillStyle='#a2aea2';ctx.strokeStyle='#63735d';ctx.lineWidth=.6;
    const step=Math.max(1,Math.ceil(w*grid.resolution_m/10));
    for(let x=Math.ceil(grid.origin_x_m/step)*step;x<=grid.origin_x_m+w*grid.resolution_m;x+=step){const [px]=point(x,grid.origin_y_m);ctx.beginPath();ctx.moveTo(px,top+h*size);ctx.lineTo(px,top+h*size+5);ctx.stroke();ctx.textAlign='center';ctx.fillText(String(x),px,top+h*size+(width<500?16:23));}
    for(let y=Math.ceil(grid.origin_y_m/step)*step;y<=grid.origin_y_m+h*grid.resolution_m;y+=step){const [,py]=point(grid.origin_x_m,y);ctx.textAlign='right';ctx.fillText(String(y),left-10,py+4);}
    ctx.textAlign='left';ctx.fillText('Y / m',Math.max(2,left-36),Math.max(16,top-15));ctx.textAlign='right';ctx.fillText('X / m',left+w*size,top+h*size+(width<500?28:41));
    if(preview&&display===data){
      ctx.strokeStyle='#d6f281';ctx.lineWidth=3;ctx.beginPath();
      preview.path_m.forEach(([x,y],i)=>{const p=point(x,y);i?ctx.lineTo(...p):ctx.moveTo(...p);});ctx.stroke();
      for(const f of preview.frontiers.slice(0,12)){const p=point(...f.goal_m);ctx.fillStyle='#d6f281';ctx.beginPath();ctx.arc(...p,4,0,Math.PI*2);ctx.fill();}
    }
    if(display===data&&data.last_pose){
      const p=point(data.last_pose.x_m,data.last_pose.y_m),radius=Number($('metricRadius').value)/grid.resolution_m*size;
      ctx.strokeStyle='#8ed9af';ctx.lineWidth=1.5;ctx.setLineDash([5,4]);ctx.beginPath();ctx.arc(...p,radius,0,Math.PI*2);ctx.stroke();ctx.setLineDash([]);
      ctx.fillStyle='#d6f281';ctx.beginPath();ctx.arc(...p,6,0,Math.PI*2);ctx.fill();
      ctx.strokeStyle='#d6f281';ctx.lineWidth=3;ctx.beginPath();ctx.moveTo(...p);ctx.lineTo(p[0]+Math.cos(data.last_pose.yaw_rad)*22,p[1]-Math.sin(data.last_pose.yaw_rad)*22);ctx.stroke();
    }
    $('metricMapCaption').textContent=display===data?`${grid.resolution_m.toFixed(3)} m / 格 · X向右，Y向上 · 已提供位姿，回放时间`:`外部扫地机坐标 · ${grid.resolution_m.toFixed(3)} m / 格 · 未配准，独立先验`;
  }
  async function calculate(goal){
    if(!data||$('metricLayer').value!=='robot')return;
    const version=generation,id=data.id,revision=data.revision;
    notice('正在按已知自由区、未知边界与整车包络计算…');
    const result=await request('/sessions/'+id+'/plan',{expected_revision:revision,robot_radius_m:Number($('metricRadius').value),...(goal?{goal_m:goal}:{})});
    if(version!==generation||data.id!==id||data.revision!==revision)return;
    preview=result;$('metricPlanResult').textContent=messages[result.status]||result.status;
    const list=$('metricCandidates');list.replaceChildren();
    result.frontiers.slice(0,3).forEach(f=>{const item=document.createElement('li');item.textContent=`观察位 (${f.goal_m.map(n=>n.toFixed(2)).join(', ')}) m · 路径 ${f.path_length_m.toFixed(2)} m`;list.append(item);});
    notice((messages[result.status]||result.status)+' 本页未发送行驶命令。');draw();
  }
  $('metricDemo').addEventListener('click',()=>action(async()=>{notice('正在生成合成扫描并重放…');const result=await request('/demo',{});await listing(result.id);}));
  $('metricRefresh').addEventListener('click',()=>action(()=>listing(data?.id)));
  $('metricSession').addEventListener('change',()=>action(()=>load($('metricSession').value)));
  $('metricPlan').addEventListener('click',()=>action(()=>calculate()));
  $('metricRadius').addEventListener('change',()=>{preview=null;draw();$('metricPlanResult').textContent='包络余量已改变，请重新计算。';});
  $('metricLayer').addEventListener('change',()=>action(async()=>{const pid=$('metricLayer').value,version=++generation;if(pid==='robot')display=data;else{const result=await request('/sessions/'+data.id+'/priors/'+encodeURIComponent(pid));if(version!==generation)return;display=result;}preview=null;$('metricCandidates').replaceChildren();$('metricPlanResult').textContent=pid==='robot'?'可计算本机回放中的路径预览。':'外部自由格不表示本机净空；先验层不参与本机路径计算。';draw();}));
  canvas.addEventListener('click',event=>{if(!transform||!data||display!==data)return;const rect=canvas.getBoundingClientRect(),t=transform,x=(event.clientX-rect.left)*t.width/rect.width,y=(event.clientY-rect.top)*t.height/rect.height;const g=t.grid,cx=(x-t.left)/t.size,cy=g.height-(y-t.top)/t.size;if(cx<0||cy<0||cx>=g.width||cy>=g.height)return;action(()=>calculate([g.origin_x_m+cx*g.resolution_m,g.origin_y_m+cy*g.resolution_m]));});
  new ResizeObserver(()=>draw()).observe(canvas.parentElement);
  action(()=>listing());
}
