"""Mapping from upstream Xbox example; documented local safety adaptations."""
from .control_mapping import entry
ORIGINAL_MAP=[
 entry('lx','左摇杆 X','左臂平面高度 / 肩转 / 腕转','左臂','直接拨杆；按LS切肩转；按LB切腕转','默认左右改变二连杆平面高度；LS时改变肩水平；LB时改变腕旋转','轻推慢，回中保持；实机按通道校准与限位放行',axis=0),
 entry('ly','左摇杆 Y','左臂伸缩 / 腕俯仰','左臂','直接前后拨杆；按LB切俯仰','默认前推伸出、后拉收回；LB时前推增加俯仰','各臂平面坐标，不是任意车体XYZ',axis=1),
 entry('rx','右摇杆 X','右臂平面高度 / 肩转 / 腕转','右臂','直接拨杆；按RS切肩转；按RB切腕转','对应右臂；双臂可同时操作','轻推慢，回中保持；实机按通道校准与限位放行',axis=2),
 entry('ry','右摇杆 Y','右臂伸缩 / 腕俯仰','右臂','直接前后拨杆；按RB切俯仰','对应右臂；双臂可同时操作','各臂平面坐标',axis=3),
 entry('lb','LB','左腕姿态修饰键','左臂','按住LB并拨左摇杆','左右调腕旋转，上下调腕俯仰；行驶中点按可减速','原版中不是使能键；接管后直接拨杆即可动作',4),
 entry('rb','RB','右腕姿态修饰键','右臂','按住RB并拨右摇杆','左右调腕旋转，上下调腕俯仰；行驶中点按可加速','不是独立夹爪键；不增加新按键布局',5),
 entry('ls','LS按住','左肩水平旋转','左臂肩水平','按住左摇杆再左右拨','只调左肩水平；LB优先于LS','当前关节需有可用行程',8),
 entry('rs','RS按住','右肩水平旋转','右臂肩水平','按住右摇杆再左右拨','只调右肩水平；RB优先于RS','当前关节需有可用行程',9),
 entry('lt','LT','左夹爪','左臂夹爪','压过75%关闭，松开打开','沿用原脚本压下关闭/松开打开，执行有速度限制','实机端点/方向未确认时仅提示；启动不自动开爪',10,4),
 entry('rt','RT','右夹爪','右臂夹爪','压过75%关闭，松开打开','沿用原脚本压下关闭/松开打开，执行有速度限制','实机端点/方向未确认时仅提示；启动不自动开爪',11,5),
 entry('up','十字键 ↑','底盘前进','双轮差速底盘','按住↑','车身向前行驶；松开立即零速','沿用上游get_base_action（↑发k，注释为Forward），2026-10-30实测本机↑为前进；如反了在控制设置切换前进方向',12),
 entry('down','十字键 ↓','底盘后退','双轮差速底盘','按住↓','向车身后方行驶；松开立即零速','与↑同一套前进方向设置',13),
 entry('left','十字键 ←','底盘左转','双轮差速底盘','按住←','双轮差速原地左转','转向反了：控制设置 → 转向方向 → 反向',14),
 entry('right','十字键 →','底盘右转','双轮差速底盘','按住→','双轮差速原地右转','与←同一套转向设置；不会切换关节',15),
 entry('x','X','头部水平增加','头部1','按住X','对应原版head_motor_1+','不切换手臂模式',2),
 entry('b','B','头部水平减少','头部1','按住B','对应原版head_motor_1−；B不再是停止键','暂停用Menu或页面暂停，结束用View',1),
 entry('a','A','头部俯仰增加','头部2','按住A','对应原版head_motor_2+','不是查看说明快捷键',0),
 entry('y','Y','头部俯仰减少','头部2','按住Y','对应原版head_motor_2−','不是界面简明/完整快捷键',3),
 entry('menu','Menu','暂停动作、保留接管','全机动作','点按Menu','停止运动，回中后在页面手动恢复','本机安全适配：不触发归零',7),
 entry('view','View','由你结束接管','接管会话','点按View','结束并保持姿态，不执行上游直接归零','遵守用户接管规则',6),
 entry('xbox','Xbox系统键','系统保留','Windows','系统处理','XInput不提供状态','不会伪造输入',16,group='system'),
 entry('paddles','Elite背键','沿用主键映射','已配置主键','按下背键','主键映射与上述功能一致','无独立背键身份',group='system'),
]

# Explicit fallback for commissioning; these are encoder directions, not Cartesian motion.
JOINT_MAP=[dict(item) for item in ORIGINAL_MAP]
for item in JOINT_MAP:
    if item['key'] in ('lx','rx'):
        item.update(role='肩抬升计数 / 肩水平 / 腕旋转',gesture='直接拨杆调肩抬升；LS/RS调肩水平；LB/RB调腕旋转',
                    result='仅控制对应单关节；方向以编码器正负为准',requirement='本轮起点±32计数；无需猜测末端零位')
    elif item['key'] in ('ly','ry'):
        item.update(role='肘关节计数 / 腕俯仰',gesture='直接前后拨杆调肘；LB/RB时调腕俯仰',
                    result='仅控制对应单关节；不是末端伸缩',requirement='本轮起点±32计数，保存限位内')
    elif item['key'] in ('lt','rt'):
        item.update(role='夹爪局部计数测试',gesture='首次压过75%接入；压下向高计数，松开向低计数',result='限于本轮小范围，开合方向尚待现场核对',
                    requirement='未确认完整开合端点，不执行全开全关')
    elif item['key'] in ('up','down','left','right'):
        item.update(result='双轮最多180ms低速短脉冲，松开方向键后才可再次触发',requirement='双轮已零速准备；此模式不作距离控制')

# Preset direct control: the upstream button layout, each stick axis driving
# one joint over its saved servo limit. Tuning applies on the next motion.
PRESET_MAP=[dict(item) for item in ORIGINAL_MAP]
TUNE='方向反了或快慢不合适：在“控制设置 · 逐轴调参”改，接管中保存即时生效'
for item in PRESET_MAP:
    if item['key'] in ('lx','rx'):
        item.update(role='肩抬升 / 肩水平 / 腕旋转',gesture='直接左右拨杆转肩抬升；按住LS/RS转肩水平；按住LB/RB转腕旋转',
                    result='对应关节按设定速度持续转动，回中即停，到舵机限位自动停住',requirement=TUNE)
    elif item['key'] in ('ly','ry'):
        item.update(role='肘 / 腕俯仰',gesture='直接前后拨杆转肘；按住LB/RB前后拨转腕俯仰',
                    result='对应关节按设定速度持续转动，回中即停',requirement=TUNE)
    elif item['key'] in ('ls','rs'):
        item.update(result='只转肩水平；LB/RB优先于LS/RS',requirement=TUNE)
    elif item['key'] in ('lt','rt'):
        item.update(role='夹爪',gesture='首次压过75%后接入：压下闭合，松开张开',
                    result='按夹爪速度移动，到限位停住',requirement='开合反了：逐轴调参把夹爪设为反向')
    elif item['key'] in ('x','b','a','y'):
        item.update(requirement=TUNE)
