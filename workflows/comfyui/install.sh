#!/usr/bin/env bash
# Install ComfyUI at a pinned commit, plus (with --with-nodes) the two custom nodes and the DWPose weights the Wan2.2 Animate workflow needs.
# Idempotent. Two uses: baked into the prebuilt image at build time (Dockerfile) and run at the start of a job on the stock image; on the
# prebuilt image it finds everything in place and returns in seconds.   Usage: bash install.sh [--with-nodes]
set -euo pipefail

COMFY_HOME=${COMFY_HOME:-/opt/comfyui}
COMFY_TAG=v0.42.1
COMFY_SHA=63be2f9b82ad0dac88a7ceaa28a9cf3154125a18
NODES=(                                              # name  repository  commit
  "comfyui_controlnet_aux Fannovel16/comfyui_controlnet_aux a587a4c1594524c1d2de5cd1a86cd868ff448e1c"
  "ComfyUI-KJNodes kijai/ComfyUI-KJNodes 17195fcfa8a05f601b766a0f09c9733284f47416"
)
AUX=$COMFY_HOME/custom_nodes/comfyui_controlnet_aux/ckpts       # where the DWPose preprocessor looks for its weights (nothing is downloaded at run time)
WEIGHTS=(                                            # path under $AUX   sha256   url (immutable commit)
  "yzd-v/DWPose/yolox_l.onnx 7860ae79de6c89a3c1eb72ae9a2756c0ccfbe04b7791bb5880afabd97855a411 https://huggingface.co/yzd-v/DWPose/resolve/1a7144101628d69ee7a3768d1ee3a094070dc388/yolox_l.onnx"
  "hr16/DWPose-TorchScript-BatchSize5/dw-ll_ucoco_384_bs5.torchscript.pt d86a0b2b59fddc0901a7076e9f59c9f8602602133ed72511c693fd11eea23d91 https://huggingface.co/hr16/DWPose-TorchScript-BatchSize5/resolve/359d662a9b33b73f6d0f21732baf8845f17bb4be/dw-ll_ucoco_384_bs5.torchscript.pt"
)
with_nodes=0
[ "${1:-}" = "--with-nodes" ] && with_nodes=1
say() { echo "[install $(date +%H:%M:%S)] $*"; }

# on a job box (DDC_JOB is set), fail fast when there is no usable GPU; while building the image there is none
if [ -n "${DDC_JOB:-}" ]; then
  nvidia-smi --query-gpu=name,memory.total,driver_version --format=csv,noheader || { echo "no usable NVIDIA GPU on this box" >&2; exit 1; }
fi

mkdir -p "$(dirname "$COMFY_HOME")"
if [ ! -d "$COMFY_HOME/.git" ]; then
  say "cloning ComfyUI $COMFY_TAG"
  git clone -q --depth 1 --branch "$COMFY_TAG" https://github.com/Comfy-Org/ComfyUI.git "$COMFY_HOME"
fi
[ "$(git -C "$COMFY_HOME" rev-parse HEAD)" = "$COMFY_SHA" ] || { echo "ComfyUI at $COMFY_HOME is not commit $COMFY_SHA ($COMFY_TAG): refusing to run a different version" >&2; exit 1; }

# the torch the image brought (CUDA build) must survive every pip install below
pip freeze 2>/dev/null | grep -E '^(torch|torchvision|torchaudio|triton)==' > "$COMFY_HOME/.constraints.txt" || true
if [ ! -e "$COMFY_HOME/.installed-core" ]; then
  say "ComfyUI requirements"
  pip install -q --no-cache-dir -c "$COMFY_HOME/.constraints.txt" -r "$COMFY_HOME/requirements.txt"
  touch "$COMFY_HOME/.installed-core"
fi

if [ "$with_nodes" = 1 ]; then
  for entry in "${NODES[@]}"; do
    read -r name repo sha <<<"$entry"
    d=$COMFY_HOME/custom_nodes/$name
    if [ -e "$d/.installed" ]; then continue; fi
    say "custom node $name"
    mkdir -p "$d" && git -C "$d" init -q && git -C "$d" fetch -q --depth 1 "https://github.com/$repo" "$sha" && git -C "$d" checkout -q FETCH_HEAD
    [ "$(git -C "$d" rev-parse HEAD)" = "$sha" ] || { echo "$name is not commit $sha" >&2; exit 1; }
    # onnxruntime-gpu is what DWPose needs on a GPU box; the other requirements are taken as listed
    [ -f "$d/requirements.txt" ] && pip install -q --no-cache-dir -c "$COMFY_HOME/.constraints.txt" -r "$d/requirements.txt"
    touch "$d/.installed"
  done
  for entry in "${WEIGHTS[@]}"; do
    read -r rel sha url <<<"$entry"
    f=$AUX/$rel
    if [ -s "$f" ] && [ "$(sha256sum "$f" | cut -d' ' -f1)" = "$sha" ]; then continue; fi
    say "DWPose weights $(basename "$f")"
    mkdir -p "$(dirname "$f")"
    curl -fsSL --retry 3 --connect-timeout 15 -o "$f" "$url"
    [ "$(sha256sum "$f" | cut -d' ' -f1)" = "$sha" ] || { echo "sha256 mismatch for $rel" >&2; rm -f "$f"; exit 1; }
  done
fi
say "ready: ComfyUI $COMFY_TAG in $COMFY_HOME$([ "$with_nodes" = 1 ] && echo ' with the Animate custom nodes')"
