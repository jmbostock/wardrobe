# ComfyUI + CatVTON, baked for zero manual steps on the target machine.
# The CatVTON custom node is cloned into the image; weights are downloaded by
# scripts/bootstrap-comfyui.sh into ./data/comfyui/models (bind-mounted).
#
# NOTE (Phase 2): verify the exact CatVTON install command against the repo README
# (https://github.com/Zheng-Chong/CatVTON). The clone target below matches the
# official ComfyUI image layout (/root/comfyui/custom_nodes).
FROM comfyanonymous/comfyui:latest

RUN git clone --depth 1 https://github.com/Zheng-Chong/CatVTON.git \
        /root/comfyui/custom_nodes/CatVTON \
 && cd /root/comfyui/custom_nodes/CatVTON \
 && (pip install --no-cache-dir -r requirements.txt || true)
