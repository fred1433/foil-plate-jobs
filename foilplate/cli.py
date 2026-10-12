"""Command line: render one job, record goldens, compare against goldens."""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
from pathlib import Path

from .compare import compare, rasterise
from .engine import EngineError, Params, render

ROOT = Path(__file__).resolve().parent.parent


def _cmd_render(a):
    try:
        m = render(Path(a.input), Path(a.outdir), Params.from_dict(json.loads(a.params)))
    except EngineError as e:
        print(json.dumps({"error": str(e)}))
        return 3
    print(json.dumps(m))
    return 0


def _fixtures(names):
    fx = ROOT / "fixtures"
    return [fx / f"{n}.svg" for n in names] if names else sorted(fx.glob("*.svg"))


def _cmd_golden(a):
    tol = json.loads((ROOT / "goldens" / "tolerance.json").read_text())
    for svg in _fixtures(a.fixture):
        gdir = ROOT / "goldens" / svg.stem
        gdir.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory() as td:
            m = render(svg, Path(td))
            rasterise(Path(td) / "plate.pdf", gdir / "plate.png", tol["dpi"])
            shutil.copy(Path(td) / "proof.png", gdir / "proof.png")
            shutil.copy(Path(td) / "plate.pdf", gdir / "plate.pdf")
            shutil.copy(Path(td) / "preflight.json", gdir / "preflight.json")
        (gdir / "expected.json").write_text(json.dumps(
            {"input_sha256": m["input_sha256"], "findings": m["findings"], "tools": m["tools"]},
            indent=2) + "\n")
        print(f"golden recorded: {gdir.relative_to(ROOT)}")
    return 0


def _cmd_compare(a):
    tol = json.loads((ROOT / "goldens" / "tolerance.json").read_text())
    results = []
    for svg in _fixtures(a.fixture):
        src = Path(a.input) if a.input else svg
        with tempfile.TemporaryDirectory() as td:
            try:
                m = render(src, Path(td))
                findings = m["findings"]
            except EngineError:
                findings = json.loads((Path(td) / "manifest.json").read_text())["findings"]
            results.append(compare(Path(td) / "plate.pdf", findings, ROOT / "goldens" / svg.stem, tol))
    print(json.dumps(results, indent=2))
    return 0 if all(r["passed"] for r in results) else 1


def main(argv=None):
    p = argparse.ArgumentParser(prog="foilplate")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("render", help="render one job")
    r.add_argument("input")
    r.add_argument("outdir")
    r.add_argument("--params", default="{}")
    r.set_defaults(fn=_cmd_render)
    g = sub.add_parser("golden", help="record reference outputs (run inside the pinned image)")
    g.add_argument("--fixture", action="append")
    g.set_defaults(fn=_cmd_golden)
    c = sub.add_parser("compare", help="compare current outputs with the references")
    c.add_argument("--fixture", action="append")
    c.add_argument("--input", help="render this file instead of the fixture (change detection)")
    c.set_defaults(fn=_cmd_compare)
    a = p.parse_args(argv)
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
