#!/usr/bin/env bash
# Bootstrap ComfyUI for altacloset on ANY host (202 or the target machine).
# Idempotent: safe to re-run. Downloads CatVTON weights into ./data/comfyui/models
# so the target machine never re-downloads them after scripts/migrate-to-target.sh.
#
# Phase 2 TODO: fill in the exact `huggingface-cli download` / wget commands for the
# CatVTON weights (SD1.5 inpainting checkpoint + CatVTON LoRA + DensePose/SCHP
# parsers). See docs/tryon-pipeline.md for the model list.
set -euo pipefail
cd "$(dirname "$0")/.."

MODELS_DIR=./data/comfyui/models
mkdir -p "$MODELS_DIR"

echo ">> ComfyUI bootstrap"
echo "   models dir : $MODELS_DIR"

# 1) weights (idempotent)
if [ -z "$(ls -A "$MODELS_DIR" 2>/dev/null)" ]; then
  echo "   no weights yet — downloading (Phase 2: add commands here)."
  echo "   e.g.: huggingface-cli download <repo> --local-dir $MODELS_DIR"
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
