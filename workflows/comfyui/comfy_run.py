#!/usr/bin/env python3
"""Run one ready-made ComfyUI workflow headless on a `ddc job` box and leave the video in /work/out.

    python comfy_run.py WORKFLOW

    ordinary generation (text / image to video):   wan22-ti2v-5b | wan22-t2v-a14b | wan22-i2v-a14b
    character swap / animation (driving video + reference image):   wan22-animate | scail2
    your own graph (ComfyUI "Save (API Format)" as inputs/graph.json; see workflows/templates/):   custom

The job specs in workflows/ upload this file to /work/in/, download the models into /work/comfy/models/ and install ComfyUI (install.sh).
Settings come from environment variables (the [env] table of the spec); every one has a default.  The prompt may also be a file: put
`prompt.txt` in the spec's `inputs`.  Stdlib only (OpenCV and PyAV come with ComfyUI's dependencies): it runs with the box's python.
The graphs are the official ComfyUI workflow templates (Comfy-Org/workflow_templates) as API prompts, node for node; the long-video chaining
(Animate windows of 77 frames / 72 new, SCAIL-2 segments of 81 frames / 76 new with colour matching) is the one verified on A100/H100 boxes.
"""
from __future__ import annotations

import atexit
import json
import math
import os
import random
import shlex
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

WORK = Path(os.environ.get("DDC_WORK", "/work"))
COMFY = Path(os.environ.get("COMFY_HOME", "/opt/comfyui"))   # install.sh puts ComfyUI here (baked into the prebuilt image, or installed at start)
BASE = WORK / "comfy"           # models/ (filled by the spec's [cache] items), input/, temp/, user/; custom_nodes stay in COMFY_HOME
INBOX = WORK / "in"             # files the spec uploads (inputs = [...])
OUT = WORK / "out"              # the spec's artifacts: out/*.mp4 and out/*.json
PORT = int(os.environ.get("COMFY_PORT", "8188"))
URL = f"http://127.0.0.1:{PORT}"
TIMEOUT_S = float(os.environ.get("COMFY_TIMEOUT_S", "10800"))   # the whole prompt; the lease's own runtime limit is the hard stop
IMAGES, VIDEOS = (".png", ".jpg", ".jpeg", ".webp"), (".mp4", ".mov", ".mkv", ".webm")
OUTPUT_SUFFIXES = (".mp4", ".webm", ".mkv", ".gif", *IMAGES)   # what a workflow may save; the ready-made ones save video only, a custom graph may save images too
MAX_FRAMES = 300                 # default cap on the length of a character-swap video (about 10 s at 30 fps)

T5 = "umt5_xxl_fp8_e4m3fn_scaled.safetensors"
LIGHTX2V = "lightx2v_I2V_14B_480p_cfg_step_distill_rank64_bf16.safetensors"
YUNET = BASE / "models" / "face" / "face_detection_yunet_2023mar.onnx"
# the negative prompt of the official Wan templates (Chinese: oversaturated, static, blurry details, subtitles, worst quality, extra fingers ...)
NEG = "色调艳丽，过曝，静态，细节模糊不清，字幕，风格，作品，画作，画面，静止，整体发灰，最差质量，低质量，JPEG压缩残留，丑陋的，残缺的，多余的手指，画得不好的手部，画得不好的脸部，畸形的，毁容的，形态畸形的肢体，手指融合，静止不动的画面，杂乱的背景，三条腿，背景人很多，倒着走"
ANIMATE_WINDOW, ANIMATE_NEW = 77, 72     # Wan2.2 Animate: frames per window, new frames per extra window (5 are the continuation anchor)
SCAIL_SEGMENT, SCAIL_NEW = 81, 76        # SCAIL-2: frames per segment, new frames per extra segment (5 anchor frames)


class RunError(Exception):
    pass


def log(msg: str) -> None:
    print(f"[comfy_run {time.strftime('%H:%M:%S')}] {msg}", flush=True)


# ---------------------------------------------------------------- settings
def env_int(name: str, default: int, lo: int, hi: int, multiple: int = 1, offset: int = 0) -> int:
    """An integer setting from the environment; `multiple`/`offset` say the value must be multiple*k+offset (Wan: frames are 4k+1, sizes multiples of 16 or 32)."""
    raw = os.environ.get(name, "").strip()
    try:
        v = int(raw) if raw else default
    except ValueError:
        raise RunError(f"{name}={raw!r} is not a whole number") from None
    if not lo <= v <= hi or (v - offset) % multiple:
        what = f"{multiple}k{offset:+d}" if offset else f"a multiple of {multiple}"
        raise RunError(f"{name}={v} must be {what} between {lo} and {hi}")
    return v


def env_float(name: str, default: float, lo: float, hi: float) -> float:
    raw = os.environ.get(name, "").strip()
    try:
        v = float(raw) if raw else default
    except ValueError:
        raise RunError(f"{name}={raw!r} is not a number") from None
    if not lo <= v <= hi:
        raise RunError(f"{name}={v} must be between {lo:g} and {hi:g}")
    return v


def env_flag(name: str, default: bool) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    if raw not in ("0", "1", "true", "false", "yes", "no", "on", "off"):
        raise RunError(f"{name}={raw!r} must be 1/0, true/false, yes/no or on/off")
    return raw in ("1", "true", "yes", "on")


def prompt_setting(name: str, default: str) -> str:
    """The prompt: the environment variable, else `prompt.txt` among the uploaded inputs (long natural-language prompts are easier as a file), else the default."""
    text = os.environ.get(name, "").strip()
    if not text and (INBOX / "prompt.txt").is_file():
        text = (INBOX / "prompt.txt").read_text(encoding="utf-8").strip()
    return text or default


def pick_input(env_name: str, extensions: tuple[str, ...], required: bool, what: str) -> str | None:
    """The file named by `env_name`, else the only file with one of these extensions in /work/in."""
    named = os.environ.get(env_name, "").strip()
    if named:
        if "/" in named or "\\" in named or not (INBOX / named).is_file():
            raise RunError(f"{env_name}={named!r}: no such file in {INBOX} (list it under `inputs` in the spec; give the file name only)")
        return named
    found = sorted(p.name for p in INBOX.glob("*") if p.suffix.lower() in extensions and p.is_file()) if INBOX.is_dir() else []
    if len(found) > 1:
        raise RunError(f"more than one {what} in {INBOX} ({', '.join(found)}): set {env_name} to the one to use")
    if not found and required:
        raise RunError(f"no {what} in {INBOX}: add one to `inputs` in the spec (or set {env_name})")
    return found[0] if found else None


def seed_setting(name: str) -> int:
    v = env_int(name, -1, -1, 2**63 - 1)
    return random.SystemRandom().randrange(2**48) if v < 0 else v


# ---------------------------------------------------------------- the graph
class Graph:
    """A ComfyUI API prompt: {id: {"class_type", "inputs"}}; a link is [source id, output index]."""

    def __init__(self) -> None:
        self.nodes: dict[str, dict[str, Any]] = {}

    def add(self, class_type: str, **inputs: Any) -> str:
        nid = str(len(self.nodes) + 1)
        self.nodes[nid] = {"class_type": class_type, "inputs": inputs}
        return nid


def link(nid: str, index: int = 0) -> list[Any]:
    return [nid, index]


def save_video(g: Graph, images: list[Any], fps: Any, prefix: str) -> None:
    video = g.add("CreateVideo", images=images, fps=fps)
    g.add("SaveVideo", video=link(video), filename_prefix=prefix, format="mp4", **{"format.codec": "h264"})


def wan_loaders(g: Graph, vae_name: str) -> tuple[str, str]:
    clip = g.add("CLIPLoader", clip_name=T5, type="wan", device="default")
    vae = g.add("VAELoader", vae_name=vae_name)
    return clip, vae


def wan_text(g: Graph, clip: str, prompt: str, negative: str) -> tuple[str, str]:
    return g.add("CLIPTextEncode", text=prompt, clip=link(clip)), g.add("CLIPTextEncode", text=negative, clip=link(clip))


# ---------------------------------------------------------------- ordinary generation: Wan 2.2 text / image to video
def build_wan_5b(stage: list[str]) -> tuple[Graph, dict[str, Any]]:
    """Wan2.2 TI2V 5B (template video_wan2_2_5B_ti2v): one model, text-to-video, or image-to-video when an image is given."""
    p = {"prompt": prompt_setting("WAN_PROMPT", "A red fox runs through fresh snow at sunrise, cinematic, natural light, detailed fur."),
         "negative": os.environ.get("WAN_NEGATIVE", NEG),
         "width": env_int("WAN_WIDTH", 1280, 64, 2048, 32), "height": env_int("WAN_HEIGHT", 704, 64, 2048, 32),
         "frames": env_int("WAN_FRAMES", 121, 5, 241, 4, 1), "fps": env_int("WAN_FPS", 24, 1, 60),
         "steps": env_int("WAN_STEPS", 20, 1, 100), "cfg": env_float("WAN_CFG", 5.0, 0.0, 30.0), "seed": seed_setting("WAN_SEED"),
         "image": pick_input("WAN_IMAGE", IMAGES, False, "start image")}
    g = Graph()
    unet = g.add("UNETLoader", unet_name="wan2.2_ti2v_5B_fp16.safetensors", weight_dtype="default")
    clip, vae = wan_loaders(g, "wan2.2_vae.safetensors")
    model = g.add("ModelSamplingSD3", model=link(unet), shift=8.0)
    pos, neg = wan_text(g, clip, p["prompt"], p["negative"])
    latent_in: dict[str, Any] = {"vae": link(vae), "width": p["width"], "height": p["height"], "length": p["frames"], "batch_size": 1}
    if p["image"]:
        stage.append(p["image"])
        latent_in["start_image"] = link(g.add("LoadImage", image=p["image"]))
    latent = g.add("Wan22ImageToVideoLatent", **latent_in)
    sampled = g.add("KSampler", model=link(model), seed=p["seed"], steps=p["steps"], cfg=p["cfg"], sampler_name="uni_pc", scheduler="simple",
                    positive=link(pos), negative=link(neg), latent_image=link(latent), denoise=1.0)
    save_video(g, link(g.add("VAEDecode", samples=link(sampled), vae=link(vae))), float(p["fps"]), f"wan22-ti2v-5b-{p['seed']}")
    return g, p


def build_wan_a14b(task: str, stage: list[str]) -> tuple[Graph, dict[str, Any]]:
    """Wan2.2 A14B (templates video_wan2_2_14B_t2v / _i2v): a high-noise expert for the first half of the steps, a low-noise expert for the rest.
    WAN_FAST=1 adds the lightx2v 4-step LoRAs (4 steps, cfg 1): about 5x faster, a little less detail."""
    i2v = task == "i2v"
    fast = env_flag("WAN_FAST", False)
    default_prompt = ("The camera slowly pushes in while the character looks around, soft wind moves the clothes and hair, cinematic lighting." if i2v
                      else "A red fox runs through fresh snow at sunrise, cinematic, natural light, detailed fur.")
    steps = env_int("WAN_STEPS", 4 if fast else 20, 2, 100)
    p = {"prompt": prompt_setting("WAN_PROMPT", default_prompt), "negative": os.environ.get("WAN_NEGATIVE", NEG),
         "width": env_int("WAN_WIDTH", 832, 64, 1920, 16), "height": env_int("WAN_HEIGHT", 480, 64, 1920, 16),
         "frames": env_int("WAN_FRAMES", 81, 5, 241, 4, 1), "fps": env_int("WAN_FPS", 16, 1, 60),
         "steps": steps, "cfg": env_float("WAN_CFG", 1.0 if fast else 3.5, 0.0, 30.0), "fast": fast, "seed": seed_setting("WAN_SEED"),
         "image": pick_input("WAN_IMAGE", IMAGES, i2v, "start image")}
    split = max(1, steps // 2)
    g = Graph()
    kind = "i2v" if i2v else "t2v"
    experts = []
    for noise in ("high", "low"):
        m = g.add("UNETLoader", unet_name=f"wan2.2_{kind}_{noise}_noise_14B_fp8_scaled.safetensors", weight_dtype="default")
        if fast:
            lora = f"wan2.2_{kind}_lightx2v_4steps_lora_v1{'' if i2v else '.1'}_{noise}_noise.safetensors"
            m = g.add("LoraLoaderModelOnly", model=link(m), lora_name=lora, strength_model=1.0)
        experts.append(g.add("ModelSamplingSD3", model=link(m), shift=5.0))
    clip, vae = wan_loaders(g, "wan_2.1_vae.safetensors")
    pos, neg = wan_text(g, clip, p["prompt"], p["negative"])
    if i2v:
        stage.append(p["image"])
        cond = g.add("WanImageToVideo", positive=link(pos), negative=link(neg), vae=link(vae), width=p["width"], height=p["height"], length=p["frames"],
                     batch_size=1, start_image=link(g.add("LoadImage", image=p["image"])))
        pos, neg, latent = link(cond, 0), link(cond, 1), link(cond, 2)
    else:
        pos, neg = link(pos), link(neg)
        latent = link(g.add("EmptyHunyuanLatentVideo", width=p["width"], height=p["height"], length=p["frames"], batch_size=1))
    common = {"steps": steps, "cfg": p["cfg"], "sampler_name": "euler", "scheduler": "simple", "positive": pos, "negative": neg}
    first = g.add("KSamplerAdvanced", model=link(experts[0]), add_noise="enable", noise_seed=p["seed"], latent_image=latent, start_at_step=0, end_at_step=split,
                  return_with_leftover_noise="enable", **common)
    second = g.add("KSamplerAdvanced", model=link(experts[1]), add_noise="disable", noise_seed=0, latent_image=link(first), start_at_step=split, end_at_step=steps,
                   return_with_leftover_noise="disable", **common)
    save_video(g, link(g.add("VAEDecode", samples=link(second), vae=link(vae))), float(p["fps"]), f"wan22-{kind}-a14b-{p['seed']}")
    return g, p


# ---------------------------------------------------------------- character swap / animation: shared helpers
def probe_video(path: Path) -> tuple[int, int, int]:
    """(width, height, frames) of a video, with PyAV (a ComfyUI dependency)."""
    import av  # noqa: PLC0415 - only the box has it; the unit tests never reach this

    with av.open(str(path)) as c:
        s = c.streams.video[0]
        frames = s.frames
        if not frames and s.duration and s.average_rate:
            frames = int(float(s.duration * s.time_base) * float(s.average_rate))
        return s.codec_context.width, s.codec_context.height, int(frames or 0)


def driving_clip(driving: str) -> tuple[int, int, int]:
    try:
        return probe_video(INBOX / driving)
    except Exception as e:   # unreadable or exotic container: the caller falls back to SWAP_WIDTH/HEIGHT defaults and the length cap
        log(f"could not read {driving} ({e}); using the default size and the length cap")
        return 0, 0, 0


def swap_frames(prefix: str, clip_frames: int) -> int:
    """Frames to generate: `<prefix>_FRAMES`, else the whole driving clip up to MAX_FRAMES."""
    n = env_int(f"{prefix}_FRAMES", 0, 0, 3000)
    n = n or min(clip_frames or MAX_FRAMES, MAX_FRAMES)
    if clip_frames:
        n = min(n, clip_frames)
    if n < 5:
        raise RunError(f"{prefix}_FRAMES / the driving clip leave only {n} frame(s): at least 5 are needed")
    return n


def windows(n: int, first: int, new: int) -> int:
    return 1 + max(0, math.ceil((n - first) / new))


def nvenc_available() -> bool:
    try:
        import av  # noqa: PLC0415

        return "h264_nvenc" in av.codecs_available
    except Exception:
        return False


def encode_h264(frames: Iterator[Any], path: Path, size: int, fps: int) -> None:
    """Write BGR frames as H.264.  NVENC first (the rented boxes have an NVIDIA GPU; PyAV wheels often ship without it), libx264 on the CPU otherwise."""
    import av  # noqa: PLC0415

    cache = list(frames)

    def write(codec: str, options: dict[str, str], bit_rate: int | None = None) -> None:
        with av.open(str(path), "w") as out:
            stream = out.add_stream(codec, rate=fps)
            stream.width = stream.height = size
            stream.pix_fmt = "yuv420p"
            stream.options = options
            if bit_rate is not None:
                stream.bit_rate = bit_rate
            for frame in cache:
                for packet in stream.encode(av.VideoFrame.from_ndarray(frame, format="bgr24")):
                    out.mux(packet)
            for packet in stream.encode():
                out.mux(packet)

    if nvenc_available():
        try:
            write("h264_nvenc", {"preset": "p7", "tune": "hq", "rc": "vbr", "cq": "15", "maxrate": "20M", "bufsize": "40M", "spatial-aq": "1", "temporal-aq": "1", "bf": "3",
                                 "b_ref_mode": "middle", "rc-lookahead": "32", "multipass": "fullres"}, bit_rate=0)      # -b:v 0: the quality target (cq) decides, maxrate caps
            return
        except Exception as e:
            log(f"NVENC failed ({str(e)[:120]}): encoding the face video with libx264 on the CPU")
    else:
        log("NVENC is not available in this PyAV build: encoding the face video with libx264 on the CPU")
    write("libx264", {"crf": "10", "preset": "fast"})


def face_crop_video(src: Path, dst: Path, scale: float = 1.5, size: int = 512) -> bool:
    """Real 512x512 face crops of the driving clip for WanAnimateToVideo's face_video: the official template feeds rendered DWPose face dots, which carry
    much less expression (the new character kept the reference photo's smile).  YuNet detects per frame; gaps (hands over the face) are interpolated and the
    box is smoothed.  Returns False (use the dots) when no face is found or OpenCV / the detector are missing."""
    try:
        import cv2  # noqa: PLC0415
        import numpy as np  # noqa: PLC0415
    except ImportError:
        log("OpenCV is missing: using the DWPose face dots for face_video")
        return False
    if not YUNET.is_file():
        log(f"{YUNET} is missing: using the DWPose face dots for face_video")
        return False
    cap, frames = cv2.VideoCapture(str(src)), []
    while True:
        ok, f = cap.read()
        if not ok:
            break
        frames.append(f)
    if not frames:
        return False
    h, w = frames[0].shape[:2]
    det = cv2.FaceDetectorYN.create(str(YUNET), "", (w, h), 0.6)
    raw, prev = [], None
    for f in frames:
        _, faces = det.detect(f)
        if faces is None or not len(faces):
            raw.append(None)
            continue
        c = [(x + bw / 2, y + bh / 2, max(bw, bh)) for x, y, bw, bh, *_ in faces]
        best = min(c, key=lambda t: (t[0] - prev[0]) ** 2 + (t[1] - prev[1]) ** 2) if prev else max(c, key=lambda t: t[2])   # keep the subject
        prev = best
        raw.append(best)
    hit = [i for i, r in enumerate(raw) if r]
    if not hit:
        log("no face found in the driving video: using the DWPose face dots for face_video")
        return False
    arr = np.array([raw[i] for i in hit], dtype=float)
    idx = np.arange(len(frames))
    track = np.stack([np.interp(idx, hit, arr[:, k]) for k in range(3)], 1)   # gaps: linear, ends: hold
    k = 7                                                                      # centered moving average against jitter
    pad = np.pad(track, ((k // 2, k // 2), (0, 0)), mode="edge")
    track = np.stack([np.convolve(pad[:, j], np.ones(k) / k, mode="valid") for j in range(3)], 1)
    track[:, 2] = np.median(track[:, 2]) * scale                               # constant crop size: Wan expects the face scale, not zoom changes
    log(f"face crops: face found in {len(hit)}/{len(frames)} frames, crop side {track[0, 2]:.0f}px")

    def crop(f: Any, cx: float, cy: float, side: float) -> Any:
        m = np.float32([[size / side, 0, size / 2 - cx * size / side], [0, size / side, size / 2 - cy * size / side]])
        return cv2.warpAffine(f, m, (size, size), flags=cv2.INTER_AREA if side > size else cv2.INTER_LANCZOS4, borderMode=cv2.BORDER_REPLICATE)

    fps = int(round(cap.get(cv2.CAP_PROP_FPS))) or 30
    encode_h264((crop(f, *t) for f, t in zip(frames, track)), dst, size, fps)
    return True


# ---------------------------------------------------------------- character swap: Wan 2.2 Animate
def build_animate(stage: list[str]) -> tuple[Graph, dict[str, Any]]:
    """Wan2.2 Animate (template video_wan2_2_14B_animate): the person of the driving video becomes the character of the reference image.
    replace ("Mix", default): the character is put into the driving video's own scene, in the place of the person SAM3 finds (background and camera kept);
    move: the character performs the motion in the reference image's own scene.  Long videos: windows of 77 frames, each continuing from the last 5."""
    reference = pick_input("ANIMATE_REFERENCE", IMAGES, True, "reference image (the new character)")
    driving = pick_input("ANIMATE_DRIVING", VIDEOS, True, "driving video (the person to replace)")
    mode = os.environ.get("ANIMATE_MODE", "replace").strip().lower()
    if mode not in ("replace", "move"):
        raise RunError(f"ANIMATE_MODE={mode!r} must be replace or move")
    face_mode = os.environ.get("ANIMATE_FACE", "crops").strip().lower()
    if face_mode not in ("crops", "dots"):
        raise RunError(f"ANIMATE_FACE={face_mode!r} must be crops or dots")
    stage += [reference, driving]
    vw, vh, vframes = driving_clip(driving)
    long_side = env_int("ANIMATE_LONG_SIDE", 832, 128, 1920, 16)
    short_side = max(16, round(long_side * min(vw, vh) / max(vw, vh) / 16) * 16) if vw and vh else long_side * 9 // 16 // 16 * 16
    portrait = vh > vw
    n = swap_frames("ANIMATE", vframes)
    p = {"prompt": prompt_setting("ANIMATE_PROMPT", "The character is dancing in the room."), "negative": os.environ.get("ANIMATE_NEGATIVE", NEG),
         "mode": mode, "face": face_mode, "reference": reference, "driving": driving, "driving_frames": vframes, "frames": n, "windows": windows(n, ANIMATE_WINDOW, ANIMATE_NEW),
         "width": env_int("ANIMATE_WIDTH", short_side if portrait else long_side, 64, 1920, 16), "height": env_int("ANIMATE_HEIGHT", long_side if portrait else short_side, 64, 1920, 16),
         "steps": env_int("ANIMATE_STEPS", 6, 1, 100), "cfg": env_float("ANIMATE_CFG", 1.0, 0.0, 30.0), "seed": seed_setting("ANIMATE_SEED"),
         "mask_text": os.environ.get("ANIMATE_MASK_TEXT", "person"), "mask_grow": env_int("ANIMATE_MASK_GROW", 10, 0, 100),
         "unet": os.environ.get("ANIMATE_UNET", "wan2.2_animate_14B_bf16.safetensors")}
    if face_mode == "crops":
        face_file = f"{Path(driving).stem}-faces.mp4"
        if face_crop_video(INBOX / driving, BASE / "input" / face_file):
            p["face_file"] = face_file
        else:
            p["face"] = "dots"
    g = Graph()
    video = g.add("LoadVideo", file=driving)
    parts = g.add("GetVideoComponents", video=link(video))
    scaled = g.add("ImageScale", image=link(parts), upscale_method="lanczos", width=p["width"], height=p["height"], crop="center")
    res = g.add("PixelPerfectResolution", original_image=link(parts), image_gen_width=p["width"], image_gen_height=p["height"], resize_mode="Just Resize")
    pose_in = {"image": link(scaled), "resolution": link(res), "bbox_detector": "yolox_l.onnx", "pose_estimator": "dw-ll_ucoco_384_bs5.torchscript.pt", "scale_stick_for_xinsr_cn": "disable"}
    body = g.add("DWPreprocessor", detect_hand="enable", detect_body="enable", detect_face="disable", **pose_in)
    if "face_file" in p:
        face_video = link(g.add("GetVideoComponents", video=link(g.add("LoadVideo", file=p["face_file"]))))
    else:
        face_video = link(g.add("DWPreprocessor", detect_hand="disable", detect_body="disable", detect_face="enable", **pose_in))
    ref_image = g.add("LoadImage", image=reference)
    vision_out = g.add("CLIPVisionEncode", clip_vision=link(g.add("CLIPVisionLoader", clip_name="clip_vision_h.safetensors")), image=link(ref_image), crop="none")
    clip, vae = wan_loaders(g, "wan_2.1_vae.safetensors")
    pos, neg = wan_text(g, clip, p["prompt"], p["negative"])
    unet = g.add("UNETLoader", unet_name=p["unet"], weight_dtype="default")
    distill = g.add("LoraLoaderModelOnly", model=link(unet), lora_name=LIGHTX2V, strength_model=1.0)
    relight = g.add("LoraLoaderModelOnly", model=link(distill), lora_name="WanAnimate_relight_lora_fp16.safetensors", strength_model=1.0)
    model = g.add("ModelSamplingSD3", model=link(relight), shift=8.0)
    window_in: dict[str, Any] = {"positive": link(pos), "negative": link(neg), "vae": link(vae), "width": p["width"], "height": p["height"], "length": ANIMATE_WINDOW, "batch_size": 1,
                                 "clip_vision_output": link(vision_out), "reference_image": link(ref_image), "face_video": face_video, "pose_video": link(body),
                                 "continue_motion_max_frames": 5}
    if mode == "replace":   # SAM3 finds the person in every frame; the driving video with that person blacked out is the background
        sam = g.add("CheckpointLoaderSimple", ckpt_name="sam3.1_multiplex_fp16.safetensors")
        track = g.add("SAM3_VideoTrack", images=link(scaled), model=link(sam), conditioning=link(g.add("CLIPTextEncode", text=p["mask_text"], clip=link(sam, 1))),
                      detection_threshold=0.5, max_objects=1, detect_interval=1)
        mask = g.add("BlockifyMask", masks=link(g.add("GrowMask", mask=link(g.add("SAM3_TrackToMask", track_data=link(track), object_indices="")), expand=p["mask_grow"], tapered_corners=True)),
                     block_size=32)
        window_in["background_video"] = link(g.add("DrawMaskOnImage", image=link(scaled), mask=link(mask), color="0, 0, 0"))
        window_in["character_mask"] = link(mask)
    acc: list[Any] = []
    offset: list[Any] = []
    for _ in range(p["windows"]):
        cond = g.add("WanAnimateToVideo", video_frame_offset=offset or 0, **({"continue_motion": acc} if acc else {}), **window_in)
        sampled = g.add("KSampler", model=link(model), seed=p["seed"], steps=p["steps"], cfg=p["cfg"], sampler_name="euler", scheduler="simple",
                        positive=link(cond, 0), negative=link(cond, 1), latent_image=link(cond, 2), denoise=1.0)
        trimmed = g.add("TrimVideoLatent", samples=link(sampled), trim_amount=link(cond, 3))
        new = g.add("ImageFromBatch", image=link(g.add("VAEDecode", samples=link(trimmed), vae=link(vae))), batch_index=link(cond, 4), length=4096)
        acc = link(new) if not acc else link(g.add("ImageBatch", image1=acc, image2=link(new)))
        offset = link(cond, 5)
    final = g.add("ImageFromBatch", image=acc, batch_index=0, length=n)
    save_video(g, link(final), link(parts, 2), f"wan22-animate-{mode}-{p['seed']}")
    return g, p


# ---------------------------------------------------------------- character swap: SCAIL-2
def build_scail2(stage: list[str]) -> tuple[Graph, dict[str, Any]]:
    """SCAIL-2 (template video_wan21_scail2_character_replacement): animate the character of a reference image with the motion of a driving video
    (SCAIL_MODE=animation), or put it in the driving video's place (replacement).  SAM3 finds the people in both, so no masks are needed.
    Long videos: segments of 81 frames, each anchored on the last 5 of the previous output and colour-matched to it."""
    reference = pick_input("SCAIL_REFERENCE", IMAGES, True, "reference image")
    driving = pick_input("SCAIL_DRIVING", VIDEOS, True, "driving video")
    mode = os.environ.get("SCAIL_MODE", "animation").strip().lower()
    if mode not in ("animation", "replacement"):
        raise RunError(f"SCAIL_MODE={mode!r} must be animation or replacement")
    turbo = env_flag("SCAIL_TURBO", True)
    relight = env_float("SCAIL_RELIGHT", 1.0 if mode == "replacement" else 0.0, 0.0, 2.0)   # the relight LoRA blends a replaced character into the scene's lighting
    stage += [reference, driving]
    vw, vh, vframes = driving_clip(driving)
    n = swap_frames("SCAIL", vframes)
    portrait = vh > vw
    p = {"prompt": prompt_setting("SCAIL_PROMPT", "A person is dancing."), "mode": mode, "turbo": turbo, "relight": relight, "reference": reference, "driving": driving,
         "driving_frames": vframes, "frames": n, "segments": windows(n, SCAIL_SEGMENT, SCAIL_NEW), "unet": os.environ.get("SCAIL_UNET", "wan2.1_14B_SCAIL_2_fp16.safetensors"),
         "width": env_int("SCAIL_WIDTH", 512 if portrait else 896, 64, 1920, 32), "height": env_int("SCAIL_HEIGHT", 896 if portrait else 512, 64, 1920, 32),
         "steps": env_int("SCAIL_STEPS", 6 if turbo else 40, 1, 100), "cfg": env_float("SCAIL_CFG", 1.0 if turbo else 5.0, 0.0, 30.0),
         "sam3_driving": os.environ.get("SCAIL_SAM3_DRIVING", "human"), "sam3_reference": os.environ.get("SCAIL_SAM3_REFERENCE", "human"), "seed": seed_setting("SCAIL_SEED")}
    replacement = mode == "replacement"
    g = Graph()
    parts = g.add("GetVideoComponents", video=link(g.add("LoadVideo", file=driving)))
    ref_image = g.add("LoadImage", image=reference)
    unet = g.add("UNETLoader", unet_name=p["unet"], weight_dtype="default")
    model = g.add("LoraLoaderModelOnly", model=link(unet), lora_name="wan2.1_SCAIL_2_DPO_lora_bf16.safetensors", strength_model=1.0)
    if relight:
        model = g.add("LoraLoaderModelOnly", model=link(model), lora_name="wan2.1_SCAIL_2_relight_lora_bf16.safetensors", strength_model=relight)
    distill = g.add("LoraLoaderModelOnly", model=link(model), lora_name=LIGHTX2V, strength_model=0.8)
    patched = g.add("ModelSamplingSD3", model=link(distill if turbo else model), shift=5.0)
    clip = g.add("CLIPLoader", clip_name=T5, type="wan", device="default")
    vae = g.add("VAELoader", vae_name="Wan2_1_VAE_bf16.safetensors")
    vision_out = g.add("CLIPVisionEncode", clip_vision=link(g.add("CLIPVisionLoader", clip_name="clip_vision_h.safetensors")), image=link(ref_image), crop="none")
    sam = g.add("CheckpointLoaderSimple", ckpt_name="sam3.1_multiplex_fp16.safetensors")
    track_in = {"model": link(sam), "detection_threshold": 0.5, "max_objects": 4, "detect_interval": 1}
    reference_track = g.add("SAM3_VideoTrack", images=link(ref_image), conditioning=link(g.add("CLIPTextEncode", text=p["sam3_reference"], clip=link(sam, 1))), **track_in)
    driving_text = g.add("CLIPTextEncode", text=p["sam3_driving"], clip=link(sam, 1))
    positive = g.add("CLIPTextEncode", text=p["prompt"], clip=link(clip))
    negative = g.add("CLIPTextEncode", text="", clip=link(clip))
    sigmas = g.add("BasicScheduler", model=link(distill), scheduler="simple", steps=p["steps"], denoise=1.0)
    sampler = g.add("KSamplerSelect", sampler_name="euler")
    acc: list[Any] = []
    for k in range(p["segments"]):
        window = g.add("ImageFromBatch", image=link(parts), batch_index=SCAIL_NEW * k, length=SCAIL_SEGMENT)
        pose = g.add("ResizeImageMaskNode", input=link(window), resize_type="scale dimensions", scale_method="area",
                     **{"resize_type.width": p["width"], "resize_type.height": p["height"], "resize_type.crop": "center"})
        driving_track = g.add("SAM3_VideoTrack", images=link(pose), conditioning=link(driving_text), **track_in)
        masks = g.add("SCAIL2ColoredMask", driving_track_data=link(driving_track), ref_track_data=link(reference_track), object_indices="", sort_by="left_to_right",
                      replacement_mode=replacement)
        cond = g.add("WanSCAILToVideo", positive=link(positive), negative=link(negative), vae=link(vae), width=p["width"], height=p["height"], length=SCAIL_SEGMENT, batch_size=1,
                     pose_video=link(pose), pose_video_mask=link(masks, 0), replacement_mode=replacement, pose_strength=1.0, pose_start=0.0, pose_end=1.0,
                     reference_image=link(ref_image), reference_image_mask=link(masks, 1), clip_vision_output=link(vision_out), video_frame_offset=0, previous_frame_count=5,
                     **({"previous_frames": acc} if acc else {}))
        sampled = g.add("SamplerCustom", model=link(patched), add_noise=True, noise_seed=p["seed"], cfg=p["cfg"], positive=link(cond, 0), negative=link(cond, 1),
                        sampler=link(sampler), sigmas=link(sigmas), latent_image=link(cond, 2))
        decoded = g.add("VAEDecode", samples=link(sampled, 1), vae=link(vae))
        if not acc:
            acc = link(decoded)
        else:   # drop the 5 anchor frames, match the colours of the new frames to the last frame so far, append
            new = g.add("ImageFromBatch", image=link(decoded), batch_index=5, length=4096)
            last = g.add("ImageFromBatch", image=acc, batch_index=-1, length=1)
            matched = g.add("ColorTransfer", image_target=link(new), image_ref=link(last), method="reinhard_lab", source_stats="per_frame", strength=1.0)
            acc = link(g.add("ImageBatch", image1=acc, image2=link(matched)))
    final = g.add("ImageFromBatch", image=acc, batch_index=0, length=n)
    save_video(g, link(final), link(parts, 2), f"scail2-{mode}-{p['seed']}")
    return g, p


def build_custom(stage: list[str]) -> tuple[Graph, dict[str, Any]]:
    """Bring your own graph: `graph.json` from the spec's inputs, exported with ComfyUI's "Save (API Format)".  Images and videos uploaded beside it are copied
    to ComfyUI's input directory, so a LoadImage / LoadVideo node can name them.  Models come from the spec's [cache] items; extra custom nodes are not installed."""
    path = INBOX / "graph.json"
    if not path.is_file():
        raise RunError("no graph.json in /work/in: list your exported graph under `inputs` in the spec")
    try:
        nodes = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as e:
        raise RunError(f"graph.json is not valid JSON: {e}") from None
    if isinstance(nodes, dict) and isinstance(nodes.get("nodes"), list):
        raise RunError('graph.json is a workflow saved from the ComfyUI editor; export it with "Save (API Format)" (enable dev mode options in the settings) instead')
    if not isinstance(nodes, dict) or not nodes or not all(isinstance(n, dict) and "class_type" in n and isinstance(n.get("inputs"), dict) for n in nodes.values()):
        raise RunError('graph.json is not an API-format graph ({"1": {"class_type": ..., "inputs": {...}}, ...})')
    g = Graph()
    g.nodes = nodes
    stage += sorted(f.name for f in INBOX.iterdir() if f.suffix.lower() in IMAGES + VIDEOS)
    return g, {"graph": "graph.json", "media": list(stage)}


WORKFLOWS = {
    "wan22-ti2v-5b": lambda stage: build_wan_5b(stage),
    "wan22-t2v-a14b": lambda stage: build_wan_a14b("t2v", stage),
    "wan22-i2v-a14b": lambda stage: build_wan_a14b("i2v", stage),
    "wan22-animate": lambda stage: build_animate(stage),
    "scail2": lambda stage: build_scail2(stage),
    "custom": lambda stage: build_custom(stage),
}
CUSTOM_NODES = {"wan22-animate": ["comfyui_controlnet_aux", "ComfyUI-KJNodes"]}   # every other workflow runs on ComfyUI's own nodes


# ---------------------------------------------------------------- the box
def gpu_info() -> dict[str, Any]:
    """The GPU torch sees, measured by a short-lived subprocess (so the server starts with an untouched GPU)."""
    code = ("import json,torch;a=torch.cuda.is_available();p=torch.cuda.get_device_properties(0) if a else None;"
            "print(json.dumps({'cuda':a,'name':p.name if a else '','gib':round(p.total_memory/2**30,1) if a else 0,'cap':[p.major,p.minor] if a else [0,0],"
            "'arches':torch.cuda.get_arch_list() if a else [],'torch':torch.__version__,'count':torch.cuda.device_count()}))")
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=300)
    if r.returncode != 0:
        raise RunError(f"torch could not start: {r.stderr.strip()[-400:]}")
    info = json.loads(r.stdout.strip().splitlines()[-1])
    if not info["cuda"]:
        raise RunError("torch sees no CUDA GPU on this box (driver or container problem): nothing to run on")
    if not arch_usable(info["cap"], info["arches"]):
        raise RunError(f"this torch build ({info['torch']}: {' '.join(info['arches'])}) has no kernels for the GPU's compute capability {info['cap'][0]}.{info['cap'][1]}: use a newer torch image")
    return info


def arch_usable(cap: list[int], arches: list[str]) -> bool:
    """A cubin (sm_XY) runs on the same major architecture with minor >= Y (an sm_86 build runs on an RTX 4090, 8.9); PTX (compute_XY) runs on anything newer."""
    for a in arches:
        kind, _, digits = a.partition("_")
        digits = "".join(c for c in digits if c.isdigit())
        if len(digits) < 2:
            continue
        major, minor = int(digits[:-1]), int(digits[-1])
        if kind == "sm" and major == cap[0] and minor <= cap[1]:
            return True
        if kind == "compute" and (major, minor) <= tuple(cap):
            return True
    return False


def server_args(info: dict[str, Any], workflow: str) -> list[str]:
    """GPU configuration: ComfyUI sizes its offloading from the VRAM it finds; with 60 GiB or more everything stays resident (--highvram), which is faster.
    Custom nodes are loaded only for the workflows that use them."""
    args = ["--listen", "127.0.0.1", "--port", str(PORT), "--disable-auto-launch", "--offline", "--models-directory", str(BASE / "models"), "--input-directory", str(BASE / "input"),
            "--temp-directory", str(BASE / "temp"), "--user-directory", str(BASE / "user"), "--output-directory", str(OUT)]
    if info["gib"] >= 60:
        args.append("--highvram")
    wanted = shlex.split(os.environ.get("COMFY_NODES", "")) if workflow == "custom" else CUSTOM_NODES.get(workflow, [])   # custom: node folders already in COMFY_HOME/custom_nodes
    args += ["--disable-all-custom-nodes"] + (["--whitelist-custom-nodes", *wanted] if wanted else [])
    return args + shlex.split(os.environ.get("COMFY_EXTRA_ARGS", ""))


def request(path: str, payload: dict[str, Any] | None = None) -> Any:
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(URL + path, data=data, headers={"Content-Type": "application/json"} if data else {})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode() or "{}")


def start_server(info: dict[str, Any], workflow: str) -> subprocess.Popen[bytes]:
    cmd = [sys.executable, "main.py", *server_args(info, workflow)]
    log("starting ComfyUI: " + " ".join(cmd[1:]))
    env = dict(os.environ, PYTORCH_CUDA_ALLOC_CONF=os.environ.get("PYTORCH_CUDA_ALLOC_CONF", "expandable_segments:True"), PYTHONUNBUFFERED="1")
    proc = subprocess.Popen(cmd, cwd=COMFY, env=env)
    atexit.register(stop_server, proc)
    end = time.monotonic() + 600
    while True:
        if proc.poll() is not None:
            raise RunError(f"ComfyUI exited with code {proc.returncode} while starting (see its output above)")
        try:
            request("/system_stats")
            return proc
        except (urllib.error.URLError, ConnectionError, TimeoutError, OSError):
            if time.monotonic() > end:
                raise RunError("ComfyUI did not answer within 10 minutes") from None
            time.sleep(2)


def stop_server(proc: subprocess.Popen[bytes]) -> None:
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            proc.kill()


def missing_nodes(graph: Graph) -> list[str]:
    """Classes the graph uses that this server does not have (a custom node that failed to load), found before anything is queued."""
    have = request("/object_info")
    return sorted({n["class_type"] for n in graph.nodes.values()} - set(have))


def run_prompt(graph: Graph, proc: subprocess.Popen[bytes]) -> dict[str, Any]:
    gone = missing_nodes(graph)
    if gone:
        raise RunError(f"this ComfyUI has no node class {', '.join(gone)} (a custom node failed to load: see the server output above)")
    try:
        reply = request("/prompt", {"prompt": graph.nodes, "client_id": str(uuid.uuid4())})
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:4000]
        raise RunError(f"ComfyUI rejected the workflow (HTTP {e.code}): {detail}") from None
    pid = reply["prompt_id"]
    log(f"queued prompt {pid}")
    start = last = time.monotonic()
    while True:
        if proc.poll() is not None:
            raise RunError(f"ComfyUI died with code {proc.returncode} while running (out of memory kills it without a message: see the log above)")
        hist = request(f"/history/{pid}").get(pid)
        if hist and hist.get("status", {}).get("completed") is not None:
            status = hist["status"]
            if status.get("status_str") != "success":
                errors = [m[1] for m in status.get("messages", []) if m and m[0] == "execution_error"]
                raise RunError("the workflow failed: " + json.dumps(errors or status.get("messages", []))[:4000])
            return hist
        now = time.monotonic()
        if now - start > TIMEOUT_S:
            raise RunError(f"the workflow did not finish within {TIMEOUT_S:g} s (COMFY_TIMEOUT_S)")
        if now - last >= 60:
            last = now
            log(f"running, {int(now - start)} s")
        time.sleep(2)


def collect_outputs(hist: dict[str, Any]) -> list[Path]:
    files = []
    for node in hist.get("outputs", {}).values():
        for item in node.get("images", []) + node.get("videos", []):
            if item.get("type") == "output":
                path = (OUT / item.get("subfolder", "") / item["filename"]).resolve()
                if OUT.resolve() in path.parents and path.suffix.lower() in OUTPUT_SUFFIXES and path.is_file():
                    files.append(path)
    return files


def main(argv: list[str]) -> int:
    if len(argv) != 2 or argv[1] not in WORKFLOWS:
        print(__doc__)
        return 2
    name = argv[1]
    for d in (OUT, BASE / "models", BASE / "input", BASE / "temp", BASE / "user"):
        d.mkdir(parents=True, exist_ok=True)
    stage: list[str] = []
    graph, params = WORKFLOWS[name](stage)
    for f in stage:                      # LoadImage / LoadVideo read from ComfyUI's input directory
        (BASE / "input" / f).write_bytes((INBOX / f).read_bytes())
    log(f"{name}: " + json.dumps({k: v for k, v in params.items() if k not in ("prompt", "negative")}))
    info = gpu_info()
    log(f"GPU: {info['name']}, {info['gib']} GiB, compute capability {info['cap'][0]}.{info['cap'][1]}, torch {info['torch']}" + (f", {info['count']} GPUs (the first is used)" if info["count"] > 1 else ""))
    proc = start_server(info, name)
    try:
        hist = run_prompt(graph, proc)
    finally:
        stop_server(proc)
    videos = collect_outputs(hist)
    if not videos:
        raise RunError("the workflow finished but saved no video, image or gif to ComfyUI's output directory")
    (OUT / f"{videos[0].stem}.json").write_text(json.dumps({"workflow": name, "params": params, "gpu": info, "prompt": graph.nodes}, indent=1, ensure_ascii=False), encoding="utf-8")
    for v in videos:
        log(f"saved {v} ({v.stat().st_size / 2**20:.1f} MiB)")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv))
    except RunError as e:
        log(f"ERROR: {e}")
        sys.exit(1)
