import json
import os
from pathlib import Path

import pytest

from foilplate.compare import compare
from foilplate.engine import EngineError, Params, render

ROOT = Path(__file__).resolve().parent.parent
FIXTURE = ROOT / "fixtures" / "card-foil-hairline.svg"
IN_IMAGE = os.environ.get("FOILPLATE_PINNED_IMAGE") == "1"   # goldens are only valid in the image


def test_hairline_is_an_advisory_and_is_left_as_drawn(tmp_path):
    m = render(FIXTURE, tmp_path)
    findings = json.loads((tmp_path / "preflight.json").read_text())
    hair = [f for f in findings if f["code"] == "FOIL_LINE_BELOW_TARGET"]
    assert len(hair) == 1
    assert hair[0]["level"] == "advisory" and hair[0]["value_mm"] == 0.12
    assert not m["blocking"]
    assert "stroke-width=\"0.12\"" in FIXTURE.read_text()


def test_plate_stays_vector(tmp_path):
    m = render(FIXTURE, tmp_path)
    assert "PLATE_NOT_VECTOR" not in m["findings"]


def test_two_metal_tones_block_the_job(tmp_path):
    src = tmp_path / "two-tones.svg"
    src.write_text(FIXTURE.read_text().replace('data-foil-tone="gold"', 'data-foil-tone="silver"', 1))
    with pytest.raises(EngineError, match="one metal tone"):
        render(src, tmp_path / "out")


def test_line_below_floor_blocks(tmp_path):
    with pytest.raises(EngineError, match="floor"):
        render(FIXTURE, tmp_path, Params(line_floor_mm=0.13))


@pytest.mark.skipif(not IN_IMAGE, reason="goldens are recorded in the pinned image")
def test_golden_reproduces(tmp_path):
    tol = json.loads((ROOT / "goldens" / "tolerance.json").read_text())
    m = render(FIXTURE, tmp_path)
    r = compare(tmp_path / "plate.pdf", m["findings"], ROOT / "goldens" / FIXTURE.stem, tol)
    assert r["passed"], r


@pytest.mark.skipif(not IN_IMAGE, reason="goldens are recorded in the pinned image")
def test_golden_detects_an_intentional_change(tmp_path):
    """Move the hairline down by 0.5 mm: the comparison must fail."""
    moved = tmp_path / "moved.svg"
    s = FIXTURE.read_text()
    y = s.split('id="hairline-rule"')[0].rsplit('y1="', 1)[1].split('"')[0]
    moved.write_text(s.replace(f'y1="{y}" x2', f'y1="{float(y) + 0.5}" x2')
                      .replace(f'y2="{y}"', f'y2="{float(y) + 0.5}"'))
    tol = json.loads((ROOT / "goldens" / "tolerance.json").read_text())
    m = render(moved, tmp_path / "out")
    r = compare(tmp_path / "out" / "plate.pdf", m["findings"], ROOT / "goldens" / FIXTURE.stem, tol)
    assert not r["passed"], r
    assert r["changed_fraction"] > tol["max_changed_fraction"]
