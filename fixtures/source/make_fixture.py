"""Build the authored vector fixture used by the engine tests.

Text is converted to outlines here, once, so the committed SVG carries no font
dependency and renders the same everywhere. Fonts are SIL OFL 1.1 (Cormorant
Garamond, Jost); pass their paths on the command line.

    python fixtures/source/make_fixture.py CormorantGaramond.ttf \
        CormorantGaramond-Italic.ttf Jost.ttf fixtures/card-foil-hairline.svg
"""
import sys

from fontTools.pens.svgPathPen import SVGPathPen
from fontTools.pens.transformPen import TransformPen
from fontTools.ttLib import TTFont
from fontTools.varLib.instancer import instantiateVariableFont

BLEED = 3.175            # 0.125 in
TRIM_W, TRIM_H = 88.9, 50.8   # 3.5 x 2 in
W, H = TRIM_W + 2 * BLEED, TRIM_H + 2 * BLEED


def load(path, wght):
    f = TTFont(path)
    if "fvar" in f:
        f = instantiateVariableFont(f, {"wght": wght})
    return f


def text_path(font, text, x, y, size, tracking=0.0, anchor="start"):
    """Return SVG path data for `text` with its baseline at (x, y), in mm."""
    gs = font.getGlyphSet()
    cmap = font.getBestCmap()
    upm = font["head"].unitsPerEm
    s = size / upm
    names = [cmap.get(ord(c), ".notdef") for c in text]
    width = sum(font["hmtx"][n][0] * s + tracking for n in names) - tracking
    if anchor == "middle":
        x -= width / 2
    elif anchor == "end":
        x -= width
    pen = SVGPathPen(gs, ntos=lambda v: f"{v:.3f}".rstrip("0").rstrip("."))
    cx = x
    for n in names:
        tp = TransformPen(pen, (s, 0, 0, -s, cx, y))
        gs[n].draw(tp)
        cx += font["hmtx"][n][0] * s + tracking
    return pen.getCommands()


def main(serif, italic, sans, out):
    f_name = load(serif, 600)
    f_mono = load(italic, 500)
    f_sans = load(sans, 400)
    ox, oy = BLEED, BLEED            # trim origin
    ink = "#1f3a33"
    cx, cy, r = ox + 19.5, oy + 25.4, 11.0

    foil = []
    foil.append(f'<circle cx="{cx}" cy="{cy}" r="{r}" fill="none" stroke="#c9a14a" '
                f'stroke-width="0.45" data-spot="FOIL" data-foil-tone="gold"/>')
    foil.append(f'<circle cx="{cx}" cy="{cy}" r="{r - 1.4}" fill="none" stroke="#c9a14a" '
                f'stroke-width="0.2" data-spot="FOIL" data-foil-tone="gold"/>')
    foil.append(f'<path d="{text_path(f_mono, "A", cx - 0.2, cy + 4.6, 15.5, anchor="middle")}" '
                f'fill="#c9a14a" data-spot="FOIL" data-foil-tone="gold"/>')
    foil.append(f'<path d="{text_path(f_name, "ASHGROVE", ox + 37.5, oy + 21.0, 5.6, tracking=0.55)}" '
                f'fill="#c9a14a" data-spot="FOIL" data-foil-tone="gold"/>')
    foil.append(f'<path d="{text_path(f_mono, "Bindery & Letterworks", ox + 37.6, oy + 27.2, 3.9)}" '
                f'fill="#c9a14a" data-spot="FOIL" data-foil-tone="gold"/>')
    # The hairline the preflight is expected to flag: 0.12 mm, under the 0.15 mm target.
    foil.append(f'<line x1="{ox + 37.6}" y1="{oy + 30.4}" x2="{ox + 82.6}" y2="{oy + 30.4}" '
                f'stroke="#c9a14a" stroke-width="0.12" data-spot="FOIL" data-foil-tone="gold" '
                f'id="hairline-rule"/>')

    flat = []
    lines = [("Marguerite Ellis, head binder", 36.2), ("14 Larkspur Lane, Hollow Creek", 39.7),
             ("studio@ashgrove.example", 43.2)]
    for t, y in lines:
        flat.append(f'<path d="{text_path(f_sans, t, ox + 37.6, oy + y, 2.45, tracking=0.08)}" '
                    f'fill="{ink}" data-spot="INK"/>')

    svg = f'''<svg xmlns="http://www.w3.org/2000/svg" width="{W:.3f}mm" height="{H:.3f}mm" viewBox="0 0 {W:.3f} {H:.3f}" data-trim-inset="{BLEED}" data-stock="cotton-600gsm">
  <title>Fixture: two-colour card, gold foil plus one flat ink, with a 0.12 mm foil hairline</title>
  <g id="flat-ink">
    {chr(10).join("    " + e for e in flat).strip()}
  </g>
  <g id="foil">
    {chr(10).join("    " + e for e in foil).strip()}
  </g>
</svg>
'''
    with open(out, "w") as fh:
        fh.write(svg)


if __name__ == "__main__":
    main(*sys.argv[1:5])
