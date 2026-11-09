"""Receive an Android camera over USB and probe Studio's existing vision API.

Requires matching scrcpy 5.0.1 server, an already authorized ADB device,
PyAV, OpenCV and requests. No APK install, audio, screen mirroring or motor I/O.
Source timestamps are encoder timestamps; no optical end-to-end latency claim.
"""
from __future__ import annotations

import argparse
from fractions import Fraction
import hashlib
import json
from pathlib import Path
import secrets
import socket
import struct
import subprocess
import time
from urllib.parse import urlsplit

VERSION = '5.0.1'
MAX_PACKET = 16_000_000


def receive_exact(stream, size):
    data = bytearray()
    while len(data) < size:
        part = stream.recv(size-len(data))
        if not part:
            raise EOFError('Camera transport closed')
        data.extend(part)
    return bytes(data)


def receive_packet(stream):
    header = receive_exact(stream, 12)
    flags_pts, size = struct.unpack('>QI', header)
    # scrcpy 5 distinguishes dimension/session headers using bit 63.
    # Media config/keyframe flags moved to bits 62/61 (PTS is 61 bits).
    if flags_pts & (1 << 63):
        _, width, height = struct.unpack('>III',header)
        if not 0 < width <= 16384 or not 0 < height <= 16384:
            raise ValueError('Invalid camera session dimensions')
        return None, False, b''
    if not 0 < size <= MAX_PACKET:
        raise ValueError('Invalid camera packet size')
    return flags_pts & ((1 << 61)-1), bool(flags_pts & (1 << 62)), receive_exact(stream, size)


def probe(*, adb_path, server_path, output, duration=60, camera_id='0', serial=None, studio_url=None):
    import av
    import cv2
    import numpy as np
    import requests

    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    if not 1 <= duration <= 1800:
        raise ValueError('Probe duration must be 1..1800 seconds')
    if not camera_id or not camera_id.isascii() or not camera_id.isdecimal():
        raise ValueError('Camera ID must be the enumerated numeric Android camera ID')
    if studio_url:
        parsed = urlsplit(studio_url)
        if parsed.scheme != 'http' or parsed.port != 8770 or parsed.path not in ('','/') or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError('Use the existing http://Studio-host:8770 origin')
        studio_url = studio_url.rstrip('/')

    def adb(*args):
        cmd = [str(adb_path)] + (['-s', serial] if serial else []) + list(args)
        return subprocess.run(cmd, capture_output=True, check=True, timeout=15).stdout.decode('utf-8','replace').strip()

    if serial is None:
        devices = [line.split() for line in adb('devices').splitlines()[1:] if line.strip()]
        devices = [row[0] for row in devices if len(row)>1 and row[1]=='device']
        if len(devices) != 1:
            raise ValueError('Exactly one authorized device required; otherwise select --serial')
        serial = devices[0]
    scid = secrets.token_hex(4)
    scid = f'{int(scid,16) & 0x7fffffff:08x}'
    device_server = '/data/local/tmp/xlerobot-phone-'+scid+'.jar'
    socket_name = 'localabstract:scrcpy_'+scid
    proc = None
    channel = None
    port = None
    times, pts_values, decode_ms, sizes, qualities = [], [], [], [], []
    digest = hashlib.sha256()
    error = None
    result = {'transport':'USB ADB / scrcpy camera H.264', 'camera_id':camera_id,
              'scrcpy_server_version':VERSION, 'requested_size':[1280,720], 'requested_fps':15,
              'requested_duration_s':duration, 'audio':False, 'robot_commands':0,
              'studio_quality_samples':qualities}
    before = time.monotonic()
    log = (output/'android-server-private.log').open('wb')
    try:
        adb('push', str(server_path), device_server)
        port = int(adb('forward','tcp:0',socket_name))
        options = [VERSION, 'scid='+scid, 'video_source=camera', 'camera_id='+camera_id,
                   'camera_size=1280x720', 'camera_fps=15', 'video_codec=h264', 'video_bit_rate=4000000',
                   'audio=false', 'control=false', 'tunnel_forward=true', 'cleanup=false',
                   'send_device_meta=false', 'send_stream_meta=false', 'send_dummy_byte=true', 'send_frame_meta=true']
        proc = subprocess.Popen([str(adb_path),'-s',serial,'shell','CLASSPATH='+device_server,
                                 'app_process','/','com.genymobile.scrcpy.Server',*options],
                                stdout=log, stderr=subprocess.STDOUT)
        deadline = time.monotonic()+8
        while True:
            try:
                channel = socket.create_connection(('127.0.0.1',port),timeout=2)
                # adb accepts TCP before the abstract device socket is ready.
                # Require scrcpy's dummy-byte handshake before trusting it.
                if receive_exact(channel,1) != b'\x00':
                    raise ValueError('Unexpected camera transport handshake')
                break
            except (ConnectionRefusedError, EOFError):
                if channel:
                    channel.close()
                    channel = None
                if time.monotonic()>=deadline or proc.poll() is not None:
                    raise RuntimeError('Camera server did not become ready; see private server log')
                time.sleep(.1)
        channel.settimeout(5)
        decoder = av.CodecContext.create('h264','r')
        config = b''
        first = None
        first_frame_deadline = time.monotonic()+10
        next_quality = 2.
        while first is None or time.monotonic()-first < duration:
            if first is None and time.monotonic()>first_frame_deadline:
                raise TimeoutError('No decoded camera frame within 10 seconds')
            pts, is_config, raw = receive_packet(channel)
            if pts is None:
                continue
            if is_config:
                config += raw
                continue
            pkt = av.Packet(config+raw)
            config = b''
            pkt.pts = pkt.dts = pts
            pkt.time_base = Fraction(1,1_000_000)
            tick = time.monotonic()
            frames = decoder.decode(pkt)
            decode_ms.append((time.monotonic()-tick)*1000)
            for frame in frames:
                now = time.monotonic()
                first = now if first is None else first
                times.append(now)
                pts_values.append(pts)
                sizes.append(len(raw))
                bgr = frame.to_ndarray(format='bgr24')
                digest.update(raw)
                cv2.imwrite(str(output/'latest.jpg'),bgr,[cv2.IMWRITE_JPEG_QUALITY,85])
                if len(times)==1:
                    cv2.imwrite(str(output/'first.jpg'),bgr)
                    result['actual_size']=[frame.width,frame.height]
                    result['first_frame_after_launch_s']=round(now-before,3)
                    print(json.dumps({'event':'first_camera_frame','size':result['actual_size']},ensure_ascii=False),flush=True)
                elapsed = now-first
                if studio_url and elapsed>=next_quality:
                    next_quality = elapsed+15
                    ok,jpeg=cv2.imencode('.jpg',bgr,[cv2.IMWRITE_JPEG_QUALITY,80])
                    if not ok:
                        raise ValueError('JPEG encode failed')
                    sent=time.monotonic()
                    try:
                        response=requests.post(studio_url+'/api/platform/vision/quality',data=jpeg.tobytes(),
                                               headers={'Content-Type':'image/jpeg','Origin':studio_url},timeout=3)
                        response.raise_for_status()
                        qualities.append({'frame':len(times),'source_pts_us':pts,'http_roundtrip_ms':round((time.monotonic()-sent)*1000,2),
                                          'response':response.json()})
                    except requests.RequestException as exc:
                        qualities.append({'frame':len(times),'error':type(exc).__name__+': '+str(exc)})
        result['completed'] = True
    except Exception as exc:
        error = type(exc).__name__+': '+str(exc)
        result['completed'] = False
        result['error'] = error
    finally:
        if channel:
            channel.close()
        if proc:
            try:
                proc.wait(timeout=6)
            except subprocess.TimeoutExpired:
                proc.terminate()
                proc.wait(timeout=5)
        log.close()
        if port is not None:
            try:
                adb('forward','--remove','tcp:'+str(port))
            except (subprocess.SubprocessError,OSError):
                result['forward_cleanup_failed']=True
        try:
            adb('shell','rm',device_server)
        except (subprocess.SubprocessError,OSError):
            result['server_file_cleanup_failed']=True
        if times:
            span = times[-1]-times[0]
            source_span=(pts_values[-1]-pts_values[0])/1e6
            result.update(frame_count=len(times),receive_span_s=round(span,3),source_span_s=round(source_span,3),
                          received_fps=round((len(times)-1)/span,3) if span else None,
                          source_fps=round((len(times)-1)/source_span,3) if source_span>0 else None,
                          source_pts_strictly_increasing=all(b>a for a,b in zip(pts_values,pts_values[1:])),
                          host_interframe_p95_ms=round(float(np.percentile(np.diff(times)*1000,95)),2) if len(times)>1 else None,
                          host_max_interframe_ms=round(max(np.diff(times))*1000,2) if len(times)>1 else None,
                          decode_p95_ms=round(float(np.percentile(decode_ms,95)),2),
                          video_megabits_per_s=round(sum(sizes)*8/max(span,.001)/1e6,3),
                          encoded_payload_sha256=digest.hexdigest())
        (output/'result.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(result,ensure_ascii=False,indent=2),flush=True)
    return result


def main():
    import sys
    sys.stdout.reconfigure(encoding='utf-8')
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--adb-path',required=True)
    parser.add_argument('--server-path',required=True)
    parser.add_argument('--output',required=True)
    parser.add_argument('--duration',type=int,default=60)
    parser.add_argument('--camera-id',default='0')
    parser.add_argument('--serial')
    parser.add_argument('--studio-url')
    args=parser.parse_args()
    result=probe(**vars(args))
    raise SystemExit(0 if result.get('completed') else 1)


if __name__=='__main__':
    main()
