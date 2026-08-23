# Wardrobe Project — Architectural & Efficiency Review (Host 202)

**Target Hardware:** Single NVIDIA GPU with **16GB VRAM** (RTX 5060 Ti 16GB baseline)  
**Target Performance:** Near real-time user experience (< 5–10s total end-to-end response for interactive flows)  
**Evaluated Version:** v0.14.0 (`altacloset`)

> **Status update (later same day):** §3 Hotspot 1 and §4 have been resolved by
> **removing the model-based orientation entirely**. The 3-vote "which edge is the
> top" detector (`ai_upright_rotation` / `_top_edge`) was unreliable (the small VLM
> flip-flopped between left/down/up/right on folded flat-lays) and its 90° rotations
> produced **landscape** photos, which the user forbade. Orientation is now fully
> **deterministic** (`media.normalize_orientation`): EXIF righting → optional 180°
> flip from the tag-reader → hard guarantee the saved photo is portrait. `ai_orientation`
> now only tests 0°/180° (2 VLM calls, not 4). The review's broader VRAM/SVD/photopick
> findings still stand.

---

## 1. Executive Summary & Verdict

The `altacloset` project provides a rich virtual wardrobe experience with solid foundational choices (e.g., CatVTON for garment try-on, rule-based recommendation, pure-PIL image quality checks). 

However, **the current model pipeline suffers from VRAM contention, model swapping, and unnecessary multi-call VLM latency**, preventing it from operating in near real-time on a 16GB VRAM card.

### Key Diagnoses:
1. **VRAM Over-subscription (~20GB peak on a 16GB card):**
   Running **CatVTON (~8GB)**, **SVD-XT (~9GB)**, and **Qwen2.5-VL 3B (~3GB)** alongside dual PyTorch/Ollama CUDA contexts exceeds the 16GB VRAM budget. This forces PyTorch into CPU RAM offloading/swapping, severely inflating latency.
2. **VLM Hammering & Redundant Round-Trips:**
   On a single garment upload, the system makes **up to 4 sequential Ollama VLM calls** (3 calls for 3-vote orientation + 1 call for metadata tag-read). This adds 10–20s of pure API latency and risks model output corruption under rapid calls.
3. **Uncached Multi-Image VLM Inferences (`photopick.py`):**
   Selecting a garment triggers a 1-garment + 10-candidate-photo VLM inference (5–15s). Doing this dynamically on dropdown changes over-engineers the selection path when a pure-PIL heuristic (`imageqa.suitability`) runs in <2ms.

---

## 2. VRAM Footprint & Model Swap Audit

| Component / Model | Role | Peak VRAM | Latency per Call | VRAM Footprint Risk on 16GB Card |
|---|---|---|---|---|
| **CatVTON** | Virtual Try-on (Diffusion) | ~7.5 – 8.5 GB | 10 – 18s | **Primary Workhorse.** Fits well on 16GB when alone. |
| **SVD-XT** (`svd_xt.safetensors`) | 3s WEBP Motion Animation | ~8.5 – 9.5 GB | 35 – 90s | ⚠️ **High Risk.** Concurrent CatVTON + SVD causes VRAM swapping to host RAM. |
| **Qwen2.5-VL 3B** (`qwen2.5vl:3b`) | Tag read, orientation, photo pick | ~2.5 – 3.2 GB | 2 – 5s / call | ⚠️ **Contention Risk.** Loaded into Ollama engine simultaneously. |
| **Dual CUDA Contexts** | PyTorch (ComfyUI) + llama.cpp (Ollama) | ~1.2 – 1.8 GB | Overhead | Fixed system overhead reducing available VRAM budget to ~14.2GB. |

### Diagnostic Findings:
- **Total Concurrent Memory Demand:** ~20.5 GB VRAM.
- When CatVTON and SVD run in ComfyUI while Qwen2.5-VL is active in Ollama, system memory fallback triggers. On host 202 (where system RAM available was as low as ~5.7GB during peak utilization), swapping to host RAM creates severe slowdowns.

---

## 3. Pipeline Efficiency & Over-Engineering Analysis

```
[ Current Upload Pipeline ]
Garment Image ---> Ollama (Orient 1) -\
               ---> Ollama (Orient 2) --+-> Vote Majority ---> Ollama (Tag Read) ---> Done (12-20s)
               ---> Ollama (Orient 3) -/

[ Recommended Streamlined Upload Pipeline ]
Garment Image ---> Ollama (Single Pass: Orient + Tag Read + Category) ---> Done (2-4s)
```

### Hotspot 1: Garment Upload & Orientation (`aifill.py`)
- **Current Flow:**
  - `ai_upright_rotation()` calls `_top_edge()`, which executes a `for _ in range(3)` loop issuing up to 3 separate Ollama VLM requests per image for majority voting.
  - `ai_fill_garment()` issues a 4th separate Ollama VLM request for tag reading (brand, color, category, sizes).
- **Inefficiency:** 4 sequential HTTP calls + 4 VLM forward passes for 1 uploaded image.
- **Solution:** Combine orientation detection AND tag/metadata extraction into a **single VLM prompt** executed once with `temperature=0`.

### Hotspot 2: Auto Photo Pick (`photopick.py`)
- **Current Flow:**
  - `GET /api/photos/best-for-garment/{id}` sends 11 images (garment + candidates) in one multi-image VLM request to Qwen2.5-VL.
  - Takes 6–15 seconds to return a recommendation.
- **Inefficiency:** Dynamic VLM calls on interactive UI events (picking items in dropdowns) create high perceived latency.
- **Solution:**
  1. Default instantly to `imageqa.suitability()` (pure PIL, <2ms execution time).
  2. Perform VLM photo ranking asynchronously when candidate photos are first uploaded, caching results in SQLite (`garment_photo_rankings`).

### Hotspot 3: Image Pre-processing (`tryon.py`)
- **Current Flow:** `_prep_person()` performs EXIF normalization and letterboxing on every try-on render invocation.
- **Solution:** Pre-compute and store normalized 768×1024 base assets upon photo upload.

---

## 4. Evaluation of the Orientation Approach ("Look, then rotate")

### Evaluation:
- **Concept:** Asking *"which edge of the image is closest to the TOP of the garment (top/bottom/left/right)?"* is significantly more resilient for flat-lays than OCR tag reading or naive aspect-ratio checks.
- **Implementation Flaws:**
  1. **3-Vote Voting Loop:** The 3-vote loop was implemented to combat small VLM unreliability, but querying Ollama 3 times back-to-back causes VRAM thrashing and `????` response garbage under rapid load.
  2. **Decoupled Metadata:** Running orientation separately from metadata extraction duplicates input decoding and image tokenization overhead.

### Consolidated Single-Pass VLM Prompt:
By combining both tasks into one prompt, latency drops by **~75%**:

```text
Analyze this clothing photo. Respond with EXACTLY 6 labeled lines:
TOP_EDGE: <top|bottom|left|right>
NAME: <short descriptive name>
BRAND: <brand on visible tag, else blank>
COLOR: <primary color>
CATEGORY: <top|bottom|dress|outerwear|footwear|accessory>
SIZES: <visible sizes comma-separated, else blank>
```

---

## 5. Real-Time & 16GB VRAM Optimization Roadmap

### Phase 1: VLM & API Call Reduction (Immediate 3-4x Speedup)
1. **Single-Pass Garment Processing (`aifill.py`):**
   Replace `ai_upright_rotation` + `ai_fill_garment` with `ai_analyze_garment()`, executing exactly **one** Ollama VLM call per upload.
2. **Ollama Keep-Alive Configuration:**
   Set `OLLAMA_KEEP_ALIVE=2m` in `docker-compose.yml` so Qwen2.5-VL unloads from VRAM after 2 minutes of idle time, freeing ~3GB VRAM for CatVTON try-on renders.

### Phase 2: VRAM Budget & ComfyUI Tuning
1. **ComfyUI Sequential Model Unloading:**
   Ensure ComfyUI uses `--gpu-only` or `--lowvram` memory management so CatVTON weights and SVD weights are not held in VRAM simultaneously.
2. **Background SVD Processing:**
   Treat SVD animation clips as an explicit background queue (`clips` table is already async). Ensure try-on renders prioritize GPU execution over background SVD jobs.

### Phase 3: Fast Heuristic-First UX
1. **Instant Photo Pick:** Use `imageqa.suitability` for synchronous UI updates (<2ms).
2. **Persistent Caching:** Store computed phash, suitability, and VLM metadata directly in SQLite (`garments` and `photos` tables) to eliminate redundant processing.

---

## 6. Recommended Metric Checklist for 202 Deployment

| Operation | Current Latency | Target Latency | Key Fix Required |
|---|---|---|---|
| **Garment Upload & Auto-Orient** | ~12 – 20s | **< 3s** | Single-pass VLM (1 call instead of 4). |
| **Garment Selection (Best Photo)** | ~6 – 12s | **< 10ms** | PIL heuristic default + VLM response caching. |
| **CatVTON Render** | ~12 – 20s | **< 10s** | Full VRAM availability (Ollama auto-unload). |
| **SVD Clip Generation** | ~40 – 90s | **Async Background** | Strict queue prioritization over interactive render. |
