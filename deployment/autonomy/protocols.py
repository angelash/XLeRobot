"""Experiment catalogue; mirrors docs/autonomy/LEARNING_PROTOCOLS_ZH.md."""

COMMON_STOPS = ['手柄接管或任意输入', '画面中有人进入运动范围', '温度≥55°C或读数跳变', '电压连续低于10.5V',
                '通信错误', '停车证据不全', '观察超过60秒']


def _p(id, group, title, motion, purpose, method, truth, samples, threshold, model, prerequisites=()):
    return {'id': id, 'group': group, 'title': title, 'motion': motion, 'purpose': purpose, 'method': method,
            'truth': truth, 'samples': samples, 'threshold': threshold, 'model_kind': model,
            'prerequisites': list(prerequisites), 'stops': COMMON_STOPS}


PROTOCOLS = [
    _p('W0', 'wheel', '轮组读数健康', 'none_then_base', '复现并解释动作中出现的57°C温度跳变', '静止10Hz×60s读轮组寄存器，再在架空或受限低速移动中采样，保留原始字节',
       ['encoder'], '静止≥600帧', '异常帧逐条保留并分类', None),
    _p('W1', 'wheel', '架空轮速曲线', 'wheels_lifted', '原始轮速→计数速率、死区、启动/停车延迟', '轮离地，对60–220原始轮速各做短段并记录计数轨迹',
       ['encoder'], '每档≥3次', '线性段R²≥0.98', 'wheel_speed', ['底盘架空，驱动轮离地']),
    _p('W2', 'wheel', '直线距离标定', 'base', '计数→地面距离，有效轮径', '地面胶带刻度，按3/5cm计数目标前进与后退，卷尺+地面单应测位移',
       ['tape_measure', 'ground_homography'], '每档每方向≥3次', '残差≤3mm（5cm段）', 'wheel_distance', ['C2地面单应（可选）', '测试区清空']),
    _p('W3', 'wheel', '原地转角标定', 'base', '左右计数差→航向，有效轮距', '原地小角度转身，头部看墙面AprilTag或地面特征求转角',
       ['marker_vision', 'user_judgment'], '≥6次', '角度残差≤2°', 'wheel_rotation', ['C1头部内参', 'AprilTag贴在墙面']),
    _p('W4', 'wheel', '停车滑行与重复性', 'base', '零速后额外位移与重复性', '随W2记录零速后计数变化',
       ['encoder', 'ground_homography'], '随W2收集', '用于安全余量', None),
    _p('A1', 'arm', '单关节阶跃响应', 'arm', '步长×速度×扭矩上限→到位误差、用时、负载', '已知安全范围内逐关节±5°/10°/20°步进并采样寄存器',
       ['encoder'], '每组合≥3次', '统计稳态误差与回程差', 'joint_step'),
    _p('A2', 'arm', '观测姿态重复性', 'arm', '回到同一观测姿态的偏差', '收起↔观测姿态往返N次，腕相机看固定标定板',
       ['marker_vision'], '≥5次', '标定板位姿离散≤2mm/0.3°', None, ['C1腕部内参']),
    _p('A3', 'arm', '重力负载基线', 'arm', '各姿态静止负载', '随A1/A2采样', ['encoder'], '每姿态≥3次', '用于碰阻判断', None),
    _p('A4', 'arm', '夹爪开口标定', 'arm', '编码器→开口宽度与夹住判定', '低扭矩夹已知宽度物体（10/20/40mm）',
       ['tape_measure'], '每宽度≥3次', '宽度残差≤2mm', 'gripper_width'),
    _p('C1', 'camera', '单相机内参', 'head_or_arm', '焦距、主点、畸变', '9×6内角点25mm棋盘格，每相机≥15个位置角度',
       ['marker_vision'], '每相机≥15张', '重投影RMS≤0.8px', None, ['打印棋盘格并核对尺寸']),
    _p('C2', 'camera', '地面单应', 'head', '导航头部姿态下像素→地面坐标', '棋盘格平放地面并量到车头中心的位置',
       ['tape_measure'], '≥3个验证点', '1m内误差≤1cm', None, ['C1头部内参', '固定头部导航姿态']),
    _p('C3', 'camera', '双目观测姿态标定', 'arm', '观测姿态下左右腕外参', '双臂到观测姿态，标定板放多处使左右同时可见',
       ['marker_vision'], '≥12组', '双目RMS≤1px', None, ['C1左右腕内参', 'A2重复性']),
    _p('C4', 'camera', '手眼标定', 'arm', '末端→相机变换（二级抓取）', '标定板固定，手臂换≥10个姿态', ['marker_vision'], '≥10姿态',
       '平移残差≤5mm', None, ['C1腕部内参']),
    _p('C5', 'camera', '采集时差与曝光', 'none', '左右图采集间隔与亮度差', '记录每次抓拍开始/完成时刻与亮度', ['encoder'], '随各实验',
       '确定测距有效条件', None),
    _p('G1', 'grasp', '双目测距验证', 'none', '双目距离精度', '已知位置物体在0.25–0.7m内多处测量', ['tape_measure'], '≥10点',
       '0.6m内误差≤1cm', 'stereo_range', ['C3双目标定']),
    _p('G2', 'grasp', '预抓取到位', 'arm', '目标基座坐标→手臂到位误差', '手臂到目标上方5cm，头部与腕部相机测偏差', ['marker_vision', 'llm_judgment'],
       '≥5次', '偏差≤1.5cm', None, ['G1达标']),
    _p('G3', 'grasp', '抓取尝试', 'arm', '成功率与失败分类', '低扭矩接近、对准、闭合、抬升复核', ['llm_judgment', 'user_judgment'],
       '每物体≥5次', '记录成功率与失败原因', None, ['G2达标']),
]

# Default promotion thresholds used by learning.validate().
VALIDATION = {'wheel_distance': {'max_rmse_m': 0.003}, 'wheel_rotation': {'max_rmse_deg': 2.0},
              'wheel_speed': {'min_r2': 0.98}, 'gripper_width': {'max_rmse_m': 0.002},
              'stereo_range': {'max_rmse_m': 0.01}, 'joint_step': {'max_abs_error_counts': 30}}


def get(protocol_id):
    for protocol in PROTOCOLS:
        if protocol['id'] == protocol_id:
            return protocol
    raise KeyError(protocol_id)
