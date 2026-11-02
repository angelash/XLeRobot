"""Generate printable calibration targets at exact physical size (A4, 300 dpi PDF)."""
from __future__ import annotations

import argparse
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont

DPI = 300
A4_LANDSCAPE_MM = (297, 210)


def px(mm):
    return int(round(mm/25.4*DPI))


def _font(size):
    for path in ('C:/Windows/Fonts/msyh.ttc', 'C:/Windows/Fonts/simhei.ttf', '/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc'):
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default()


def _ruler(draw, x, y, length_mm, label):
    draw.line([(x, y), (x+px(length_mm), y)], fill=0, width=3)
    for mm in range(0, length_mm+1, 10):
        h = px(4 if mm % 50 else 7)
        draw.line([(x+px(mm), y), (x+px(mm), y-h)], fill=0, width=2)
    draw.text((x, y+px(2)), label, fill=0, font=_font(px(3.2)))


def checkerboard(path, inner=(9, 6), square_mm=25):
    width, height = (px(v) for v in A4_LANDSCAPE_MM)
    page = Image.new('L', (width, height), 255)
    draw = ImageDraw.Draw(page)
    cols, rows = inner[0]+1, inner[1]+1
    left = (width-px(cols*square_mm))//2
    top = px(14)
    for r in range(rows):
        for c in range(cols):
            if (r+c) % 2 == 0:
                draw.rectangle([left+px(c*square_mm), top+px(r*square_mm), left+px((c+1)*square_mm)-1, top+px((r+1)*square_mm)-1], fill=0)
    text = f'XLeRobot 标定棋盘格：{inner[0]}×{inner[1]}内角点，方格{square_mm}mm。A4横向，按100%实际大小打印（不要缩放）。打印后10格应为{10*square_mm}mm。'
    draw.text((px(12), px(4)), text, fill=0, font=_font(px(3.4)))
    _ruler(draw, px(12), height-px(10), 100, '100 mm 校验尺')
    page.save(path, 'PDF', resolution=DPI)
    return {'file': Path(path).name, 'board_mm': [cols*square_mm, rows*square_mm], 'inner_corners': list(inner), 'square_mm': square_mm}


def apriltags(path, ids=(0, 1, 2, 3), tag_mm=80):
    width, height = (px(v) for v in A4_LANDSCAPE_MM)
    page = Image.new('L', (width, height), 255)
    draw = ImageDraw.Draw(page)
    dictionary = cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_APRILTAG_36h11)
    gap = px(30)
    size = px(tag_mm)
    total = len(ids)*size+(len(ids)-1)*gap
    left = (width-total)//2
    top = (height-size)//2
    for index, tag_id in enumerate(ids):
        image = cv2.aruco.generateImageMarker(dictionary, int(tag_id), size)
        x = left+index*(size+gap)
        page.paste(Image.fromarray(np.asarray(image)), (x, top))
        draw.text((x, top+size+px(4)), f'ID {tag_id}', fill=0, font=_font(px(4)))
    text = f'AprilTag 36h11，黑色外框边长{tag_mm}mm（位姿解算用此值）。A4横向，100%实际大小打印；四周保留白边。'
    draw.text((px(12), px(8)), text, fill=0, font=_font(px(3.4)))
    _ruler(draw, px(12), height-px(10), 100, '100 mm 校验尺')
    page.save(path, 'PDF', resolution=DPI)
    return {'file': Path(path).name, 'family': 'tag36h11', 'ids': list(ids), 'tag_mm': tag_mm}


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--out', default=str(Path(__file__).resolve().parents[2]/'docs'/'autonomy'/'assets'))
    out = Path(parser.parse_args(argv).out)
    out.mkdir(parents=True, exist_ok=True)
    print(checkerboard(out/'checkerboard-9x6-25mm.pdf'))
    print(apriltags(out/'apriltag36h11-80mm.pdf'))


if __name__ == '__main__':
    main()
