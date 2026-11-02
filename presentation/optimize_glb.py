"""Repack self-contained GLB images without modifying mesh buffers.

Uses JPEG only for opaque base-color and ORM maps; normals, emissive maps and
alpha-bearing images remain lossless PNG. No external image/decoder dependency.
"""
from __future__ import annotations

import argparse
from hashlib import sha256
from io import BytesIO
import json
from pathlib import Path
import struct

from PIL import Image


def repack(data: bytes, *, quality=94):
    magic, version, length = struct.unpack_from('<4sII', data)
    if (magic, version, length) != (b'glTF', 2, len(data)):
        raise ValueError('Expected a glTF 2.0 binary')
    json_size, json_kind = struct.unpack_from('<II', data, 12)
    if json_kind != 0x4E4F534A:
        raise ValueError('Expected JSON first')
    document = json.loads(data[20:20+json_size])
    bin_size, bin_kind = struct.unpack_from('<II', data, 20+json_size)
    if bin_kind != 0x004E4942:
        raise ValueError('Expected binary buffer')
    binary = data[28+json_size:28+json_size+bin_size]
    if len(document['buffers']) != 1 or 'uri' in document['buffers'][0]:
        raise ValueError('Only a single embedded buffer is supported')

    lossy, lossless = set(), set()
    for material in document.get('materials', []):
        pbr = material.get('pbrMetallicRoughness', {})
        for texture in [pbr.get('baseColorTexture'), pbr.get('metallicRoughnessTexture'), material.get('occlusionTexture')]:
            if texture:
                lossy.add(document['textures'][texture['index']]['source'])
        for texture in [material.get('normalTexture'), material.get('emissiveTexture')]:
            if texture:
                lossless.add(document['textures'][texture['index']]['source'])
    images = {image['bufferView']: (index, image) for index, image in enumerate(document['images'])}
    output = bytearray()
    receipt = []
    for index, view in enumerate(document['bufferViews']):
        offset = view.get('byteOffset', 0)
        original = binary[offset:offset+view['byteLength']]
        chunk = original
        if index in images:
            image_index, metadata = images[index]
            with Image.open(BytesIO(original)) as image:
                has_alpha = 'A' in image.getbands() and image.getchannel('A').getextrema() != (255,255)
                image = image.convert('RGBA' if has_alpha else 'RGB')
                encoded = BytesIO()
                jpeg = image_index in lossy and image_index not in lossless and not has_alpha
                image.save(encoded, format='JPEG' if jpeg else 'PNG',
                           **({'quality':quality,'subsampling':0} if jpeg else {'optimize':True}))
                chunk = encoded.getvalue()
                metadata['mimeType'] = 'image/jpeg' if jpeg else 'image/png'
                receipt.append({'image':image_index,'before':len(original),'after':len(chunk),
                                'mime':metadata['mimeType'],'dimensions':list(image.size),'alpha':has_alpha})
        else:
            # Every non-image buffer view, including positions/normals/UVs,
            # is copied byte-for-byte, with no remeshing or quantization.
            assert chunk == original
        view['byteOffset'] = len(output)
        view['byteLength'] = len(chunk)
        output.extend(chunk)
        output.extend(b'\0' * (-len(output)%4))
    document['buffers'][0]['byteLength'] = len(output)
    payload = json.dumps(document,separators=(',',':'),ensure_ascii=False).encode()
    payload += b' ' * (-len(payload)%4)
    result = (struct.pack('<4sII',b'glTF',2,28+len(payload)+len(output)) +
              struct.pack('<I4s',len(payload),b'JSON') + payload +
              struct.pack('<I4s',len(output),b'BIN\0') + output)
    return result, {'bytes':len(result),'sha256':sha256(result).hexdigest(),'images':receipt,
                    'mesh_buffers':'unchanged','jpeg_quality':quality,'jpeg_subsampling':0}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source',type=Path)
    parser.add_argument('output',type=Path)
    args=parser.parse_args()
    data,receipt=repack(args.source.read_bytes())
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_bytes(data)
    args.output.with_suffix('.receipt.json').write_text(json.dumps(receipt,indent=2),encoding='utf-8')
    print(json.dumps({'output':str(args.output),'bytes':len(data),'sha256':receipt['sha256']}))


if __name__=='__main__':
    main()
