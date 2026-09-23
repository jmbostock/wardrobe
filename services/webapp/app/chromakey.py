"""Green-screen (chroma-key) handling for person/base photos.

A green-screen full-body base has a clean, uniform background, which gives the
render a neutral studio background instead of a saturated green. We detect a
green screen by the color of the image border and, when found, chroma-key it
out — keying the screen-green by HUE (so enclosed green gaps between the
legs/arms and edge spill are removed too, not just the border-connected
background), protecting any large solid green blob (clothing), despilling the
silhouette edge, and re-compositing the person onto a neutral gray.

Not a green screen → the image is returned unchanged (safe no-op), so ordinary
bases are never touched. Pure PIL, deterministic, no model — mirrors
media.remove_garment_background.

Note: since the CatVTON → Qwen swap (2026-09-23) nothing in the render path
calls chroma_key() any more — the Qwen path edits the photo as-is and has no
letterbox step to match. It is kept because it is still a correct, tested
utility (imageqa uses is_green_screen to reward green-screen bases when
ranking), and removing it would drop that base-quality signal too.
"""
from __future__ import annotations

from collections import deque
from colorsys import rgb_to_hsv

from PIL import Image, ImageFilter

# Neutral composite color for the keyed background. This matched CatVTON's
# letterbox gray; it is kept as-is so any already-keyed assets stay consistent.
BG = (128, 128, 128)

# Border green fraction above which we treat a photo as a green-screen shot.
GREEN_SCREEN_THRESHOLD = 0.5


def is_green_pixel(r: int, g: int, b: int) -> bool:
    """True when a pixel is a screen-green: G clearly dominant over R and B."""
    return g > 55 and g >= r + 35 and g >= b + 35


def _border_fraction(img: Image.Image) -> float:
    """Fraction of the border ring that is screen-green (0-1). Downscaled for
    speed; a full-body green-screen shot has green on every outer edge, so a
    frame where the head nudges the top still leaves a green majority."""
    w, h = img.size
    small = img.resize((max(48, w // 16), max(48, h // 16)))
    sw, sh = small.size
    px = small.load()
    green = total = 0
    for x in range(sw):
        for y in (0, sh - 1):
            total += 1
            if is_green_pixel(*px[x, y][:3]):
                green += 1
    for y in range(sh):
        for x in (0, sw - 1):
            total += 1
            if is_green_pixel(*px[x, y][:3]):
                green += 1
    return green / total if total else 0.0


def is_green_screen(img: Image.Image, threshold: float = GREEN_SCREEN_THRESHOLD) -> bool:
    """True when the outer border is mostly screen-green, so the subject is shot
    on a green screen. A green wall/yard behind a person also qualifies (still a
    clean uniform background that keys cleanly)."""
    try:
        return _border_fraction(img.convert("RGB")) >= threshold
    except Exception:  # noqa: BLE001
        return False


def _hue(c: tuple) -> float:
    """Hue of an RGB pixel in degrees 0-360."""
    r, g, b = (v / 255.0 for v in c[:3])
    h, s, v = rgb_to_hsv(r, g, b)
    return h * 360.0


def _hue_diff(a: float, b: float) -> float:
    """Smallest angular distance between two hues (0-180)."""
    d = abs(a - b) % 360
    return min(d, 360 - d)


def _screen_green_hue(img: Image.Image) -> float | None:
    """Median hue of the green border ring = the screen-green hue for this shot
    (robust to a lighting gradient but specific to this screen)."""
    w, h = img.size
    small = img.resize((max(48, w // 16), max(48, h // 16)))
    sw, sh = small.size
    px = small.load()
    hues = [
        _hue(px[x, y][:3])
        for x in range(sw) for y in (0, sh - 1)
        if is_green_pixel(*px[x, y][:3])
    ]
    hues += [
        _hue(px[x, y][:3])
        for y in range(sh) for x in (0, sw - 1)
        if is_green_pixel(*px[x, y][:3])
    ]
    if not hues:
        return None
    return sorted(hues)[len(hues) // 2]


def _is_screen_green(c: tuple, ref_hue: float) -> bool:
    """True when a pixel is this screen's green: green-dominant and the same hue
    family as the border (catches shadowed/enclosed green)."""
    r, g, b = c[:3]
    return (g >= r + 15 and g >= b + 15 and _hue_diff(_hue((r, g, b)), ref_hue) <= 48)


def chroma_key(img: Image.Image) -> Image.Image:
    """Chromakey a green-screen photo: key out the screen-green (by hue — so
    enclosed green gaps between the legs/arms AND edge spill are removed, not
    just the border-connected background), despill green fringes on the
    silhouette edge, and composite the person onto a neutral gray. A large solid
    green blob (e.g. green clothing) is protected and kept. Returns the input
    unchanged when it is not (detected as) a green screen. Pure PIL; never
    raises — a failure returns the original."""
    try:
        img = img.convert("RGB")
        w, h = img.size
        if not is_green_screen(img):
            return img
        ref_hue = _screen_green_hue(img)
        if ref_hue is None:
            return img

        # Key the screen-green globally at a downscale (fast), then protect any
        # LARGE SOLID green blob (likely clothing) by connected-component size.
        # w//2 keeps the silhouette fine enough that the upscaled edge isn't
        # blocky, while staying fast enough for the render-time prep.
        small = img.resize((max(192, w // 2), max(192, h // 2)))
        sw, sh = small.size
        px = small.load()
        green = [[_is_screen_green(px[x, y][:3], ref_hue) for x in range(sw)]
                 for y in range(sh)]

        comp = [[-1] * sw for _ in range(sh)]
        components: list[tuple[int, list[tuple[int, int]], bool]] = []
        cid = 0
        for y in range(sh):
            for x in range(sw):
                if green[y][x] and comp[y][x] < 0:
                    q = deque([(x, y)])
                    comp[y][x] = cid
                    cells: list[tuple[int, int]] = []
                    touches = False
                    while q:
                        cx, cy = q.popleft()
                        cells.append((cx, cy))
                        if cx == 0 or cy == 0 or cx == sw - 1 or cy == sh - 1:
                            touches = True
                        for nx, ny in ((cx + 1, cy), (cx - 1, cy), (cx, cy + 1), (cx, cy - 1)):
                            if (
                                0 <= nx < sw and 0 <= ny < sh
                                and green[ny][nx] and comp[ny][nx] < 0
                            ):
                                comp[ny][nx] = cid
                                q.append((nx, ny))
                    components.append((cid, cells, touches))
                    cid += 1

        bg = [[False] * sw for _ in range(sh)]
        total = sw * sh
        for _cid, cells, touches in components:
            remove = touches  # border-connected background + spill → key
            if not remove:
                xs = [c[0] for c in cells]
                ys = [c[1] for c in cells]
                bbox = (max(xs) - min(xs) + 1) * (max(ys) - min(ys) + 1)
                fill = len(cells) / bbox if bbox else 0.0
                # a big SOLID green blob is clothing — keep it; a thin enclosed
                # green gap (between legs/arms) is the screen behind — key it.
                remove = not (len(cells) > 0.04 * total and fill > 0.5)
            if remove:
                for cx, cy in cells:
                    bg[cy][cx] = True

        if sum(row.count(True) for row in bg) / total > 0.90:
            return img  # safety: key swallowed the whole frame — not a green screen

        # Foreground mask = not background; upscale to full res with a smooth
        # filter and a small blur so the silhouette edge is anti-aliased (soft
        # feather) rather than the blocky stair-step of a coarse hard mask.
        mask = Image.new("L", (sw, sh), 0)
        mp = mask.load()
        for y in range(sh):
            for x in range(sw):
                mp[x, y] = 0 if bg[y][x] else 255
        mask = mask.resize((w, h), Image.BICUBIC)
        mask = mask.filter(ImageFilter.GaussianBlur(1.5))

        # Despill: in the thin silhouette edge band only, pull green dominance
        # down toward the max of R/B so green fringing disappears without
        # touching real green clothing on the body. inner = mask eroded a few
        # pixels; edge band = foreground pixels outside it.
        inner = mask.filter(ImageFilter.MinFilter(5))
        out = img.copy()
        op = out.load()
        mpp = mask.load()
        ip = inner.load()
        for y in range(h):
            for x in range(w):
                if mpp[x, y] > 128 and ip[x, y] < 128:
                    r, g, b = op[x, y]
                    if g > r + 12 and g > b + 12:
                        op[x, y] = (r, max(r, b), b)

        bg_img = Image.new("RGB", (w, h), BG)
        return Image.composite(out, bg_img, mask)
    except Exception:  # noqa: BLE001 — never break a render on a key failure
        return img
