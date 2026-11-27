"""Optional VR-only exclusions. This file can restrict channels, never clear a hold."""
import json
from pathlib import Path

PATH = Path(__file__).resolve().parents[3] / '.local-deploy/vr-teleop-policy.json'
CHANNELS = ('left', 'right', 'head', 'base')


def read(path=None):
    path = Path(path) if path is not None else PATH
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding='utf-8-sig'))
        disabled = data['disabled']
        if (data.get('schema') != 1 or not isinstance(disabled, dict)
                or set(disabled) - set(CHANNELS)
                or any(not isinstance(v, str) or not v.strip() for v in disabled.values())):
            raise ValueError('invalid channel exclusions')
        return {k: v[:500] for k, v in disabled.items()}
    except (OSError, ValueError, TypeError, KeyError) as exc:
        raise ValueError('VR通道限制文件无法验证，保持停止') from exc


def motors(disabled, arm_ports):
    result = {}
    for side in ('left', 'right'):
        if side in disabled:
            result.update({(arm_ports[side], i): disabled[side] for i in range(1, 7)})
    if 'head' in disabled:
        result.update({('COM3', i): disabled['head'] for i in (7, 8)})
    if 'base' in disabled:
        result.update({('COM4', i): disabled['base'] for i in (9, 10)})
    return result
