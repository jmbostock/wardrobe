"""Recommender smoke tests — runnable without pytest:
    python services/webapp/tests/test_recommender.py
also collected by pytest if installed.
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

# MUST be set before importing app.* — app.config reads env at import time
os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="altacloset-test-"))

# allow running as a plain script from the repo root
# .../services/webapp/tests/test_recommender.py → parents[1] = .../services/webapp
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.recommender import Weather, recommend  # noqa: E402


def _pick(outfit, role):
    return outfit[role]


def test_rainy_office_gets_waterproof():
    out = recommend(
        Weather(temp_c=13, feels_like_c=12, condition="rain", wind_kph=20),
        "office",
    )["outfit"]
    outer = _pick(out, "outerwear")
    assert outer is not None and outer["waterproof"] == 1, f"expected waterproof outer, got {outer}"
    assert out["top"]["formality"] in ("business", "smart-casual")


def test_hot_beach_is_light():
    out = recommend(
        Weather(temp_c=30, feels_like_c=31, condition="clear", uv_index=9),
        "beach",
    )["outfit"]
    assert out["top"]["warmth"] <= 2
    assert out["bottom"]["category"] == "bottom"
    assert any("sun hat" in a["name"].lower() for a in out["accessories"])


def test_cold_hiking_layers():
    out = recommend(
        Weather(temp_c=8, feels_like_c=6, condition="cloudy", wind_kph=15),
        "hiking",
    )["outfit"]
    assert out["outerwear"] is not None, "cold hike should layer an outerwear piece"


def test_formal_picks_dress_and_no_bottom():
    out = recommend(
        Weather(temp_c=14, feels_like_c=13, condition="cloudy"),
        "formal",
        prompt="navy",
    )["outfit"]
    assert out["top"]["category"] == "dress"
    assert out["bottom"] is None, "a dress covers the bottom slot"


def test_reasoning_is_explainable():
    res = recommend(Weather(temp_c=13, condition="rain"), "office", prompt="navy")
    assert len(res["reasoning"]) >= 3
    assert res["weather_used"]["condition"] == "rain"


def _run_all():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failures = 0
    for fn in tests:
        try:
            fn()
            print(f"  PASS {fn.__name__}")
        except AssertionError as e:
            failures += 1
            print(f"  FAIL {fn.__name__}: {e}")
    print(f"\n{len(tests) - failures}/{len(tests)} passed")
    return failures


if __name__ == "__main__":
    sys.exit(1 if _run_all() else 0)
