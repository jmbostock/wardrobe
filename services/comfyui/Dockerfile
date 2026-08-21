# altacloset comfyui — ComfyUI + CatVTON virtual try-on worker.
# Base is CUDA 12.8 + Python (the RTX 5060 Ti is Blackwell sm_120 → needs torch
# built with CUDA >= 12.8). Detectron2/DensePose (used for body-part masking):
# built CPU-only (FORCE_CUDA=0) in the layer below because the runtime base has
# no nvcc; once we install the CUDA toolkit compiler later in the file, a follow-
# up layer reinstalls detectron2 with FORCE_CUDA=1 so its kernels run on the GPU.
#
# All CatVTON material is CC BY-NC-SA 4.0 (non-commercial).
FROM nvidia/cuda:12.8.1-cudnn-runtime-ubuntu22.04

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1

# base tools + python (python3-dev provides Python.h for C++ extension builds)
RUN apt-get update && apt-get install -y --no-install-recommends \
      python3 python3-pip python3-venv python3-dev \
      git curl unzip build-essential ninja-build \
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
 && cp -r /tmp/ComfyUI-CatVTON/. /opt/ComfyUI/custom_nodes/ComfyUI-CatVTON/ \
 && rm -rf /tmp/ComfyUI-CatVTON /tmp/ComfyUI-CatVTON.zip

# CatVTON node python deps (pinned to the versions the node was tested with)
RUN pip install --no-cache-dir \
      diffusers==0.29.2 accelerate==0.31.0 huggingface_hub==0.23.4 \
      transformers==4.27.3 scipy scikit-image opencv-python tqdm pybind11

# Detectron2 + DensePose from facebookresearch. CPU-only compile here (no nvcc
# yet); a layer later in this file reinstalls detectron2 with FORCE_CUDA=1 (see
# the "Rebuild detectron2's _C extension WITH CUDA ops" comment below).
ENV FORCE_CUDA=0
RUN git clone --depth 1 https://github.com/facebookresearch/detectron2.git /opt/detectron2 \
 && cd /opt/detectron2 && pip install --no-build-isolation -e . \
 && pip install --no-build-isolation -e /opt/detectron2/projects/DensePose

WORKDIR /opt/ComfyUI
ENV HF_HOME=/root/.cache/huggingface
EXPOSE 8188
CMD ["python", "main.py", "--listen", "0.0.0.0", "--port", "8188", "--disable-auto-launch"]

# ComfyUI's requirements may have upgraded torch to a cu13 build that needs
# libcudart.so.13 — re-pin torch/torchvision/torchaudio to CUDA 12.8 (matches
# this base image / Blackwell sm_120) and expose the CUDA runtime libs.
RUN pip install --no-cache-dir torch==2.7.1 torchvision==0.22.1 torchaudio==2.7.1 \
      --index-url https://download.pytorch.org/whl/cu128
ENV LD_LIBRARY_PATH=/usr/local/cuda/lib64:${LD_LIBRARY_PATH:-}

# The node pins an old transformers (4.27.3) that breaks ComfyUI's Qwen2 nodes.
# Late 4.x has both Qwen2Tokenizer (ComfyUI) and top-level CLIPTextModel (node).
RUN pip install --no-cache-dir "transformers>=4.44,<5"

# ======================================================================
# SCHP inplace_abn CUDA extension fix (torch 2.x + Blackwell sm_120) — 2026-08-21
# ----------------------------------------------------------------------
# The CatVTON node's SCHP parsing network (model/SCHP/networks/AugmentCE2P.py)
# is built on InPlaceABNSync, a C++/CUDA extension that modules/functions.py
# compiles at import time via torch.utils.cpp_extension.load(). It failed on
# the running stack for two reasons:
#   1) This base image is the CUDA *runtime* variant — no nvcc, so the .cu
#      kernels died with "/bin/sh: 1: /usr/local/cuda/bin/nvcc: not found".
#   2) The extension is written for torch 1.x and passes z.type() (removed in
#      torch 2.x: "cannot convert DeprecatedTypeProperties to ScalarType") to
#      AT_DISPATCH_FLOATING_TYPES.
# Fix: install the CUDA 12.8 toolkit compiler + dev headers (nvcc, thrust via
# cuda-cccl, plus cublas/cusparse/cusolver/cudss headers pulled in by torch's
# ATen/cuda/CUDAContextLight.h), patch z.type()->z.scalar_type() in the .cpp
# and .cu kernels (and the wrapper + checks.h), then prebuild so the compiled
# .so is baked into the image — no compile on first startup. Versions are
# pinned; libcublas is a held package in this base image so we pass
# --allow-change-held-packages. cudss ships headers under /usr/include/libcudss
# (Debian-style), so symlink them into /usr/local/cuda/include.
RUN apt-get update && apt-get install -y --no-install-recommends \
      cuda-nvcc-12-8 cuda-cudart-dev-12-8 \
 && apt-get install -y --no-install-recommends --allow-change-held-packages \
      libcublas-12-8=12.8.5.5-1 libcublas-dev-12-8=12.8.5.5-1 \
      libcusparse-12-8=12.5.8.93-1 libcusparse-dev-12-8=12.5.8.93-1 \
      libcusolver-dev-12-8=11.7.3.90-1 \
      libcudss0-dev-cuda-12=0.8.0.10-1 \
 && ln -sf /usr/include/libcudss/12/cudss.h                    /usr/local/cuda/include/cudss.h \
 && ln -sf /usr/include/libcudss/12/cudss_data_types.h         /usr/local/cuda/include/cudss_data_types.h \
 && ln -sf /usr/include/libcudss/12/cudss_distributed_interface.h /usr/local/cuda/include/cudss_distributed_interface.h \
 && ln -sf /usr/include/libcudss/12/cudss_threading_interface.h  /usr/local/cuda/include/cudss_threading_interface.h \
 && rm -rf /var/lib/apt/lists/*

# Blackwell sm_120 (RTX 5060 Ti). Also keeps the prebuilt extension cache keys
# identical between image build and container runtime (no recompile at boot).
ENV TORCH_CUDA_ARCH_LIST=12.0

# Rebuild detectron2's _C extension WITH CUDA ops. The earlier layer built it
# CPU-only (FORCE_CUDA=0 — there was no nvcc at that point), so DensePose
# body-part masking fell back to torchvision ops for ROIAlign/NMS/deform. Now
# that nvcc + dev headers are installed, rebuild so those kernels run on the
# GPU (R_50 backbone already runs via pure-PyTorch on GPU). Note: `pip install
# -e .` in the original layer produced a *regular* (non-editable) install in
# site-packages — we mirror that with a non-editable install here.
RUN cd /opt/detectron2 \
 && FORCE_CUDA=1 pip install --no-build-isolation . \
 && FORCE_CUDA=1 pip install --no-build-isolation -e projects/DensePose

# Patch torch 1.x API usages in the inplace_abn sources
# (Tensor::type() -> Tensor::scalar_type(); use .is_cuda() directly).
RUN sed -i 's/AT_DISPATCH_FLOATING_TYPES(z\.type()/AT_DISPATCH_FLOATING_TYPES(z.scalar_type()/g' \
        /opt/ComfyUI/custom_nodes/ComfyUI-CatVTON/model/SCHP/modules/src/inplace_abn_cpu.cpp \
 && sed -i -e 's/AT_DISPATCH_FLOATING_TYPES(x\.type()/AT_DISPATCH_FLOATING_TYPES(x.scalar_type()/g' \
           -e 's/AT_DISPATCH_FLOATING_TYPES(z\.type()/AT_DISPATCH_FLOATING_TYPES(z.scalar_type()/g' \
        /opt/ComfyUI/custom_nodes/ComfyUI-CatVTON/model/SCHP/modules/src/inplace_abn_cuda.cu \
 && sed -i 's/\.type()\.scalarType()/.scalar_type()/g' \
        /opt/ComfyUI/custom_nodes/ComfyUI-CatVTON/model/SCHP/modules/src/inplace_abn.cpp \
 && sed -i 's/\.type()\.is_cuda()/.is_cuda()/g' \
        /opt/ComfyUI/custom_nodes/ComfyUI-CatVTON/model/SCHP/modules/src/utils/checks.h

# Prebuild the inplace_abn extension (same load() call as modules/functions.py)
# so the compiled .so is baked into the image — first startup imports it in <1s.
RUN cd /opt/ComfyUI/custom_nodes/ComfyUI-CatVTON \
 && python -c "from torch.utils.cpp_extension import load; import os; _s='model/SCHP/modules/src'; load(name='inplace_abn', extra_cflags=['-O3'], sources=[os.path.join(_s,f) for f in ['inplace_abn.cpp','inplace_abn_cpu.cpp','inplace_abn_cuda.cu','inplace_abn_cuda_half.cu']], extra_cuda_cflags=['--expt-extended-lambda']); print('inplace_abn prebuilt OK')"

