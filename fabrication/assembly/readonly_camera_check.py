"""Capture a short external-camera stream to the null sink; no image files."""
import concurrent.futures
import datetime
import json
from pathlib import Path
import subprocess
import sys

def check(number):
    cmd = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "info",
           "-f", "dshow", "-video_device_number", str(number),
           "-video_size", "640x480", "-framerate", "30",
           "-i", "video=USB2.0_CAM1", "-an", "-frames:v", "30",
           "-f", "null", "-"]
    try:
        p = subprocess.run(cmd, capture_output=True, timeout=25,
                           encoding="utf-8", errors="replace")
        return {"external_camera_number": number, "returncode": p.returncode,
                "log": p.stderr, "command": cmd}
    except subprocess.TimeoutExpired as e:
        return {"external_camera_number": number, "timeout": True,
                "log": str(e.stderr), "command": cmd}

with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
    result = {"timestamp": datetime.datetime.now().astimezone().isoformat(),
              "mode": "three simultaneous external camera streams, no saved frames",
              "cameras": list(pool.map(check, range(3)))}
Path(sys.argv[1]).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
for camera in result["cameras"]:
    print(json.dumps(camera, ensure_ascii=False))
