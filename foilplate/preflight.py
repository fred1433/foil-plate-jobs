"""Preflight checks. Thresholds are configurable fixture assumptions, not a print spec."""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

_NUM = re.compile(r"[-+]?\d*\.?\d+(?:[eE][-+]?\d+)?")


def _scale_of(transform: str | None) -> float:
    """Approximate uniform scale of a transform list (enough for stroke widths)."""
    if not transform:
        return 1.0
    s = 1.0
    for name, args in re.findall(r"(\w+)\s*\(([^)]*)\)", transform):
        v = [float(x) for x in _NUM.findall(args)]
        if name == "scale" and v:
            s *= (abs(v[0] * (v[1] if len(v) > 1 else v[0]))) ** 0.5
        elif name == "matrix" and len(v) == 6:
            s *= abs(v[0] * v[3] - v[1] * v[2]) ** 0.5
    return s


def _stroke_width(el) -> float | None:
    style = dict(
        kv.split(":", 1) for kv in (el.get("style") or "").replace(" ", "").split(";") if ":" in kv
    )
    stroke = style.get("stroke", el.get("stroke"))
    if stroke in (None, "none"):
        return None
    w = style.get("stroke-width", el.get("stroke-width", "1"))
    return float(_NUM.findall(w)[0])


def _foil_elements(root):
    def walk(parent, spot, tone, scale):
        for child in parent:
            s = child.get("data-spot", spot)
            t = child.get("data-foil-tone", tone)
            sc = scale * _scale_of(child.get("transform"))
            if child.tag.split("}")[-1] == "g":
                yield from walk(child, s, t, sc)
            elif s == "FOIL":
                yield child, t, sc
    yield from walk(root, None, None, 1.0)


def run_preflight(root, plate_pdf: Path, params) -> list[dict]:
    findings: list[dict] = []
    tones = set()
    for el, tone, scale in _foil_elements(root):
        tones.add(tone or "unspecified")
        w = _stroke_width(el)
        if w is None:
            continue
        mm = round(w * scale, 4)
        ref = el.get("id") or el.tag.split("}")[-1]
        if mm < params.line_floor_mm:
            findings.append({
                "code": "FOIL_LINE_BELOW_FLOOR", "level": "blocking", "element": ref,
                "value_mm": mm, "limit_mm": params.line_floor_mm,
                "message": f"Foil stroke {mm} mm on {ref} is below the {params.line_floor_mm} mm floor.",
            })
        elif mm < params.line_target_mm:
            findings.append({
                "code": "FOIL_LINE_BELOW_TARGET", "level": "advisory", "element": ref,
                "value_mm": mm, "limit_mm": params.line_target_mm,
                "message": (f"Foil stroke {mm} mm on {ref} is under the {params.line_target_mm} mm "
                            f"design target (floor {params.line_floor_mm} mm). Left as drawn; "
                            "a person decides."),
            })
    if len(tones) > 1:
        findings.append({
            "code": "FOIL_MULTIPLE_TONES", "level": "blocking", "element": "plate",
            "message": f"One plate carries one metal tone; found {sorted(tones)}.",
        })

    # Poppler: the plate must stay vector (no raster image objects).
    r = subprocess.run(["pdfimages", "-list", str(plate_pdf)], capture_output=True, text=True,
                       timeout=60)
    images = max(0, len(r.stdout.strip().splitlines()) - 2)
    if r.returncode != 0 or images:
        findings.append({
            "code": "PLATE_NOT_VECTOR", "level": "blocking", "element": "plate",
            "message": f"Plate PDF contains {images} raster image(s) or could not be inspected.",
        })

    # Ghostscript: measured extent of the marking artwork on the plate page.
    r = subprocess.run(["gs", "-q", "-dSAFER", "-dBATCH", "-dNOPAUSE", "-sDEVICE=bbox",
                        str(plate_pdf)], capture_output=True, text=True, timeout=60)
    m = re.search(r"%%HiResBoundingBox:\s*([\d.\s-]+)", r.stderr)
    if m:
        x0, y0, x1, y1 = (float(v) * 25.4 / 72 for v in m.group(1).split())
        findings.append({
            "code": "PLATE_EXTENT", "level": "info", "element": "plate",
            "value_mm": [round(x0, 2), round(y0, 2), round(x1, 2), round(y1, 2)],
            "message": f"Foil artwork spans {x1 - x0:.1f} x {y1 - y0:.1f} mm on the plate.",
        })
    return findings
