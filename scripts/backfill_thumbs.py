#!/usr/bin/env python3
"""Backfill responsive WebP variants (thumb + detail) for existing images.

Run inside the webapp container so it sees the same DATA_DIR/config:
    docker exec -w /app -e PYTHONPATH=/app altacloset-webapp python /tmp/backfill_thumbs.py

Covers:
  - garment originals   data/wardrobe/<uid>/<gid>.*        -> <gid>.thumb.webp / .detail.webp
  - person photos       data/uploads/<uid>/photos/<name>   -> <name>.thumb.webp / .detail.webp
  - outfit renders      data/uploads/<uid>/out/<name>      -> <name>.thumb.webp / .detail.webp

Idempotent: skips variants that already exist (or regenerates with --force).
"""
from __future__ import annotations

import argparse
import io
import os
import sys
from pathlib import Path

from PIL import Image, ImageOps

# make `app` importable when run from /app or via PYTHONPATH
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "services" / "webapp"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "services" / "webapp" / "app"))

from app import db  # noqa: E402
from app.config import settings  # noqa: E402
from app.media import (  # noqa: E402
    DETAIL_PX,
    THUMB_PX,
    _is_variant,
    _variant_path,
)


def _ensure_variants(orig: Path, force: bool) -> int:
    """Generate missing variants for one original; returns count generated."""
    made = 0
    data = orig.read_bytes()
    for size, px in (("thumb", THUMB_PX), ("detail", DETAIL_PX)):
        vp = _variant_path(orig, size)
        if force or not vp.is_file():
            try:
                img = Image.open(io.BytesIO(data))
                img = ImageOps.exif_transpose(img).convert("RGB")
                img.thumbnail((px, px), Image.LANCZOS)
                tmp = vp.with_name(vp.name + ".tmp")
                img.save(tmp, "WEBP", quality=80, method=4)
                os.replace(tmp, vp)
                made += 1
            except Exception as exc:  # noqa: BLE001
                print(f"  ! variant {vp.name} failed: {exc}")
    return made


def main() -> None:
    ap = argparse.ArgumentParser(description="Backfill responsive WebP variants")
    ap.add_argument("--force", action="store_true", help="regenerate existing variants")
    ap.add_argument("--skip-photos", action="store_true", help="skip person photos")
    ap.add_argument("--skip-uploads", action="store_true", help="skip outfit renders")
    args = ap.parse_args()

    base = Path(settings.data_dir)
    total_made = 0
    total_skipped = 0

    # 1. garments
    wdir = base / "wardrobe"
    for udir in sorted(wdir.iterdir()) if wdir.is_dir() else []:
        if not udir.is_dir():
            continue
        for orig in sorted(udir.iterdir()):
            if not orig.is_file() or _is_variant(orig):
                continue
            made = _ensure_variants(orig, args.force)
            total_made += made
            if made == 0:
                total_skipped += 1
    print(f"garments: {total_made} variants written, {total_skipped} already present")

    # 2. person photos
    if not args.skip_photos:
        p_made = p_skip = 0
        pdir = base / "uploads"
        for udir in sorted(pdir.iterdir()) if pdir.is_dir() else []:
            pd = udir / "photos"
            if not pd.is_dir():
                continue
            for orig in sorted(pd.iterdir()):
                if not orig.is_file() or _is_variant(orig):
                    continue
                made = _ensure_variants(orig, args.force)
                p_made += made
                p_skip += 0 if made else 1
        print(f"photos: {p_made} variants written, {p_skip} already present")

    # 3. outfit renders
    if not args.skip_uploads:
        u_made = u_skip = 0
        pdir = base / "uploads"
        for udir in sorted(pdir.iterdir()) if pdir.is_dir() else []:
            out = udir / "out"
            if not out.is_dir():
                continue
            for orig in sorted(out.iterdir()):
                if not orig.is_file() or _is_variant(orig):
                    continue
                made = _ensure_variants(orig, args.force)
                u_made += made
                u_skip += 0 if made else 1
        print(f"uploads: {u_made} variants written, {u_skip} already present")

    print("done.")


if __name__ == "__main__":
    main()
