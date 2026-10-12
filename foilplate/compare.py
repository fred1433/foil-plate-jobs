"""Golden comparison: rasterise the plate with Poppler and diff it against a reference.

The tolerance lives in goldens/tolerance.json. Agreement shows the packaged
engine reproduces its reference; it does not by itself establish print correctness.
"""
from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path

from PIL import Image, ImageChops


def rasterise(pdf: Path, out_png: Path, dpi: int) -> Path:
    stem = out_png.with_suffix("")
    subprocess.run(["pdftoppm", "-r", str(dpi), "-gray", "-png", "-singlefile", str(pdf), str(stem)],
                   check=True, timeout=120)
    return out_png


def diff_fraction(a: Path, b: Path, level: int, diff_out: Path | None = None) -> float:
    ia, ib = Image.open(a).convert("L"), Image.open(b).convert("L")
    if ia.size != ib.size:
        return 1.0
    d = ImageChops.difference(ia, ib).point(lambda v: 255 if v > level else 0)
    if diff_out:
        d.save(diff_out)
    hist = d.histogram()
    return hist[255] / float(ia.size[0] * ia.size[1])


def compare(plate_pdf: Path, findings: list[str], golden_dir: Path, tolerance: dict) -> dict:
    expected = json.loads((golden_dir / "expected.json").read_text())
    with tempfile.TemporaryDirectory() as td:
        cur = rasterise(plate_pdf, Path(td) / "plate.png", tolerance["dpi"])
        frac = diff_fraction(cur, golden_dir / "plate.png", tolerance["pixel_level"],
                             golden_dir.parent / f"{golden_dir.name}.diff.png"
                             if tolerance.get("write_diff") else None)
    ok_pixels = frac <= tolerance["max_changed_fraction"]
    ok_findings = sorted(findings) == sorted(expected["findings"])
    return {"fixture": golden_dir.name, "changed_fraction": round(frac, 7),
            "max_changed_fraction": tolerance["max_changed_fraction"],
            "pixels_ok": ok_pixels, "findings_ok": ok_findings,
            "passed": ok_pixels and ok_findings}
