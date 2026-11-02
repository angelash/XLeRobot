from hashlib import sha256
from io import BytesIO
import json
from pathlib import Path
import struct

import numpy as np
from PIL import Image

from presentation.api import create_app
from presentation.optimize_glb import repack
from presentation.state import MAX_AVATAR, Profile, validate_glb
from fastapi.testclient import TestClient


ROOT=Path(__file__).resolve().parents[1]


def parse(data):
    size=struct.unpack_from('<I',data,12)[0]
    return json.loads(data[20:20+size]),data[28+size:]


def test_packaged_default_model_is_complete_attributed_and_within_import_budget(tmp_path):
    directory=ROOT/'static/models'
    manifest=json.loads((directory/'manifest.json').read_text(encoding='utf-8'))
    data=(directory/manifest['file']).read_bytes()
    assert len(data)==manifest['bytes']<MAX_AVATAR
    assert sha256(data).hexdigest()==manifest['sha256']
    document=validate_glb(data)
    assert len(document['meshes'])==4 and len(document['images'])==13
    triangles=sum(document['accessors'][p.get('indices',p['attributes']['POSITION'])]['count']//3
                  for mesh in document['meshes'] for p in mesh['primitives'])
    assert triangles==32982
    assert all('bufferView' in image and 'uri' not in image for image in document['images'])
    _,binary=parse(data)
    glass=next(material for material in document['materials'] if material['name']=='Moss_Glass')
    image=document['images'][document['textures'][glass['pbrMetallicRoughness']['baseColorTexture']['index']]['source']]
    view=document['bufferViews'][image['bufferView']]
    with Image.open(BytesIO(binary[view['byteOffset']:view['byteOffset']+view['byteLength']])) as decoded:
        assert decoded.mode=='RGBA' and decoded.getchannel('A').getextrema()[0]<128
    assert 'CC BY 4.0' in json.dumps(document) and 'Jom' in json.dumps(document)
    assert 'https://creativecommons.org/licenses/by/4.0/' in (directory/'ATTRIBUTION.md').read_text(encoding='utf-8')
    with TestClient(create_app(tmp_path)) as client:
        assert "connect-src 'self' blob:" in client.get('/presence/').headers['Content-Security-Policy']
        profile=client.get('/api/presence/config').json()
        assert profile['avatar']['kind']=='licensed-moss' and profile['avatar']['asset_id'] is None
        profile['name']='CUSTOM NAME'
        assert client.put('/api/presence/config',json=profile).status_code==200
        assert client.get('/presence/assets/models/moss-550w-jom.glb').content==data
        assert Profile.model_validate(profile).name=='CUSTOM NAME'


def test_texture_repack_keeps_geometry_normals_and_transparency():
    rng=np.random.default_rng(550)
    rgba=np.full((64,64,4),127,dtype=np.uint8);rgba[:,:,3]=rng.integers(32,256,(64,64),dtype=np.uint8)
    normal=rng.integers(0,256,(64,64,3),dtype=np.uint8)
    opaque=rng.integers(0,256,(128,128,3),dtype=np.uint8)
    chunks=[struct.pack('<9f',0,0,0,1,0,0,0,1,0)]
    for pixels in [opaque,rgba,normal]:
        stream=BytesIO();Image.fromarray(pixels).save(stream,format='PNG');chunks.append(stream.getvalue())
    binary=bytearray();views=[]
    for chunk in chunks:
        views.append({'buffer':0,'byteOffset':len(binary),'byteLength':len(chunk)})
        binary.extend(chunk);binary.extend(b'\0'*(-len(binary)%4))
    doc={'asset':{'version':'2.0'},'buffers':[{'byteLength':len(binary)}],'bufferViews':views,
         'images':[{'bufferView':i,'mimeType':'image/png'} for i in [1,2,3]],
         'textures':[{'source':i} for i in range(3)],
         'materials':[{'pbrMetallicRoughness':{'baseColorTexture':{'index':0}}},
                      {'pbrMetallicRoughness':{'baseColorTexture':{'index':1}},'alphaMode':'BLEND','normalTexture':{'index':2}}]}
    payload=json.dumps(doc).encode();payload+=b' '*(-len(payload)%4)
    original=struct.pack('<4sII',b'glTF',2,28+len(payload)+len(binary))+struct.pack('<I4s',len(payload),b'JSON')+payload+struct.pack('<I4s',len(binary),b'BIN\0')+binary
    output,receipt=repack(original)
    changed,packed=parse(output)
    assert packed[:36]==chunks[0]
    assert changed['images'][0]['mimeType']=='image/jpeg'
    for index,expected in [(1,rgba),(2,normal)]:
        image=changed['images'][index];view=changed['bufferViews'][image['bufferView']]
        assert image['mimeType']=='image/png'
        decoded=Image.open(BytesIO(packed[view['byteOffset']:view['byteOffset']+view['byteLength']]))
        assert np.array_equal(np.asarray(decoded),expected)
    assert receipt['mesh_buffers']=='unchanged'
