# altacloset comfyui — ComfyUI + CatVTON virtual try-on worker.
# Base is CUDA 12.8 + Python (the RTX 5060 Ti is Blackwell sm_120 → needs torch
# built with CUDA >= 12.8). Detectron2/DensePose (used for body-part masking) are
# compiled CPU-only (FORCE_CUDA=0) so no CUDA toolkit/nvcc is needed; the R_50
# model still runs on the GPU via pure-PyTorch ops.
#
# All CatVTON material is CC BY-NC-SA 4.0 (non-commercial).
FROM nvidia/cuda:12.8.1-cudnn-runtime-ubuntu22.04

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1

# base tools + python
RUN apt-get update && apt-get install -y --no-install-recommends \
      python3 python3-pip python3-venv git curl unzip build-essential ninja-build \
      ffmpeg libgl1 libglib2.0-0 ca-certificates \
 && rm -rf /var/lib/apt/lists/* \
 && ln -sf /usr/bin/python3 /usr/local/bin/python \
 && python -m pip install --no-cache-dir --upgrade pip setuptools wheel

# PyTorch for CUDA 12.8 (Blackwell sm_120 support)
RUN pip install --no-cache-dir torch==2.7.1 torchvision==0.22.1 \
      --index-url https://download.pytorch.org/whl/cu128

# ComfyUI (pip-installable; frontend package comes via requirements)
RUN git clone --depth 1 https://github.com/Comfy-Org/ComfyUI.git /opt/ComfyUI \
 && pip install --no-cache-dir -r /opt/ComfyUI/requirements.txt

# CatVTON ComfyUI custom node (official release zip)
ADD https://github.com/Zheng-Chong/CatVTON/releases/download/ComfyUI/ComfyUI-CatVTON.zip /tmp/ComfyUI-CatVTON.zip
RUN mkdir -p /opt/ComfyUI/custom_nodes/ComfyUI-CatVTON \
 && cd /tmp && unzip -q ComfyUI-CatVTON.zip \
 && cp -r /tmp/ComfyUI-CatVTON/ComfyUI-CatVTON/. /opt/ComfyUI/custom_nodes/ComfyUI-CatVTON/ \
 && rm -rf /tmp/ComfyUI-CatVTON /tmp/ComfyUI-CatVTON.zip

# CatVTON node python deps (pinned to the versions the node was tested with)
RUN pip install --no-cache-dir \
      diffusers==0.29.2 accelerate==0.31.0 huggingface_hub==0.23.4 \
      transformers==4.27.3 scipy scikit-image opencv-python tqdm pybind11

# Detectron2 + DensePose from facebookresearch (CPU-only compile — no nvcc)
ENV FORCE_CUDA=0
RUN git clone --depth 1 https://github.com/facebookresearch/detectron2.git /opt/detectron2 \
 && cd /opt/detectron2 && pip install --no-build-isolation -e . \
 && pip install --no-build-isolation -e /opt/detectron2/projects/DensePose

WORKDIR /opt/ComfyUI
ENV HF_HOME=/root/.cache/huggingface
EXPOSE 8188
CMD ["python", "main.py", "--listen", "0.0.0.0", "--port", "8188", "--disable-auto-launch"]

