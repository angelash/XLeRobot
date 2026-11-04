"""Object facts and advisory grip assessment. This module never commands motors."""
from __future__ import annotations

import math

MATERIALS = ('unknown', 'rigid_plastic', 'metal', 'glass_ceramic', 'paper_cardboard', 'fabric', 'soft_food')
FRAGILITY = ('unknown', 'durable', 'fragile', 'deformable')
SURFACES = ('unknown', 'smooth', 'rough', 'slippery')
SOURCES = ('unknown', 'user', 'measured', 'catalog', 'visual_estimate')
HAZARDS = ('live_power', 'hot', 'sharp', 'liquid', 'damaged_battery')
FIELDS = {'category', 'material', 'fragility', 'surface', 'mass_g', 'mass_source', 'mass_confidence', 'friction_range',
          'friction_source', 'contact_condition', 'source', 'confidence', 'evidence', 'grasp_region', 'hazards'}


def _number(value):
    return type(value) in (int, float) and math.isfinite(value)


def _range(value, low, high, name):
    if value is None:
        return None
    if (not isinstance(value, list) or len(value) != 2 or not all(_number(v) for v in value)
            or not low <= value[0] <= value[1] <= high):
        raise ValueError(name + '需为有效的上下限区间')
    return list(value)


def normalize_profile(profile):
    if not isinstance(profile, dict) or set(profile) - FIELDS:
        raise ValueError('物品档案包含未知字段或不是对象')
    p = {'category': 'unknown', 'material': 'unknown', 'fragility': 'unknown', 'surface': 'unknown',
         'mass_g': None, 'mass_source': 'unknown', 'mass_confidence': 0.0, 'friction_range': None, 'source': 'unknown',
         'friction_source': 'unknown', 'contact_condition': '',
         'confidence': 0.0, 'evidence': '', 'grasp_region': '', 'hazards': [], **profile}
    for key, choices in [('material', MATERIALS), ('fragility', FRAGILITY), ('surface', SURFACES),
                         ('source', SOURCES), ('mass_source', SOURCES), ('friction_source', SOURCES)]:
        if p[key] not in choices:
            raise ValueError(key + '分类无效')
    if any(not _number(p[key]) or not 0 <= p[key] <= 1 for key in ('confidence', 'mass_confidence')):
        raise ValueError('置信度需在0–1之间')
    for key, limit in [('category', 80), ('evidence', 500), ('grasp_region', 160), ('contact_condition', 200)]:
        if not isinstance(p[key], str) or len(p[key]) > limit:
            raise ValueError(key + '文字过长或类型无效')
    if not isinstance(p['hazards'], list) or any(h not in HAZARDS for h in p['hazards']):
        raise ValueError('危险项分类无效')
    p['hazards'] = sorted(set(p['hazards']))
    p['mass_g'] = _range(p['mass_g'], 0.1, 10000, '重量克数')
    p['friction_range'] = _range(p['friction_range'], 0.01, 2.0, '接触摩擦系数')
    if p['mass_g'] is not None and p['mass_source'] == 'unknown':
        raise ValueError('提供重量估计时必须标明依据')
    if p['friction_range'] is not None and (p['friction_source'] == 'unknown' or not p['contact_condition'].strip()):
        raise ValueError('摩擦系数必须说明来源及夹爪与物体的接触条件')
    return p


def assess_grip(profile=None):
    p = normalize_profile({} if profile is None else profile)
    reasons = []
    blocked = bool(p['hazards'])
    delicate = p['fragility'] in ('fragile', 'deformable') or p['material'] in ('glass_ceramic', 'soft_food', 'fabric')
    if blocked:
        ladder = []; reasons.append('先处理危险项或改用专用工具，不靠加力解决')
    elif delicate:
        ladder = [250, 350] if p['fragility'] != 'deformable' else [200, 300]
        reasons.append('易碎或易变形物品使用更低的候选扭矩上限，观察变形')
    elif p['material'] in ('rigid_plastic', 'metal'):
        ladder = [450, 600, 750] if p['fragility'] == 'durable' and p['confidence'] >= 0.7 else [450, 600]
        reasons.append('刚性物品先确认包覆与接触区域，再按实测反馈逐级加力')
    else:
        ladder = [350, 450]; reasons.append('物性未知，先低力接触观察，补充材质与耐压信息')
    if p['surface'] in ('smooth', 'slippery'):
        reasons.append('光滑表面先改善包覆和防滑接触，滑移时放回重抓，禁止无限加力')
    mass = p['mass_g']
    mass_band = 'unknown' if mass is None else ('light_estimate' if mass[1] <= 100 else
                                              'medium_estimate' if mass[1] <= 300 else 'capacity_review_required')
    if mass is None:
        reasons.append('重量未知；先做有支撑的低高度试提，不估造实际重量')
    if mass_band == 'capacity_review_required':
        reasons.append('估计重量较大，先核验整臂负载能力；较大重量不提高硬上限')
    normal = None
    if mass and p['friction_range']:
        # Two opposing contacts, safety factor 2; a mechanics estimate, not servo calibration.
        mu = p['friction_range']
        normal = [round(2 * mass[0]/1000 * 9.81/(2*mu[1]), 3),
                  round(2 * mass[1]/1000 * 9.81/(2*mu[0]), 3)]
    return {'version': 1, 'execution': 'advisory_only', 'auto_apply': False, 'hazards_block_motion': blocked,
            'mass_band': mass_band, 'mass_source': p['mass_source'], 'mass_confidence': p['mass_confidence'],
            'material_source': p['source'],
            'confidence': p['confidence'], 'torque_ladder_raw': ladder, 'hard_torque_max_raw': 750,
            'per_jaw_normal_force_N_estimate': normal, 'force_to_servo_calibration_available': False,
            'closing_target_raw': None, 'reasons': reasons,
            'required_checks': ['核对抓取区域与两指包覆', '每级检查实际位置、负载和目标领先量',
                                '侧视小幅试提并检查滑移和变形', '稳定后搬运，低处承托后松手']}
