"""Reproduce the small, offline Three.js runtime from the pinned npm tarball."""
import base64
import hashlib
from io import BytesIO
import json
from pathlib import Path
import tarfile
from urllib.request import urlopen


VERSION = '0.180.0'
FILES = {
    'build/three.module.min.js': 'three.module.min.js',
    'build/three.core.min.js': 'three.core.min.js',
    'examples/jsm/loaders/GLTFLoader.js': 'addons/loaders/GLTFLoader.js',
    'examples/jsm/exporters/GLTFExporter.js': 'addons/exporters/GLTFExporter.js',
    'examples/jsm/utils/BufferGeometryUtils.js': 'addons/utils/BufferGeometryUtils.js',
    'examples/jsm/controls/OrbitControls.js': 'addons/controls/OrbitControls.js',
    'LICENSE': 'LICENSE',
}


def main():
    with urlopen(f'https://registry.npmjs.org/three/{VERSION}', timeout=30) as response:
        metadata = json.load(response)
    with urlopen(metadata['dist']['tarball'], timeout=30) as response:
        archive = response.read()
    integrity = 'sha512-' + base64.b64encode(hashlib.sha512(archive).digest()).decode()
    if integrity != metadata['dist']['integrity']:
        raise ValueError('npm tarball integrity mismatch')
    root = Path(__file__).resolve().parent / 'static/vendor'
    hashes = {}
    with tarfile.open(fileobj=BytesIO(archive), mode='r:gz') as bundle:
        for source, destination in FILES.items():
            data = bundle.extractfile('package/' + source).read()
            if destination.startswith('addons/'):
                data = data.replace(b"from 'three';", b"from '../../three.module.min.js';")
            path = root / destination
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            hashes[destination] = hashlib.sha256(data).hexdigest()
    (root / 'manifest.json').write_text(json.dumps({'package': 'three', 'version': VERSION,
        'source': metadata['dist']['tarball'], 'integrity': integrity,
        'modification': 'Addon bare three imports changed to local relative imports; all other code unchanged.',
        'sha256': hashes}, indent=2) + '\n', encoding='utf-8', newline='\n')
    print(f'Three.js {VERSION}: {len(hashes)} files; npm integrity and local SHA-256 recorded')


if __name__ == '__main__':
    main()
