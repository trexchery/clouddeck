"""`ddc job`: spec parsing, SDL generation (and its agreement with clouddeck.sdlgen), preflight, the clamp to the controller's limits, shipped examples.
Offline: stubbed Akash API, fake clock (tests/job_helpers.py)."""
import json
import textwrap
import tomllib
from pathlib import Path

import pytest
from job_helpers import make_spec

from clouddeck import sdlgen
from clouddeck.engine import ddc
from clouddeck.job import preflight
from clouddeck.job import spec as specmod

ROOT = Path(__file__).resolve().parent.parent
PUB = "ssh-ed25519 AAAA k"


def checks(w, s, **kw):
    return {c.name: c for c in preflight.run(s, clock=w.clock, **kw)}


# ------------------------------------------------------------------ spec / SDL
def test_sdl_cpu_only_has_no_gpu_and_gpu_spec_lists_models(tmp_path):
    cpu = make_spec(tmp_path, hardware={"cpu": 8, "memory": "16Gi", "disk": "50Gi", "max_usd_hr": 0.5})
    d = specmod.build_sdl(cpu, "ubuntu:24.04", PUB)
    assert "gpu" not in d["profiles"]["compute"]["job"]["resources"]
    assert specmod.validate_sdl(d) == []
    gpu = make_spec(tmp_path, hardware={"cpu": 8, "memory": "64Gi", "disk": "100Gi", "max_usd_hr": 2.0, "gpu": {"units": 2, "vendor": "vendor-a", "models": ["model-b:24Gi", "model-c"]}})
    r = specmod.build_sdl(gpu, "x/y:1", PUB)["profiles"]["compute"]["job"]["resources"]["gpu"]
    assert r["units"] == 2 and r["attributes"]["vendor"]["vendor-a"] == [{"model": "model-b", "ram": "24Gi"}, {"model": "model-c"}]


def test_vendor_is_a_parameter_not_a_constant(tmp_path):
    s = make_spec(tmp_path, hardware={"cpu": 4, "memory": "8Gi", "disk": "20Gi", "max_usd_hr": 1.0, "gpu": {"vendor": "AnyVendor", "models": ["Some Model:16Gi"]}})
    g = specmod.build_sdl(s, "x/y:1", PUB)["profiles"]["compute"]["job"]["resources"]["gpu"]
    assert g["attributes"]["vendor"] == {"anyvendor": [{"model": "somemodel", "ram": "16Gi"}]}      # same folding as sdlgen: a new vendor / model needs no code change


def test_sdl_yaml_roundtrips_and_price_cap_is_enforced(tmp_path):
    yaml = pytest.importorskip("yaml")
    s = make_spec(tmp_path)
    text = specmod.render_sdl(s, "ghcr.io/example/base:1.0", PUB)
    d = yaml.safe_load(text)
    assert d == json.loads(json.dumps(specmod.build_sdl(s, "ghcr.io/example/base:1.0", PUB)))
    assert d["profiles"]["placement"]["akash"]["pricing"]["job"]["amount"] == ddc.uact_block_from_usd_hr(3.0) == 5000  # $3.00/h at 600 blocks/h: the controller's own conversion
    assert "dummy" not in text


@pytest.mark.parametrize("gpu", [
    {"units": 1, "vendor": "vendor-a", "models": ["model-a:80Gi"]},
    {"units": 2, "vendor": "NVIDIA", "models": ["model-a:80Gi", "Model-B:80Gi"]},
    {"units": 1, "vendor": "other", "models": ["RTX 4090:24Gi"]},
    {"units": 4, "vendor": "vendor-a", "models": ["model-a", "model-b"]},
])
def test_gpu_section_agrees_with_sdlgen(tmp_path, gpu):
    """The job SDL builder and sdlgen.gpu_block (what the controller's profiles use) spell the GPU attributes identically: casing, folding,
    quoting rules for ids and VRAM.  sdlgen takes one VRAM for all models, so the comparison uses the models' common VRAM."""
    s = make_spec(tmp_path, hardware={"cpu": 4, "memory": "8Gi", "disk": "20Gi", "max_usd_hr": 1.0, "gpu": gpu})
    models = s["hardware"]["gpu"]["models"]
    assert len({m["ram"] for m in models}) == 1
    profile = {"gpu": s["hardware"]["gpu"]["units"], "gpu_vendor": gpu["vendor"], "gpu_model": models[0]["model"], "gpu_models": [m["model"] for m in models[1:]],
               **({"gpu_ram": models[0]["ram"]} if models[0]["ram"] else {})}
    want = sdlgen.gpu_block(profile)
    res = specmod.build_sdl(s, "x/y:1", PUB)["profiles"]["compute"]["job"]["resources"]["gpu"]
    text = "\n".join(specmod.emit_yaml({"gpu": res}))

    def norm(block: str) -> list[str]:
        return [ln.strip().replace('"', "") for ln in block.splitlines() if ln.strip()]
    assert norm(text) == norm(want)
    yaml = pytest.importorskip("yaml")
    assert yaml.safe_load(text) == yaml.safe_load(textwrap.dedent(want))


def test_spec_rejects_secret_in_env_missing_cap_and_bad_needs(tmp_path):
    with pytest.raises(specmod.SpecError) as e:
        specmod.normalize({"name": "x", "env": {"HF_TOKEN": "abc"}, "image": {"stock": "u"}, "hardware": {}, "step": [{"name": "a", "run": "x", "needs": ["nope"]}]}, tmp_path)
    m = str(e.value)
    assert "HF_TOKEN" in m and "max_usd_hr" in m and "unknown need" in m


@pytest.mark.parametrize("over, needle", [
    ({"inputs": ["../outside.bin"]}, "inputs"),
    ({"inputs": ["/etc/passwd"]}, "inputs"),
    ({"artifacts_dir": "../up"}, "artifacts_dir"),
    ({"cache": {"item": [{"name": "m", "dest": "../x", "url": "https://e/x"}]}}, "dest"),
    ({"step": [{"name": "a", "run": "x", "artifacts": ["../../etc/*"]}]}, "artifact pattern"),
    ({"step": [{"name": "a", "run": "x", "artifacts": ["out/$(id)"]}]}, "artifact pattern"),
    ({"env": {"DDC_AUTHORIZED_KEYS_B64": "x"}}, "reserved"),
    ({"env": {"bad name": "x"}}, "not a valid variable name"),
    ({"env": {"secrets": ["not valid"]}}, "environment variable name"),
    ({"image": {"stock": "ubuntu:24.04", "apt": ["ok", "x; rm -rf /"]}}, "Debian package"),
    ({"image": {"stock": "x y"}}, "image reference"),
    ({"deposit_usd": 5}, "deposit_usd"),
    ({"hardware": {"cpu": 4, "memory": "8Gi", "disk": "20Gi", "max_usd_hr": -1}}, "max_usd_hr"),
    ({"hardware": {"cpu": 4, "memory": "8Gi", "disk": "20Gi", "max_usd_hr": 1, "gpu": {"vendor": "vendor-a", "models": ["bad:model;x"]}}}, "VRAM"),
    ({"hardware": {"cpu": 4, "memory": "8Gi", "disk": "20Gi", "max_usd_hr": 1, "gpu": {"models": ["a"], "vendor": "nv idia"}}}, "vendor"),
    ({"hardware": {"cpu": 4, "memory": "8Gi", "disk": "20Gi", "max_usd_hr": 1, "gpu": {"vendor": "vendor-a", "models": ["a"], "units": 0}}}, "units"),
])
def test_spec_rejects_unsafe_or_malformed_values(tmp_path, over, needle):
    with pytest.raises(specmod.SpecError, match=needle):
        make_spec(tmp_path, **over)


def test_secrets_are_never_in_the_sdl(tmp_path, monkeypatch):
    monkeypatch.setenv("MY_SECRET_THING", "s3cr3t-value-123")
    s = make_spec(tmp_path, env={"MODE": "fast", "secrets": ["MY_SECRET_THING"]})
    text = specmod.render_sdl(s, "ubuntu:24.04", PUB)
    assert "s3cr3t-value-123" not in text and "MY_SECRET_THING" not in text and "MODE=fast" in text


# ------------------------------------------------------------------ the spec's own caps are clamped to the controller's configuration
def test_apply_limits_clamps_price_runtime_and_cost_to_the_config_never_above(tmp_path):
    ddc.CFG.update(max_price_usd_hr=2.0, budget_usd=7.0)
    s = make_spec(tmp_path, runtime_hours=48, max_cost_usd=500, hardware={"cpu": 4, "memory": "8Gi", "disk": "20Gi", "max_usd_hr": 9.0})
    notes = preflight.apply_limits(s)
    assert s["hardware"]["max_usd_hr"] == 2.0 and s["runtime_hours"] == ddc.HARD_MAX_RUNTIME_H and s["max_cost_usd"] == 7.0
    assert len(notes) == 3 and preflight.limit_h(s) == ddc.HARD_MAX_RUNTIME_H
    assert preflight.apply_limits(s) == []                                  # idempotent


def test_apply_limits_leaves_a_spec_inside_the_limits_alone_and_unsafe_lifts_only_the_hard_ones(tmp_path):
    ddc.CFG["max_price_usd_hr"] = 5.0
    s = make_spec(tmp_path)
    assert preflight.apply_limits(s) == [] and s["hardware"]["max_usd_hr"] == 3.0
    ddc.CFG.update(max_price_usd_hr=2.0, unsafe_disable_limits=True)
    s = make_spec(tmp_path, runtime_hours=48, hardware={"cpu": 4, "memory": "8Gi", "disk": "20Gi", "max_usd_hr": 9.0})
    assert preflight.apply_limits(s) == [] and s["hardware"]["max_usd_hr"] == 9.0 and s["runtime_hours"] == 48      # unsafe_disable_limits: as everywhere else
    ddc.CFG["budget_guard"] = False
    big = make_spec(tmp_path, max_cost_usd=10_000)
    assert preflight.apply_limits(big) == [] and big["max_cost_usd"] == 10_000                                      # guard off: the budget is not enforced either


# ------------------------------------------------------------------ preflight
def test_preflight_ok_and_each_failure_mode(jw):
    s = make_spec(jw.tmp)
    cs = checks(jw, s)
    assert not preflight.failed(list(cs.values())), {k: v.detail for k, v in cs.items() if v.status == "fail"}
    assert cs["budget"].status == "ok" and "$6.00" in cs["budget"].detail
    assert cs["spend_gate"].status == "ok"                                   # the controller's own check_spend, not a copy
    bad = checks(jw, make_spec(jw.tmp, max_cost_usd=5), offline=True)       # budget guard: 2 h x $3 = $6 > $5
    assert bad["budget"].status == "fail"
    (jw.tmp / "in.bin").unlink()
    assert checks(jw, s, offline=True)["inputs"].status == "fail"
    (jw.tmp / "in.bin").write_bytes(b"x")
    small = make_spec(jw.tmp, hardware={"cpu": 4, "memory": "16Gi", "disk": "5Gi", "max_usd_hr": 3.0, "gpu": {"vendor": "vendor-a", "models": ["model-a"]}})
    assert checks(jw, small, offline=True)["disk"].status == "fail"          # 1 GB cache + 10 GB scratch > 5 GiB
    (ddc.SECRETS_DIR / "secrets.env").write_text("")
    assert checks(jw, s)["api_key"].status == "fail"


def test_preflight_runs_check_spend_not_its_own_budget_copy(jw, monkeypatch):
    seen = []
    real = ddc.check_spend
    monkeypatch.setattr(ddc, "check_spend", lambda *a, **k: (seen.append((a, k)), real(*a, **k))[1])
    assert checks(jw, make_spec(jw.tmp))["spend_gate"].status == "ok"
    label, n, hours, cap, role = seen[0][0]
    assert (n, hours, cap, role) == (1, 2, 3.0, "job") and "t1" in label


@pytest.mark.parametrize("what", ["balance", "lease_cap", "budget"])
def test_preflight_spend_gate_fails_where_the_launch_would_be_refused(jw, what):
    if what == "balance":
        jw.akash.balance = 3.0                                              # balance - worst case < reserve
    elif what == "lease_cap":
        ddc.CFG["max_total_leases"] = 1
        ddc.update_dep("1", ours=True, name="ddc-dev-x", role="dev", status="ready", usd_hr=0.3, lease_at=jw.clock.time(), limit_h=8)
    else:
        ddc.CFG["budget_usd"] = 5.0                                         # 2 h x $3 = $6 > $5 (max_cost_usd is raised so only the controller's own budget refuses)
    c = checks(jw, make_spec(jw.tmp, max_cost_usd=100))["spend_gate"]
    assert c.status == "fail" and "would refuse" in c.detail


def test_preflight_warns_about_mutable_image_tags_in_the_controllers_words(jw):
    cs = checks(jw, make_spec(jw.tmp, image={"prebuilt": "ghcr.io/example/base@sha256:" + "a" * 64, "stock": "ubuntu:24.04"}), offline=True)
    assert cs["image_tag[stock]"].status == "warn" and "mutable tag, not pinned by @sha256:<digest>" in cs["image_tag[stock]"].detail
    assert "image_tag[prebuilt]" not in cs


def test_preflight_price_below_market_and_balance(jw):
    cheap = make_spec(jw.tmp, hardware={"cpu": 4, "memory": "16Gi", "disk": "50Gi", "max_usd_hr": 1.0, "gpu": {"vendor": "vendor-a", "models": ["model-a:80Gi"]}})
    r = checks(jw, cheap)
    assert r["price_vs_cap"].status == "fail" and "no bid can win" in r["price_vs_cap"].detail
    jw.akash.balance = 3.0
    assert checks(jw, make_spec(jw.tmp))["spend_gate"].status == "fail"
    assert all(m not in ("POST", "DELETE") for m, _ in jw.akash.calls)      # preflight is read-only


def test_preflight_hardware_limits_come_from_the_config(jw):
    ddc.CFG["max_gpu"] = 1
    two = make_spec(jw.tmp, hardware={"cpu": 4, "memory": "16Gi", "disk": "50Gi", "max_usd_hr": 3.0, "gpu": {"units": 2, "vendor": "vendor-a", "models": ["model-a"]}})
    assert checks(jw, two, offline=True)["hardware_limits"].status == "fail"


# ------------------------------------------------------------------ shipped examples
@pytest.mark.parametrize("name", ["cpu-smoke", "gpu-batch"])
def test_shipped_examples_are_valid_and_pass_offline_preflight(jw, monkeypatch, name):
    raw = tomllib.loads((ROOT / "examples" / f"{name}.toml").read_text(encoding="utf-8"))
    s = specmod.normalize(raw, jw.tmp)
    for p in s["inputs"]:
        if p not in s["prep"]["produces"]:
            (jw.tmp / p).parent.mkdir(parents=True, exist_ok=True)
            (jw.tmp / p).write_bytes(b"x")
    monkeypatch.setenv("EXAMPLE_API_TOKEN", "dummy")
    ddc.CFG["max_price_usd_hr"] = 5.0
    cs = preflight.run(s, offline=True)
    assert not preflight.failed(cs), [(c.name, c.detail) for c in cs if c.status == "fail"]
    for _kind, image in specmod.image_kinds(s):
        assert specmod.validate_sdl(specmod.build_sdl(s, image, PUB)) == []
    assert (s["hardware"]["gpu"] is not None) == (name == "gpu-batch")


def test_examples_hold_placeholders_only():
    text = "".join(p.read_text(encoding="utf-8") for p in sorted((ROOT / "examples").glob("*.toml")))
    assert "0" * 64 in text                                                  # the all-zero sha256 is the obvious dummy
    assert "akash1" not in text and "BEGIN" not in text
