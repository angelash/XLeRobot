'use strict';
// Native SVG: all labels are authored strings or escaped runtime numbers.
const StudioVisuals = (() => {
  const paths={home:'M3 10l9-7 9 7M5 9v12h14V9M9 21v-7h6v7',control:'M7 8h10c5 0 7 12 3 12-2 0-3-4-5-4H9c-2 0-3 4-5 4C0 20 2 8 7 8zM7 10v5M4.5 12.5h5M16 11h.1M19 14h.1',devices:'M4 3h6v6H4zM14 3h6v6h-6zM4 15h6v6H4zM14 15h6v6h-6zM7 9v6M17 9v6M10 6h4M10 18h4',data:'M4 6c0-4 16-4 16 0s-16 4-16 0zm0 0v12c0 4 16 4 16 0V6M4 12c0 4 16 4 16 0',simulation:'M12 2L2 7v10l10 5 10-5V7zM2 7l10 5 10-5M12 12v10M7 4.5l10 5',vision:'M2 12s4-7 10-7 10 7 10 7-4 7-10 7S2 12 2 12zm13 0a3 3 0 1 1-6 0 3 3 0 0 1 6 0',learning:'M9 3a4 4 0 0 0-6 5c-3 2-2 7 1 7-1 4 4 7 7 3V5a3 3 0 0 0-2-2zM15 3a4 4 0 0 1 6 5c3 2 2 7-1 7 1 4-4 7-7 3V5a3 3 0 0 1 2-2zM5 9l4 2M5 16l4-2M19 9l-4 2M19 16l-4-2',manual:'M3 4h7l2 2 2-2h7v16h-7l-2 2-2-2H3zM12 6v16M6 8h3M6 12h3M15 8h3M15 12h3',capabilities:'M4 4h6v6H4zM14 4h6v6h-6zM4 14h6v6H4zM17 13v8M13 17h8'};
  function icon(id){return `<svg viewBox="0 0 24 24" aria-hidden="true"><path d="${paths[id]||paths.capabilities}" stroke-linecap="round" stroke-linejoin="round"/></svg>`;}
  const text=(x,y,t,size=12,fill='#64824d')=>`<text x="${x}" y="${y}" fill="${fill}" font-size="${size}" font-family="Segoe UI,Microsoft YaHei,sans-serif">${t}</text>`;
  const line=(x,y,a,b,c='#9db28a',w=2)=>`<path d="M${x} ${y}L${a} ${b}" stroke="${c}" stroke-width="${w}" fill="none"/>`;
  function diagram(kind){
    let inner='';
    const flows={bringup:['连接与识别','只读状态','标定与限位','小范围动作'],data:['手柄 / 相机','同步采集','回看与筛选','训练数据'],vision:['原始画面','检测 / 分割','坐标与可达性','确认后执行'],learning:['固定任务','成功演示','训练策略','独立评估'],agent:['语言目标','技能选择','受限控制器','反馈与接管'],architecture:['远程工作端','SSH加密通道','Windows中控','设备与反馈'],vr:['双手位姿','相对锚点','坐标映射','受限双臂'],fabrication:['需求尺寸','CAD与干涉','打印与试装','负载验收'],simulation:['运动学','碰撞 / 动力学','模型对齐','实机核验']};
    if(kind==='gamepad'){
      inner=`<path d="M170 70Q150 74 140 120L122 180Q120 201 139 197L190 155H350L401 197Q420 201 418 180L400 120Q390 74 370 70Z" fill="#e0eacb" stroke="#92ac78" stroke-width="2"/><circle cx="205" cy="112" r="21" fill="#b8ce9b"/><circle cx="322" cy="155" r="21" fill="#b8ce9b"/><path d="M240 143h-16v-13h-13v-16h13v-13h16v13h13v16h-13Z" fill="#729651" transform="translate(9 29) scale(.75)"/><g fill="#789950"><circle cx="350" cy="103" r="7"/><circle cx="372" cy="124" r="7"/><circle cx="350" cy="144" r="7"/><circle cx="329" cy="124" r="7"/></g>${text(26,57,'左杆 → 左臂')}${line(115,60,190,102)}${text(395,57,'右杆 → 右臂')}${line(435,65,330,145)}${text(24,218,'LB / RB + 摇杆：腕姿')}${text(329,218,'LT / RT：对应夹爪')}${text(187,246,'Menu 暂停 · View 结束接管',11)}`;
    }else if(kind==='limits'){
      inner=`${text(35,55,'设备行程',12)}<rect x="35" y="77" width="490" height="20" rx="10" fill="#dae4cc"/><rect x="190" y="77" width="180" height="20" rx="10" fill="#a3bd7e"/><circle cx="272" cy="87" r="15" fill="#3d6d3d" stroke="#fff" stroke-width="4"/>${line(272,110,272,157)}${text(224,183,'当前位置',12)}${text(180,52,'本轮 ±96 raw 与设备行程的交集',11)}${text(35,227,'边界阻挡 → 提示原因 → 允许反向 → 接管保留',13)}`;
    }else{
      const nodes=flows[kind]||flows.bringup;
      nodes.forEach((n,i)=>{let x=25+i*137;inner+=`<rect x="${x}" y="78" width="113" height="83" rx="10" fill="${i===3?'#d7eead':'#fff'}" stroke="#d0dfbb"/>${text(x+13,103,'0'+(i+1),10,'#9cb583')}${text(x+13,137,n,12,'#4b6c34')}`;if(i<3)inner+=`<path d="M${x+118} 120h13m-4-4 4 4-4 4" fill="none" stroke="#9bb580"/>`;});
      inner+=text(25,213,{data:'状态轨迹已接入 · 同步图像的训练采集需适配',vision:'质量检查已接入 · 识别与抓取链路待适配',architecture:'当前仅本机运行 · 不自动开放公网或远程控制',vr:'PICO Neo 3：待验证 WebXR 与双手输入',learning:'配置可保存 · 训练执行与策略验收为后续步骤',agent:'语言选择技能 · 控制器执行经过验收的动作',simulation:'不同仿真层级，回答不同问题'}[kind]||'先验证链路，再扩大能力范围',11);
    }
    return `<svg viewBox="0 0 570 270" role="img" aria-label="${kind}流程图">${inner}</svg>`;
  }
  function robot(state){
    if(!state?.arms)return `<svg viewBox="0 0 720 315" role="img" aria-label="等待运动学状态">${text(250,160,'等待可视化状态',14)}</svg>`;
    let svg='';
    for(const [i,s] of ['left','right'].entries()){
      const a=state.arms[s];if(!a)continue;
      const q=a.joints,x=70+i*340,y=245,scale=680;
      const ang=(90-q.lift)*Math.PI/180-Math.atan2(.028,.11257);
      const ex=x+.1159*Math.cos(ang)*scale,ey=y-.1159*Math.sin(ang)*scale;
      const tx=x+a.plane_m[0]*scale,ty=y-a.plane_m[1]*scale;
      svg+=`<path d="M${x-20} 245h270M${x} 45v210" stroke="#dbe5ce" stroke-width="1" stroke-dasharray="3 5"/>${text(x,33,s==='left'?'LEFT / 左臂':'RIGHT / 右臂',11)}<path d="M${x} ${y}L${ex} ${ey}L${tx} ${ty}" stroke="${i?'#537752':'#9fb966'}" stroke-width="13" stroke-linecap="round" stroke-linejoin="round" fill="none"/><g fill="#f5f8ee" stroke="#577b40" stroke-width="3"><circle cx="${x}" cy="${y}" r="9"/><circle cx="${ex}" cy="${ey}" r="7"/><circle cx="${tx}" cy="${ty}" r="6"/></g><path d="M${tx+5} ${ty-4}l18 ${-3-q.grip/8}M${tx+5} ${ty+4}l18 ${3+q.grip/8}" stroke="#547742" stroke-width="3"/>${text(x,282,`${(a.plane_m[0]*1000).toFixed(0)}mm 径向 / ${(a.plane_m[1]*1000).toFixed(0)}mm 高度`,10)}${text(x,300,`腕 ${Number(q.wrist).toFixed(1)}° · 夹爪 ${Number(q.grip).toFixed(0)}`,10,'#9bab8b')}`;
    }
    return `<svg viewBox="0 0 720 315" role="img" aria-label="双臂计划关节的二连杆平面示意">${svg}</svg>`;
  }
  return {icon,diagram,robot};
})();
