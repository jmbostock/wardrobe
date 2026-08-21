#!/usr/bin/env bash
# Bootstrap ComfyUI for altacloset on ANY host (202 or the target machine).
# Idempotent: safe to re-run. CatVTON weights are fetched AUTOMATICALLY by the
# ComfyUI node on the first try-on (HF cache at ./data/comfyui/models) — no
# manual download commands needed. Use scripts/migrate-to-target.sh to carry
# the cached weights to the target machine instead of re-downloading (~4-6GB).
#
# The image already bakes in: ComfyUI source, the CatVTON node, detectron2/
# DensePose, AND the SCHP inplace_abn CUDA extension fix (nvcc + torch 2.x
# patch + prebuilt .so) — see services/comfyui/Dockerfile.
set -euo pipefail
cd "$(dirname "$0")/.."

MODELS_DIR=./data/comfyui/models
mkdir -p "$MODELS_DIR"

echo ">> ComfyUI bootstrap"
echo "   models dir : $MODELS_DIR"

# 1) weights — auto-downloaded on first try-on by the CatVTON node
if [ -z "$(ls -A "$MODELS_DIR" 2>/dev/null)" ]; then
  echo "   no weights yet — they auto-download on the first try-on (~4-6GB)."
  echo "   (trigger: POST /api/tryon, or run scripts/tryon-test.sh)"
else
  echo "   weights already present, skipping download."
fi

# 2) custom node — the compose image bakes it in, but support dev without rebuild
NODE_DIR=./data/comfyui/custom_nodes/CatVTON
if [ ! -d "$NODE_DIR" ]; then
  echo ">> cloning CatVTON custom node (dev fallback) into $NODE_DIR"
  git clone --depth 1 https://github.com/Zheng-Chong/CatVTON.git "$NODE_DIR"
else
  echo ">> CatVTON node present."
fi

echo
echo ">> Next: docker compose up -d --build"
echo ">> Verify: curl -s http://127.0.0.1:28188/system_stats | head -c 200"
