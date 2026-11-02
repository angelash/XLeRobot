"""Camera, stereo and ground-plane geometry.

Pure computation on images and pixel coordinates; nothing here opens a camera
or talks to the robot. Lengths are metres unless a name says otherwise.
"""
from __future__ import annotations

import base64
import math

import cv2
import numpy as np


def decode_image(value, gray=False):
    """Accept raw bytes, base64 text or an ndarray and return an image."""
    if isinstance(value, np.ndarray):
        image = value
    else:
        data = base64.b64decode(value, validate=True) if isinstance(value, str) else bytes(value)
        image = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
        if image is None:
            raise ValueError('图像无法解码')
    if gray and image.ndim == 3:
        image = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    return image


def encode_jpeg(image, quality=90):
    ok, data = cv2.imencode('.jpg', image, [int(cv2.IMWRITE_JPEG_QUALITY), int(quality)])
    if not ok:
        raise ValueError('图像编码失败')
    return base64.b64encode(data.tobytes()).decode('ascii')


def board_points(cols, rows, square):
    """Inner-corner coordinates of a chessboard lying in its own z=0 plane."""
    grid = np.zeros((rows*cols, 3), np.float32)
    grid[:, :2] = np.mgrid[0:cols, 0:rows].T.reshape(-1, 2)*float(square)
    return grid


def detect_chessboard(image, cols, rows):
    """Return refined inner corners (N×2) or None."""
    gray = decode_image(image, gray=True)
    pattern = (int(cols), int(rows))
    corners = None
    if hasattr(cv2, 'findChessboardCornersSB'):
        found, corners = cv2.findChessboardCornersSB(gray, pattern, cv2.CALIB_CB_NORMALIZE_IMAGE | cv2.CALIB_CB_EXHAUSTIVE)
        if not found:
            corners = None
    if corners is None:
        found, corners = cv2.findChessboardCorners(gray, pattern, cv2.CALIB_CB_ADAPTIVE_THRESH | cv2.CALIB_CB_NORMALIZE_IMAGE)
        if not found:
            return None
        criteria = (cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER, 40, 1e-3)
        corners = cv2.cornerSubPix(gray, corners, (5, 5), (-1, -1), criteria)
    return canonical_order(corners.reshape(-1, 2).astype(np.float64))


def canonical_order(corners):
    """Resolve the 180° ordering ambiguity so stereo pairs share corner indices."""
    corners = np.asarray(corners, np.float64).reshape(-1, 2)
    return corners[::-1].copy() if corners[0].sum() > corners[-1].sum() else corners


def _as_points(values, dims=2):
    array = np.asarray(values, dtype=np.float64)
    if array.ndim != 2 or array.shape[1] != dims or not np.isfinite(array).all():
        raise ValueError(f'需要N×{dims}的有限坐标')
    return array


def _camera(calibration):
    K = np.asarray(calibration['K'], np.float64).reshape(3, 3)
    dist = np.asarray(calibration.get('dist', [0, 0, 0, 0, 0]), np.float64).ravel()
    return K, dist


def calibrate_intrinsics(object_points, image_points, image_size, rational=False):
    """Single-camera calibration; returns K, distortion and per-view errors."""
    if len(object_points) < 3 or len(object_points) != len(image_points):
        raise ValueError('内参标定至少需要3组对应的标定板视图')
    objects = [np.asarray(o, np.float32).reshape(-1, 3) for o in object_points]
    images = [np.asarray(i, np.float32).reshape(-1, 2) for i in image_points]
    flags = cv2.CALIB_RATIONAL_MODEL if rational else 0
    rms, K, dist, rvecs, tvecs = cv2.calibrateCamera(objects, images, tuple(int(v) for v in image_size), None, None, flags=flags)
    errors = []
    for obj, img, rvec, tvec in zip(objects, images, rvecs, tvecs):
        projected, _ = cv2.projectPoints(obj, rvec, tvec, K, dist)
        errors.append(float(np.sqrt(np.mean(np.sum((projected.reshape(-1, 2)-img)**2, axis=1)))))
    width, height = image_size
    return {'K': K.tolist(), 'dist': dist.ravel().tolist(), 'rms_px': float(rms), 'image_size': [int(width), int(height)],
            'views': len(objects), 'per_view_rms_px': errors,
            'fov_deg': [math.degrees(2*math.atan(width/(2*K[0, 0]))), math.degrees(2*math.atan(height/(2*K[1, 1])))],
            'poses': [{'rvec': r.ravel().tolist(), 'tvec': t.ravel().tolist()} for r, t in zip(rvecs, tvecs)],
            'model': 'rational' if rational else 'standard'}


def stereo_calibrate(object_points, left_points, right_points, left, right, image_size):
    """Extrinsics of the right camera relative to the left at one fixed arm pose."""
    if len(object_points) < 3 or not len(object_points) == len(left_points) == len(right_points):
        raise ValueError('双目标定至少需要3组左右同时可见的视图')
    K1, d1 = _camera(left)
    K2, d2 = _camera(right)
    objects = [np.asarray(o, np.float32).reshape(-1, 3) for o in object_points]
    lp = [np.asarray(p, np.float32).reshape(-1, 2) for p in left_points]
    rp = [np.asarray(p, np.float32).reshape(-1, 2) for p in right_points]
    criteria = (cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER, 100, 1e-6)
    rms, K1, d1, K2, d2, R, T, E, F = cv2.stereoCalibrate(objects, lp, rp, K1, d1, K2, d2, tuple(int(v) for v in image_size),
                                                          criteria=criteria, flags=cv2.CALIB_FIX_INTRINSIC)
    rvec, _ = cv2.Rodrigues(R)
    return {'left': {'K': K1.tolist(), 'dist': d1.ravel().tolist()}, 'right': {'K': K2.tolist(), 'dist': d2.ravel().tolist()},
            'R': R.tolist(), 'T': T.ravel().tolist(), 'E': E.tolist(), 'F': F.tolist(), 'rms_px': float(rms),
            'baseline_m': float(np.linalg.norm(T)), 'rotation_deg': float(np.degrees(np.linalg.norm(rvec))),
            'image_size': [int(v) for v in image_size], 'views': len(objects)}


def depth_sigma(depth, focal_px, baseline, disparity_sigma_px=0.5):
    """First-order depth uncertainty for a rectified pair: dZ = Z²/(f·B)·dd."""
    if focal_px <= 0 or baseline <= 0:
        raise ValueError('焦距和基线必须为正')
    return float(depth)**2/(float(focal_px)*float(baseline))*float(disparity_sigma_px)


def triangulate(stereo, left_px, right_px, disparity_sigma_px=0.5):
    """Triangulate pixel pairs to left-camera coordinates with reprojection checks."""
    left_px = _as_points(left_px)
    right_px = _as_points(right_px)
    if len(left_px) != len(right_px) or not len(left_px):
        raise ValueError('左右像素点数量必须一致且非空')
    K1, d1 = _camera(stereo['left'])
    K2, d2 = _camera(stereo['right'])
    R = np.asarray(stereo['R'], np.float64).reshape(3, 3)
    T = np.asarray(stereo['T'], np.float64).reshape(3, 1)
    nl = cv2.undistortPoints(left_px.reshape(-1, 1, 2), K1, d1).reshape(-1, 2).T
    nr = cv2.undistortPoints(right_px.reshape(-1, 1, 2), K2, d2).reshape(-1, 2).T
    P1 = np.hstack([np.eye(3), np.zeros((3, 1))])
    P2 = np.hstack([R, T])
    homogeneous = cv2.triangulatePoints(P1, P2, nl, nr)
    points = (homogeneous[:3]/homogeneous[3]).T
    rl, _ = cv2.projectPoints(points, np.zeros(3), np.zeros(3), K1, d1)
    rr, _ = cv2.projectPoints(points, cv2.Rodrigues(R)[0], T, K2, d2)
    err_l = np.linalg.norm(rl.reshape(-1, 2)-left_px, axis=1)
    err_r = np.linalg.norm(rr.reshape(-1, 2)-right_px, axis=1)
    baseline = float(np.linalg.norm(T))
    focal = float((K1[0, 0]+K1[1, 1]+K2[0, 0]+K2[1, 1])/4)
    result = []
    for point, el, er in zip(points, err_l, err_r):
        depth = float(point[2])
        result.append({'xyz_m': [float(v) for v in point], 'distance_m': float(np.linalg.norm(point)), 'depth_m': depth,
                       'reprojection_px': [float(el), float(er)],
                       'depth_sigma_m': depth_sigma(abs(depth), focal, baseline, disparity_sigma_px) if depth > 0 else None,
                       'in_front': bool(depth > 0 and (R @ point.reshape(3, 1)+T)[2, 0] > 0)})
    return {'points': result, 'baseline_m': baseline, 'focal_px': focal, 'frame': 'left_camera'}


def rectify(stereo, alpha=0.0):
    K1, d1 = _camera(stereo['left'])
    K2, d2 = _camera(stereo['right'])
    size = tuple(int(v) for v in stereo['image_size'])
    R = np.asarray(stereo['R'], np.float64).reshape(3, 3)
    T = np.asarray(stereo['T'], np.float64).reshape(3, 1)
    R1, R2, P1, P2, Q, roi1, roi2 = cv2.stereoRectify(K1, d1, K2, d2, size, R, T, flags=cv2.CALIB_ZERO_DISPARITY, alpha=alpha)
    maps = (cv2.initUndistortRectifyMap(K1, d1, R1, P1, size, cv2.CV_32FC1),
            cv2.initUndistortRectifyMap(K2, d2, R2, P2, size, cv2.CV_32FC1))
    return {'R1': R1, 'R2': R2, 'P1': P1, 'P2': P2, 'Q': Q, 'maps': maps, 'size': size}


def disparity_depth(stereo, left_image, right_image, roi=None, num_disparities=96, block_size=5):
    """Semi-global matching on a rectified pair; median depth inside an ROI."""
    rect = rectify(stereo)
    left = decode_image(left_image, gray=True)
    right = decode_image(right_image, gray=True)
    if left.shape[::-1] != rect['size'] or right.shape[::-1] != rect['size']:
        raise ValueError('图像尺寸与双目标定不一致')
    (l1, l2), (r1, r2) = rect['maps']
    left_r = cv2.remap(left, l1, l2, cv2.INTER_LINEAR)
    right_r = cv2.remap(right, r1, r2, cv2.INTER_LINEAR)
    num = max(16, int(num_disparities)//16*16)
    block = int(block_size) | 1
    matcher = cv2.StereoSGBM_create(minDisparity=0, numDisparities=num, blockSize=block, P1=8*block*block, P2=32*block*block,
                                    uniquenessRatio=10, speckleWindowSize=60, speckleRange=2, disp12MaxDiff=1,
                                    mode=cv2.STEREO_SGBM_MODE_SGBM_3WAY)
    disparity = matcher.compute(left_r, right_r).astype(np.float32)/16.0
    focal = float(rect['P1'][0, 0])
    baseline = abs(float(rect['P2'][0, 3])/float(rect['P2'][0, 0]))
    valid = disparity > 0.5
    summary = {'valid_fraction': float(valid.mean()), 'focal_px': focal, 'baseline_m': baseline, 'num_disparities': num}
    if roi is not None:
        x, y, w, h = (int(v) for v in roi)
        window = disparity[max(0, y):y+h, max(0, x):x+w]
        good = window[window > 0.5]
        if good.size >= 10:
            d = float(np.median(good))
            depth = focal*baseline/d
            summary.update(roi=[x, y, w, h], roi_valid_fraction=float(good.size/window.size), disparity_px=d, depth_m=depth,
                           depth_sigma_m=depth_sigma(depth, focal, baseline), disparity_iqr_px=float(np.subtract(*np.percentile(good, [75, 25]))))
        else:
            summary.update(roi=[x, y, w, h], roi_valid_fraction=float(good.size/max(1, window.size)), depth_m=None,
                           note='ROI内有效视差不足，可能是低纹理、遮挡或超出视差范围')
    return summary


def ground_homography(image_points, floor_points, intrinsics=None):
    """Image pixels → floor coordinates (metres, robot frame) for one fixed head pose."""
    image = _as_points(image_points)
    floor = _as_points(floor_points)
    if len(image) < 4 or len(image) != len(floor):
        raise ValueError('地面单应至少需要4组像素—地面对应点')
    undistorted = image
    if intrinsics:
        K, dist = _camera(intrinsics)
        undistorted = cv2.undistortPoints(image.reshape(-1, 1, 2), K, dist, P=K).reshape(-1, 2)
    H, _ = cv2.findHomography(undistorted, floor, 0)
    if H is None:
        raise ValueError('对应点退化，无法求单应')
    back = cv2.perspectiveTransform(undistorted.reshape(-1, 1, 2), H).reshape(-1, 2)
    residual = np.linalg.norm(back-floor, axis=1)
    return {'H': H.tolist(), 'points': len(image), 'rms_m': float(np.sqrt(np.mean(residual**2))),
            'max_error_m': float(residual.max()), 'undistorted': bool(intrinsics), 'frame': 'robot_floor_xy'}


def ground_project(homography, pixels, intrinsics=None):
    pixels = _as_points(pixels)
    if intrinsics:
        K, dist = _camera(intrinsics)
        pixels = cv2.undistortPoints(pixels.reshape(-1, 1, 2), K, dist, P=K).reshape(-1, 2)
    H = np.asarray(homography, np.float64).reshape(3, 3)
    return cv2.perspectiveTransform(pixels.reshape(-1, 1, 2), H).reshape(-1, 2).tolist()


def detect_markers(image, dictionary='apriltag_36h11', marker_length=None, intrinsics=None):
    names = {'apriltag_36h11': cv2.aruco.DICT_APRILTAG_36h11, 'aruco_4x4_50': cv2.aruco.DICT_4X4_50}
    if dictionary not in names:
        raise ValueError('不支持的标记字典')
    gray = decode_image(image, gray=True)
    detector = cv2.aruco.ArucoDetector(cv2.aruco.getPredefinedDictionary(names[dictionary]), cv2.aruco.DetectorParameters())
    corners, ids, _ = detector.detectMarkers(gray)
    markers = []
    for corner, marker_id in zip(corners or [], [] if ids is None else ids.ravel()):
        entry = {'id': int(marker_id), 'corners_px': corner.reshape(-1, 2).tolist()}
        if marker_length and intrinsics:
            K, dist = _camera(intrinsics)
            half = float(marker_length)/2
            obj = np.array([[-half, half, 0], [half, half, 0], [half, -half, 0], [-half, -half, 0]], np.float32)
            ok, rvec, tvec = cv2.solvePnP(obj, corner.reshape(-1, 2).astype(np.float32), K, dist, flags=cv2.SOLVEPNP_IPPE_SQUARE)
            if ok:
                entry.update(rvec=rvec.ravel().tolist(), tvec_m=tvec.ravel().tolist(), distance_m=float(np.linalg.norm(tvec)))
        markers.append(entry)
    return {'markers': markers, 'dictionary': dictionary}


def hand_eye(gripper_rotations, gripper_translations, target_rotations, target_translations):
    """Gripper→camera transform (Tsai). Requires an OpenCV build with calibrateHandEye."""
    if not hasattr(cv2, 'calibrateHandEye'):
        raise RuntimeError('当前OpenCV缺少calibrateHandEye，请改由工作端计算')
    if len(gripper_rotations) < 3:
        raise ValueError('手眼标定至少需要3个姿态')
    as_mats = lambda values: [np.asarray(v, np.float64).reshape(3, 3) if np.size(v) == 9 else cv2.Rodrigues(np.asarray(v, np.float64))[0] for v in values]
    R, t = cv2.calibrateHandEye(as_mats(gripper_rotations), [np.asarray(v, np.float64).reshape(3, 1) for v in gripper_translations],
                                as_mats(target_rotations), [np.asarray(v, np.float64).reshape(3, 1) for v in target_translations],
                                method=cv2.CALIB_HAND_EYE_TSAI)
    return {'R': R.tolist(), 't_m': t.ravel().tolist(), 'method': 'tsai'}


def render_chessboard(cols, rows, square, K, rvec, tvec, size=(640, 480), dist=None):
    """Synthetic view of a chessboard, used by benchmarks and tests."""
    pixels_per_m = 2000
    margin = square
    width = int(((cols+1)*square+2*margin)*pixels_per_m)
    height = int(((rows+1)*square+2*margin)*pixels_per_m)
    board = np.full((height, width), 255, np.uint8)
    cell = int(square*pixels_per_m)
    offset = int(margin*pixels_per_m)
    for r in range(rows+1):
        for c in range(cols+1):
            if (r+c) % 2 == 0:
                board[offset+r*cell:offset+(r+1)*cell, offset+c*cell:offset+(c+1)*cell] = 0
    # Inner corner (0,0) sits one square plus the margin inside the image.
    origin = margin+square
    corners_m = np.array([[-origin, -origin, 0], [width/pixels_per_m-origin, -origin, 0],
                          [width/pixels_per_m-origin, height/pixels_per_m-origin, 0], [-origin, height/pixels_per_m-origin, 0]], np.float32)
    projected, _ = cv2.projectPoints(corners_m, np.asarray(rvec, np.float64), np.asarray(tvec, np.float64),
                                     np.asarray(K, np.float64), np.zeros(5) if dist is None else np.asarray(dist, np.float64))
    src = np.array([[0, 0], [width, 0], [width, height], [0, height]], np.float32)
    H = cv2.getPerspectiveTransform(src, projected.reshape(-1, 2).astype(np.float32))
    image = cv2.warpPerspective(board, H, tuple(size), borderValue=200)
    return image
