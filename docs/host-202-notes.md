# altacloset — Host Notes: 202 (test host) + target-machine checklist

Verified **2026-08-21** over SSH (`bostock@10.0.1.202`).

## 1. Verified hardware/software on 202

```
GPU            : NVIDIA GeForce RTX 5060 Ti, 16311 MiB (~16GB)
Driver         : 580.119.02
nvidia toolkit : nvidia-container-toolkit 1.12.1-0pop1 (base too)
Docker         : 29.4.1 (build 055a478)  — Compose v5.1.3
Docker runtimes: io.containerd.runc.v2, nvidia (DEFAULT runtime = nvidia)
Disk           : /dev/nvme0n1p3  1.9T total, 361G free (80% used)
RAM            : 62Gi total — ~5.7Gi available at check time (busy box!)
```

### Flags
- **RAM headroom is thin right now** (51Gi used). Heavy test runs (ComfyUI + model load)
  should happen off-peak or after freeing memory. The **target machine** is where the
  clean, always-on stack should live.
- GPU may be shared with other workloads — check `nvidia-smi` before benchmarks.

## 2. Target-machine requirements (the "other computer with the same GPU")

Same GPU family is enough (any 16GB+ NVIDIA card). Minimum checklist:

- [ ] NVIDIA GPU with **≥16GB VRAM** (RTX 5060 Ti 16GB = the verified baseline)
- [ ] NVIDIA driver **≥ 535** (580.x verified on 202; must support CUDA 12.x used by ComfyUI)
- [ ] `nvidia-container-toolkit` **≥ 1.12** + `nvidia-ctk runtime configure --runtime=docker`
- [ ] Docker **≥ 24** + Compose **v2** (29.4.1/v5.1.3 verified)
- [ ] `nvidia` runtime shown in `docker info` (default not required, compose uses `gpus: all`)
- [ ] ~**25GB free disk** (CatVTON ~6GB + ComfyUI image ~8GB + base ~5GB + data)
- [ ] Internet on first boot (model downloads) — or rsync `data/` from 202
      via `scripts/migrate-to-target.sh` (recommended: no re-download)

Quick verify command (run on target):

```bash
nvidia-smi --query-gpu=name,memory.total,driver_version --format=csv,noheader
docker info | grep -i runtime
```

## 3. Smoke test after migration (run on target)

```bash
docker compose up -d --wait
curl -s http://127.0.0.1:28082/health
curl -s http://127.0.0.1:28082/api/weather
curl -s -X POST http://127.0.0.1:28082/api/recommend \
  -H 'Content-Type: application/json' -d '{"activity":"office"}'
# try-on: POST a known test person photo + garment_id, expect an image back
```

## 4. Resource state — updated 2026-08-21

**comfy-ui (the big RAM/GPU consumer) was STOPPED and disabled on 202.**

- What it was: `comfy-ui` container, running 14d+ with `--lowvram`, holding
  ~34GiB RAM + ~11.5GB VRAM (a large diffusion model resident in system RAM).
- Action taken: `docker stop comfy-ui`; `docker update --restart=no comfy-ui`;
  patched `/home/bostock/comfy-ui/docker-compose-202.yml` line 5 → `restart: "no"`.
- Left untouched: `comfy-api` (small 7MB service, policy `unless-stopped`), and
  the other GPU procs (`python3.12` ~1.1GB, `speech-rvc` ~300MB).
- No systemd unit / watchtower / autoheal / timer references it → nothing will
  auto-restart it.

**Current headroom (2026-08-21):**

```
GPU : 2.3GB / 16.3GB used   → ~14GB free for CatVTON + Ollama
RAM : 14Gi used / 41Gi avail (was 51Gi / 5.7Gi before the stop)
Disk: 361G free
```

Phase 2 (CatVTON try-on) can now run on 202 without contention. If `comfy-ui` is
ever needed again: `docker compose -f /home/bostock/comfy-ui/docker-compose-202.yml up -d`.

