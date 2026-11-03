"""Compute capabilities shared by the laptop executor and the workstation node.

Each capability is a pure function: JSON-like payload in, JSON-like result out.
Images arrive as base64 JPEG/PNG text. No capability touches robot hardware.
"""
from __future__ import annotations

import base64
import os
import platform
import time

import numpy as np

REGISTRY = {}
# Implemented only on the workstation node (see worker.py); listed here so the
# Studio can describe and route them even when no node is online.
WORKER_ONLY = {
    'speech.transcribe': {'title': '整句语音识别（faster-whisper）', 'tier': 'worker',
                          'note': '工作端GPU优先；模型须已在本地缓存，未缓存不会自动下载'},
    'llm.chat': {'title': '本地语言/视觉模型对话', 'tier': 'worker',
                 'note': '需要工作端运行OpenAI兼容服务（如Ollama、llama.cpp）'},
    'vision.hand_eye': {'title': '手眼标定（Tsai）', 'tier': 'worker',
                        'note': '车载OpenCV 5.0缺少calibrateHandEye'},
    'vision.find_by_example': {'title': '以图找物（OWL-ViT，抓取知识库模板）', 'tier': 'worker',
                               'note': '工作端CPU约1–2秒/帧；模型须在本地目录，未下载不注册'},
}
TIERS = {'laptop': '笔记本可做', 'either': '优先工作端，可回退笔记本', 'worker': '仅工作端'}


def capability(name, title, tier, note=''):
    def register(function):
        REGISTRY[name] = {'fn': function, 'title': title, 'tier': tier, 'note': note}
        return function
    return register


def describe():
    items = [{'name': k, 'title': v['title'], 'tier': v['tier'], 'note': v['note']} for k, v in REGISTRY.items()]
    items += [{'name': k, **v} for k, v in WORKER_ONLY.items() if k not in REGISTRY]
    return sorted(items, key=lambda x: x['name'])


def run(name, payload):
    entry = REGISTRY.get(name)
    if not entry:
        raise KeyError(name)
    if not isinstance(payload, dict):
        raise ValueError('任务参数必须是JSON对象')
    return entry['fn'](payload)


def _pattern(payload):
    cols, rows = (int(v) for v in payload.get('pattern', (9, 6)))
    square = float(payload.get('square_m', 0.025))
    if not (3 <= cols <= 30 and 3 <= rows <= 30 and 0.002 <= square <= 0.2):
        raise ValueError('标定板规格无效')
    return cols, rows, square


def _images(payload, key):
    values = payload.get(key)
    if not isinstance(values, list) or not values:
        raise ValueError(f'{key}需要非空图像列表')
    if len(values) > 60:
        raise ValueError('单个任务最多60张图')
    return values


@capability('system.echo', '连通与时延测试', 'laptop')
def echo(payload):
    return {'message': str(payload.get('message', ''))[:200], 'node_time': time.time(), 'pid': os.getpid()}


@capability('vision.quality', '画面亮度与清晰度', 'laptop')
def quality(payload):
    import cv2
    from .stereo import decode_image
    gray = decode_image(payload['image'], gray=True)
    return {'width': int(gray.shape[1]), 'height': int(gray.shape[0]), 'brightness': float(gray.mean()),
            'laplacian_variance': float(cv2.Laplacian(gray, cv2.CV_64F).var()),
            'dark_fraction': float((gray < 25).mean()), 'bright_fraction': float((gray > 245).mean())}


@capability('vision.chessboard', '标定板角点检测', 'laptop')
def chessboard(payload):
    from .stereo import decode_image, detect_chessboard
    cols, rows, square = _pattern(payload)
    views = []
    for index, image in enumerate(_images(payload, 'images')):
        started = time.perf_counter()
        gray = decode_image(image, gray=True)
        corners = detect_chessboard(gray, cols, rows)
        entry = {'index': index, 'found': corners is not None, 'image_size': [int(gray.shape[1]), int(gray.shape[0])],
                 'ms': round((time.perf_counter()-started)*1000, 1)}
        if corners is not None:
            span = corners.max(axis=0)-corners.min(axis=0)
            entry.update(corners=corners.round(3).tolist(),
                         coverage=float(span[0]*span[1]/(gray.shape[0]*gray.shape[1])))
        views.append(entry)
    return {'pattern': [cols, rows], 'square_m': square, 'views': views, 'found': sum(v['found'] for v in views)}


def _corner_sets(payload, key):
    """Either precomputed corner lists or images that are detected here."""
    from .stereo import detect_chessboard, decode_image
    cols, rows, _ = _pattern(payload)
    if payload.get(key+'_corners') is not None:
        sets = payload[key+'_corners']
        size = payload.get('image_size')
        if not size:
            raise ValueError('使用角点输入时需要image_size')
        return sets, tuple(size)
    sets = []
    size = None
    for image in _images(payload, key):
        gray = decode_image(image, gray=True)
        size = (gray.shape[1], gray.shape[0])
        sets.append(detect_chessboard(gray, cols, rows))
    return sets, size


@capability('vision.calibrate_intrinsics', '单相机内参标定', 'either')
def calibrate_intrinsics(payload):
    from .stereo import board_points, calibrate_intrinsics as solve
    cols, rows, square = _pattern(payload)
    sets, size = _corner_sets(payload, 'images')
    used = [i for i, corners in enumerate(sets) if corners is not None]
    if len(used) < 3:
        raise ValueError(f'只有{len(used)}张图检测到完整标定板，至少需要3张')
    result = solve([board_points(cols, rows, square)]*len(used), [sets[i] for i in used], size, bool(payload.get('rational')))
    result.update(used_views=used, rejected_views=[i for i in range(len(sets)) if i not in used], pattern=[cols, rows], square_m=square)
    return result


@capability('vision.stereo_calibrate', '双目外参标定（固定观测姿态）', 'either')
def stereo_calibrate(payload):
    from .stereo import board_points, calibrate_intrinsics as solve, stereo_calibrate as stereo
    cols, rows, square = _pattern(payload)
    left_sets, size = _corner_sets(payload, 'left')
    right_sets, size_r = _corner_sets(payload, 'right')
    if tuple(size) != tuple(size_r) or len(left_sets) != len(right_sets):
        raise ValueError('左右图数量或尺寸不一致')
    pairs = [i for i, (a, b) in enumerate(zip(left_sets, right_sets)) if a is not None and b is not None]
    if len(pairs) < 3:
        raise ValueError(f'左右同时检测到标定板的只有{len(pairs)}组，至少需要3组')
    obj = board_points(cols, rows, square)
    left = payload.get('left_intrinsics') or solve([obj]*len([s for s in left_sets if s is not None]), [s for s in left_sets if s is not None], size)
    right = payload.get('right_intrinsics') or solve([obj]*len([s for s in right_sets if s is not None]), [s for s in right_sets if s is not None], size)
    result = stereo([obj]*len(pairs), [left_sets[i] for i in pairs], [right_sets[i] for i in pairs], left, right, size)
    result.update(used_pairs=pairs, pattern=[cols, rows], square_m=square,
                  left_intrinsics_rms_px=left.get('rms_px'), right_intrinsics_rms_px=right.get('rms_px'))
    if payload.get('pose_id'):
        result['pose_id'] = str(payload['pose_id'])[:64]
    return result


@capability('vision.triangulate', '双目像素对三角测量', 'laptop')
def triangulate(payload):
    from .stereo import triangulate as solve
    return solve(payload['stereo'], payload['left_px'], payload['right_px'], float(payload.get('disparity_sigma_px', 0.5)))


@capability('vision.disparity', '校正视差与区域深度', 'either')
def disparity(payload):
    from .stereo import disparity_depth
    return disparity_depth(payload['stereo'], payload['left'], payload['right'], payload.get('roi'),
                           int(payload.get('num_disparities', 96)), int(payload.get('block_size', 5)))


@capability('vision.markers', 'AprilTag/ArUco检测与位姿', 'laptop')
def markers(payload):
    from .stereo import detect_markers
    return detect_markers(payload['image'], payload.get('dictionary', 'apriltag_36h11'), payload.get('marker_length_m'),
                          payload.get('intrinsics'))


@capability('vision.ground_homography', '地面单应标定', 'laptop')
def ground_homography(payload):
    from .stereo import ground_homography as solve
    result = solve(payload['image_px'], payload['floor_m'], payload.get('intrinsics'))
    if payload.get('head_pose'):
        result['head_pose'] = payload['head_pose']
    return result


@capability('vision.ground_project', '像素换算到地面坐标', 'laptop')
def ground_project(payload):
    from .stereo import ground_project as project
    return {'floor_m': project(payload['H'], payload['pixels'], payload.get('intrinsics'))}


def _synthetic_pair():
    from .stereo import render_chessboard
    K = np.array([[320., 0, 320], [0, 320., 240], [0, 0, 1]])
    left = render_chessboard(9, 6, 0.025, K, [0.15, -0.2, 0.05], [-0.09, -0.06, 0.45])
    right = render_chessboard(9, 6, 0.025, K, [0.15, -0.2, 0.05], [-0.19, -0.06, 0.45])
    return K, left, right


@capability('system.benchmark', '标准视觉基准', 'either')
def benchmark(payload):
    """Repeatable micro-benchmarks for compute placement decisions."""
    import cv2
    from .stereo import detect_chessboard, disparity_depth, detect_markers
    repeats = max(1, min(10, int(payload.get('repeats', 3))))
    K, left, right = _synthetic_pair()
    stereo = {'left': {'K': K.tolist(), 'dist': [0]*5}, 'right': {'K': K.tolist(), 'dist': [0]*5},
              'R': np.eye(3).tolist(), 'T': [-0.1, 0, 0], 'image_size': [640, 480]}
    ok, jpeg = cv2.imencode('.jpg', cv2.cvtColor(left, cv2.COLOR_GRAY2BGR))
    tag = cv2.aruco.generateImageMarker(cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_APRILTAG_36h11), 3, 120)
    tag_scene = np.full((480, 640), 255, np.uint8)
    tag_scene[180:300, 260:380] = tag
    tests = {
        'jpeg_decode_640x480': lambda: cv2.imdecode(jpeg, cv2.IMREAD_COLOR),
        'chessboard_detect_640x480': lambda: detect_chessboard(left, 9, 6),
        'apriltag_detect_640x480': lambda: detect_markers(tag_scene),
        'sgbm_disparity_640x480': lambda: disparity_depth(stereo, left, right, None, 96, 5),
        'matmul_256': lambda: np.random.default_rng(1).random((256, 256)) @ np.random.default_rng(2).random((256, 256)),
    }
    timings = {}
    for name, test in tests.items():
        test()
        samples = []
        for _ in range(repeats):
            started = time.perf_counter()
            test()
            samples.append((time.perf_counter()-started)*1000)
        timings[name] = {'median_ms': round(float(np.median(samples)), 2), 'min_ms': round(min(samples), 2)}
    checks = {'chessboard_found': detect_chessboard(left, 9, 6) is not None,
              'apriltag_ids': [m['id'] for m in detect_markers(tag_scene)['markers']]}
    return {'timings': timings, 'checks': checks, 'repeats': repeats, 'cv_threads': cv2.getNumThreads(),
            'opencv': cv2.__version__, 'numpy': np.__version__, 'python': platform.python_version(), 'host': platform.node()}
