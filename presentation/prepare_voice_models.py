"""Prepare a small, hash-pinned local model cache. No audio devices or playback.

Use --archives for offline/Windows enterprise TLS downloads. The manifest pins
official archive bytes and every selected model, not a floating model alias.
"""
import argparse
from hashlib import sha256
import json
from pathlib import Path
import shutil
import tarfile
import urllib.request


ROOT = Path(__file__).resolve().parent
KWS = 'sherpa-onnx-kws-zipformer-zh-en-3M-2025-12-20'
ASR = 'sherpa-onnx-streaming-zipformer-small-bilingual-zh-en-2023-02-16'
SELECTION = {
    'kws': {'encoder': KWS + '/encoder-epoch-13-avg-2-chunk-8-left-64.int8.onnx',
            'decoder': KWS + '/decoder-epoch-13-avg-2-chunk-8-left-64.onnx',
            'joiner': KWS + '/joiner-epoch-13-avg-2-chunk-8-left-64.int8.onnx',
            'tokens': KWS + '/tokens.txt', 'lexicon': KWS + '/en.phone'},
    'asr': {'encoder': ASR + '/encoder-epoch-99-avg-1.int8.onnx',
            'decoder': ASR + '/decoder-epoch-99-avg-1.int8.onnx',
            'joiner': ASR + '/joiner-epoch-99-avg-1.int8.onnx', 'tokens': ASR + '/tokens.txt'},
}


def prepare(root, archives=None):
    root = Path(root)
    spec = json.loads((ROOT / 'voice-models.lock.json').read_text(encoding='utf-8'))
    downloads = Path(archives) if archives else root / 'downloads'
    downloads.mkdir(parents=True, exist_ok=True)
    manifest = {'version': 1, 'sources': spec, 'kws': {}, 'asr': {}, 'vad': {}, 'sha256': {}}
    for section in ('kws', 'asr', 'vad'):
        source = spec[section]
        archive = downloads / source['cache_name']
        if not archive.exists():
            if archives:
                raise ValueError('Missing verified archive: ' + str(archive))
            partial = archive.with_suffix('.download')
            with urllib.request.urlopen(source['url'], timeout=60) as response, partial.open('wb') as target:
                shutil.copyfileobj(response, target)
            partial.replace(archive)
        if sha256(archive.read_bytes()).hexdigest() != source['sha256']:
            raise ValueError('Archive SHA-256 mismatch: ' + str(archive))
        directory = root / section
        directory.mkdir(parents=True, exist_ok=True)
        if section == 'vad':
            selected = [('model', archive.name, archive.read_bytes())]
        else:
            wanted = {name: key for key, name in SELECTION[section].items()}
            selected = []
            # Stream through bz2 exactly once and only copy fixed selected files.
            with tarfile.open(archive, mode='r|bz2') as package:
                for member in package:
                    if member.name in wanted:
                        if not member.isfile() or member.size > 100 * 1024**2:
                            raise ValueError('Unsafe model member')
                        selected.append((wanted.pop(member.name), Path(member.name).name, package.extractfile(member).read()))
            if wanted:
                raise ValueError('Selected model files are missing')
        for key, name, data in selected:
            path = directory / name
            temporary = path.with_suffix('.tmp')
            temporary.write_bytes(data)
            temporary.replace(path)
            relative = path.relative_to(root).as_posix()
            manifest[section][key] = relative
            manifest['sha256'][relative] = sha256(data).hexdigest()
    empty = root / 'kws/keywords.txt'
    empty.write_bytes(b'')
    manifest['kws']['keywords_file'] = 'kws/keywords.txt'
    manifest['sha256']['kws/keywords.txt'] = sha256(b'').hexdigest()
    temporary = root / 'manifest.tmp'
    temporary.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
    temporary.replace(root / 'manifest.json')
    return manifest


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', required=True, help='Existing presence data directory + /voice-models')
    parser.add_argument('--archives', help='Directory with the three exact cache_name files')
    args = parser.parse_args()
    result = prepare(args.root, args.archives)
    print(json.dumps({'prepared': True, 'files': len(result['sha256']), 'recording_started': False}))
