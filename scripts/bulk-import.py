#!/usr/bin/env python3
"""Bulk-import garment photos into Clueless Closet.

Reads every photo in a folder, has the app's AI tag-reader fill name/brand/
color/category/sizes (from visible tags), creates a garment per photo, uploads
the image, and flags near-duplicates via perceptual hash (dHash).

Two ways to run:

1) From a machine with httpx-free stdlib + Pillow, against the app API:
   python3 scripts/bulk-import.py --dir DIR --email you@x.com --password ... \
       --base http://localhost:28085

2) For HEIC (iPhone) photos, run INSIDE the webapp container (has pillow-heif,
   which this host's python lacks), pointing at the container's own API:
   docker cp DIR altacloset-webapp:/tmp/photos
   cat scripts/bulk-import.py | ssh host 'cat > /tmp/bulk-import.py && \
       docker cp /tmp/bulk-import.py altacloset-webapp:/tmp/bulk-import.py'
   docker exec -w /app -e PYTHONPATH=/app altacloset-webapp python /tmp/bulk-import.py \
       --dir /tmp/photos/<DIR> --email you@x.com --password ... --base http://localhost:8000

Flags:
  --include-dups   add a garment even when the photo is a near-duplicate
                   (default: still adds it but prints a loud warning)
  --skip-dups      skip near-duplicate photos entirely (don't create)
  --dry-run        show what would happen without calling the API
"""
from __future__ import annotations

import argparse
import io
import json
import os
import sys
import urllib.request
import uuid
from pathlib import Path

IMAGE_EXTS = {".heic", ".jpg", ".jpeg", ".png", ".webp"}
SKIP_EXTS = {".mp4", ".mov", ".avi", ".aae"}


def api(base: str, token: str, method: str, path: str, json_body=None,
        file_bytes=None, filename="image.jpg", timeout=180) -> dict:
    url = base + path
    headers = {}
    if token:
        headers["Authorization"] = "Bearer " + token
    if json_body is not None:
        data = json.dumps(json_body).encode()
        headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
    elif file_bytes is not None:
        boundary = uuid.uuid4().hex
        pre = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"image\"; "
               f"filename=\"{filename}\"\r\nContent-Type: application/octet-stream\r\n\r\n").encode()
        post = f"\r\n--{boundary}--\r\n".encode()
        headers["Content-Type"] = f"multipart/form-data; boundary={boundary}"
        req = urllib.request.Request(url, data=pre + file_bytes + post, headers=headers, method=method)
    else:
        req = urllib.request.Request(url, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        try:
            return {"error": json.loads(body).get("detail", body)}
        except Exception:  # noqa: BLE001
            return {"error": body}


def heic_to_jpeg(data: bytes) -> bytes:
    import pillow_heif  # noqa: F401 — registers the opener
    from PIL import Image, ImageOps
    img = Image.open(io.BytesIO(data))
    img = ImageOps.exif_transpose(img)
    buf = io.BytesIO()
    img.convert("RGB").save(buf, "JPEG", quality=90)
    return buf.getvalue()


def is_heic(data: bytes) -> bool:
    return len(data) >= 12 and data[4:8] == b"ftyp" and data[8:12] in {
        b"heic", b"heix", b"hevc", b"hevx", b"mif1", b"msf1", b"avif"}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", required=True)
    ap.add_argument("--base", default="http://localhost:8000")
    ap.add_argument("--email", required=True)
    ap.add_argument("--password", required=True)
    ap.add_argument("--include-dups", action="store_true")
    ap.add_argument("--skip-dups", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    d = Path(args.dir)
    files = sorted(
        p for p in d.iterdir()
        if p.is_file() and p.suffix.lower() in IMAGE_EXTS
    )
    if not files:
        print(f"no image files in {d}")
        return 1
    print(f"{len(files)} photo(s) in {d}")

    base = args.base.rstrip("/")
    token = ""
    if not args.dry_run:
        r = api(base, "", "POST", "/api/auth/login",
                json_body={"email": args.email, "password": args.password})
        token = r.get("token", "")
        if not token:
            print("login failed:", r)
            return 1
        print("logged in as", args.email)

    added = skipped = dup_warnings = 0
    for p in files:
        raw = p.read_bytes()
        is_heic_file = is_heic(raw)
        # AI needs a decodable format; convert HEIC to JPEG first
        jpg = raw
        try:
            if is_heic_file:
                jpg = heic_to_jpeg(raw)
        except Exception as e:  # noqa: BLE001
            print(f"!! {p.name}: cannot decode HEIC ({e}) — run inside the webapp container")
            skipped += 1
            continue

        fields = {"name": "", "brand": "", "color": "", "category": "", "sizes": ""}
        if not args.dry_run:
            r = api(base, token, "POST", "/api/wardrobe/ai-fill",
                    file_bytes=jpg, filename=p.name)
            if r.get("available"):
                fields = {**fields, **r.get("fields", {})}
            else:
                print(f"  {p.name}: AI unavailable ({r.get('error')}) — manual fields")

        name = (fields["name"] or p.stem).strip()[:200]
        body = {
            "name": name,
            "category": fields["category"] or "top",
            "color": fields["color"],
            "brand": fields["brand"],
            "sizes": fields["sizes"],
            "owned": True,
        }
        meta = ", ".join(f"{k}={v}" for k, v in body.items() if v and k != "owned")
        if args.dry_run:
            print(f"  would add: {p.name}  ({meta})")
            added += 1
            continue

        created = api(base, token, "POST", "/api/wardrobe", json_body=body)
        if "error" in created or "id" not in created:
            print(f"!! {p.name}: create failed: {created.get('error', created)}")
            skipped += 1
            continue
        gid = created["id"]

        up = api(base, token, "POST", f"/api/wardrobe/{gid}/image",
                 file_bytes=raw, filename=p.name)
        nd = up.get("near_dup_of") or created.get("near_dup_of")
        if nd:
            dup_warnings += 1
            line = f"  {p.name} -> '{name}' (#{gid})  ⚠ NEAR-DUPLICATE of '{nd['name']}' (dist {nd['distance']})"
            if args.skip_dups:
                api(base, token, "DELETE", f"/api/wardrobe/{gid}")
                line += "  — skipped (--skip-dups)"
                skipped += 1
            else:
                line += "  — added anyway" if args.include_dups else "  — review/delete if unwanted"
            print(line)
        else:
            print(f"  {p.name} -> '{name}' (#{gid})")
        added += 1

    print(f"\ndone: {added} added, {skipped} skipped, {dup_warnings} near-duplicate(s) flagged")
    return 0


if __name__ == "__main__":
    sys.exit(main())
