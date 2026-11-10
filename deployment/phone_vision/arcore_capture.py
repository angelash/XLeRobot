"""Read bounded atomic snapshots produced by the local ARCore Android probe."""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
import struct
import subprocess
import time

HEADER=struct.Struct('>8sIIIII')
LIMITS=(64_000,3_000_000,2_000_000,2_000_000,1_000_000)
REMOTE='/sdcard/Android/data/org.xlerobot.arvision/files/snapshot.bin'


def parse_snapshot(raw):
    if len(raw)<HEADER.size:raise ValueError('ARCore snapshot header incomplete')
    magic,*sizes=HEADER.unpack_from(raw)
    if magic!=b'XLAR1\0\0\0' or any(n>limit for n,limit in zip(sizes,LIMITS)) or len(raw)!=HEADER.size+sum(sizes):
        raise ValueError('ARCore snapshot bounds invalid')
    parts=[];offset=HEADER.size
    for n in sizes:parts.append(raw[offset:offset+n]);offset+=n
    meta=json.loads(parts[0])
    if not isinstance(meta,dict) or meta.get('schema')!=1 or not isinstance(meta.get('capture_session'),str):raise ValueError('ARCore snapshot metadata invalid')
    for index,prefix,bpp in ((2,'depth',2),(3,'raw_depth',2),(4,'confidence',1)):
        if parts[index]:
            width,height=meta.get(prefix+'_width'),meta.get(prefix+'_height')
            if type(width)!=int or type(height)!=int or not 0<width<=4096 or not 0<height<=4096 or len(parts[index])!=width*height*bpp:
                raise ValueError('ARCore depth dimensions invalid')
    if parts[1] and not parts[1].startswith(b'\xff\xd8'):raise ValueError('ARCore RGB must be JPEG')
    return meta,*parts[1:]


def probe(adb,output,*,serial=None,duration=60):
    if not 1<=duration<=180:raise ValueError('ARCore probe duration must be 1..180 seconds')
    root=Path(output);root.mkdir(parents=True,exist_ok=False)
    report={'samples':[],'robot_commands':0,'capture_started_by_probe':False};seen=set();started=time.monotonic();deadline=started+duration
    while time.monotonic()<deadline:
        command=[str(adb)]+(['-s',serial] if serial else ['-d'])+['exec-out','cat',REMOTE]
        result=subprocess.run(command,capture_output=True,timeout=8)
        if result.returncode:report['last_error']='Native AR snapshot not available';time.sleep(.5);continue
        try:meta,rgb,depth,raw_depth,confidence=parse_snapshot(result.stdout)
        except (ValueError,UnicodeDecodeError) as exc:report['last_error']=str(exc);time.sleep(.5);continue
        key=(meta['capture_session'],meta['frame_timestamp_ns'])
        if key in seen:time.sleep(.2);continue
        seen.add(key);row=dict(meta);row['snapshot_sha256']=hashlib.sha256(result.stdout).hexdigest();row['received_elapsed_s']=time.monotonic()-started;report['samples'].append(row)
        if rgb and (depth or raw_depth) and meta['tracking']=='TRACKING':
            (root/'latest-snapshot.bin').write_bytes(result.stdout)
            for name,data in (('rgb.jpg',rgb),('depth.u16',depth),('raw-depth.u16',raw_depth),('confidence.u8',confidence)):
                if data:(root/name).write_bytes(data)
            (root/'latest.json').write_text(json.dumps(meta,indent=2),encoding='utf8')
        time.sleep(.3)
    valid=[s for s in report['samples'] if s.get('tracking')=='TRACKING' and (s.get('depth_positive_fraction',0)>0 or s.get('raw_depth_positive_fraction',0)>0)]
    report['tracking_depth_samples']=len(valid)
    report['distinct_depth_timestamps']=len({s.get('depth_timestamp_ns',s.get('raw_depth_timestamp_ns')) for s in valid})
    report['ok']=len(valid)>=5 and report['distinct_depth_timestamps']>=5
    report['elapsed_s']=time.monotonic()-started
    (root/'result.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf8')
    return report


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--adb',required=True);p.add_argument('--output',required=True);p.add_argument('--serial');p.add_argument('--duration',type=int,default=60)
    a=p.parse_args();r=probe(a.adb,a.output,serial=a.serial,duration=a.duration)
    print(json.dumps({'ok':r['ok'],'samples':len(r['samples']),'last_tracking':r['samples'][-1].get('tracking') if r['samples'] else None,'robot_commands':0}))
