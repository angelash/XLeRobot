"""Convert Jom's user-supplied MOSS FBX archive into an attributed local GLB.

Development tool only: Pillow, NumPy, Playwright and a local Chromium required.
The source archive stays untouched. The conversion workspace stays outside Git.
"""
from __future__ import annotations

import argparse
import base64
from functools import partial
from hashlib import sha256, sha512
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import tarfile
import threading
from urllib.request import urlopen
import zipfile

import numpy as np
from PIL import Image

from .optimize_glb import repack
from .state import validate_glb


ROOT=Path(__file__).resolve().parent
GROUPS=['Moss_Main','Moss_arm','Moss_Annexes','Moss_Glass']


def extract_source(archive, work):
    original=(work/'original').resolve()
    with zipfile.ZipFile(archive) as bundle:
        if len(bundle.infolist())>100 or sum(item.file_size for item in bundle.infolist())>512*1024**2:
            raise ValueError('Unexpected model archive size')
        for item in bundle.infolist():
            name=PurePosixPath(item.filename)
            target=original/item.filename
            if name.is_absolute() or '..' in name.parts or not target.resolve().is_relative_to(original):
                raise ValueError('Archive path escapes workspace')
            if item.is_dir():continue
            target.parent.mkdir(parents=True,exist_ok=True)
            target.write_bytes(bundle.read(item))  # ZipFile verifies each CRC.
    return original


def tooling(work):
    manifest=json.loads((ROOT/'static/vendor/manifest.json').read_text(encoding='utf-8'))
    with urlopen(manifest['source'],timeout=30) as response:archive=response.read()
    if 'sha512-'+base64.b64encode(sha512(archive).digest()).decode()!=manifest['integrity']:
        raise ValueError('Pinned Three.js package integrity mismatch')
    target_root=work/'tooling/three'
    with tarfile.open(fileobj=BytesIO(archive),mode='r:gz') as bundle:
        for item in bundle.getmembers():
            if not item.isfile() or not item.name.startswith(('package/build/','package/examples/jsm/')):continue
            relative=PurePosixPath(item.name).relative_to('package')
            target=target_root/str(relative)
            if not target.resolve().is_relative_to(target_root.resolve()):raise ValueError('Tool path escape')
            target.parent.mkdir(parents=True,exist_ok=True)
            target.write_bytes(bundle.extractfile(item).read())
    return manifest


def prepare_textures(original, work):
    prepared=work/'prepared';prepared.mkdir(exist_ok=True)
    config={}
    for group in GROUPS:
        edge=1024 if group=='Moss_Glass' else 2048
        def read(prefix,mode='RGB'):
            path=next((original/'textures').glob(group+'_'+prefix+'*.png'))
            with Image.open(path) as image:
                return image.convert(mode).resize((edge,edge),Image.Resampling.LANCZOS)
        # Source PNGs declare sRGB. Color maps use sRGB; utility maps remain raw.
        base=read('Base_color')
        base_name=group+'-base.jpg'
        if group=='Moss_Glass':
            # glTF stores opacity in baseColor.a; GLTFExporter ignores alphaMap.
            base.putalpha(read('Opacity','L'));base_name=group+'-base.png'
            base.save(prepared/base_name,optimize=True)
        else:base.save(prepared/base_name,quality=94,subsampling=0)
        ao,rough,metal=(read(name,'L') for name in ['Mixed_AO','Roughness','Metallic'])
        Image.merge('RGB',(ao,rough,metal)).save(prepared/(group+'-orm.png'),optimize=True)
        maps={'base':base_name,'orm':group+'-orm.png'}
        if group!='Moss_Glass':
            normal=np.asarray(read('Normal'),dtype=np.float32)/127.5-1
            normal/=np.maximum(np.linalg.norm(normal,axis=2,keepdims=True),1e-6)
            Image.fromarray(np.clip((normal+1)*127.5,0,255).round().astype('uint8')).save(prepared/(group+'-normal.png'),optimize=True)
            maps['normal']=group+'-normal.png'
        if group in ('Moss_Main','Moss_Annexes'):
            read('Emissive').save(prepared/(group+'-emissive.png'),optimize=True)
            maps['emissive']=group+'-emissive.png'
        if group=='Moss_Glass':
            maps['transparent']=True
        config[group]=maps
    (prepared/'textures.json').write_text(json.dumps(config,indent=2),encoding='utf-8')


def convert(work, chromium=None):
    from playwright.sync_api import sync_playwright
    shutil.copyfile(ROOT/'tools/moss-converter.html',work/'convert.html')
    class Quiet(SimpleHTTPRequestHandler):
        def log_message(self,*args):pass
    server=ThreadingHTTPServer(('127.0.0.1',0),partial(Quiet,directory=str(work)))
    threading.Thread(target=server.serve_forever,daemon=True).start()
    try:
        with sync_playwright() as playwright:
            executable=Path(chromium or playwright.chromium.executable_path)
            if not executable.is_file() and os.name=='nt':
                candidates=list((Path(os.environ['LOCALAPPDATA'])/'ms-playwright').glob('chromium-*/chrome-win*/chrome.exe'))
                if candidates:executable=max(candidates,key=lambda p:int(p.parents[1].name.split('-')[-1]))
            browser=playwright.chromium.launch(executable_path=str(executable),headless=True,
                       args=['--use-angle=swiftshader','--enable-unsafe-swiftshader'])
            page=browser.new_page(viewport={'width':1260,'height':964},device_scale_factor=1)
            errors=[];requests=[]
            page.on('pageerror',lambda error:errors.append(str(error)))
            origin=f'http://127.0.0.1:{server.server_port}'
            page.on('request',lambda r:requests.append(r.url) if r.url.startswith(('http:','https:')) and not r.url.startswith(origin+'/') else None)
            page.goto(origin+'/convert.html')
            page.wait_for_function('window.report !== undefined',timeout=120000)
            report=page.evaluate('window.report')
            if not report.get('ready'):raise ValueError(report)
            for name,yaw,pitch in [('quarter',-25,4),('front',0,0),('side',-90,0),('rear',180,0)]:
                page.evaluate('([y,p])=>window.setView(y,p)',[yaw,pitch]);page.wait_for_timeout(100)
                page.locator('canvas').screenshot(path=str(work/f'jom-{name}.png'))
            with page.expect_download(timeout=60000) as download:page.evaluate('window.exportModel()')
            intermediate=work/'moss-550w-jom-unpacked.glb'
            download.value.save_as(str(intermediate))
            browser.close()
            if errors or requests:raise ValueError({'page_errors':errors,'external_requests':requests})
            return intermediate,report
    finally:
        server.shutdown();server.server_close()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--archive',type=Path,required=True)
    parser.add_argument('--work-dir',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--chromium',type=Path)
    args=parser.parse_args()
    work=args.work_dir.resolve();work.mkdir(parents=True,exist_ok=True)
    original=extract_source(args.archive,work)
    print('Source extracted and CRC checked',flush=True)
    dependency=tooling(work)
    prepare_textures(original,work)
    print('PBR maps prepared; converting FBX locally',flush=True)
    intermediate,statistics=convert(work,args.chromium)
    data,receipt=repack(intermediate.read_bytes())
    document=validate_glb(data)
    if statistics['triangles']!=32982 or len(document['meshes'])!=4:
        raise ValueError('Unexpected model content; inspect before adoption')
    args.output.parent.mkdir(parents=True,exist_ok=True);args.output.write_bytes(data)
    receipt.update(statistics=statistics,source_sha256=sha256(args.archive.read_bytes()).hexdigest(),
                   source_bytes=args.archive.stat().st_size,three_version=dependency['version'])
    (work/'conversion-receipt.json').write_text(json.dumps(receipt,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'output':str(args.output),'bytes':len(data),'sha256':receipt['sha256'],'triangles':statistics['triangles']}),flush=True)


if __name__=='__main__':
    main()
