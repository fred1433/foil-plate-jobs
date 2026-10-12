"""Reference foil-plate engine.

Input: an authored vector card (SVG, millimetre user units, bleed included) whose
foil artwork is tagged with the spot colour FOIL (`data-spot="FOIL"`).
Output, in an output directory:

  plate.pdf       vector plate: the FOIL artwork only, solid black, page = card with bleed
  proof.png       on-screen proof: the card at trim on paper, foil shown as metal, sRGB tagged
  preflight.json  advisory and blocking findings
  manifest.json   checksums, tool versions, parameters, timings

The engine never separates artwork from an illustration and never edits the
customer's geometry: findings are reported, not "fixed".
"""
from __future__ import annotations

import copy
import hashlib
import io
import json
import os
import subprocess
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

import cairosvg
from PIL import Image, ImageCms

from . import __version__
from .preflight import run_preflight

SVG_NS = "http://www.w3.org/2000/svg"
ET.register_namespace("", SVG_NS)

PAPER = "#f3eee4"
FOIL_GRADIENT = """<linearGradient id="foil-metal" gradientUnits="userSpaceOnUse" x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}">
  <stop offset="0" stop-color="#8c6428"/><stop offset="0.35" stop-color="#d8b866"/>
  <stop offset="0.55" stop-color="#a87a30"/><stop offset="0.78" stop-color="#dcc07c"/>
  <stop offset="1" stop-color="#94692a"/></linearGradient>"""


class EngineError(Exception):
    """A deterministic engine failure (bad input, blocking finding)."""


@dataclass
class Params:
    proof_dpi: int = 300
    line_target_mm: float = 0.15     # design target: thinner lines are flagged for review
    line_floor_mm: float = 0.10      # below this the finding blocks the job
    work_seconds: float = 0.0        # simulated extra workload for infrastructure tests
    extra: dict = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: dict | None) -> "Params":
        d = dict(d or {})
        known = {k: d.pop(k) for k in list(d) if k in ("proof_dpi", "line_target_mm",
                                                       "line_floor_mm", "work_seconds")}
        return cls(**known, extra=d)


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def _repaint(el, value):
    """Replace every non-none fill and stroke paint, in attributes and in `style`."""
    for attr in ("fill", "stroke"):
        v = el.get(attr)
        if v is not None and v != "none":
            el.set(attr, value)
    style = el.get("style")
    if style:
        parts = []
        for decl in style.split(";"):
            if ":" in decl:
                k, v = (x.strip() for x in decl.split(":", 1))
                if k in ("fill", "stroke") and v != "none":
                    v = value
                parts.append(f"{k}:{v}")
        el.set("style", ";".join(parts))


def _paint_in_style(el, prop):
    return any(d.split(":", 1)[0].strip() == prop for d in (el.get("style") or "").split(";") if ":" in d)


def _effective_spot(el, inherited):
    return el.get("data-spot", inherited)


def _strip_to_foil(root: ET.Element) -> int:
    """Remove every drawable element that is not FOIL; paint FOIL solid black."""
    kept = 0

    def walk(parent, inherited):
        nonlocal kept
        for child in list(parent):
            tag = child.tag.split("}")[-1]
            spot = _effective_spot(child, inherited)
            if tag in ("title", "desc", "defs", "metadata"):
                continue
            if tag == "g":
                walk(child, spot)
                if len(child) == 0:
                    parent.remove(child)
                continue
            if spot != "FOIL":
                parent.remove(child)
                continue
            _repaint(child, "#000000")
            if (child.get("fill") is None and not _paint_in_style(child, "fill")
                    and tag not in ("line", "polyline")):
                child.set("fill", "#000000")
            kept += 1

    walk(root, None)
    return kept


def _foil_as_metal(root: ET.Element, vb):
    # userSpaceOnUse: a horizontal hairline has a zero-height box and would vanish otherwise.
    grad = FOIL_GRADIENT.format(x1=vb[0], y1=vb[1], x2=vb[0] + vb[2] * 0.6, y2=vb[1] + vb[3])
    defs = ET.fromstring(f'<defs xmlns="{SVG_NS}">{grad}</defs>')
    root.insert(0, defs)

    def walk(parent, inherited):
        for child in list(parent):
            spot = _effective_spot(child, inherited)
            if child.tag.split("}")[-1] == "g":
                walk(child, spot)
            elif spot == "FOIL":
                _repaint(child, "url(#foil-metal)")
    walk(root, None)


def _tool_versions() -> dict:
    out = {"engine": __version__, "cairo": cairosvg.surface.cairo.cairo_version_string()}
    for name, cmd in (("ghostscript", ["gs", "--version"]), ("poppler", ["pdftoppm", "-v"])):
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
            out[name] = (r.stdout or r.stderr).strip().splitlines()[0]
        except Exception as e:  # pragma: no cover
            out[name] = f"unavailable: {e}"
    out["pillow_littlecms"] = getattr(ImageCms.core, "littlecms_version", "unknown")
    return out


def render(input_svg: Path, outdir: Path, params: Params | None = None) -> dict:
    params = params or Params()
    t0 = time.monotonic()
    outdir.mkdir(parents=True, exist_ok=True)
    raw = input_svg.read_bytes()
    try:
        tree_root = ET.fromstring(raw)
    except ET.ParseError as e:
        raise EngineError(f"input is not well-formed SVG: {e}") from e
    if tree_root.tag != f"{{{SVG_NS}}}svg":
        raise EngineError("input root element is not <svg>")
    vb = [float(v) for v in tree_root.get("viewBox", "").split()]
    if len(vb) != 4:
        raise EngineError("input must declare a viewBox in millimetres")
    inset = float(tree_root.get("data-trim-inset", "0"))

    if params.work_seconds:
        time.sleep(params.work_seconds)

    # Plate: FOIL only, solid black, full page with bleed, vector.
    plate_root = copy.deepcopy(tree_root)
    if _strip_to_foil(plate_root) == 0:
        raise EngineError("no artwork tagged with the FOIL spot colour")
    plate_pdf = outdir / "plate.pdf"
    cairosvg.svg2pdf(bytestring=ET.tostring(plate_root), write_to=str(plate_pdf))

    # Proof: whole card at trim, on paper, foil shown as metal.
    proof_root = copy.deepcopy(tree_root)
    _foil_as_metal(proof_root, vb)
    tw, th = vb[2] - 2 * inset, vb[3] - 2 * inset
    proof_root.set("viewBox", f"{vb[0] + inset} {vb[1] + inset} {tw} {th}")
    proof_root.set("width", f"{tw}mm")
    proof_root.set("height", f"{th}mm")
    png = cairosvg.svg2png(bytestring=ET.tostring(proof_root), dpi=params.proof_dpi,
                           background_color=PAPER)
    img = Image.open(io.BytesIO(png)).convert("RGB")
    srgb = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB"))
    proof_png = outdir / "proof.png"
    img.save(proof_png, icc_profile=srgb.tobytes(), dpi=(params.proof_dpi, params.proof_dpi))

    findings = run_preflight(tree_root, plate_pdf, params)
    (outdir / "preflight.json").write_text(json.dumps(findings, indent=2) + "\n")
    blocking = [f for f in findings if f["level"] == "blocking"]

    manifest = {
        "engine_version": __version__,
        "input_sha256": hashlib.sha256(raw).hexdigest(),
        "params": {"proof_dpi": params.proof_dpi, "line_target_mm": params.line_target_mm,
                   "line_floor_mm": params.line_floor_mm, "work_seconds": params.work_seconds},
        "outputs": {p.name: {"sha256": sha256_file(p), "bytes": p.stat().st_size}
                    for p in (plate_pdf, proof_png, outdir / "preflight.json")},
        "findings": [f["code"] for f in findings],
        "blocking": bool(blocking),
        "tools": _tool_versions(),
        "seconds": round(time.monotonic() - t0, 3),
        "pid": os.getpid(),
    }
    (outdir / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    if blocking:
        raise EngineError("; ".join(f["message"] for f in blocking))
    return manifest
