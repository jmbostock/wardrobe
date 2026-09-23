"""ComfyUI / Qwen-Image-2.1 client for virtual try-on + outfit refinement.

ONE engine, deliberately. Qwen-Image-2.1 is a general image editor given the
person as <image1> plus up to 9 garment references, so a whole outfit renders in
one pass with no render-onto-render chaining. It replaced the CatVTON / IDM-VTON
inpainting stack (both removed 2026-09-23): those needed a mask (AutoMasker /
DensePose), a per-piece geometry pass plus a texture pass, and still lost
garment colour, sleeve length and print detail — and they needed face-restore
and colour-correction repairs on top to paper over the damage. Qwen holds the
garment reference far better and needs none of that machinery.

Flow:
  1. upload the person + garment images to ComfyUI /upload/image
  2. build the QwenImage21 graph (see _qwen_run)
  3. submit to /prompt, poll /history/{id}
  4. return the rendered image bytes

CRITICAL: this runs on a SEPARATE ComfyUI instance (`settings.qwen_comfyui_url`,
202:8188) from the historic `settings.comfyui_url` — the Qwen instance needs
ComfyUI >= 0.37 + ComfyUI-GGUF.
"""
from __future__ import annotations

import asyncio
import base64
import io
import json
import os
import random
import re
import time
from pathlib import Path

import httpx
from PIL import Image, ImageOps

from . import db
from .config import settings
from .wardrobe import Garment


class ComfyUnavailable(Exception):
    """ComfyUI is missing, errored, or timed out — surfaced as HTTP 503."""


# garment category -> which half of the body the garment occupies.
# Used for two things: grouping lowers from uppers when an outfit is split into
# several passes (_qwen_passes), and the reference ROLE wording in the prompt.
CLOTH_TYPE = {
    "top": "upper",
    "outerwear": "upper",
    "dress": "overall",
    "swimsuit": "overall",
    "bottom": "lower",
    "bra": "upper",
}


# --------------------------------------------------------------------------- #
# public entry points                                                          #
# --------------------------------------------------------------------------- #

async def run_tryon_model(
    model: str, person_bytes: bytes, garment: Garment, user_id: int
) -> bytes:
    """Render one garment with a named model backend."""
    if model == "qwen_edit":
        return await _run_qwen_edit(person_bytes, [garment], user_id)
    raise ComfyUnavailable(f"model '{model}' has no renderer wired in tryon.py")


async def run_tryon_outfit_model(
    model: str, person_bytes: bytes, garments: list[Garment], user_id: int
) -> bytes:
    """Render a WHOLE outfit (multiple garments) with a named model backend.

    qwen_edit — every garment goes in as its own reference image. One pass when
                there are <=2 garments, otherwise a short chained schedule (see
                _qwen_passes); never a single oversized multi-reference pass.
    """
    if model == "qwen_edit":
        return await _run_qwen_edit(person_bytes, list(garments), user_id)
    raise ComfyUnavailable(
        f"model '{model}' has a workflow but no outfit renderer wired in tryon.py"
    )


# --------------------------------------------------------------------------- #
# Qwen-Image-2.1 image-edit backend                                            #
# --------------------------------------------------------------------------- #
QWEN_MODELS = {
    "unet": "qwen-image-2.1-Q4_K_M.gguf",
    # Abliterated ("Heretic") text encoder, swapped in 2026-09-23. The text
    # encoder — Qwen3-VL-8B — is what reads BOTH the prompt and the reference
    # images and turns them into conditioning. It is the largest single
    # component (~6.3 GB) and the only place any refusal behaviour lives (the
    # DiT has no refusal mechanism, and the VAE is a pure codec). This build is
    # the same W4A8 format Comfy-Org ships, so it is a drop-in replacement:
    # identical tensor names, genuinely re-weighted values.
    #
    # To A/B it against the stock encoder, change this ONE string back to
    # "qwen3vl_8b_w4a8.safetensors" — nothing else needs to move.
    "clip": "qwen3vl_8b_w4a8_heretic.safetensors",
    "vae": "qwen_image_2.1_vae_bf16.safetensors",
}
# Reference images are VAE-encoded into the text-encoder sequence. This is a
# TOTAL PIXEL BUDGET, not a width/height: 768 caps the reference at ~768^2 px,
# which with the source-latent canvas downscaled an 848x1264 photo to 640x928.
# 1024 (~1MP, the model's official default) keeps near-native output at the
# same VRAM cost as the 1024x1024 blank canvas that already worked. 0 = native
# and is the one setting that has OOM'd the encoder on large phone photos.
QWEN_REF_RES = 1024
QWEN_STEPS = 25
QWEN_TIMEOUT = 900.0


# Garment category -> the BODY SLOT that reference fills in the prompt.
# Deliberately generic words: the reference image defines appearance, and any
# noun here is only telling the model WHERE the item goes.
_QWEN_SLOT = {
    "top": "top",
    "bra": "top",
    "bottom": "bottom half",
    "outerwear": "outer layer",
    "dress": "dress",
    "swimsuit": "swimsuit",
}


def _qwen_edit_prompt(garments: list[Garment]) -> str:
    """Instruction that names each reference by ROLE only — never by looks.

    THE RULE: any appearance claim in the prompt OVERRIDES the reference image.
    Both metadata fields are unreliable, so neither may be injected:

      - `vision_desc` (generated) said the near-BLACK blazer 539 was "navy blue
        ... with silver trim". Injecting it made the model draw silver trim
        along the lapels and hem that the blazer does not have (outfits 114/115).
      - `g.name` (user-entered) says "Navy crewneck" for a DARK GREY sweater and
        "Navy blazer" for that black blazer. Injecting it turned BOTH garments
        navy — the sweater lost its correct grey colour (outfit 118, flagged by
        the user as a regression).

    Fixed-seed A/B on the same 3-garment look showed exactly this:
      name-based  -> sweater navy  (wrong), blazer navy (wrong)
      desc-based  -> sweater dark grey (right), blazer had invented trim (wrong)
      ROLE-based  -> sweater dark grey (right), blazer near-black (right),
                     no invented trim  <- shipped

    So the prompt states only which slot each image fills. Colour, fabric,
    print and trim all come from the reference image, which is the ground truth.
    Still POSITIVE throughout: cfg is 1.0, so a negative prompt is inert, and
    "no collage / no side-by-side" in the positive prompt measurably backfires.
    """
    parts = []
    for i, g in enumerate(garments, start=2):
        slot = _QWEN_SLOT.get((g.category or "").strip().lower(), "item")
        parts.append("the item shown in <image%d> as the %s" % (i, slot))

    if len(parts) == 1:
        dress = parts[0]
    elif len(parts) == 2:
        dress = "%s and %s" % (parts[0], parts[1])
    else:
        dress = ", ".join(parts[:-1]) + ", and " + parts[-1]

    return (
        "Edit <image1>. Keep the SAME person: identical face, hair, skin tone, "
        "body shape, pose and the original background and lighting. Wear %s. "
        "Reproduce each referenced item exactly as it appears in its own image "
        "— its real colour, fabric, printed graphic and length — and do not add "
        "or omit any feature. Photorealistic fashion photo, sharp detail."
        % dress
    )


def _qwen_refine_prompt(instruction: str) -> str:
    """Prompt for refining an ALREADY-RENDERED outfit (the Outfits page
    "Refine this outfit" action).

    This is the one place the user's own words go into the prompt, which is the
    opposite of _qwen_edit_prompt's rule — but not a contradiction of it. That
    rule exists because GARMENT METADATA is an unreliable *claim about how a
    garment looks*, and a claim in the prompt beats the reference image. An
    explicit instruction from the user is not a claim, it IS the intent, so it
    belongs here.

    Note what is deliberately NOT pinned: the pose. Identity and the scene are
    held (so a clothes-only tweak never drifts the face or the room), but saying
    "identical pose" would actively fight a request like "turn her to the side".
    Leaving pose unstated lets the model keep it by default and change it when
    asked — which is exactly the capability this feature is for.
    """
    return (
        "Edit <image1>. Keep the same person — identical face, hair, skin tone "
        "and body — and keep the same background, lighting and framing. "
        "%s. Photorealistic fashion photo, sharp detail."
        % instruction.strip().rstrip(".")
    )


async def _run_qwen_edit(
    person_bytes: bytes, garments: list[Garment], user_id: int
) -> bytes:
    """Render person + garments with Qwen-Image-2.1.

    Fewer than 3 garments = one pass. More = a chained schedule, because a
    single multi-reference pass with 3+ references collapses.
    """
    if not garments:
        raise ComfyUnavailable("qwen_edit: no garments supplied")
    out = person_bytes
    for batch in _qwen_passes(garments):
        refs = [_garment_reference_bytes(g, user_id) for g in batch]
        out = await _qwen_run(_qwen_edit_prompt(batch), [out] + refs)
    return out


def _qwen_passes(garments: list[Garment]) -> list[list[Garment]]:
    """Group garments into edit passes of at most 2 references.

    <=2 garments: one pass (already reliable).

    More: lowers first, then uppers, **never mixed within a pass**. Grouping
    matters as much as ordering. A naive chunk-by-two of [jeans, top, blazer]
    yields [[jeans, top], [blazer]] — which leaves the OUTERWEAR alone in the
    final pass and drops it (verified: blazer vanished). The working schedule
    keeps the top and jacket together and gives the bottom its own pass:
        [[jeans], [top, blazer]]  -> 3/3 correct

    Verified 3-way at a fixed seed (base 53, crewneck + jeans + navy blazer):
      single pass, 3 refs                  -> 2/3, jacket bled into sleeves
      pass1 [jeans] -> pass2 [crew+blazer] -> 3/3  CORRECT
      pass1 [crew+jeans] -> pass2 [blazer] -> blazer DROPPED
    So each category group is chunked separately, lowers first."""
    if len(garments) <= 2:
        return [list(garments)]
    lowers = [g for g in garments if CLOTH_TYPE.get(g.category, "upper") == "lower"]
    uppers = [g for g in garments if CLOTH_TYPE.get(g.category, "upper") != "lower"]
    batches: list[list[Garment]] = []
    for group in (lowers, uppers):
        for i in range(0, len(group), 2):
            batches.append(group[i:i + 2])
    return batches


async def refine_render(base_bytes: bytes, instruction: str) -> bytes:
    """Apply a free-text refinement to an existing render (the Outfits page
    "Refine this outfit" action).

    The render goes in as <image1> with NO garment references: the clothes are
    already on the person, so the model only has to follow the instruction —
    restyle the outfit, change the pose, warm the light, and so on.
    """
    instruction = (instruction or "").strip()
    if not instruction:
        raise ComfyUnavailable("refine: instruction required")
    return await _qwen_run(_qwen_refine_prompt(instruction), [base_bytes])


async def _qwen_run(prompt: str, images: list[bytes]) -> bytes:
    """One Qwen-Image-2.1 edit pass.

    `images[0]` is the image being edited (<image1>); the rest are references
    (<image2>...). Returns the rendered PNG."""
    if not settings.qwen_comfyui_url:
        raise ComfyUnavailable("qwen_edit not configured (set QWEN_COMFYUI_URL)")
    if not images:
        raise ComfyUnavailable("qwen_edit: no images supplied")
    base = settings.qwen_comfyui_url
    timeout = httpx.Timeout(300.0, read=300.0)

    async with httpx.AsyncClient(timeout=timeout) as client:
        person_name = await _upload(client, "qwen_person.png", images[0], base)

        wf: dict = {
            "1": {"class_type": "UnetLoaderGGUF",
                  "inputs": {"unet_name": QWEN_MODELS["unet"]}},
            "2": {"class_type": "CLIPLoader",
                  "inputs": {"clip_name": QWEN_MODELS["clip"],
                             "type": "qwen_image", "device": "default"}},
            "3": {"class_type": "VAELoader",
                  "inputs": {"vae_name": QWEN_MODELS["vae"]}},
            "10": {"class_type": "LoadImage", "inputs": {"image": person_name}},
        }
        enc: dict = {
            "clip": ["2", 0],
            "prompt": prompt,
            "negative_prompt": "",
            "resolution": QWEN_REF_RES,
            # CRITICAL: TextEncodeQwenImage21's optional `vae` input MUST be
            # wired. Without it the model silently IGNORES the reference images
            # and hands back the base photo unchanged — a "no-op" that looks
            # like a successful render. Verified 3-way at a fixed seed (424242),
            # base 53 + a brown hoodie:
            #   no vae + .clean.png  -> base returned untouched (no-op)
            #   vae    + .clean.png  -> hoodie applied correctly
            #   vae    + .cutout.png -> hoodie applied correctly
            # The no-op output was byte-identical across runs, so this was
            # deterministic model behaviour, not sampling noise. The historic
            # background-removal harness hit the same bug: no vae meant RGBA
            # output that was 0% transparent.
            "vae": ["3", 0],
            "images.image_1": ["10", 0],
        }
        for idx, data in enumerate(images[1:], start=2):
            nut = await _upload(client, "qwen_ref_%d.png" % idx, data, base)
            nid = str(20 + idx)
            wf[nid] = {"class_type": "LoadImage", "inputs": {"image": nut}}
            enc["images.image_%d" % idx] = [nid, 0]

        wf["50"] = {"class_type": "TextEncodeQwenImage21", "inputs": enc}
        # Canvas comes from the ENCODER's latent (the source photo), NOT a
        # blank EmptyLatentImage. This is the official template's default
        # (`custom_size: false` -> the ComfySwitchNode picks on_false =
        # TextEncodeQwenImage21.latent). A blank canvas makes the model
        # GENERATE a fresh person; the source latent makes it EDIT the real
        # one, which preserves identity, pose and framing. Output size follows
        # the source, bounded by QWEN_REF_RES (a total-pixel budget).
        wf["52"] = {"class_type": "KSampler",
                    "inputs": {
                        "model": ["1", 0], "positive": ["50", 0],
                        "negative": ["50", 1], "latent_image": ["50", 2],
                        "seed": (settings.tryon_seed
                                 if settings.tryon_seed is not None
                                 else random.randint(0, 2 ** 31)),
                        "steps": QWEN_STEPS, "cfg": 1.0,
                        "sampler_name": "euler", "scheduler": "simple",
                        "denoise": 1.0}}
        wf["53"] = {"class_type": "VAEDecode",
                    "inputs": {"samples": ["52", 0], "vae": ["3", 0]}}
        wf["54"] = {"class_type": "SaveImage",
                    "inputs": {"images": ["53", 0],
                               "filename_prefix": "qwen_edit"}}

        prompt_id = await _submit(client, wf, base)
        entry = await _poll(client, prompt_id, timeout=QWEN_TIMEOUT, base_url=base)
        return await _fetch_output(client, entry, base)


# --------------------------------------------------------------------------- #
# garment reference images                                                     #
# --------------------------------------------------------------------------- #

def _garment_reference_bytes(garment: Garment, user_id: int) -> bytes:
    """The garment reference for the Qwen edit path: the garment on a plain
    WHITE background, no flat-lay backdrop.

    History, so this isn't re-litigated — two reference experiments were tried
    while chasing the "invented white trim" bug and are deliberately NOT used:

      - raw `.cutout.png` (RGBA) -> the model reads the ALPHA CHANNEL AS FABRIC
        and renders a semi-TRANSPARENT jacket (outfit 115, O-WFJ4MR). Confirmed
        regression.
      - `.cutout.png` composited onto solid grey -> worked on the blazer, but
        unproven in general and risks a grey garment vanishing into a grey
        backdrop. Not worth an unproven behaviour change.

    (The trim itself was never a reference problem at all — it was the PROMPT
    passing `vision_desc`, which said "with silver trim". See _qwen_edit_prompt.)

    Prefers the stored <gid>.clean.png (written at save time / nightly backfill);
    falls back to cleaning on the fly only for garments that predate cleaning.
    """
    try:
        d = Path(settings.data_dir) / "wardrobe" / str(garment.user_id)
        clean = d / f"{garment.id}.clean.png"
        if clean.is_file():
            return clean.read_bytes()
    except Exception:  # noqa: BLE001
        pass
    from .media import remove_garment_background as _clean

    return _clean(_load_garment_image(garment, user_id), tolerance=28)


def _load_garment_bytes(garment: Garment, user_id: int) -> bytes:
    """Load a garment's image bytes (used by the vision classifier)."""
    try:
        return _load_garment_image(garment, user_id)
    except Exception:  # noqa: BLE001
        return b""


def _load_garment_image(g: Garment, user_id: int) -> bytes:
    """Resolve the garment image file (any extension) — uses the recorded
    image_path if present, else globs data/wardrobe/<owner>/<gid>.*. Images
    live under the OWNER's dir, so g.user_id is used (the `user_id` arg is the
    person trying it on — kept for call-site clarity)."""
    d = Path(settings.data_dir) / "wardrobe" / str(g.user_id)
    candidate: Path | None = None
    if g.image_path:
        p = d / g.image_path
        if p.is_file():
            candidate = p
    if candidate is None and d.is_dir():
        for p in sorted(d.glob(f"{g.id}.*")):
            if p.is_file():
                candidate = p
                break
    if candidate is None:
        raise ComfyUnavailable(
            f"garment image missing for #{g.id} — add one in the Wardrobe tab "
            f"(upload or paste a product image link)"
        )
    return candidate.read_bytes()


# --------------------------------------------------------------------------- #
# vision classification (garment type + base-photo style)                      #
# --------------------------------------------------------------------------- #

def _is_shorts(garment: Garment) -> bool:
    """A bottom garment whose name says shorts. Category is 'bottom' for both
    shorts and pants, so the name is the signal — and a "shortsleeve top"
    (category 'top') is correctly excluded. Only used as the last-resort
    fallback when vision is unavailable (see base_type_for)."""
    return garment.category == "bottom" and "short" in (garment.name or "").lower()


_GARMENT_DESCRIBE_PROMPT = (
    "Look at this single clothing item (flat-lay or product photo). Reply with "
    "EXACTLY these two lines and nothing else:\n"
    "TYPE: one of SHORTS, PANTS, DRESS, SKIRT, TOP, OUTERWEAR, OTHER\n"
    "DESC: one short accurate sentence describing the garment the way a stylist "
    "would (colour, cut, length, key features) — enough to re-identify it and "
    "to know what body it covers.\n"
    "Example:\n"
    "TYPE: SHORTS\n"
    "DESC: olive green cargo shorts ending above the knee with side pockets.\n"
)

# What a person photo shows on the lower body — used to match a BASE PHOTO to
# the look being rendered (a shorts look needs a bare-leg base, a dress look a
# dress base; a pants base makes a dress render as pants).
_PERSON_STYLE_PROMPT = (
    "Look at this photo of one person. What are they wearing on their LOWER "
    "body? Reply with EXACTLY one word and nothing else:\n"
    "DRESS   — a one-piece dress, jumpsuit or romper covering the torso too\n"
    "SHORTS  — shorts, or bare legs / swimwear below the waist\n"
    "PANTS   — trousers, jeans, leggings or a skirt (legs covered to the ankle)\n"
    "OTHER   — cannot tell (the lower body is cropped out, hidden or unclear)\n"
)

# TYPE -> base-photo need (see base_type_for)
_GARMENT_TYPE_TO_BASE = {
    "shorts": "shorts",          # bare legs below
    "pants": "pants",            # separates, never a dress
    "skirt": "pants",            # separates (a dress base would merge it)
    "dress": "dress",            # a dress base (pants base makes it render as pants)
    "jumpsuit": "dress",         # one-piece
    "romper": "dress",           # one-piece
}

_PERSON_STYLE_MAP = {
    "dress": "dress",
    "shorts": "shorts",
    "pants": "pants",
    "other": "unknown",
}


def _vision_image(data: bytes, max_px: int = 512) -> str:
    """Downscale an image and return it as a base64 JPEG (for the vision API)."""
    img = ImageOps.exif_transpose(Image.open(io.BytesIO(data)))
    img.thumbnail((max_px, max_px), Image.LANCZOS)
    buf = io.BytesIO()
    img.convert("RGB").save(buf, "JPEG", quality=85)
    return base64.b64encode(buf.getvalue()).decode("ascii")


async def _vision_ask(prompt: str, image_bytes: bytes) -> str:
    """Ask the vision model about ONE image and return its raw text reply.

    Shared by the garment describer and the base-photo style classifier so both
    use the same engine plumbing. `temperature: 0` so classification is as
    reproducible as the backend allows. Raises on transport/shape errors —
    callers degrade gracefully."""
    b64 = _vision_image(image_bytes)
    # long timeout: vision is wake-on-demand, first call after idle cold-starts (~60-90s)
    if settings.vision_engine == "llamacpp":
        url = f"{settings.vision_url}/v1/chat/completions"
        content = [{"type": "text", "text": prompt},
                   {"type": "image_url",
                    "image_url": {"url": f"data:image/jpeg;base64,{b64}"}}]
        payload = {"messages": [{"role": "user", "content": content}],
                   "stream": False, "temperature": 0}
        async with httpx.AsyncClient(timeout=150) as c:
            r = await c.post(url, json=payload)
        if r.status_code != 200:
            return ""
        return (r.json() or {}).get("choices", [{}])[0].get("message", {}).get("content", "")
    model = os.getenv("OLLAMA_VISION_MODEL", "qwen2.5vl:3b").strip()
    payload = {"model": model, "prompt": prompt, "images": [b64],
               "stream": False, "options": {"temperature": 0}}
    async with httpx.AsyncClient(timeout=150) as c:
        r = await c.post(f"{settings.ollama_url}/api/generate", json=payload)
    if r.status_code != 200:
        return ""
    return (r.json() or {}).get("response", "")


async def describe_garment(garment_bytes: bytes) -> dict:
    """Accurately DESCRIBE a garment image with vision (never its name):
    returns {'type': 'shorts'|'pants'|'dress'|'skirt'|'top'|'outerwear'|'other',
             'description': '...'}. `description` is a stylist's one-liner used
    for wardrobe metadata. On any failure returns
    {'type': 'other', 'description': ''} so callers degrade safely."""
    if not garment_bytes:
        return {"type": "other", "description": ""}
    try:
        text = await _vision_ask(_GARMENT_DESCRIBE_PROMPT, garment_bytes)
    except Exception:  # noqa: BLE001
        return {"type": "other", "description": ""}
    t = (text or "").strip()
    style = "other"
    desc = ""
    tm = re.search(r"TYPE:\s*([A-Z_]+)", t, re.I)
    if tm:
        word = tm.group(1).lower()
        if word in ("shorts", "pants", "dress", "skirt", "top", "outerwear",
                    "other", "jumpsuit", "romper"):
            style = word
    dm = re.search(r"DESC:\s*(.+)$", t, re.I | re.M)
    if dm:
        desc = dm.group(1).strip().strip('"').strip()
    return {"type": style, "description": desc}


async def classify_person_style(person_bytes: bytes) -> str:
    """'dress' | 'shorts' | 'pants' | 'unknown' for a base photo — what the
    person is wearing on their lower body.

    Vision, not geometry. This used to run CatVTON's AutoMasker and read the
    'lower' mask's start height plus a bare-leg skin test — which needed the GPU
    renderer online just to answer a classification question, and returned
    'unknown' whenever ComfyUI was busy. The vision model is already here for
    garment typing and answers the same question directly.

    'unknown' on any failure — callers must treat that as "cannot prove a
    mismatch", never as a mismatch."""
    if not person_bytes:
        return "unknown"
    try:
        text = await _vision_ask(_PERSON_STYLE_PROMPT, person_bytes)
    except Exception:  # noqa: BLE001
        return "unknown"
    for w in (text or "").strip().split():
        got = _PERSON_STYLE_MAP.get(w.strip(".,!*:;\n").lower())
        if got:
            return got
    return "unknown"


async def base_type_for(garment: Garment, user_id: int) -> str | None:
    """What kind of base photo a garment needs, decided by DESCRIBING the
    garment image with vision (the description is tied to the try-on composite,
    never guessed from the name):
      'shorts' — shorts → bare-leg/shorts base
      'pants'  — pants/skirt → separates base (never a dress)
      'dress'  — dress/jumpsuit/romper → dress base (a pants base renders it as pants)
      None     — top/outerwear/etc → any full-body base
    Falls back to the name keyword ONLY when vision is unavailable (graceful
    degradation — a last resort, not the primary signal)."""
    info = await describe_garment(_load_garment_bytes(garment, user_id))
    base = _GARMENT_TYPE_TO_BASE.get(info["type"])
    if base is not None:
        return base
    # vision down / unparseable → last-resort keyword (bottom garments only)
    if CLOTH_TYPE.get(garment.category, "upper") == "lower":
        return "shorts" if _is_shorts(garment) else "pants"
    return None


# --------------------------------------------------------------------------- #
# cached vision store — base picking is a DB read, not a live vision call     #
# --------------------------------------------------------------------------- #
# At upload (and the nightly rec_weekly batch) we precompute + store:
#   garments.vision_type / vision_desc  — what the garment IS + a stylist line
#   photos.vision_type                  — what the person is wearing in the base
#   photo_embeddings (FashionCLIP)      — for garment↔photo similarity ranking
# The picker below reads those FIRST; live vision is only a fallback for items
# that were never classified (so picking never blocks or flips on a model call).

def get_garment_vision(garment_id: int) -> tuple[str, str]:
    """Stored vision classification for a garment -> (type, description)."""
    conn = db.init()
    with db.lock():
        row = conn.execute(
            "SELECT vision_type, vision_desc FROM garments WHERE id=?", (garment_id,)
        ).fetchone()
    if not row:
        return "", ""
    return row["vision_type"] or "", row["vision_desc"] or ""


def set_garment_vision(garment_id: int, gtype: str, desc: str = "") -> None:
    conn = db.init()
    with db.lock():
        conn.execute(
            "UPDATE garments SET vision_type=?, vision_desc=? WHERE id=?",
            (gtype or "", desc or "", garment_id),
        )
        conn.commit()


def get_photo_vision(photo_id: int) -> str:
    """Stored 'what the person is wearing' classification for a base photo."""
    conn = db.init()
    with db.lock():
        row = conn.execute(
            "SELECT vision_type FROM photos WHERE id=?", (photo_id,)
        ).fetchone()
    return row["vision_type"] or "" if row else ""


def set_photo_vision(photo_id: int, ptype: str) -> None:
    conn = db.init()
    with db.lock():
        conn.execute("UPDATE photos SET vision_type=? WHERE id=?", (ptype or "", photo_id))
        conn.commit()


async def garment_base_type(garment: Garment, user_id: int) -> str | None:
    """What base a garment needs, using the STORED vision classification when
    present (computed at upload / nightly — no live vision). Falls back to a
    live vision describe only for garments that were never classified."""
    gtype, _ = get_garment_vision(garment.id)
    if gtype:
        return _GARMENT_TYPE_TO_BASE.get(gtype)
    return await base_type_for(garment, user_id)


async def photo_style_cached(person_bytes: bytes, photo_id: int | None) -> str:
    """'dress'|'shorts'|'pants'|'unknown' for a base photo, from the STORED
    photo vision when available (no live vision at pick time). Falls back to the
    live classifier only when the photo has no stored classification yet."""
    if photo_id is not None:
        stored = get_photo_vision(photo_id)
        if stored:
            return stored
    return await classify_person_style(person_bytes)


# --------------------------------------------------------------------------- #
# ComfyUI HTTP plumbing                                                        #
# --------------------------------------------------------------------------- #

async def free_renderer_models(base_url: str | None = None) -> bool:
    """Ask a ComfyUI instance to unload every loaded model and clear the torch
    cache. Returns True if it answered.

    WHY THIS EXISTS. ComfyUI caches loaded models indefinitely. Qwen-Image-2.1
    holds ~11-12GB of a 16GB card while resident — and that residency is the
    *point*: it is why back-to-back try-ons are fast instead of paying an ~11GB
    reload every time. The cost is that nothing else can have the GPU while it
    sits there.

    So this is for HANDING THE CARD OVER, not for routine hygiene: call it
    before a different renderer needs the GPU (the Wan video stack in
    ~/comfy-ui on 202 binds the same device) and again after that renderer is
    done, so each one gets the whole card in turn.

    Deliberately NOT called in the try-on / refine path — freeing after every
    render would force a full model reload on the next one and make the common
    case much slower.

    Defaults to the Qwen instance because that is the only renderer the app
    uses; pass `base_url` to free a different ComfyUI. Failures are swallowed:
    a stale load risks a later OOM, never a 500."""
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(
                f"{base_url or settings.qwen_comfyui_url}/free",
                json={"unload_models": True, "free_memory": True},
            )
            return r.status_code == 200
    except Exception:  # noqa: BLE001
        return False


async def _upload(
    client: httpx.AsyncClient, name: str, data: bytes, base_url: str | None = None
) -> str:
    r = await client.post(
        f"{base_url or settings.comfyui_url}/upload/image",
        files={"image": (name, data, "image/png")},
    )
    r.raise_for_status()
    return r.json()["name"]


async def _submit(
    client: httpx.AsyncClient, workflow: dict, base_url: str | None = None
) -> str:
    r = await client.post(
        f"{base_url or settings.comfyui_url}/prompt", json={"prompt": workflow}
    )
    if r.status_code != 200:
        raise ComfyUnavailable(f"ComfyUI rejected prompt: {r.text[:300]}")
    return r.json()["prompt_id"]


async def _poll(
    client: httpx.AsyncClient,
    prompt_id: str,
    timeout: float = 240.0,
    base_url: str | None = None,
) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        r = await client.get(f"{base_url or settings.comfyui_url}/history/{prompt_id}")
        r.raise_for_status()
        entry = r.json().get(prompt_id)
        if entry:
            status = entry.get("status", {})
            if status.get("completed"):
                return entry
            if status.get("status_str") == "error":
                raise ComfyUnavailable(f"ComfyUI error: {status.get('messages')}")
        await asyncio.sleep(2)
    raise ComfyUnavailable(f"ComfyUI timeout after {timeout:.0f}s")


async def _fetch_output(
    client: httpx.AsyncClient, entry: dict, base_url: str | None = None
) -> bytes:
    for node in entry.get("outputs", {}).values():
        for img in node.get("images", []):
            r = await client.get(
                f"{base_url or settings.comfyui_url}/view",
                params={
                    "filename": img["filename"],
                    "subfolder": img.get("subfolder", ""),
                    "type": img.get("type", "output"),
                },
            )
            r.raise_for_status()
            return r.content
    raise ComfyUnavailable("ComfyUI finished but produced no image")
