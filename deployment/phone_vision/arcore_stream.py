"""Read the dedicated AR app using the existing authorized ADB transport.

Depth uses the SDK's texture-to-CPU mapping. Cached depth is never refreshed by
a new RGB packet; RGB remains available when tracking or depth is unavailable.
"""
from __future__ import annotations
import subprocess
import socket
import time
from .arcore_capture import HEADER, LIMITS, parse_snapshot


def depth_age_seconds(meta, prefix='depth'):
    """Compare Android Image timestamps, without assuming Frame's clock base."""
    return (meta['rgb_timestamp_ns'] - meta[prefix+'_timestamp_ns']) / 1e9


def packet_age_seconds(meta, device_now):
    return device_now - meta['host_device_monotonic_ms'] / 1000.


class DeviceClock:
    """Conservative host clock bounds, including asymmetric ADB round trips."""
    def __init__(self):
        self.floor=None
        self.ceiling=None
        self.updated=None

    def observe(self, started, device_seconds, finished):
        # /proc/uptime reports hundredths of a second, rounded down.
        floor=started-device_seconds-.01
        ceiling=finished-device_seconds
        self.floor=floor if self.floor is None else max(self.floor,floor)
        self.ceiling=ceiling if self.ceiling is None else min(self.ceiling,ceiling)
        self.updated=finished
        if self.floor>self.ceiling:
            raise ValueError('ARCore设备时钟已改变，请重新连接')

    def acquired_floor(self, device_ms, host_now):
        # Retain a drift allowance; never use network receipt as capture time.
        return device_ms/1000.+self.floor-.005-max(0.,host_now-self.updated)*.0002


def read_packet(connection, stop):
    def read_exact(size):
        chunks=[];remaining=size;deadline=time.monotonic()+20
        while remaining and not stop.is_set():
            try:
                chunk=connection.recv(min(remaining,262144))
            except socket.timeout:
                if time.monotonic()>deadline:raise TimeoutError('ARCore native stream unresponsive')
                continue
            if not chunk:raise EOFError('ARCore native stream disconnected')
            chunks.append(chunk);remaining-=len(chunk)
        if remaining:return None
        return b''.join(chunks)
    header=read_exact(HEADER.size)
    if header is None:return None
    magic,*sizes=HEADER.unpack(header)
    if magic!=b'XLAR1\0\0\0' or any(n>limit for n,limit in zip(sizes,LIMITS)):
        raise ValueError('ARCore native stream bounds invalid')
    payload=read_exact(sum(sizes))
    return None if payload is None else parse_snapshot(header+payload)


def prepare(meta, rgb, auto_depth, raw_depth, confidence, rotation):
    import cv2
    import numpy as np
    from .worker import orient_frame
    color = cv2.imdecode(np.frombuffer(rgb, dtype=np.uint8), cv2.IMREAD_COLOR)
    if color is None:
        raise ValueError('ARCore RGB解码失败')
    iw, ih = meta['image_dimensions']
    if color.shape[:2] != (ih, iw):
        raise ValueError('ARCore CPU相机尺寸与内参不一致')
    details = {
        'rgbd': False, 'camera_id': '0', 'android_mode': 'arcore',
        'sensor_raw_depth': False, 'calibrated': False,
        'confidence_available': False, 'tracking': meta['tracking'],
        'arcore_capture_session': meta['capture_session'],
        'frame_timestamp_ns': meta['frame_timestamp_ns'],
        'rgb_timestamp_ns': meta['rgb_timestamp_ns'],
        'pose_translation': meta['pose_translation'],
        'pose_quaternion': meta['pose_quaternion'],
        'pose_coordinates': 'arcore_world_opengl_not_robot_frame',
        'pose_valid': meta['tracking'] == 'TRACKING',
        'rotation_deg': rotation, 'fps': 2.,
    }
    prefix = 'depth' if auto_depth else 'raw_depth'
    data = auto_depth or raw_depth
    depth = None
    age = depth_age_seconds(meta, prefix) if data else None
    details['depth_age_ms'] = age*1000 if age is not None else None
    if raw_depth:
        details['raw_depth_estimate_age_ms'] = depth_age_seconds(meta, 'raw_depth')*1000
    if not data or meta['tracking'] != 'TRACKING':
        details['depth_unavailable_reason'] = '彩色已连接；ARCore等待跟踪／深度：'+str(meta.get('tracking_failure', meta['tracking']))
    elif not np.frombuffer(data,dtype='<u2').any():
        details['depth_unavailable_reason'] = '彩色已连接；ARCore暂无有效距离，请对准纹理区域缓慢移动'
    elif not -.02 <= age <= .8:
        details['depth_unavailable_reason'] = '彩色已连接；ARCore深度已过期，等待新的深度估计'
    else:
        width, height = meta[prefix+'_width'], meta[prefix+'_height']
        if meta.get('depth_registration') != 'arcore_texture_normalized_to_cpu_image':
            raise ValueError('ARCore缺少SDK坐标映射，请更新采集App')
        corners = np.asarray(meta['depth_uv_to_image_pixels'], dtype=np.float64).reshape(4, 2)
        origin, horizontal, vertical, end = corners
        dx, dy = horizontal-origin, vertical-origin
        # Current camera-axis intrinsics require a positive axis-aligned crop.
        # Reject unfamiliar rotations, reflections or nonlinear transforms.
        if (not np.isfinite(corners).all() or dx[0] <= 0 or dy[1] <= 0
                or abs(dx[1]) > .05 or abs(dy[0]) > .05
                or np.max(np.abs(end-origin-dx-dy)) > .05):
            raise ValueError('ARCore SDK映射超出已验证的相机轴裁剪')
        if np.any(corners < -.1) or np.any(corners > np.array([iw, ih])+.1):
            raise ValueError('ARCore SDK映射超出CPU画面')
        # Same coordinate convention as Google's depth-to-CPU sample: x/w,y/h.
        x, y = np.meshgrid(np.arange(width)/width, np.arange(height)/height)
        map_x = np.asarray(origin[0]+x*dx[0], dtype=np.float32)
        map_y = np.asarray(origin[1]+y*dy[1], dtype=np.float32)
        color = cv2.remap(color, map_x, map_y, cv2.INTER_LINEAR)
        depth = np.frombuffer(data, dtype='<u2').reshape(height, width).astype(np.float32)*.001
        fx, fy = meta['focal_length']; cx, cy = meta['principal_point']
        fx *= width/dx[0]; fy *= height/dy[1]
        cx = (cx-origin[0])*width/dx[0]; cy = (cy-origin[1])*height/dy[1]
        if rotation == 90:
            fx, fy, cx, cy = fy, fx, height-1-cy, cx
        elif rotation == 180:
            cx, cy = width-1-cx, height-1-cy
        elif rotation == 270:
            fx, fy, cx, cy = fy, fx, cy, width-1-cx
        details.update(
            rgbd=True, depth_source='arcore_'+prefix+'_mm',
            intrinsics={'fx': float(fx), 'fy': float(fy), 'tx': float(cx), 'ty': float(cy)},
            intrinsics_image_rotation_deg=rotation, depth_native_dimensions=[width, height],
            registration='arcore_sdk_texture_to_cpu_crop',
            depth_uv_to_image_pixels=corners.ravel().tolist(),
            depth_timestamp_ns=meta[prefix+'_timestamp_ns'],
            positive_depth_fraction=float((depth > 0).mean()),
            depth_02_to_5m_fraction=float(((depth >= .2) & (depth <= 5)).mean()),
        )
        # ARCore confidence applies ONLY to RAW depth, never the automatic map.
        if confidence and raw_depth:
            conf = np.frombuffer(confidence, dtype=np.uint8)
            details.update(raw_depth_confidence_available=True,
                           raw_depth_confidence_mean=float(conf.mean()),
                           raw_depth_high_confidence_fraction=float((conf >= 128).mean()),
                           confidence_scope='arcore_raw_depth_only')
    color = orient_frame(color, rotation)
    details.update(width=int(color.shape[1]), height=int(color.shape[0]))
    ok, jpeg = cv2.imencode('.jpg', color, [cv2.IMWRITE_JPEG_QUALITY, 78])
    if not ok:
        raise ValueError('ARCore JPEG编码失败')
    if depth is None:
        return details, jpeg.tobytes(), b'', b''
    depth = orient_frame(depth, rotation)
    valid = depth > 0
    display = np.where(valid, np.clip((depth-.2)/4.8, 0, 1)*254+1, 0).astype(np.uint8)
    preview = cv2.applyColorMap(255-display, cv2.COLORMAP_TURBO)
    preview[~valid] = 0
    ok, encoded = cv2.imencode('.jpg', preview, [cv2.IMWRITE_JPEG_QUALITY, 78])
    if not ok:
        raise ValueError('ARCore深度预览编码失败')
    return details, jpeg.tobytes(), encoded.tobytes(), np.asarray(depth, dtype='<f4').tobytes()


def stream(args, wire, stop, status):
    from .android_network import select_device, wifi_endpoint
    def adb(*values, binary=False):
        result = subprocess.run([args.adb, *values], capture_output=True, check=True, timeout=8,
                                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        return result.stdout if binary else result.stdout.decode('utf8', 'replace').strip()
    transport = args.android_transport
    if transport == 'wifi':
        try:
            adb('connect', wifi_endpoint(args.android_endpoint))
        except subprocess.CalledProcessError:
            pass
    rows = [line.split() for line in adb('devices').splitlines()[1:] if line.strip()]
    serial, reason = select_device(rows, transport, args.android_endpoint)
    if serial is None:
        status('waiting', reason)
        return
    clock=DeviceClock()
    def sync_clock():
        started=time.monotonic()
        now=float(adb('-s',serial,'shell','cat','/proc/uptime').split()[0])
        clock.observe(started,now,time.monotonic())
    for _ in range(3):sync_clock()
    scid=f'{int(args.session[:8],16)&0x7fffffff:08x}'
    target='localabstract:xlerobot_ar_'+scid
    # singleTop/onNewIntent reconnects the pipe without resetting AR tracking.
    adb('-s', serial, 'shell', 'am', 'start', '-n', 'org.xlerobot.arvision/.MainActivity',
        '--es','receiver_session',scid)
    port=int(adb('-s',serial,'forward','tcp:0',target))
    try:
        with socket.create_connection(('127.0.0.1',port),timeout=8) as connection:
            connection.settimeout(.5)
            _read_loop(args,wire,stop,status,connection,clock,sync_clock,transport)
    finally:
        try:adb('-s',serial,'forward','--remove','tcp:'+str(port))
        except (OSError,subprocess.SubprocessError):pass


def _read_loop(args, wire, stop, status, connection, clock, sync_clock, transport):
    from .protocol import write_message
    last_key = None; sequence = 0; last_device_time=0
    while not stop.is_set():
        packet=read_packet(connection,stop)
        if packet is None:return
        try:
            meta, rgb, depth, raw_depth, confidence = packet
            key = (meta['capture_session'], meta['frame_timestamp_ns'])
            if key == last_key:
                continue
            if not rgb:
                status('waiting', 'ARCore等待相机初始化')
                continue
            now=time.monotonic()
            if now-clock.updated>30:sync_clock()
            if meta['host_device_monotonic_ms']<last_device_time:
                raise ValueError('ARCore设备时钟倒退')
            last_device_time=meta['host_device_monotonic_ms']
            acquired=clock.acquired_floor(last_device_time,now)
            age=time.monotonic()-acquired
            if not -.2 <= age <= .8:
                status('waiting', 'ARCore画面已过期，请保持采集App在前台')
                continue
            details, a, b, values = prepare(meta, rgb, depth, raw_depth, confidence, int(args.android_rotation))
            sequence += 1; last_key = key
            depth_age=max(0.,details['depth_age_ms']/1000.) if details['rgbd'] else 0.
            details.update(kind='frame', sequence=sequence, transport=transport, role=args.android_role,
                           host_acquired_monotonic=min(now,acquired)-depth_age, absolute_latency_verified=False,
                           device_clock_uncertainty_ms=(clock.ceiling-clock.floor)*1000,
                           packet_transport='adb_forward_native_stream')
            write_message(wire, details, a, b, values)
        except subprocess.CalledProcessError:
            raise ConnectionError('ARCore设备时钟检查中断') from None
