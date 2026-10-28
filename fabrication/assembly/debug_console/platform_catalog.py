"""Repository-backed capability catalog. Availability is explicit, never inferred from demos."""
from pathlib import Path
import hashlib

REPO=Path(__file__).resolve().parents[3]
DOC='docs/zh/source/software/getting_started/'
SIM='docs/zh/source/simulation/getting_started/'

def guide(id,title,kicker,summary,diagram,steps,sources,limits,category='操作手册',image=None):
    return dict(id=id,title=title,kicker=kicker,summary=summary,diagram=diagram,steps=steps,
                sources=sources,limits=limits,category=category,image=image)

GUIDES=[
 guide('start','从连接到第一次动作','START HERE','先知道自己控制的是实机还是预览，再把每一次输入变成可解释的反馈。','bringup',[
  ['连接与观测','两块CH343控制板按USB序列号识别，逻辑COM3/COM4分别表示左臂与右臂总线；Windows实际端口可以不同。设备页各8个电机；三路UVC的物理对应需遮挡确认。'],
  ['选择控制目标','控制中心默认运动学预览。先让手柄全部回中，确认目标再开启。切换目标前由你主动结束旧接管。'],
  ['小范围实机','实机通道依赖实际行程、扭矩/模式和标定。触限保留接管；Menu暂停，View结束。不要把预览图中的末端位置当作实测。'],
  ['建立可重复的任务','固定相机、物体和篮子位置。先录制一次状态轨迹，检查动作，再进入训练数据采集。']],
 [DOC+'XLeRobot_teleop.md','fabrication/assembly/debug_console/ORIGINAL_XBOX_ZH.md','fabrication/assembly/BRINGUP_CHECKLIST_ZH.md'],
 '页面不会自动扩大设备限位、修改零位或让机械臂归零。'),
 guide('xbox','Xbox整机操作图解','TELEOPERATION','两根摇杆分别控制双臂，用肩键和按杆调整姿态。','gamepad',[
  ['默认双臂平面控制','左杆对应左臂，右杆对应右臂。原脚本中杆前后控制径向伸缩，杆左右控制二连杆平面高度，不是车体平面左右平移。'],
  ['肩水平与腕姿','按住LS/RS并左右拨对应杆调肩水平；按住LB/RB并拨对应杆调腕俯仰和腕旋转。LB/RB在原版映射中不是使能键。'],
  ['夹爪、头部与底盘','LT/RT压下关闭、松开打开夹爪；X/B调头部水平，A/Y调头部俯仰。方向键按源码实际映射为↑后退、↓前进、←左转、→右转。'],
  ['暂停与恢复','Menu暂停动作并保留接管，输入回中后点击恢复；View结束接管。普通触限不会退出接管。']],
 ['software/examples/5_xlerobot_teleop_xbox.py','fabrication/assembly/debug_console/original_mapping.py'],
 '启动自动归零已取消；B是头部控制键。原版与单关节调试映射不同。'),
 guide('limits','理解限位与标定','CALIBRATION','设备范围、本次小范围和运动学参考，是三个不同的约束。','limits',[
  ['读取设备范围','设备页读取Min/Max Position、Homing Offset和当前位置；这是一份寄存器快照，并不证明完整机械行程。'],
  ['本次操作范围','受限实机目前使用设备范围与起点±96raw的交集。单关节目标截断到边界，末端联动涉及整组关节，超限时可能整步被拒绝。'],
  ['确认运动学参考','双臂末端需要真实参考角度、编码器值及方向；夹爪需要开合端点。由人工支撑确认行程，不通过猜测扩大数字。'],
  ['复位的区别','当前恢复动作只同步实际位置、不执行回零。上游有归零示例，但缺少经过本机确认的姿态和沿途轨迹，平台将其保留为操作说明。']],
 ['fabrication/assembly/debug_console/ORIGINAL_XBOX_ZH.md','software/src/model/SO101Robot.py','software/src/robots/xlerobot_2wheels/xlerobot_2wheels.py'],
 '模型角度截断不能替代机械限位或碰撞检测；本版没有全机避障规划。'),
 guide('record','记录、回看与训练数据','DATA STUDIO','把操作过程留下来，再决定哪些可以成为可学习的演示。','data',[
  ['录制状态轨迹','数据工作室按2/5/10Hz记录当前服务返回的计划关节、原生手柄和实测编码器。预览与实机来源随每一帧记录。'],
  ['回看而不重放到实机','拖动时间轴检查双臂、头部和底盘计划值。网页回看只驱动示意图，不发送关节或轮速。'],
  ['导出与质量','可导出JSONL及描述清单，查看丢帧、来源变化和暂停帧。此录制未同步相机图像，不能当作原生LeRobot训练数据。'],
  ['升级到训练采集','仓库的record.py和VR录制示例已有LeRobotDataset链路。要接本机双轮和当前手柄适配，固定相机，并验证图像、状态、动作时间对齐。']],
 ['software/src/record.py','software/examples/8_vr_teleop_with_dataset_recording.py',DOC+'VLA_ACT.md'],
 '状态回看与实机轨迹执行、训练数据录制是不同功能。实机回放尚未接入。'),
 guide('vision','视觉识别与辅助对准','PERCEPTION','从能看见、看得清，走向识别目标和辅助抓取。','vision',[
  ['检查画面','设备页查看三路视频；视觉实验室可以对选定相机的单帧或你选择的图片做分辨率、曝光与清晰度检查。'],
  ['复用YOLO示例','仓库有检测、分割和跟随三个单臂示例。示例会连接手臂并可能驱动运动，因此不直接作为网页按钮启动。'],
  ['从二维走向抓取','先识别和框选，再标定相机与机械臂坐标；已知桌面可使用平面约束，任意空间物体还需要深度或其他三维定位。'],
  ['反馈和确认','把目标置信度、目标位置和可达性呈现出来。低置信度或不可达时等待用户选择，不把识别框当成成功抓取。']],
 ['software/examples/3_so100_yolo_ee_control.py','software/examples/3_so100_yolo_ee_control_seg.py','software/examples/3_so100_yolo_ee_follow.py'],
 '图像质量分析已接入；YOLO自动跟随和抓取仍为需适配的仓库示例。'),
 guide('vr','用PICO体验双手遥操作','IMMERSIVE CONTROL','把双手的位姿变成夹爪目标，也让示教更直观。','vr',[
  ['已有设备','项目记录中你拥有PICO Neo 3。上游明确验证的是Quest 3，PICO的浏览器位姿、按键和WebXR兼容仍需真机测试。'],
  ['检查输入链路','先验证双手位姿、按键和坐标系，不连接运动输出。上游VRMonitor包含机器专用路径，需要改成当前环境。'],
  ['建立位置锚点','把当前手的位置与当前夹爪位置绑定，使用相对位姿控制，避免戴上头显后机器人突然跳到绝对坐标。'],
  ['接入录制','先单臂，再双臂，最后移动底盘。同步采集相机与动作后进入模仿学习。']],
 ['software/examples/8_xlerobot_teleop_vr.py','software/src/teleporators/xlerobot_vr/vr_monitor.py',SIM+'vr_sim.md'],
 '本平台提供流程和源代码说明，不宣称PICO已接通实机。',image='vr'),
 guide('act','先学会一个具体动作：ACT','IMITATION LEARNING','从固定工位的一件小任务开始，获得可测量的成功率。','learning',[
  ['限定任务','建议单臂把同一种玩具放进篮子。底盘固定、相机固定、光照稳定，明确开始和成功结束条件。'],
  ['采集演示','使用原生LeRobot数据链路录制相机、动作和关节状态，检查对齐和成功样本。先做小批验证，再增加变化。'],
  ['生成实验配置','在学习实验室选择ACT、数据集名称、步数和batch size。保存的是配置与待执行命令，不会替你上传数据或启动训练。'],
  ['评估与接管','使用没出现在训练中的物体位置做多次测试，记录成功/失败及原因，策略执行必须沿用限位、看门狗和人工接管。']],
 [DOC+'VLA_ACT.md',DOC+'VLA_smol.md','software/src/record.py'],
 'ACT是模仿学习策略，不是语言模型。普通状态轨迹不含训练所需的同步相机帧。',image='pick'),
 guide('smolvla','让视觉和语言进入策略','VISION · LANGUAGE · ACTION','在清晰的小任务基线上，再尝试更丰富的目标表达。','learning',[
  ['先验证输入输出','确认策略需要的相机名称、动作顺序、归一化及机器人形态。其他人的模型不能默认直接控制本机。'],
  ['选择合适模型','SmolVLA可作为后续实验；pi0.5文档主要指向社区分支和OpenPI。8GB级GPU不能承诺无条件运行大模型训练。'],
  ['异步推理','家中台式机承担策略推理，机器人侧网关负责低层控制与超时；手机负责画面与交互。'],
  ['实测而非猜测','先测单步推理延迟、显存与网络，再评估完整任务。离线成功不等于可安全连续驱动机器人。']],
 [DOC+'VLA_smol.md',DOC+'VLA_pi05.md',DOC+'VLA_ACT.md','PROJECT_ARCHITECTURE.md'],
 '训练命令受LeRobot版本、社区分支和模型依赖影响，需要环境检查后执行。',image='learn'),
 guide('agent','把自然语言变成已验证技能','TASK ORCHESTRATION','让语言模型选择技能，让控制器负责每一步如何执行。','agent',[
  ['先定义技能','将看向目标、移动到工位、抓取、放入篮子等动作封装成有输入输出和失败状态的工具。'],
  ['复用RoboCrew教程','仓库文档演示相机、语音、移动工具和VLA策略组合；依赖外部RoboCrew，不是本机已安装的全屋助手。'],
  ['约束工具范围','模型只选择经过验收的动作；动作继续受真实限位、超时与用户接管控制，不直接向舵机发送任意数字。'],
  ['面向家庭的任务','从“把桌上这个玩具放进篮子”逐步扩展；不把一条语言指令等同于已经具备全屋导航与家务能力。']],
 [DOC+'LLM_agent.md',DOC+'VLA_ACT.md'],
 '本平台提供技能路线和文档，未连接付费模型账号，也未自动调用外部模型。',image='agent'),
 guide('simulation','仿真先行，实机有据可依','SIMULATION LAB','独立运动学沙盒、现有MuJoCo研究和ManiSkill场景，各自说明能验证什么。','simulation',[
  ['运动学沙盒','平台内可独立演示伸缩、抬高和腕姿，或加载状态轨迹回看；不会写入实机控制器。'],
  ['MuJoCo对比','已有两层推车、低位前伸和升降方案研究图。图中结果是简化碰撞和可达性筛选，不代表真实夹持和稳定性。'],
  ['ManiSkill任务','上游提供Xbox/VR/键盘控制与采集示例，可探索方块抓取、抽屉和室内场景；对应依赖与GPU环境需另验。'],
  ['与实机对齐','统一关节符号、零位、安装变换、夹爪长度与双轮模型。旧三全向轮模型不能充当当前双轮行走验收。']],
 ['simulation/mujoco/README.md','simulation/mujoco/CANDIDATE_SCHEMES.md',SIM+'index.md',SIM+'simdemos.md'],
 '沙盒为运动学，不含动力学与碰撞；RL完整整机训练在上游仍以外部项目或后续工作为主。',image='pick'),
 guide('mobile','车载Windows中控与远程部署','ONBOARD WINDOWS','按2026-10-29新方案：笔记本放上机器人，远程工作端管理部署与平台。','architecture',[
  ['车载笔记本','准备Windows x64运行环境、FFmpeg和独立虚拟环境。新机配置待确认，先运行隔离预览，不自动开启扭矩或接管。'],
  ['设备身份','同一控制板按USB序列号识别，保留左右臂逻辑角色；新Windows的实际COM号可不同。手柄要连接到车载电脑，相机顺序重新核对。'],
  ['远程工作端','通过Windows OpenSSH密钥登录；同一局域网直连，跨网可用Tailscale。SSH隧道在工作端18770访问远端Studio，服务仍只绑定环回地址。'],
  ['算力与终端扩展','新机承担设备中控，家中台式机可按需要提供训练或推理算力。原手机终端方案保留为可选扩展，当前优先车载Windows路线。']],
 ['deployment/windows/README_ZH.md','PROJECT_ARCHITECTURE.md','web_control/README.md'],
 '新机尚未就绪，远程连通和硬件链路待现场验收。迁移不会恢复旧在线接管或自动应用历史标定。'),
 guide('assembly','拼装、清点与硬件维护','HARDWARE HANDBOOK','把本机装配记录和原厂说明一起放进工作台。','bringup',[
  ['先看本机装配指南','按已到货设备和双轮布局逐步安装，阻断件优先清点，螺丝等小件在实际装配中排查。'],
  ['核对身份','COM3为左臂与头部1–8，COM4为右臂1–6和双轮9/10。旧上游图文可能展示三轮7/8/9布局，不能照图改ID。'],
  ['线缆与支撑','机械臂活动区留足线缆余量，线束远离轮组和夹爪，关闭扭矩前人工支撑。'],
  ['记录变更','PROJECT_STATE.md保留已拥有设备、观察结果和决定；本机运行快照与标定文件不当作可移植配置发布。']],
 ['fabrication/assembly/ASSEMBLY_GUIDE_ZH.md','docs/zh/source/hardware/getting_started/assemble.md','PROJECT_STATE.md'],
 '本机规范优先于旧示意图；没有现场检查就不能判断螺丝、结构或夹爪装配已验收。'),
 guide('fabrication','结构改造与3D打印','FABRICATION','先用仿真排除不合适结构，再打印和验证受力件。','fabrication',[
  ['已有研究','仓库保留低位前伸、两层和升降方案的仿真结论，以及本机CAD原型。'],
  ['选择应用','地面捡玩具与桌面抓取的高度需求不同；低位前伸利于地面，兼顾两种高度需要换装或升降。'],
  ['打印与装配','区分外观件与受力件，核对尺寸、材料、层方向、紧固件和金属加强。平台可读研究图与原始结论。'],
  ['重新验收','改变臂根或工具长度后重新标定工作区、相机外参、重心和线缆，不能沿用原末端位置。']],
 ['simulation/mujoco/CANDIDATE_SCHEMES.md','simulation/mujoco/TWO_LAYER_SIMULATION.md','fabrication/cad/xlerobot_low_front_prototype.py'],
 '未对接拓竹设备或自动打印；打印及采购是后续集成，不能根据模型可达性推断结构强度。'),
 guide('navigation','移动与导航的能力边界','MOBILE MANIPULATION','双轮速度控制已有，全屋自主导航需要独立集成。','architecture',[
  ['遥控接近工位','当前可按原版手柄意图控制差速底盘，真实轮模式、方向与零速通过验收后开放。'],
  ['固定工位','先做固定位置或标记辅助停车，再运行已验证抓放任务，降低同时移动与操作的难度。'],
  ['导航集成','社区ROS 2和MoveIt项目可参考，SLAM/Nav2、定位传感器和避障并非本机主仓库已接好。'],
  ['逐项验收','先离地轮速，再低速停车，再定位，最后移动抓取；每阶段保留人工接管和本地看门狗。']],
 ['software/src/robots/xlerobot_2wheels/xlerobot_2wheels.py',DOC+'LLM_agent.md','docs/zh/source/relatedworks/index.md'],
 'RoboCrew的视觉移动演示不等于地图导航；不能宣称已实现全屋自动清理。'),
]

CAPABILITIES=[
 dict(id='teleop',title='Xbox整机遥操作',group='control',state='integrated',route='control',guide='xbox',summary='双臂、腕姿、夹爪、头部与双轮，沿用原版语义。',sources=['software/examples/5_xlerobot_teleop_xbox.py']),
 dict(id='diagnostic',title='关节与扭矩调试',group='control',state='integrated',route='devices',guide='limits',summary='实测编码器、设备范围和扭矩管理，受现有接管约束。',sources=['fabrication/assembly/debug_console/control_runtime.py']),
 dict(id='camera',title='三路相机监看',group='vision',state='integrated',route='devices',guide='vision',summary='按需开启头部和双腕视频，直接读取当前服务。',sources=['fabrication/assembly/debug_console/app.py']),
 dict(id='state-record',title='状态轨迹录制与回看',group='data',state='integrated',route='data',guide='record',summary='新增本地轨迹管理、时间轴、导出与来源标记。',sources=['fabrication/assembly/debug_console/platform_app.py','software/src/record.py']),
 dict(id='poses',title='姿态档案',group='data',state='integrated',route='data',guide='limits',summary='保存当前计划/实测姿态，用于对比和独立沙盒查看。',sources=['fabrication/assembly/debug_console/platform_app.py']),
 dict(id='sandbox',title='独立运动学沙盒',group='simulation',state='integrated',route='simulation',guide='simulation',summary='直接拨虚拟控制、播放例程和重置预览，不占实机接管。',sources=['fabrication/assembly/debug_console/upstream_teleop.py']),
 dict(id='quality',title='画面质量检查',group='vision',state='integrated',route='vision',guide='vision',summary='新增图片/相机帧检查：分辨率、曝光、清晰度。',sources=['fabrication/assembly/debug_console/platform_app.py']),
 dict(id='yolo',title='YOLO检测、分割与跟随',group='vision',state='adaptation',route='vision',guide='vision',summary='仓库有可参考单臂脚本；自动驱动输出尚未接入本平台。',sources=['software/examples/3_so100_yolo_ee_follow.py','software/examples/3_so100_yolo_ee_control_seg.py']),
 dict(id='lerobot-data',title='相机同步演示数据',group='data',state='adaptation',route='learning',guide='record',summary='已有LeRobotDataset与VR录制，需要适配当前手柄/双轮。',sources=['software/src/record.py','software/examples/8_vr_teleop_with_dataset_recording.py']),
 dict(id='act',title='ACT模仿学习',group='learning',state='guide',route='learning',guide='act',summary='数据集、训练、评估流程与可保存实验配置。',sources=[DOC+'VLA_ACT.md']),
 dict(id='smolvla',title='SmolVLA / pi0.5',group='learning',state='guide',route='learning',guide='smolvla',summary='社区工作流、模型要求和台式机异步推理说明。',sources=[DOC+'VLA_smol.md',DOC+'VLA_pi05.md']),
 dict(id='vr',title='VR双手遥操作',group='control',state='adaptation',route='manual',guide='vr',summary='原有VR链路与PICO适配清单，尚未在本机验收。',sources=['software/examples/8_xlerobot_teleop_vr.py']),
 dict(id='joycon',title='Joy-Con与键盘',group='control',state='guide',route='manual',guide='xbox',summary='保留单臂、双臂、双轮及平滑控制等示例入口。',sources=['software/examples/7_xlerobot_2wheels_teleop_joycon_smooth.py','software/examples/4_xlerobot_2wheels_teleop_keyboard.py']),
 dict(id='mujoco',title='MuJoCo结构与可达性',group='simulation',state='guide',route='simulation',guide='simulation',summary='两层、低位前伸与升降设计的本地研究图文。',sources=['simulation/mujoco/CANDIDATE_SCHEMES.md']),
 dict(id='maniskill',title='ManiSkill / Isaac仿真',group='simulation',state='guide',route='simulation',guide='simulation',summary='任务环境、Xbox/VR示例和数据采集路线。',sources=[SIM+'index.md',SIM+'simdemos.md']),
 dict(id='agent',title='语言与语音技能编排',group='application',state='guide',route='manual',guide='agent',summary='RoboCrew集成路线，组合已验证移动与操作技能。',sources=[DOC+'LLM_agent.md']),
 dict(id='rl',title='强化学习与Sim2Real',group='learning',state='reference',route='learning',guide='simulation',summary='外部单臂教程与研究参考，不是已接通的整机训练。',sources=[DOC+'RL.md',DOC+'RL_VLA.md']),
 dict(id='navigation',title='ROS 2与移动导航',group='application',state='reference',route='manual',guide='navigation',summary='社区导航/MoveIt入口；需要额外定位与规划集成。',sources=['docs/zh/source/relatedworks/index.md']),
 dict(id='fabrication',title='拼装、CAD与打印',group='hardware',state='guide',route='manual',guide='fabrication',summary='本机装配记录、原厂指南、结构改造及采购规范。',sources=['fabrication/assembly/ASSEMBLY_GUIDE_ZH.md','simulation/mujoco/CANDIDATE_SCHEMES.md']),
 dict(id='distributed',title='车载Windows与远程工作端',group='application',state='guide',route='manual',guide='mobile',summary='新笔记本中控、SSH部署、加密Web隧道与可选台式机算力。',sources=['deployment/windows/README_ZH.md','PROJECT_ARCHITECTURE.md']),
]
MEDIA={
 'robot':'docs/zh/source/_static/WechatIMG1826-removebg-preview.png',
 'logo':'docs/zh/source/_static/XLeRobot.png',
 'vr':'docs/community_usecases/static/community/makermods.jpg',
 'pick':'docs/zh/source/_static/env_thumbnails/PickCubeSO100-v1_rt_thumb_last.png',
 'learn':'docs/community_usecases/static/community/paper_xsvla.jpg',
 'agent':'docs/community_usecases/static/community/robocrew.jpg',
 'two-layer':'fabrication/assembly/debug_console/platform_static/research/two-layer.png',
 'schemes':'fabrication/assembly/debug_console/platform_static/research/schemes.png',
 'scores':'fabrication/assembly/debug_console/platform_static/research/scores.png',
}

def source_registry():
    paths=set()
    for item in GUIDES+CAPABILITIES:paths.update(item['sources'])
    for pattern in ['software/examples/*.py','software/examples/odrive/*.py','docs/zh/source/software/getting_started/*.md','docs/zh/source/simulation/getting_started/*.md','fabrication/assembly/*.md']:
        paths.update(p.relative_to(REPO).as_posix() for p in REPO.glob(pattern))
    paths.update(['software/plugins/README.md','web_control/README.md','simulation/mujoco/README.md','fabrication/assembly/debug_console/STUDIO_ZH.md'])
    return {hashlib.sha256(p.encode()).hexdigest()[:12]:p for p in sorted(paths) if (REPO/p).is_file()}

SOURCES=source_registry()

def catalog():
    ids={p:i for i,p in SOURCES.items()}
    return {'capabilities':CAPABILITIES,'guides':GUIDES,
            'sources':[{'id':i,'path':p,'name':Path(p).name,'kind':Path(p).suffix[1:]} for i,p in SOURCES.items()],
            'source_ids':ids,'media':{k:'/api/platform/media/'+k for k,p in MEDIA.items() if (REPO/p).is_file()},
            'scope':'当前本地检出版本；不代表外部社区分支已安装或本机已验收',
            'upstream_examples':len(list((REPO/'software/examples').glob('*.py')))}
