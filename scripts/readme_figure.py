"""Compose the README figure from the recorded goldens: proof and plate side by side.

    python scripts/readme_figure.py <font.ttf> docs/img/proof-and-plate.png
"""
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

G = Path(__file__).resolve().parent.parent / "goldens" / "card-foil-hairline"
font_path, out = sys.argv[1], sys.argv[2]
BG = (236, 231, 221)
S = 2  # supersampling


def card(img, w):
    h = round(img.height * w / img.width)
    return img.resize((w, h), Image.LANCZOS)


proof = card(Image.open(G / "proof.png").convert("RGB"), 700 * S)
plate_full = Image.open(G / "plate.png").convert("L")
# Crop the plate render (page with 3.175 mm bleed) to trim, to match the proof.
bx = round(plate_full.width * 3.175 / 95.25)
by = round(plate_full.height * 3.175 / 57.15)
plate = plate_full.crop((bx, by, plate_full.width - bx, plate_full.height - by)).convert("RGB")
plate = card(plate, 700 * S)

W, H = 1600 * S, 560 * S
fig = Image.new("RGB", (W, H), BG)
pos = [(60 * S, 70 * S), (840 * S, 70 * S)]
for (x, y), im in zip(pos, (proof, plate)):
    sh = Image.new("L", (im.width + 80 * S, im.height + 80 * S), 0)
    ImageDraw.Draw(sh).rectangle((40 * S, 46 * S, 40 * S + im.width, 46 * S + im.height), fill=90)
    sh = sh.filter(ImageFilter.GaussianBlur(14 * S))
    fig.paste((120, 105, 85), (x - 40 * S, y - 40 * S), sh)
    fig.paste(im, (x, y))

d = ImageDraw.Draw(fig)
f = ImageFont.truetype(font_path, 22 * S)
fs = ImageFont.truetype(font_path, 19 * S)
ink = (52, 60, 56)
d.text((60 * S, 485 * S), "proof.png", font=f, fill=ink)
d.text((60 * S, 515 * S), "card at trim, foil shown as metal, sRGB", font=fs, fill=(110, 112, 106))
d.text((840 * S, 485 * S), "plate.pdf", font=f, fill=ink)
d.text((840 * S, 515 * S), "vector, FOIL separation only, rendered here at 600 dpi", font=fs, fill=(110, 112, 106))

# Mark the hairline the preflight flagged (y = 30.4 mm from trim top, x 37.6 to 82.6 mm).
sx = plate.width / 88.9
y = 70 * S + round(30.4 * sx)
x0, x1 = 840 * S + round(37.6 * sx), 840 * S + round(82.6 * sx)
acc = (176, 72, 44)
d.line((x0, y + 9 * S, x0, y + 16 * S), fill=acc, width=2 * S)
d.line((x1, y + 9 * S, x1, y + 16 * S), fill=acc, width=2 * S)
d.line((x0, y + 16 * S, x1, y + 16 * S), fill=acc, width=2 * S)
d.text((x0, y + 22 * S), "0.12 mm, under the 0.15 mm target", font=fs, fill=acc)
d.text((x0, y + 46 * S), "flagged for a person, left as drawn", font=fs, fill=acc)

fig.resize((W // S, H // S), Image.LANCZOS).save(out, optimize=True)
print("figure written:", out)
