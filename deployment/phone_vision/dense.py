"""Bounded RGB-D odometry and TSDF surface fusion, independent of actuators.

Open3D is imported only by the optional reconstruction worker. No holes are
filled from photographs, and each input retains its transport/depth semantics.
"""
from __future__ import annotations
from datetime import datetime
import hashlib
import json
import math
import os
from pathlib import Path
import warnings
import time
import uuid

for name in ('OMP_NUM_THREADS', 'OPENBLAS_NUM_THREADS', 'MKL_NUM_THREADS'):
    os.environ.setdefault(name, '1')


def stamp():
    return datetime.now().astimezone().isoformat()


def atomic(path, data):
    temporary = path.with_name(path.name+'.'+uuid.uuid4().hex+'.tmp')
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf8')
    for attempt in range(6):
        try:
            os.replace(temporary, path)
            return
        except PermissionError:
            if attempt == 5:
                raise
            time.sleep(.02*(attempt+1))


def checked_file(root, name):
    if not isinstance(name, str) or Path(name).name != name or name in ('.', '..'):
        raise ValueError('Invalid source filename')
    path = (root / name).resolve()
    if path.parent != root.resolve() or not path.is_file():
        raise ValueError('Missing source file')
    return path


def load_frame(folder, row):
    import cv2
    import numpy as np
    meta = row['meta']
    width, height = meta['width'], meta['height']
    if type(width) is not int or type(height) is not int or not 16 <= width <= 1024 or not 16 <= height <= 1024:
        raise ValueError('Unsupported depth dimensions')
    k = [float(meta['intrinsics'][name]) for name in ('fx', 'fy', 'tx', 'ty')]
    if not all(math.isfinite(v) for v in k) or min(k[:2]) <= 0:
        raise ValueError('Invalid intrinsics')
    raw_path, rgb_path = (checked_file(folder, row[name]) for name in ('depth_file', 'rgb_file'))
    raw, encoded = raw_path.read_bytes(), rgb_path.read_bytes()
    if len(raw) != width * height * 4 or len(encoded) > 4_000_000:
        raise ValueError('Invalid RGB-D payload size')
    if hashlib.sha256(raw).hexdigest() != row['depth_sha256'] or hashlib.sha256(encoded).hexdigest() != row['rgb_sha256']:
        raise ValueError('Input hash mismatch')
    rgb = cv2.imdecode(np.frombuffer(encoded, np.uint8), cv2.IMREAD_COLOR)
    if rgb is None or rgb.shape[:2] != (height, width):
        raise ValueError('RGB and depth not aligned in dimensions')
    depth = np.frombuffer(raw, dtype='<f4').reshape(height, width).copy()
    valid = np.isfinite(depth) & (depth >= .2) & (depth <= 2.5)
    depth[~valid] = 0
    # Smooth measured pixels locally; preserve every original invalid pixel.
    filtered = cv2.medianBlur(depth, 3)
    filtered[~valid] = 0
    return cv2.cvtColor(rgb, cv2.COLOR_BGR2RGB), np.ascontiguousarray(filtered), k, float(valid.mean())


def reconstruct(folder, output, *, voxel_m=.008, pose_mode='odometry', stop=lambda: False, progress=lambda value: None):
    import cv2
    import numpy as np
    import open3d as o3d
    folder, output = Path(folder), Path(output)
    if output.exists():
        raise ValueError('Preserve existing reconstruction')
    output.mkdir(parents=True)
    source = json.loads((folder / 'frames.json').read_text('utf8'))
    rows = source['frames']
    if not 3 <= len(rows) <= 90 or not .004 <= voxel_m <= .02 or pose_mode not in ('fixed', 'odometry'):
        raise ValueError('Need 3-90 frames and a bounded voxel size')
    if len({r['session'] for r in rows}) != 1:
        raise ValueError('Capture session changed; do not fuse across sessions')
    if len({r['sequence'] for r in rows}) != len(rows):
        raise ValueError('Duplicate source frame')
    volume = o3d.pipelines.integration.ScalableTSDFVolume(
        voxel_length=voxel_m, sdf_trunc=max(.03, voxel_m*4),
        color_type=o3d.pipelines.integration.TSDFVolumeColorType.RGB8)
    report = {'schema': 1, 'started_at': stamp(), 'engine': 'Open3D '+o3d.__version__+' RGB-D odometry + point-to-plane ICP + TSDF',
              'capture_session': rows[0]['session'], 'input_frames': len(rows), 'accepted': [], 'rejected': [],
              'voxel_m': voxel_m, 'depth_range_m': [.2, 2.5], 'motion_commands': 0,
              'pose_mode': pose_mode, 'fixed_pose_condition': 'camera_and_scene_stationary' if pose_mode == 'fixed' else None,
              'metric_accuracy_verified': False, 'registered_robot_map': False, 'navigation_validated': False}
    atomic(output / 'report.json', report)
    previous = previous_cloud = None
    pose = np.eye(4)
    trajectory = []
    previous_signature = None
    reference_depth = None
    last_time = None
    fixed_depths, fixed_colors = [], []
    for row in rows:
        if stop():
            report['cancelled'] = True
            atomic(output / 'report.json', report)
            raise InterruptedError('Dense reconstruction cancelled')
        try:
            color, depth, k, fraction = load_frame(folder, row)
            signature = (depth.shape, tuple(round(v, 4) for v in k), row['meta'].get('depth_source'))
            if previous_signature is not None and signature != previous_signature:
                raise ValueError('Resolution, intrinsics or depth source changed; start a new reconstruction')
            acquired = row['meta'].get('host_acquired_monotonic')
            if acquired is not None and last_time is not None and not 0 < acquired-last_time <= 2:
                raise ValueError('Acquisition gap exceeds local alignment budget')
            if fraction < .15 or np.count_nonzero(depth) < 1000:
                raise ValueError('Insufficient measured depth')
            h, w = depth.shape
            intrinsic = o3d.camera.PinholeCameraIntrinsic(w, h, *k)
            rgbd = o3d.geometry.RGBDImage.create_from_color_and_depth(
                o3d.geometry.Image(color), o3d.geometry.Image(depth), depth_scale=1., depth_trunc=2.5,
                convert_rgb_to_intensity=False)
            cloud = o3d.geometry.PointCloud.create_from_rgbd_image(rgbd, intrinsic).voxel_down_sample(.015)
            if len(cloud.points) < 500:
                raise ValueError('Insufficient registration geometry')
            cloud.estimate_normals(o3d.geometry.KDTreeSearchParamHybrid(radius=.06, max_nn=25))
            quality = {'fitness': 1., 'rmse_m': 0., 'translation_m': 0., 'rotation_deg': 0.}
            candidate_pose = pose
            if previous is not None:
                options = o3d.pipelines.odometry.OdometryOption()
                options.depth_diff_max, options.depth_min, options.depth_max = .06, .2, 2.5
                success, transform, information = o3d.pipelines.odometry.compute_rgbd_odometry(
                    previous, rgbd, intrinsic, np.eye(4),
                    o3d.pipelines.odometry.RGBDOdometryJacobianFromHybridTerm(), options)
                if not success or not np.isfinite(transform).all():
                    raise ValueError('RGB-D odometry failed')
                fit = o3d.pipelines.registration.registration_icp(
                    cloud, previous_cloud, .045, np.linalg.inv(transform),
                    o3d.pipelines.registration.TransformationEstimationPointToPlane(),
                    o3d.pipelines.registration.ICPConvergenceCriteria(max_iteration=25))
                delta = fit.transformation
                rotation = math.degrees(math.acos(float(np.clip((np.trace(delta[:3, :3])-1)/2, -1, 1))))
                distance = float(np.linalg.norm(delta[:3, 3]))
                quality = {'fitness': float(fit.fitness), 'rmse_m': float(fit.inlier_rmse),
                           'translation_m': distance, 'rotation_deg': rotation}
                if fit.fitness < .65 or fit.inlier_rmse > .02 or distance > .08 or rotation > 8:
                    raise ValueError('Frame alignment rejected: '+json.dumps(quality))
                # Both geometry and RGB-D estimates must agree on this short step.
                disagreement = np.linalg.inv(delta) @ np.linalg.inv(transform)
                if np.linalg.norm(disagreement[:3, 3]) > .03:
                    raise ValueError('RGB-D and geometric translation disagree')
                if pose_mode == 'fixed':
                    # Fixed mounting is an explicit capture condition, not inferred
                    # from a noisy sum of frame-to-frame estimates.
                    overlap = (reference_depth > 0) & (depth > 0)
                    changed = np.abs(reference_depth[overlap]-depth[overlap]) > .08
                    if distance > .025 or rotation > 2 or overlap.mean() < .15 or changed.mean() > .2:
                        raise ValueError('Fixed-view condition changed; keep this result local and recapture')
                else:
                    candidate_pose = pose @ delta
            if pose_mode == 'fixed':
                fixed_depths.append(depth)
                fixed_colors.append(color)
            else:
                volume.integrate(rgbd, intrinsic, np.linalg.inv(candidate_pose))
            pose = candidate_pose
            previous, previous_cloud, previous_signature = rgbd, cloud, signature
            last_time = acquired
            if reference_depth is None:
                reference_depth = depth.copy()
            trajectory.append(pose[:3, 3].tolist())
            report['accepted'].append({'index': row['index'], 'sequence': row['sequence'],
                'in_range_fraction': fraction, **quality, 'camera_to_local': pose.tolist(),
                'source_rgb_sha256': row['rgb_sha256'], 'source_depth_sha256': row['depth_sha256']})
        except (ValueError, RuntimeError) as exc:
            report['rejected'].append({'index': row['index'], 'sequence': row['sequence'], 'reason': str(exc)})
            if previous_signature is not None and ('changed' in str(exc) or 'gap exceeds' in str(exc)):
                # A stream discontinuity ends this fragment; never bridge it.
                report['ended_at_discontinuity'] = True
                atomic(output / 'report.json', report)
                break
        atomic(output / 'report.json', report)
        progress({'processed': len(report['accepted'])+len(report['rejected']), 'accepted': len(report['accepted']), 'total': len(rows)})
    if len(report['accepted']) < 3:
        raise ValueError('Fewer than three aligned measured frames')
    if pose_mode == 'fixed':
        # Temporal consensus reduces hue-video jitter without adding unmeasured
        # pixels or stretching noisy geometry into false camera motion.
        stack = np.stack(fixed_depths)
        support = (stack > 0).sum(axis=0)
        stack[stack == 0] = np.nan
        with warnings.catch_warnings():
            warnings.simplefilter('ignore', RuntimeWarning)
            median = np.nanmedian(stack, axis=0)
            mad = np.nanmedian(np.abs(stack-median), axis=0)
        supported = (support >= max(3, math.ceil(len(stack)*.6))) & (mad <= .04)
        fused = np.where(supported, median, 0).astype(np.float32)
        color = np.median(np.stack(fixed_colors), axis=0).astype(np.uint8)
        rgbd = o3d.geometry.RGBDImage.create_from_color_and_depth(
            o3d.geometry.Image(color), o3d.geometry.Image(fused), depth_scale=1., depth_trunc=2.5,
            convert_rgb_to_intensity=False)
        volume.integrate(rgbd, intrinsic, np.eye(4))
        cv2.imwrite(str(output/'support.png'), np.round(support/len(stack)*255).astype(np.uint8))
        report['temporal_filter'] = {'frames': len(stack), 'minimum_support_fraction': .6,
            'maximum_median_deviation_m': .04, 'supported_pixel_fraction': float(supported.mean()),
            'unseen_pixels_filled': 0}
        report['engine'] += ' / fixed temporal median with measured support'
    mesh = volume.extract_triangle_mesh()
    mesh.remove_degenerate_triangles().remove_duplicated_triangles().remove_duplicated_vertices().remove_unreferenced_vertices()
    vertices, triangles = np.asarray(mesh.vertices), np.asarray(mesh.triangles)
    if len(vertices) < 500 or len(triangles) < 500:
        raise ValueError('Insufficient fused surface')
    # Bound public viewing size, retaining the original fused mesh in PLY.
    mesh.compute_vertex_normals()
    o3d.io.write_triangle_mesh(str(output / 'surface-full.ply'), mesh)
    if len(triangles) > 180000:
        mesh = mesh.simplify_quadric_decimation(180000)
        mesh.remove_unreferenced_vertices().compute_vertex_normals()
    if len(mesh.vertices) > 100000:
        mesh = mesh.simplify_quadric_decimation(120000)
        mesh.remove_unreferenced_vertices().compute_vertex_normals()
    if len(mesh.vertices) > 100000 or len(mesh.triangles) > 180000:
        raise ValueError('Public surface exceeds geometry budget')
    vertices = np.asarray(mesh.vertices).copy()
    triangles = np.asarray(mesh.triangles)
    colors = np.asarray(mesh.vertex_colors)
    vertices[:, 1:] *= -1  # CV camera space to display OpenGL; det=+1.
    data = {'schema': 'tsdf_surface_v1', 'positions': np.round(vertices, 5).reshape(-1).tolist(),
            'colors': np.round(np.clip(colors, 0, 1)*255).astype(int).reshape(-1).tolist(),
            'indices': triangles.reshape(-1).tolist(), 'count': len(vertices), 'triangle_count': len(triangles),
            'coordinates': 'local_rgbd_camera_opengl', 'trajectory': [[p[0], -p[1], -p[2]] for p in trajectory],
            'metric_accuracy_verified': False, 'registered_robot_map': False, 'navigation_validated': False}
    (output / 'surface.json').write_text(json.dumps(data, separators=(',', ':')), encoding='utf8')
    display_mesh = o3d.geometry.TriangleMesh(mesh)
    display_mesh.vertices = o3d.utility.Vector3dVector(vertices)
    display_mesh.compute_vertex_normals()
    o3d.io.write_triangle_mesh(str(output / 'surface.ply'), display_mesh)
    if (output / 'surface.json').stat().st_size > 12*1024*1024:
        raise ValueError('Public surface exceeds viewing budget')
    # Connectivity describes the observed surface only; holes are not filled.
    _, counts, areas = mesh.cluster_connected_triangles()
    counts = np.asarray(counts)
    report.update(finished_at=stamp(), surface_vertices=len(mesh.vertices), surface_triangles=len(mesh.triangles),
        components=int(len(counts)), largest_component_triangle_fraction=float(counts.max()/counts.sum()),
        trajectory_extent_m=np.ptp(np.asarray(trajectory), axis=0).tolist(),
        measured_depth_fraction_median=float(np.median([r['in_range_fraction'] for r in report['accepted']])),
        alignment_rmse_median_m=float(np.median([r['rmse_m'] for r in report['accepted'][1:]])),
        source_depth_semantics=rows[0]['meta'].get('depth_source', 'unknown'),
        sensor_raw_depth=rows[0]['meta'].get('sensor_raw_depth', False),
        coverage='observed_local_surfaces_only_no_unseen_hole_filling',
        coordinates={'surface.ply': 'local_rgbd_camera_opengl', 'surface-full.ply': 'local_rgbd_camera_opencv'},
        frozen_input_manifest_sha256=hashlib.sha256((folder/'frames.json').read_bytes()).hexdigest())
    atomic(output / 'report.json', report)
    preview = checked_file(folder, rows[0]['rgb_file']).read_bytes()
    (output / 'preview.jpg').write_bytes(preview)
    manifest = {p.name: {'bytes': p.stat().st_size, 'sha256': hashlib.sha256(p.read_bytes()).hexdigest()}
                for p in output.iterdir() if p.name in ('surface.json', 'surface.ply', 'surface-full.ply', 'preview.jpg', 'support.png', 'report.json')}
    atomic(output / 'manifest.json', manifest)
    return report


def main():
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', required=True)
    parser.add_argument('--output', required=True)
    parser.add_argument('--voxel', type=float, default=.008)
    parser.add_argument('--pose-mode', choices=('fixed', 'odometry'), default='odometry')
    args = parser.parse_args()
    report = reconstruct(args.input, args.output, voxel_m=args.voxel, pose_mode=args.pose_mode)
    print(json.dumps({k: v for k, v in report.items() if k not in ('accepted', 'rejected')}, ensure_ascii=False))


if __name__ == '__main__':
    main()
