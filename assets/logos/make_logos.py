"""Draws the BarkBark cutter logo variants in this folder, in
barkbark2.ico's style (dark film strip, light-blue sprocket holes, grey
paw, soft drop shadow), each with a cutting idea. Drawn at 1024 px and
shrunk; writes barkbark-<n>-<name>.ico (16 to 256 px) next to this script.

    python make_logos.py               the .ico files
    python make_logos.py sheet.png     also a contact sheet: barkbark2.ico
                                       and every variant at 256/64/32/16 px,
                                       on light and on dark

Needs Pillow."""
import math
import os
import sys

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont

HERE = os.path.dirname(os.path.abspath(__file__))
ICO_SIZES = [(n, n) for n in (16, 24, 32, 48, 64, 128, 256)]
S = 1024
BODY = (35, 35, 35, 255)
HOLE = (166, 192, 199, 255)
PAW = (213, 213, 213, 255)
STEEL = (198, 214, 219, 255)
STEEL_DARK = (120, 146, 154, 255)
CUT = (233, 84, 62, 255)          # a warm accent for the cut line


def layer():
    return Image.new("RGBA", (S, S), (0, 0, 0, 0))


def ellipse_poly(cx, cy, rx, ry, angle=0.0, n=72):
    a = math.radians(angle)
    pts = []
    for i in range(n):
        t = 2 * math.pi * i / n
        x, y = rx * math.cos(t), ry * math.sin(t)
        pts.append((cx + x * math.cos(a) - y * math.sin(a), cy + x * math.sin(a) + y * math.cos(a)))
    return pts


def paw(d, cx, cy, s, color):
    """A paw print about s wide, centered on (cx, cy)."""
    # Main pad: three overlapping lobes, wider at the bottom.
    d.polygon(ellipse_poly(cx, cy + 0.17 * s, 0.30 * s, 0.22 * s), fill=color)
    d.polygon(ellipse_poly(cx - 0.17 * s, cy + 0.27 * s, 0.17 * s, 0.15 * s), fill=color)
    d.polygon(ellipse_poly(cx + 0.17 * s, cy + 0.27 * s, 0.17 * s, 0.15 * s), fill=color)
    d.polygon(ellipse_poly(cx, cy + 0.06 * s, 0.20 * s, 0.17 * s), fill=color)
    # Toes.
    for dx, dy, ang in ((-0.37, -0.03, -25), (-0.14, -0.27, -8), (0.14, -0.27, 8), (0.37, -0.03, 25)):
        d.polygon(ellipse_poly(cx + dx * s, cy + dy * s, 0.105 * s, 0.14 * s, ang), fill=color)


def strip(d, box, holes=5, radius=0.04):
    """barkbark2's film strip filling box (x0, y0, x1, y1), upright."""
    x0, y0, x1, y1 = box
    w, h = x1 - x0, y1 - y0
    d.rounded_rectangle(box, radius=radius * S, fill=BODY)
    if not holes:
        return
    hw, hh = 0.13 * w, 0.62 * h / holes
    gap = (h - holes * hh) / (holes + 1)
    for i in range(holes):
        y = y0 + gap + i * (hh + gap)
        for x in (x0 + 0.06 * w, x1 - 0.06 * w - hw):
            d.rounded_rectangle((x, y, x + hw, y + hh), radius=0.012 * S, fill=HOLE)


RIM = (96, 96, 96, 255)


def finish(img, angle=-12, shadow=(26, 30)):
    """Tilts the drawing, rims it in grey (so the dark body shows on a dark
    taskbar), adds the soft grey drop shadow, shrinks to 256."""
    img = img.rotate(angle, resample=Image.BICUBIC, center=(S / 2, S / 2))
    alpha = img.getchannel("A")
    grown = alpha.filter(ImageFilter.MaxFilter(13))
    rim = Image.new("RGBA", (S, S), RIM)
    rim.putalpha(grown)
    img = Image.alpha_composite(rim, img)
    alpha = img.getchannel("A")
    sh = Image.new("RGBA", (S, S), (110, 110, 110, 0))
    sh.putalpha(alpha.point(lambda a: a * 150 // 255))
    moved = layer()
    moved.paste(sh, shadow)          # shifted, not wrapped around like ImageChops.offset
    sh = moved.filter(ImageFilter.GaussianBlur(10))
    out = Image.alpha_composite(sh, img)
    bbox = out.getbbox()
    # Centered with a margin, so every variant fills the icon alike.
    crop = out.crop(bbox)
    side = int(max(crop.size) * 1.06)
    sq = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    sq.paste(crop, ((side - crop.size[0]) // 2, (side - crop.size[1]) // 2), crop)
    return sq.resize((256, 256), Image.LANCZOS)


def scissors(d, cx, cy, s, open_deg=22, angle=0.0, handle=PAW, blade=STEEL):
    """Scissors pointing up-right at angle, pivot at (cx, cy), about s long."""
    for side in (-1, 1):
        a = math.radians(angle + side * open_deg / 2)
        ux, uy = math.cos(a), -math.sin(a)          # along the blade
        px, py = -uy, ux                             # across it
        tip = (cx + ux * 0.55 * s, cy + uy * 0.55 * s)
        base = 0.075 * s
        blade_pts = [(cx + px * base, cy + py * base), tip, (cx - px * base * 0.4, cy - py * base * 0.4)]
        d.polygon(blade_pts, fill=blade)
        d.line([(cx, cy), tip], fill=STEEL_DARK, width=max(2, int(0.012 * s)))
        # Handle: a ring on the other side of the pivot, splayed wider than
        # the blades so the two rings stay apart.
        ha = math.radians(angle + side * (open_deg / 2 + 16))
        hx, hy = cx - math.cos(ha) * 0.40 * s, cy + math.sin(ha) * 0.40 * s
        d.line([(cx, cy), (hx, hy)], fill=handle, width=int(0.07 * s))
        r = 0.15 * s
        d.ellipse((hx - r, hy - r, hx + r, hy + r), outline=handle, width=int(0.065 * s))
    d.ellipse((cx - 0.045 * s, cy - 0.045 * s, cx + 0.045 * s, cy + 0.045 * s), fill=BODY,
              outline=STEEL_DARK, width=int(0.012 * s))


# ---- the variants ---------------------------------------------------------

def v1_cut_strip():
    """The strip cut in two across a frame, the halves pulled apart."""
    top, bot = layer(), layer()
    box = (300, 120, 724, 904)
    strip(ImageDraw.Draw(top), box)
    strip(ImageDraw.Draw(bot), box)
    mask_top = layer()
    md = ImageDraw.Draw(mask_top)
    md.polygon([(0, 0), (S, 0), (S, 560), (0, 610)], fill=(255, 255, 255, 255))
    top.putalpha(ImageChops.multiply(top.getchannel("A"), mask_top.getchannel("A")))
    bot.putalpha(ImageChops.subtract(bot.getchannel("A"), mask_top.getchannel("A")))
    paw(ImageDraw.Draw(top), 512, 360, 260, PAW)
    img = layer()
    img.alpha_composite(top.rotate(-4, resample=Image.BICUBIC, center=(512, 585)), (-28, -40))
    img.alpha_composite(bot.rotate(3, resample=Image.BICUBIC, center=(512, 585)), (28, 30))
    return finish(img)


def split_layer(src, p, angle, gap):
    """src cut along the line through p at angle (degrees), the two sides
    pushed gap px apart across the line. Returns the joined layer."""
    a = math.radians(angle)
    ux, uy = math.cos(a), math.sin(a)
    nx, ny = -uy, ux
    far = 3 * S
    side = layer()
    ImageDraw.Draw(side).polygon(
        [(p[0] - ux * far, p[1] - uy * far), (p[0] + ux * far, p[1] + uy * far),
         (p[0] + ux * far + nx * far, p[1] + uy * far + ny * far),
         (p[0] - ux * far + nx * far, p[1] - uy * far + ny * far)], fill=(255, 255, 255, 255))
    m = side.getchannel("A")
    one, two = src.copy(), src.copy()
    one.putalpha(ImageChops.multiply(src.getchannel("A"), m))
    two.putalpha(ImageChops.subtract(src.getchannel("A"), m))
    out = layer()
    h = gap / 2
    out.alpha_composite(ImageChops.offset(one, round(nx * h), round(ny * h)))
    out.alpha_composite(ImageChops.offset(two, round(-nx * h), round(-ny * h)))
    return out


def v2_strip_scissors():
    """barkbark2's strip and paw, with big red-handled scissors cutting
    into its side."""
    img = layer()
    d = ImageDraw.Draw(img)
    strip(d, (220, 90, 640, 930))
    paw(d, 430, 420, 300, PAW)
    scissors(d, 600, 640, 560, open_deg=34, angle=160, handle=CUT)
    return finish(img, angle=-8)


def v3_paw_cut_line():
    """The strip and paw with a red dashed cut line through it and red
    scissors on the line: the cut mark."""
    img = layer()
    d = ImageDraw.Draw(img)
    strip(d, (330, 90, 754, 900))
    paw(d, 542, 410, 290, PAW)
    y = 690
    for x in range(330, 940, 74):
        d.rounded_rectangle((x, y - 15, x + 46, y + 15), radius=12, fill=CUT)
    scissors(d, 250, y, 430, open_deg=36, angle=0, handle=CUT, blade=STEEL)
    return finish(img, angle=-10)


def v4_badge():
    """A flat rounded tile: a strip edge of holes top and bottom, the paw
    in the middle cut in two, its halves pulled apart."""
    img = layer()
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((112, 112, 912, 912), radius=170, fill=BODY)
    for y in (160, 800):
        for i in range(6):
            x = 190 + i * 112
            d.rounded_rectangle((x, y, x + 70, y + 64), radius=14, fill=HOLE)
    p = layer()
    paw(ImageDraw.Draw(p), 512, 470, 420, PAW)
    img.alpha_composite(split_layer(p, (512, 520), 62, 46))
    return finish(img, angle=0, shadow=(18, 22))


def v5_badge_cut_line():
    """The tile with the whole paw, and a red dashed cut line across
    between the paw and the bottom holes."""
    img = layer()
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((112, 112, 912, 912), radius=170, fill=BODY)
    for y in (160, 800):
        for i in range(6):
            x = 190 + i * 112
            d.rounded_rectangle((x, y, x + 70, y + 64), radius=14, fill=HOLE)
    paw(d, 512, 440, 400, PAW)
    for x in range(150, 880, 92):
        d.rounded_rectangle((x, 700, x + 58, 738), radius=16, fill=CUT)
    return finish(img, angle=0, shadow=(18, 22))


# (file name part, sheet label, drawing)
VARIANTS = [("1-cut-strip", "1 Cut strip", v1_cut_strip),
            ("2-strip-scissors", "2 Strip + scissors", v2_strip_scissors),
            ("3-cut-line", "3 Cut line", v3_paw_cut_line),
            ("4-badge-cut-paw", "4 Badge, cut paw", v4_badge),
            ("5-badge-cut-line", "5 Badge, cut line", v5_badge_cut_line)]


def write_sheet(path, images):
    """Each image at 256, 64, 32 and 16 px, on light, then on dark."""
    try:
        font = ImageFont.truetype("segoeui.ttf", 22)
    except OSError:
        font = ImageFont.load_default()
    row_h, col_w = 300, 560
    sheet = Image.new("RGBA", (col_w * 2, row_h * len(images)), (255, 255, 255, 255))
    for r, (label, im) in enumerate(images):
        for c, bg in enumerate(((246, 246, 246, 255), (32, 32, 32, 255))):
            x0, y0 = c * col_w, r * row_h
            ImageDraw.Draw(sheet).rectangle((x0, y0, x0 + col_w - 1, y0 + row_h - 1), fill=bg)
            sheet.alpha_composite(im, (x0 + 14, y0 + 34))
            x = x0 + 290
            for size in (64, 32, 16):
                small = im.resize((size, size), Image.LANCZOS)
                sheet.alpha_composite(small, (x, y0 + 150 - size // 2))
                x += size + 26
            ImageDraw.Draw(sheet).text((x0 + 14, y0 + 6), label,
                                       fill=(20, 20, 20) if c == 0 else (230, 230, 230), font=font)
    sheet.save(path)


def main():
    base = Image.open(os.path.join(HERE, "..", "barkbark2.ico")).convert("RGBA")
    images = [("barkbark2 (now)", base.resize((256, 256), Image.LANCZOS))]
    for file_part, label, draw in VARIANTS:
        im = draw()
        path = os.path.join(HERE, f"barkbark-{file_part}.ico")
        im.save(path, sizes=ICO_SIZES)
        print("wrote", path)
        images.append((label, im))
    if len(sys.argv) > 1:
        write_sheet(sys.argv[1], images)
        print("wrote", sys.argv[1])


if __name__ == "__main__":
    main()
