"""The ready-made workflows (workflows/): the specs are valid job specs, the runner and the specs agree on every model file, the graphs have the shape the
verified pipelines have, and the pinned versions line up.  Offline: nothing here needs ComfyUI, a GPU or a network (the graphs were also validated once against a real
ComfyUI v0.42.1 server; that check needs the server and is not part of the suite)."""
import importlib.util
import os
import re
import shutil
import subprocess
import tomllib
from pathlib import Path

import pytest

from clouddeck.engine import ddc
from clouddeck.job import preflight
from clouddeck.job import spec as specmod

ROOT = Path(__file__).resolve().parent.parent
WF = ROOT / "workflows"
PUB = "ssh-ed25519 AAAA k"
SPECS = sorted(p.stem for p in WF.glob("*.toml") if not p.stem.startswith("my-"))   # my-*.toml: specs people make from templates/
CHARACTER_SWAP = ("wan22-animate", "scail2", "scail2-fp8")

_loaded = importlib.util.spec_from_file_location("comfy_run", WF / "comfyui" / "comfy_run.py")
cr = importlib.util.module_from_spec(_loaded)
_loaded.loader.exec_module(cr)

# the loader inputs that name a model file, by node class
MODEL_INPUTS = {"UNETLoader": "unet_name", "CLIPLoader": "clip_name", "VAELoader": "vae_name", "CLIPVisionLoader": "clip_name",
                "LoraLoaderModelOnly": "lora_name", "CheckpointLoaderSimple": "ckpt_name"}
RUNNER_FOR = {"wan22-ti2v-5b": "wan22-ti2v-5b", "wan22-t2v-a14b": "wan22-t2v-a14b", "wan22-i2v-a14b": "wan22-i2v-a14b", "wan22-animate": "wan22-animate",
              "scail2": "scail2", "scail2-fp8": "scail2"}


def raw(name):
    return tomllib.loads((WF / f"{name}.toml").read_text(encoding="utf-8"))


def spec_of(name):
    return specmod.normalize(raw(name), WF)


@pytest.fixture
def box(monkeypatch, tmp_path):
    """A fake /work whose inbox holds the uploaded scripts only; every runner setting at its default.  `put(box, ...)` adds media."""
    for k in list(os.environ):
        if k.startswith(("WAN_", "ANIMATE_", "SCAIL_")):
            monkeypatch.delenv(k)
    (tmp_path / "in").mkdir()
    for f in ("install.sh", "comfy_run.py"):
        (tmp_path / "in" / f).write_bytes(b"x")
    monkeypatch.setattr(cr, "WORK", tmp_path)
    monkeypatch.setattr(cr, "INBOX", tmp_path / "in")
    monkeypatch.setattr(cr, "probe_video", lambda p: (896, 512, 300))      # a 300-frame landscape clip
    monkeypatch.setenv("ANIMATE_FACE", "dots")                              # no OpenCV or YuNet in the test
    return tmp_path


def put(box, *names):
    for n in names:
        (box / "in" / n).write_bytes(b"x")
    return box


def graph(name, box, **env):
    for k, v in env.items():
        os.environ[k] = str(v)
    try:
        g, params = cr.WORKFLOWS[RUNNER_FOR[name] if name in RUNNER_FOR else name]([])
    finally:
        for k in env:
            os.environ.pop(k, None)
    return g, params


def model_files(g):
    return {n["inputs"][MODEL_INPUTS[n["class_type"]]] for n in g.nodes.values() if n["class_type"] in MODEL_INPUTS}


# ------------------------------------------------------------------ the specs
@pytest.mark.parametrize("name", SPECS)
def test_spec_is_valid_and_inputs_are_top_level(name):
    r = raw(name)
    s = specmod.normalize(r, WF)
    assert s["inputs"][:2] == ["comfyui/install.sh", "comfyui/comfy_run.py"], "the scripts are uploaded as inputs"
    # `inputs` after a [table] header lands INSIDE that table and is silently ignored: it must come before the first header
    assert [t for t, v in r.items() if isinstance(v, dict) and ({"inputs", "inputs_dest"} & set(v))] == []
    assert s["inputs_dest"] == "in"
    assert s["hardware"]["gpu"] and s["hardware"]["gpu"]["vendor"] == "nvidia" and len(s["hardware"]["gpu"]["models"]) <= 8
    install, generate = s["steps"]
    assert install["name"] == "install" and install["needs"] == ["inputs"] and install["productive"] is False
    assert generate["name"] == "generate" and generate["productive"] is True
    assert set(generate["needs"]) == {"install", "inputs", *(f"cache-{i['name']}" for i in s["cache"]["items"])}
    assert generate["artifacts"] == ["out/*.mp4", "out/*.json"]
    assert f"in/comfy_run.py {RUNNER_FOR[name]}" in generate["run"]
    assert ("--with-nodes" in install["run"]) == (name == "wan22-animate")


def test_examples_keep_their_inputs_at_top_level_too():
    for p in sorted((ROOT / "examples").glob("*.toml")):
        r = tomllib.loads(p.read_text(encoding="utf-8"))
        assert [t for t, v in r.items() if isinstance(v, dict) and ({"inputs", "inputs_dest"} & set(v))] == [], p.name


@pytest.mark.parametrize("name", SPECS)
def test_spec_passes_offline_preflight_and_makes_a_valid_sdl(jw, monkeypatch, name):
    s = specmod.normalize(raw(name), jw.tmp)
    for p in s["inputs"]:
        (jw.tmp / p).parent.mkdir(parents=True, exist_ok=True)
        (jw.tmp / p).write_bytes(b"x")
    ddc.CFG["max_price_usd_hr"] = 5.0
    cs = preflight.run(s, offline=True)
    assert not preflight.failed(cs), [(c.name, c.detail) for c in cs if c.status == "fail"]
    for _kind, image in specmod.image_kinds(s):
        assert specmod.validate_sdl(specmod.build_sdl(s, image, PUB)) == []
        assert re.search(r"@sha256:[0-9a-f]{64}$", image), "the stock image is pinned by digest"


@pytest.mark.parametrize("name", SPECS)
def test_downloads_are_pinned_and_verified(name):
    s = spec_of(name)
    seen = set()
    for it in s["cache"]["items"]:
        assert re.fullmatch(r"[0-9a-f]{64}", it["sha256"]) and set(it["sha256"]) != {"0"} and not it["allow_unverified"]
        assert len(it["urls"]) == 1 and re.fullmatch(r"https://(huggingface\.co/[\w.-]+/[\w.-]+/resolve|github\.com/[\w.-]+/[\w.-]+/raw)/[0-9a-f]{40}/\S+", it["urls"][0]), it["urls"]
        assert it["dest"].startswith("comfy/models/") and it["dest"].rsplit("/", 1)[1] == it["path"]
        assert it["dest"] not in seen
        seen.add(it["dest"])
        assert it["size_gb"] > 0


def test_spec_images_dockerfile_and_install_script_agree():
    digests = {re.search(r"@(sha256:[0-9a-f]{64})", i).group(1) for n in SPECS for _, i in specmod.image_kinds(spec_of(n))}
    assert len(digests) == 1
    dockerfile = (WF / "comfyui" / "Dockerfile").read_text(encoding="utf-8")
    assert digests == set(re.findall(r"@(sha256:[0-9a-f]{64})", dockerfile))
    base_apt = set(specmod.BASE_APT)
    installed = set(re.search(r"install -y -qq --no-install-recommends ([^\\]+?)\s*\\", dockerfile).group(1).split())
    assert base_apt <= installed and {a for n in SPECS for a in spec_of(n)["image"]["apt"]} <= installed
    script = (WF / "comfyui" / "install.sh").read_text(encoding="utf-8")
    assert "\r" not in script and re.search(r"^COMFY_SHA=[0-9a-f]{40}$", script, re.M) and re.search(r"^COMFY_TAG=v\d+\.\d+\.\d+$", script, re.M)
    for sha in re.findall(r"\b([0-9a-f]{40})\b", script):
        assert not re.fullmatch(r"(.)\1+", sha)
    if shutil.which("bash"):
        assert subprocess.run(["bash", "-n", "install.sh"], cwd=WF / "comfyui", capture_output=True).returncode == 0


def test_dwpose_weights_in_the_installer_are_verified_downloads():
    script = (WF / "comfyui" / "install.sh").read_text(encoding="utf-8")
    entries = re.findall(r'^  "(\S+) ([0-9a-f]{64}) (https://huggingface\.co/\S+/resolve/[0-9a-f]{40}/\S+)"$', script, re.M)
    assert {e[0] for e in entries} == {"yzd-v/DWPose/yolox_l.onnx", "hr16/DWPose-TorchScript-BatchSize5/dw-ll_ucoco_384_bs5.torchscript.pt"}
    for rel, _sha, url in entries:
        assert url.endswith("/" + rel.split("/")[-1])


def test_readme_covers_every_spec_and_keeps_the_families_apart():
    text = (WF / "README.md").read_text(encoding="utf-8")
    for n in SPECS:
        assert f"{n}.toml" in text
    assert "Ordinary generation" in text and "Character swap" in text.replace("**", "")


# ------------------------------------------------------------------ runner <-> spec
@pytest.mark.parametrize("name,env", [
    ("wan22-ti2v-5b", {}), ("wan22-ti2v-5b", {"WAN_IMAGE": "reference.png"}),
    ("wan22-t2v-a14b", {}), ("wan22-t2v-a14b", {"WAN_FAST": 1}),
    ("wan22-i2v-a14b", {}), ("wan22-i2v-a14b", {"WAN_FAST": 1}),
    ("wan22-animate", {}), ("wan22-animate", {"ANIMATE_MODE": "move"}),
    ("scail2", {}), ("scail2", {"SCAIL_MODE": "replacement"}), ("scail2", {"SCAIL_TURBO": 0}),
    ("scail2-fp8", {"SCAIL_UNET": "wan2.1_14B_SCAIL_2_fp8_scaled.safetensors"}),
])
def test_every_model_a_graph_loads_is_a_download_of_its_spec(box, name, env):
    put(box, "driving.mp4", *(["reference.png"] if (env or "i2v" in name or "animate" in name or "scail" in name) else []))
    spec = spec_of(name)
    env = {**env, **({k: v for k, v in spec["env"].items() if k.startswith("SCAIL_")} if name.startswith("scail2") else {})}
    g, _ = graph(name, box, **env)
    have = {i["path"] for i in spec["cache"]["items"]}
    assert model_files(g) <= have, sorted(model_files(g) - have)


def test_the_face_detector_is_where_the_runner_looks():
    dest = next(i["dest"] for i in spec_of("wan22-animate")["cache"]["items"] if i["name"] == "yunet")
    assert cr.YUNET == cr.BASE / dest.removeprefix("comfy/")


def test_scail2_fp16_is_the_default_and_the_fp8_spec_overrides_it(box):
    assert spec_of("scail2")["env"]["SCAIL_UNET"] == "wan2.1_14B_SCAIL_2_fp16.safetensors"
    assert spec_of("scail2-fp8")["env"]["SCAIL_UNET"] == "wan2.1_14B_SCAIL_2_fp8_scaled.safetensors"
    put(box, "reference.png", "driving.mp4")
    assert "wan2.1_14B_SCAIL_2_fp16.safetensors" in model_files(graph("scail2", box)[0])


@pytest.mark.parametrize("name", ["wan22-ti2v-5b", "wan22-t2v-a14b", "wan22-i2v-a14b", "wan22-animate", "scail2"])
def test_graph_links_point_at_real_nodes_and_every_class_is_known(box, name):
    known = {"UNETLoader", "CLIPLoader", "VAELoader", "CLIPVisionLoader", "CLIPVisionEncode", "CLIPTextEncode", "ModelSamplingSD3", "LoraLoaderModelOnly", "LoadImage",
             "LoadVideo", "GetVideoComponents", "ImageFromBatch", "ImageBatch", "ImageScale", "ResizeImageMaskNode", "ColorTransfer", "VAEDecode", "CreateVideo", "SaveVideo",
             "KSampler", "KSamplerAdvanced", "KSamplerSelect", "SamplerCustom", "BasicScheduler", "EmptyHunyuanLatentVideo", "WanImageToVideo", "Wan22ImageToVideoLatent",
             "WanAnimateToVideo", "TrimVideoLatent", "WanSCAILToVideo", "SCAIL2ColoredMask", "SAM3_VideoTrack", "SAM3_TrackToMask", "CheckpointLoaderSimple", "GrowMask",
             "BlockifyMask", "DrawMaskOnImage", "DWPreprocessor", "PixelPerfectResolution"}
    put(box, "reference.png", "driving.mp4")
    g, _ = graph(name, box)
    assert {n["class_type"] for n in g.nodes.values()} <= known
    for nid, n in g.nodes.items():
        for k, v in n["inputs"].items():
            if isinstance(v, list) and len(v) == 2 and isinstance(v[0], str) and isinstance(v[1], int):
                assert v[0] in g.nodes and v[0] != nid, (nid, k, v)
    assert sum(n["class_type"] == "SaveVideo" for n in g.nodes.values()) == 1
    assert [n["inputs"]["format"] for n in g.nodes.values() if n["class_type"] == "SaveVideo"] == ["mp4"]


# ------------------------------------------------------------------ the chaining of long videos (verified numbers)
def test_window_and_segment_counts():
    assert cr.windows(77, cr.ANIMATE_WINDOW, cr.ANIMATE_NEW) == 1
    assert cr.windows(78, cr.ANIMATE_WINDOW, cr.ANIMATE_NEW) == 2 and cr.windows(149, cr.ANIMATE_WINDOW, cr.ANIMATE_NEW) == 2 and cr.windows(150, cr.ANIMATE_WINDOW, cr.ANIMATE_NEW) == 3
    assert cr.windows(900, cr.ANIMATE_WINDOW, cr.ANIMATE_NEW) == 13           # the 30 s clip rendered on an H100: 13 windows
    assert (cr.SCAIL_SEGMENT, cr.SCAIL_NEW) == (81, 76) and (cr.ANIMATE_WINDOW, cr.ANIMATE_NEW) == (77, 72)
    assert cr.windows(81, cr.SCAIL_SEGMENT, cr.SCAIL_NEW) == 1 and cr.windows(82, cr.SCAIL_SEGMENT, cr.SCAIL_NEW) == 2 and cr.windows(300, cr.SCAIL_SEGMENT, cr.SCAIL_NEW) == 4


def count(g, cls):
    return [n for n in g.nodes.values() if n["class_type"] == cls]


def test_animate_chains_windows_through_continue_motion_and_the_frame_offset(box):
    put(box, "reference.png", "driving.mp4")
    g, p = graph("wan22-animate", box)                       # 300 frames
    assert p["frames"] == 300 and p["windows"] == 5 and p["face"] == "dots"
    windows = count(g, "WanAnimateToVideo")
    assert len(windows) == 5 and all(w["inputs"]["length"] == 77 and w["inputs"]["continue_motion_max_frames"] == 5 for w in windows)
    assert "continue_motion" not in windows[0]["inputs"] and windows[0]["inputs"]["video_frame_offset"] == 0
    ids = [i for i, n in g.nodes.items() if n["class_type"] == "WanAnimateToVideo"]
    for prev, cur in zip(ids, ids[1:]):
        assert g.nodes[cur]["inputs"]["video_frame_offset"] == [prev, 5], "each window seeks where the previous one ended"
        assert g.nodes[cur]["inputs"]["continue_motion"][0] != prev, "the anchor is the accumulated output, not the raw window"
    assert len(count(g, "ImageBatch")) == 4
    assert count(g, "ImageFromBatch")[-1]["inputs"]["length"] == 300, "the output is cut to the requested frames"
    assert all("background_video" in w["inputs"] and "character_mask" in w["inputs"] for w in windows)       # replace mode


def test_animate_move_mode_has_no_mask_or_background_and_no_sam3(box):
    put(box, "reference.png", "driving.mp4")
    g, p = graph("wan22-animate", box, ANIMATE_MODE="move", ANIMATE_FRAMES=100)
    assert p["windows"] == 2 and not count(g, "SAM3_VideoTrack") and not count(g, "CheckpointLoaderSimple")
    assert all("background_video" not in w["inputs"] and "character_mask" not in w["inputs"] for w in count(g, "WanAnimateToVideo"))


def test_animate_size_follows_the_driving_video_and_stays_a_multiple_of_16(box, monkeypatch):
    put(box, "reference.png", "driving.mp4")
    assert (graph("wan22-animate", box)[1]["width"], graph("wan22-animate", box)[1]["height"]) == (832, 480)
    monkeypatch.setattr(cr, "probe_video", lambda p: (1080, 1920, 90))
    _, p = graph("wan22-animate", box)
    assert (p["width"], p["height"], p["frames"], p["windows"]) == (464, 832, 90, 2)       # 9:16 at 832 long side is 468 wide: the nearest multiple of 16
    assert p["width"] % 16 == 0 and p["height"] % 16 == 0


def test_scail2_chains_segments_with_colour_matching(box):
    put(box, "reference.png", "driving.mp4")
    g, p = graph("scail2", box)                              # 300 frames -> 4 segments
    assert p["segments"] == 4 and p["mode"] == "animation" and p["turbo"] and (p["steps"], p["cfg"]) == (6, 1.0)
    segs = count(g, "WanSCAILToVideo")
    assert len(segs) == 4 and all(s["inputs"]["length"] == 81 and s["inputs"]["previous_frame_count"] == 5 for s in segs)
    assert "previous_frames" not in segs[0]["inputs"] and all("previous_frames" in s["inputs"] for s in segs[1:])
    assert [n["inputs"]["batch_index"] for n in count(g, "ImageFromBatch") if n["inputs"]["length"] == 81] == [0, 76, 152, 228]
    assert len(count(g, "ColorTransfer")) == 3 and len(count(g, "SamplerCustom")) == 4
    assert all(m["inputs"]["replacement_mode"] is False for m in count(g, "SCAIL2ColoredMask"))
    assert len(count(g, "SAM3_VideoTrack")) == 1 + 4, "the reference once, the driving video once per segment"


def test_scail2_replacement_adds_the_relight_lora_and_full_quality_drops_the_distill_lora(box):
    put(box, "reference.png", "driving.mp4")
    g, p = graph("scail2", box, SCAIL_MODE="replacement")
    assert "wan2.1_SCAIL_2_relight_lora_bf16.safetensors" in model_files(g) and all(s["inputs"]["replacement_mode"] for s in count(g, "WanSCAILToVideo"))
    g, p = graph("scail2", box, SCAIL_TURBO=0)
    assert (p["steps"], p["cfg"]) == (40, 5.0) and "wan2.1_SCAIL_2_relight_lora_bf16.safetensors" not in model_files(g)
    sampler = count(g, "SamplerCustom")[0]["inputs"]
    shift = g.nodes[sampler["model"][0]]
    assert shift["class_type"] == "ModelSamplingSD3" and g.nodes[shift["inputs"]["model"][0]]["inputs"].get("lora_name") != cr.LIGHTX2V


def test_wan22_a14b_splits_the_steps_between_the_two_experts(box):
    g, p = graph("wan22-t2v-a14b", box)
    first, second = count(g, "KSamplerAdvanced")
    assert (p["steps"], p["cfg"]) == (20, 3.5) and (first["inputs"]["start_at_step"], first["inputs"]["end_at_step"]) == (0, 10)
    assert (second["inputs"]["start_at_step"], second["inputs"]["end_at_step"]) == (10, 20)
    assert first["inputs"]["add_noise"] == "enable" and second["inputs"]["add_noise"] == "disable"
    put(box, "reference.png")
    g, p = graph("wan22-i2v-a14b", box, WAN_FAST=1)
    first, second = count(g, "KSamplerAdvanced")
    assert (p["steps"], p["cfg"], first["inputs"]["end_at_step"], second["inputs"]["start_at_step"]) == (4, 1.0, 2, 2) and len(count(g, "LoraLoaderModelOnly")) == 2


def test_wan22_i2v_needs_an_image_and_the_5b_model_takes_one_optionally(box):
    with pytest.raises(cr.RunError, match="start image"):
        cr.build_wan_a14b("i2v", [])
    g, p = cr.build_wan_5b([])
    assert p["image"] is None and not count(g, "LoadImage")
    put(box, "reference.png")
    stage = []
    g, p = cr.build_wan_5b(stage)
    assert p["image"] == "reference.png" and stage == ["reference.png"] and len(count(g, "LoadImage")) == 1


# ------------------------------------------------------------------ settings
def test_settings_are_validated_before_anything_is_downloaded(box, monkeypatch):
    put(box, "reference.png", "driving.mp4")
    for env, msg in [({"WAN_FRAMES": "80"}, "4k"), ({"WAN_WIDTH": "1000"}, "multiple of 32"), ({"WAN_FRAMES": "x"}, "whole number"), ({"WAN_CFG": "99"}, "between")]:
        monkeypatch.setenv(next(iter(env)), next(iter(env.values())))
        with pytest.raises(cr.RunError, match=msg):
            cr.build_wan_5b([])
        monkeypatch.delenv(next(iter(env)))
    monkeypatch.setenv("ANIMATE_MODE", "swap")
    with pytest.raises(cr.RunError, match="replace or move"):
        cr.build_animate([])
    monkeypatch.setenv("SCAIL_MODE", "x")
    with pytest.raises(cr.RunError, match="animation or replacement"):
        cr.build_scail2([])
    monkeypatch.setenv("WAN_FAST", "maybe")
    with pytest.raises(cr.RunError, match="1/0"):
        cr.build_wan_a14b("t2v", [])


def test_the_prompt_comes_from_the_environment_then_prompt_txt_then_the_default(box, monkeypatch):
    assert cr.prompt_setting("WAN_PROMPT", "d") == "d"
    (box / "in" / "prompt.txt").write_text("  a long\nnatural-language prompt \n", encoding="utf-8")
    assert cr.prompt_setting("WAN_PROMPT", "d") == "a long\nnatural-language prompt"
    monkeypatch.setenv("WAN_PROMPT", "from the spec")
    assert cr.prompt_setting("WAN_PROMPT", "d") == "from the spec"


def test_pick_input_names_the_file_or_the_only_candidate(tmp_path, monkeypatch):
    monkeypatch.setattr(cr, "INBOX", tmp_path)
    for f in ("install.sh", "comfy_run.py", "a.png", "m.mp4"):
        (tmp_path / f).write_bytes(b"x")
    assert cr.pick_input("X", cr.IMAGES, True, "image") == "a.png" and cr.pick_input("X", cr.VIDEOS, True, "video") == "m.mp4"
    (tmp_path / "b.jpg").write_bytes(b"x")
    with pytest.raises(cr.RunError, match="more than one"):
        cr.pick_input("X", cr.IMAGES, True, "image")
    monkeypatch.setenv("X", "b.jpg")
    assert cr.pick_input("X", cr.IMAGES, True, "image") == "b.jpg"
    monkeypatch.setenv("X", "../etc/passwd")
    with pytest.raises(cr.RunError, match="no such file"):
        cr.pick_input("X", cr.IMAGES, True, "image")


def test_short_clips_and_the_frame_cap(monkeypatch):
    monkeypatch.delenv("SCAIL_FRAMES", raising=False)
    assert cr.swap_frames("SCAIL", 120) == 120 and cr.swap_frames("SCAIL", 5000) == cr.MAX_FRAMES and cr.swap_frames("SCAIL", 0) == cr.MAX_FRAMES
    monkeypatch.setenv("SCAIL_FRAMES", "81")
    assert cr.swap_frames("SCAIL", 50) == 50, "never more than the clip has"
    with pytest.raises(cr.RunError, match="at least 5"):
        cr.swap_frames("SCAIL", 3)


def test_a_cuda_build_runs_on_any_newer_minor_of_its_architecture_and_ptx_on_anything_newer():
    assert cr.arch_usable([8, 9], ["sm_80", "sm_86", "sm_90"]), "an RTX 4090 (8.9) runs the sm_86 kernels"
    assert cr.arch_usable([12, 0], ["sm_80", "sm_90", "sm_100", "sm_120"]) and cr.arch_usable([9, 0], ["sm_90a"])
    assert not cr.arch_usable([12, 0], ["sm_80", "sm_90"]) and not cr.arch_usable([7, 0], ["sm_80", "sm_90"]) and not cr.arch_usable([8, 0], ["sm_86"])
    assert cr.arch_usable([12, 0], ["sm_80", "compute_90"]), "PTX is forward compatible"


def test_server_args_keep_custom_nodes_in_the_install_and_load_only_what_a_workflow_uses():
    small, big = {"gib": 24.0}, {"gib": 80.0}
    a = cr.server_args(small, "scail2")
    assert "--highvram" not in a and "--disable-all-custom-nodes" in a and "--whitelist-custom-nodes" not in a and "--base-directory" not in a
    b = cr.server_args(big, "wan22-animate")
    assert "--highvram" in b and b[b.index("--whitelist-custom-nodes") + 1:][:2] == ["comfyui_controlnet_aux", "ComfyUI-KJNodes"]
    assert "127.0.0.1" == a[a.index("--listen") + 1] and "--offline" in a
    for flag in ("--models-directory", "--input-directory", "--temp-directory", "--user-directory", "--output-directory"):
        assert flag in a


def test_the_installer_is_documented_to_fail_without_a_gpu_only_on_a_job_box():
    script = (WF / "comfyui" / "install.sh").read_text(encoding="utf-8")
    assert 'if [ -n "${DDC_JOB:-}" ]' in script and "no usable NVIDIA GPU" in script


# ------------------------------------------------------------------ bring your own graph
CUSTOM = WF / "templates" / "custom-comfyui.toml"
API_GRAPH = {"1": {"class_type": "EmptyLatentImage", "inputs": {"width": 512, "height": 512, "batch_size": 1}}}


def test_custom_template_is_a_valid_spec_that_runs_the_custom_runner(jw):
    r = tomllib.loads(CUSTOM.read_text(encoding="utf-8"))
    s = specmod.normalize(r, WF)      # the template is meant to be copied into workflows/
    assert [t for t, v in r.items() if isinstance(v, dict) and ({"inputs", "inputs_dest"} & set(v))] == []
    install, generate = s["steps"]
    assert generate["run"] == "python in/comfy_run.py custom" and "out/*.png" in generate["artifacts"]
    assert {Path(p).name for p in s["inputs"]} == {"install.sh", "comfy_run.py", "graph.json"}
    assert (WF / s["inputs"][0]).is_file() and (WF / s["inputs"][1]).is_file(), "the template's paths reach the real scripts once it sits in workflows/"
    s = specmod.normalize(r, jw.tmp)
    for p in s["inputs"]:
        (jw.tmp / p).parent.mkdir(parents=True, exist_ok=True)
        (jw.tmp / p).write_bytes(b"x")
    ddc.CFG["max_price_usd_hr"] = 5.0
    cs = preflight.run(s, offline=True)
    assert not preflight.failed(cs), [(c.name, c.detail) for c in cs if c.status == "fail"]


def test_custom_graph_is_loaded_and_its_media_staged(box):
    (box / "in" / "graph.json").write_text(__import__("json").dumps(API_GRAPH), encoding="utf-8")
    put(box, "face.png", "clip.mp4", "notes.txt")
    stage = []
    g, params = cr.WORKFLOWS["custom"](stage)
    assert g.nodes == API_GRAPH and stage == ["clip.mp4", "face.png"] and params["media"] == stage


@pytest.mark.parametrize("content, message", [
    (None, "no graph.json"),
    ("{nope", "not valid JSON"),
    ('{"nodes": [], "links": []}', "Save (API Format)"),
    ('{"1": {"class_type": "X"}}', "not an API-format graph"),
    ("{}", "not an API-format graph"),
])
def test_custom_graph_errors_name_the_fix(box, content, message):
    if content is not None:
        (box / "in" / "graph.json").write_text(content, encoding="utf-8")
    with pytest.raises(cr.RunError, match=re.escape(message)):
        cr.WORKFLOWS["custom"]([])


def test_custom_runner_loads_only_the_nodes_the_user_names(monkeypatch):
    info = {"gib": 24}
    assert "--whitelist-custom-nodes" not in cr.server_args(info, "custom")
    monkeypatch.setenv("COMFY_NODES", "ComfyUI-KJNodes")
    args = cr.server_args(info, "custom")
    assert args[args.index("--whitelist-custom-nodes") + 1] == "ComfyUI-KJNodes"
    assert "ComfyUI-KJNodes" not in cr.server_args(info, "scail2"), "COMFY_NODES applies to the custom workflow only"
