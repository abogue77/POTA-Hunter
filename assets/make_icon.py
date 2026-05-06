"""
Generate POTA-Logger icon as PNG and ICO.
- Matches icon.svg: transparent background, 2-tier tree, 2 thick arcs
- 8x oversample + Lanczos + unsharp mask for crisp edges
- ICO written with raw PNG blobs (rcedit embeds this into the exe without re-encoding)
"""

from PIL import Image, ImageDraw, ImageFilter
import math, os, io, struct

OUT = os.path.dirname(os.path.abspath(__file__))
OVERSAMPLE = 8


def arc_band(cx, cy, inner_r, outer_r, start_deg=30, end_deg=150, steps=120):
    """Thick arc as a filled polygon, opening upward."""
    pts = []
    for i in range(steps + 1):
        a = math.radians(start_deg + (end_deg - start_deg) * i / steps)
        pts.append((cx + outer_r * math.cos(a), cy - outer_r * math.sin(a)))
    for i in range(steps, -1, -1):
        a = math.radians(start_deg + (end_deg - start_deg) * i / steps)
        pts.append((cx + inner_r * math.cos(a), cy - inner_r * math.sin(a)))
    return pts


def draw_icon(target_size):
    size = target_size * OVERSAMPLE
    s    = size / 256
    cx   = size / 2

    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d   = ImageDraw.Draw(img)

    # Trunk (matches SVG: x=111, y=193, w=34, h=36)
    tw, th = int(34 * s), int(36 * s)
    tx = int(cx - tw / 2)
    ty = int(193 * s)
    d.rounded_rectangle([tx, ty, tx + tw, ty + th],
                        radius=max(1, int(5 * s)), fill=(92, 61, 30))

    # 2-tier pine tree (matches SVG polygons)
    tiers = [
        (100 * s, 205 * s, 72 * s, (26, 107, 48)),   # bottom: 128,100 / 56,205 / 200,205
        ( 58 * s, 122 * s, 46 * s, (40, 146, 74)),   # top:    128,58  / 82,122 / 174,122
    ]
    for ay, by, hw, color in tiers:
        d.polygon([(cx, ay), (cx - hw, by), (cx + hw, by)], fill=color)

    # Radio arcs centered at dot position (128, 65 in SVG)
    arc_cy    = 65 * s
    arc_color = (33, 113, 181)
    half      = 6.5 * s   # half of SVG stroke-width 13
    for radius in (32 * s, 58 * s):
        d.polygon(arc_band(cx, arc_cy, radius - half, radius + half), fill=arc_color)

    # Dot at (128, 65), r=10
    dot_r = int(10 * s)
    d.ellipse([cx - dot_r, arc_cy - dot_r, cx + dot_r, arc_cy + dot_r], fill=arc_color)

    out = img.resize((target_size, target_size), Image.LANCZOS)
    if target_size >= 32:
        out = out.filter(ImageFilter.UnsharpMask(radius=0.6, percent=160, threshold=2))
    return out


def save_ico(images, path):
    """Write ICO storing each image as a raw PNG blob — rcedit embeds these without re-encoding."""
    blobs = []
    for img in images:
        buf = io.BytesIO()
        img.save(buf, format="PNG", optimize=False)
        blobs.append(buf.getvalue())

    count  = len(images)
    offset = 6 + 16 * count

    with open(path, "wb") as f:
        f.write(struct.pack("<HHH", 0, 1, count))
        for i, img in enumerate(images):
            w = img.width  if img.width  < 256 else 0
            h = img.height if img.height < 256 else 0
            f.write(struct.pack("<BBBBHHII",
                w, h, 0, 0, 1, 32, len(blobs[i]), offset))
            offset += len(blobs[i])
        for blob in blobs:
            f.write(blob)


ICO_SIZES = [16, 24, 32, 40, 48, 64, 96, 128, 256]

icon_256 = draw_icon(256)
icon_256.save(os.path.join(OUT, "icon.png"), "PNG")
print(f"Saved icon.png")

icons = [draw_icon(sz) for sz in ICO_SIZES]
save_ico(icons, os.path.join(OUT, "icon.ico"))
print(f"Saved icon.ico  ({len(ICO_SIZES)} sizes, PNG-blob format)")
