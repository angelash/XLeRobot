"""Public-format map conversion. External free pixels never enter robot maps."""
from __future__ import annotations

import math

from .metric import exact, number


def valetudo_v2(value):
    if not isinstance(value, dict) or value.get('__class') != 'ValetudoMap':
        raise ValueError('prior: expected a ValetudoMap v2 object')
    meta = value.get('metaData')
    if not isinstance(meta, dict) or meta.get('version') != 2:
        raise ValueError('prior: Valetudo map version 2 required')
    size = value.get('size')
    exact(size, ('x', 'y'), 'prior.size')
    x_cm = number(size['x'], 'prior.size.x (cm)', 1, 100000)
    y_cm = number(size['y'], 'prior.size.y (cm)', 1, 100000)
    pixel = number(value.get('pixelSize'), 'prior.pixelSize (cm)', 1, 20)
    full_width, full_height = math.ceil(x_cm/pixel), math.ceil(y_cm/pixel)
    # Coordinates in this public representation are pixels (not cm). Size and
    # pixelSize are cm. Preserve distinct layers until categorical conversion.
    layers = value.get('layers')
    if not isinstance(layers, list) or len(layers) > 128:
        raise ValueError('prior.layers: expected at most 128 layers')
    walls, floors = set(), set()
    runs = 0
    for layer in layers:
        if not isinstance(layer, dict):
            raise ValueError('prior.layer: expected object')
        kind = layer.get('type')
        if kind not in ('floor', 'wall', 'segment'):
            raise ValueError('prior.layer: unsupported layer type')
        pixels = layer.get('pixels')
        compressed = layer.get('compressedPixels')
        decoded = []
        if compressed is not None:
            if not isinstance(compressed, list) or len(compressed) % 3:
                raise ValueError('prior.compressedPixels: expected x,y,count runs')
            for i in range(0, len(compressed), 3):
                x, y, count = compressed[i:i+3]
                if any(type(v) is not int for v in (x, y, count)) or count < 1 or not 0 <= y < full_height or not 0 <= x < full_width or x+count > full_width:
                    raise ValueError('prior: compressed run outside bounds')
                runs += count
                if runs > 640000:
                    raise ValueError('prior: decoded pixel budget exceeded')
                decoded.extend((cx, y) for cx in range(x, x+count))
        elif pixels is not None:
            if not isinstance(pixels, list) or len(pixels) % 2:
                raise ValueError('prior.pixels: expected flat x,y pairs')
            for i in range(0, len(pixels), 2):
                x, y = pixels[i:i+2]
                if any(type(v) is not int for v in (x, y)) or not (0 <= x < full_width and 0 <= y < full_height):
                    raise ValueError('prior: pixel outside bounds')
                decoded.append((x, y))
            runs += len(decoded)
            if runs > 640000:
                raise ValueError('prior: decoded pixel budget exceeded')
        else:
            raise ValueError('prior.layer: missing pixel representation')
        target = walls if kind == 'wall' else floors
        target.update(decoded)
    all_pixels = walls | floors
    if not all_pixels:
        raise ValueError('prior: no observed pixels to import')
    left, right = min(p[0] for p in all_pixels), max(p[0] for p in all_pixels)
    top, bottom = min(p[1] for p in all_pixels), max(p[1] for p in all_pixels)
    width, height = right-left+1, bottom-top+1
    if width > 1000 or height > 1000 or width*height > 160000:
        raise ValueError('prior: observed bounding box exceeds 160000-cell budget')
    cells = [-1]*(width*height)
    for x, y in floors:
        cells[x-left+(bottom-y)*width] = 0
    for x, y in walls:
        cells[x-left+(bottom-y)*width] = 100
    return {'format': 'valetudo_v2', 'frame_id': 'external_vacuum_map',
            'registration': 'unregistered', 'units': 'metres', 'y_axis': 'up',
            'grid': {'width': width, 'height': height, 'resolution_m': pixel/100,
                     'origin_x_m': left*pixel/100, 'origin_y_m': (full_height-1-bottom)*pixel/100},
            'source_canvas_cm': {'x': x_cm, 'y': y_cm},
            'crop_pixels': {'left': left, 'top': top, 'right': right, 'bottom': bottom},
            'cells': cells, 'room_layer_count': sum(l.get('type') == 'segment' for l in layers),
            'entities_retained': False, 'navigation_use': False, 'motion_enabled': False,
            'note': 'External floor is a prior only; entity/room geometry requires a separate versioned adapter.'}
