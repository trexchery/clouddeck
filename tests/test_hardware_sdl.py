"""The SDL for any hardware: valid YAML with the right structure, legacy profiles byte-identical, no way for typed text to inject anything,
and a price cap and a runtime limit on every order.  (Needs PyYAML to parse the generated SDL: it is in the `dev` extra; the file skips without it.)"""
import json
import random
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

from clouddeck import sdlgen  # noqa: E402
from clouddeck.engine import ddc  # noqa: E402
from clouddeck.providers.base import Spec  # noqa: E402

GOLDEN = Path(__file__).parent / "fixtures" / "hardware" / "sdl_golden"
AUDITOR = "akash1AUDITORFIXTURE"


def render(pr, role="box", name="t"):
    ddc.CFG["profiles"][name] = pr
    return ddc.render_sdl(role, name)


def doc_of(pr, **kw):
    return yaml.safe_load(render(pr, **kw))


def prof(**kw):
    base = {"cpu": 8, "mem": "32Gi", "disk": "100Gi", "max_usd_hr": 0.9}
    base.update(kw)
    return base


def invariants(doc, pr):
    """What every generated SDL must be, whatever the hardware."""
    assert doc["version"] == "2.0" and set(doc["services"]) == {"box"} and set(doc["deployment"]) == {"box"}
    res = doc["profiles"]["compute"]["box"]["resources"]
    assert res["cpu"]["units"] == pr["cpu"] and res["memory"]["size"] == pr["mem"] and res["storage"][0]["size"] == pr["disk"]
    price = doc["profiles"]["placement"]["akash"]["pricing"]["box"]
    assert price["denom"] == "uact" and isinstance(price["amount"], int) and price["amount"] >= 1                       # every order carries a price cap
    assert price["amount"] * 600 / 1e6 <= pr["max_usd_hr"] + 1e-12                                                    # and it never exceeds the user's cap
    assert doc["deployment"]["box"]["akash"] == {"profile": "box", "count": 1}
    return res, doc["profiles"]["placement"]["akash"]


# ------------------------------------------------------------------ legacy profiles do not change by a byte
LEGACY = {"plain16": ("dev", "16"), "worker32": ("worker", "32"), "box64": ("box", "64"), "gpu_model": ("box", "g1"), "gpu_bare": ("box", "g2"), "gpu_a100x4": ("worker", "g3")}


@pytest.mark.parametrize("name", LEGACY)
def test_profiles_without_the_new_keys_render_exactly_as_before(name):
    ddc.CFG["profiles"]["g1"] = {"cpu": 8, "mem": "32Gi", "disk": "100Gi", "max_usd_hr": 0.8, "gpu": 1, "gpu_model": "rtx4090"}
    ddc.CFG["profiles"]["g2"] = {"cpu": 4, "mem": "8Gi", "disk": "20Gi", "max_usd_hr": 1.0, "gpu": 2}
    ddc.CFG["profiles"]["g3"] = {"cpu": 16, "mem": "64Gi", "disk": "200Gi", "max_usd_hr": 1.5, "gpu": 4, "gpu_model": "a100"}
    role, profile = LEGACY[name]
    assert ddc.render_sdl(role, profile) == (GOLDEN / f"{name}.yaml").read_bytes().decode("utf-8")      # golden files were produced by the code before this feature


def test_gpu_sdl_is_still_a_function_with_the_same_text():
    assert ddc.gpu_sdl({"gpu": 1, "gpu_model": "rtx4090"}) == "        gpu:\n          units: 1\n          attributes:\n            vendor:\n              nvidia:\n                - model: rtx4090\n"
    assert ddc.gpu_sdl({"gpu": 2}) == "        gpu:\n          units: 2\n          attributes:\n            vendor:\n              nvidia:\n"
    assert ddc.gpu_sdl({"cpu": 4}) == ""


# ------------------------------------------------------------------ structure
def test_cpu_only_order_has_no_gpu_and_no_placement_attributes():
    res, placement = invariants(doc_of(prof()), prof())
    assert "gpu" not in res and set(placement) == {"pricing"}


def test_gpu_with_every_attribute():
    pr = Spec(cpu=8, mem_gb=32, disk_gb=100, gpu=2, gpu_model="RTX 4090", gpu_ram="24Gi", gpu_interface="PCIe", max_usd_hr=0.9).to_profile()
    res, placement = invariants(doc_of(pr), pr)
    assert res["gpu"]["units"] == 2
    assert res["gpu"]["attributes"] == {"vendor": {"nvidia": [{"model": "rtx4090", "ram": "24Gi", "interface": "pcie"}]}}
    assert set(placement) == {"pricing"}


def test_any_of_several_models_keeps_their_order():
    pr = Spec(cpu=4, mem_gb=16, disk_gb=50, gpu=1, gpu_model="h100", gpu_alt=("a100", "RTX 4090", "h100"), gpu_ram="80Gi", max_usd_hr=2.0).to_profile()
    res, _ = invariants(doc_of(pr), pr)
    assert [m["model"] for m in res["gpu"]["attributes"]["vendor"]["nvidia"]] == ["h100", "a100", "rtx4090"]      # duplicates dropped, ids folded
    assert all(m["ram"] == "80Gi" for m in res["gpu"]["attributes"]["vendor"]["nvidia"])


def test_a_vendor_other_than_nvidia_and_a_vendor_without_a_model():
    pr = Spec(cpu=4, mem_gb=16, disk_gb=50, gpu=1, gpu_vendor="amd", gpu_model="mi300x", gpu_interface="oam", max_usd_hr=1.0).to_profile()
    res, _ = invariants(doc_of(pr), pr)
    assert res["gpu"]["attributes"]["vendor"] == {"amd": [{"model": "mi300x", "interface": "oam"}]}
    bare = Spec(cpu=4, mem_gb=16, disk_gb=50, gpu=3, gpu_vendor="amd", max_usd_hr=1.0).to_profile()
    res, _ = invariants(doc_of(bare), bare)
    assert res["gpu"]["units"] == 3 and res["gpu"]["attributes"] == {"vendor": {"amd": None}}                      # any model of that vendor


def test_a_model_nobody_has_heard_of_and_ids_that_look_like_numbers_stay_strings():
    for model in ("zz9000-x", "4090", "1e3"):
        pr = Spec(cpu=4, mem_gb=16, disk_gb=50, gpu=1, gpu_model=model, max_usd_hr=1.0).to_profile()
        res, _ = invariants(doc_of(pr), pr)
        assert res["gpu"]["attributes"]["vendor"]["nvidia"] == [{"model": model}] and isinstance(res["gpu"]["attributes"]["vendor"]["nvidia"][0]["model"], str)


def test_cpu_architecture_vendor_and_region_become_placement_attributes():
    pr = Spec(cpu=8, mem_gb=32, disk_gb=100, cpu_arch="ARM-64", cpu_vendor="arm", region="eu-west", max_usd_hr=0.5).to_profile()
    _, placement = invariants(doc_of(pr), pr)
    assert placement["attributes"] == {"capabilities/cpu": "arm", "capabilities/cpu/arch": "arm-64", "location-region": "eu-west"}
    assert "signedBy" not in placement


def test_audited_providers_only_becomes_signed_by_the_auditor():
    pr = prof(audited_only=True, signed_by=[AUDITOR])
    _, placement = invariants(doc_of(pr), pr)
    assert placement["signedBy"] == {"anyOf": [AUDITOR]}
    with pytest.raises(sdlgen.SdlError, match="auditor"):                                                          # the flag without an auditor is refused, never dropped
        sdlgen.placement_block(prof(audited_only=True))
    assert any("auditor" in p for p in ddc.config_problems(cfg=None, profile=None) + ddc.profile_problems("x", prof(audited_only=True)))


def test_provider_pins_are_not_part_of_the_sdl():
    pr = prof(providers=["akash1abcdef"])
    res, placement = invariants(doc_of(pr), pr)
    assert "akash1abcdef" not in render(pr) and set(placement) == {"pricing"}


def test_the_screening_request_describes_the_same_order():
    pr = Spec(cpu=4, mem_gb=8, disk_gb=20, gpu=1, gpu_model="a100", gpu_alt=("h100",), gpu_ram="80Gi", cpu_arch="x86-64", audited_only=True, max_usd_hr=1).to_profile()
    pr["signed_by"] = [AUDITOR]
    body = sdlgen.screening_request(pr, 1666)
    assert body["resources"][0]["resource"]["gpu"]["attributes"] == [{"key": "vendor/nvidia/model/a100/ram/80Gi", "value": "true"},
                                                                     {"key": "vendor/nvidia/model/h100/ram/80Gi", "value": "true"}]
    assert body["requirements"] == {"attributes": [{"key": "capabilities/cpu/arch", "value": "x86-64"}], "signedBy": {"allOf": [], "anyOf": [AUDITOR]}}
    assert body["resources"][0]["price"] == {"denom": "uact", "amount": "1666"}
    cpu_only = sdlgen.screening_request(prof(), 100)["resources"][0]["resource"]
    assert cpu_only["gpu"] == {"units": {"val": "0"}, "attributes": []} and cpu_only["cpu"]["units"]["val"] == "8000"
    assert sdlgen.screening_request({**prof(gpu=1), "gpu_vendor": "amd"}, 1)["resources"][0]["resource"]["gpu"]["attributes"] == [{"key": "vendor/amd/model/*", "value": "true"}]


# ------------------------------------------------------------------ randomised: whatever is generated parses and says what was asked
def test_random_valid_orders_always_produce_the_orders_they_were_given():
    rng = random.Random(20261017)
    tokens = ["rtx4090", "a100", "h100", "mi300x", "zz9000", "b300", "gtx1050ti", "pro6000se", "v100"]
    for _ in range(150):
        gpu = rng.choice([0, 0, 1, 2, 4, 8])
        spec = Spec(cpu=rng.randint(1, 128), mem_gb=rng.randint(1, 1024), disk_gb=rng.randint(1, 4000), gpu=gpu, max_usd_hr=round(rng.uniform(0.01, 2.0), 3), hours=rng.randint(1, 24),
                    gpu_model=rng.choice(tokens) if gpu and rng.random() < 0.8 else "", gpu_alt=tuple(rng.sample(tokens, rng.randint(0, 2))) if gpu else (),
                    gpu_vendor=rng.choice(["", "nvidia", "amd"]) if gpu else "", cpu_arch=rng.choice(["", "x86-64", "arm-64"]), cpu_vendor=rng.choice(["", "intel", "amd"]),
                    region=rng.choice(["", "eu-west", "na-us-west"]))
        if spec.gpu and not spec.gpu_model:
            spec = spec.with_(gpu_alt=())
        assert spec.validate() == [], spec
        pr = spec.to_profile()
        doc = doc_of(pr)
        res, placement = invariants(doc, pr)
        if gpu:
            assert res["gpu"]["units"] == gpu
        else:
            assert "gpu" not in res
        assert placement.get("attributes", {}).get("location-region") == (spec.region or None)


# ------------------------------------------------------------------ no text typed anywhere can change the structure
INJECTIONS = ["a100\n  evil: 1", "x: y", 'a"b', "a #b", "{a: b}", "a‮b", "é", "x" * 10_000, "", " ", "a b", "a\tb", "-", ".", "a/b", "a:b", "[a]", "a'b", "a\r\nb", "\x00", "*", "!x", "&x", "%x"]
TOKEN_FIELDS = ("gpu_vendor", "gpu_interface", "cpu_arch", "cpu_vendor", "region")


@pytest.mark.parametrize("bad", [b for b in INJECTIONS if b.strip() != ""])
def test_hostile_text_is_refused_by_the_spec_and_by_the_generator(bad):
    for field in (*TOKEN_FIELDS, "gpu_ram"):
        spec = Spec(cpu=4, mem_gb=8, disk_gb=20, gpu=1, gpu_model="a100", max_usd_hr=1.0).with_(**{field: bad})
        assert spec.validate(), (field, bad)                                                                           # Spec.validate() refuses it
        pr = {"cpu": 4, "mem": "8Gi", "disk": "20Gi", "max_usd_hr": 1.0, "gpu": 1, "gpu_model": "a100", field: bad}
        with pytest.raises(sdlgen.SdlError):                                                                           # and so does the generator, on its own
            sdlgen.gpu_block(pr) if field.startswith("gpu") else sdlgen.placement_block(pr)
    for pr in ({"gpu": 1, "gpu_model": bad}, {"gpu": 1, "gpu_model": "a100", "gpu_models": [bad]}, {"providers": [bad]}, {"signed_by": [bad]}):
        with pytest.raises(sdlgen.SdlError):
            sdlgen.gpu_block({"cpu": 4, **pr}) if "gpu" in pr else sdlgen.placement_block({"cpu": 4, **pr}) if "signed_by" in pr else sdlgen.pins(pr)


@pytest.mark.parametrize("bad", [b for b in INJECTIONS if b.strip() != ""])
def test_a_typed_model_name_is_cleaned_to_an_id_or_refused(bad):
    spec = Spec(cpu=4, mem_gb=8, disk_gb=20, gpu=1, gpu_model=bad, max_usd_hr=1.0)
    if spec.validate():
        return                                               # refused
    model = spec.to_profile()["gpu_model"]
    assert bad.replace(" ", "").isalnum() or set(bad) <= set("abcdefghijklmnopqrstuvwxyz0123456789 ._-ABCDEFGHIJKLMNOPQRSTUVWXYZ")    # only harmless characters get this far
    assert sdlgen.TOKEN_RE.fullmatch(model), model           # and what the SDL gets is a plain id


def test_hostile_text_never_reaches_a_rendered_sdl():
    for bad in INJECTIONS:
        for field in ("gpu_model", "gpu_vendor", "gpu_interface", "gpu_ram", "cpu_arch", "cpu_vendor", "region"):
            pr = {**prof(gpu=1, gpu_model="a100"), field: bad}
            ddc.CFG["profiles"]["inj"] = pr
            try:
                text = ddc.render_sdl("box", "inj")
            except ddc.DdcError:
                continue                                                        # refused: fine
            doc = yaml.safe_load(text)                                          # accepted: then it was cleaned to a plain id and the structure is untouched
            res = doc["profiles"]["compute"]["box"]["resources"]
            assert set(res) == {"cpu", "memory", "storage", "gpu"} and set(doc["profiles"]["placement"]["akash"]) <= {"attributes", "pricing"}
            assert "evil" not in text and "‮" not in text


class _StrictLoader(yaml.SafeLoader):
    """PyYAML that, like a strict SDL parser, refuses a duplicate mapping key."""


def _no_dup_keys(loader, node, deep=False):
    keys = [loader.construct_object(k, deep=True) for k, _ in node.value]
    assert len(keys) == len(set(keys)), f"duplicate key in {keys}"
    return yaml.SafeLoader.construct_mapping(loader, node, deep)


_StrictLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _no_dup_keys)

AMBIGUOUS = ["0x1f", "0o7", "0b1", "1e3", "12", "007", "1_000", "3090", "0.5", "1.5e3", "true", "false", "null", "yes", "no", "on", "off", "y", "n", "2024-01-01", "1-2"]


@pytest.mark.parametrize("token", AMBIGUOUS)
def test_tokens_a_yaml_parser_could_read_as_non_strings_stay_strings(token):
    assert sdlgen.TOKEN_RE.fullmatch(token)
    text = sdlgen.gpu_block({"gpu": 1, "gpu_model": token, "gpu_vendor": token, "gpu_models": [token + "x", "a" + token]})
    gpu = yaml.load("resources:\n" + text, Loader=_StrictLoader)["resources"]["gpu"]
    (vendor, models), = gpu["attributes"]["vendor"].items()
    assert vendor == token and isinstance(vendor, str)
    assert [m["model"] for m in models] == [token, token + "x", "a" + token] and all(isinstance(m["model"], str) for m in models)
    for iface in ("pcie", "sxm"):
        assert yaml.load("k: " + sdlgen._scalar(iface), Loader=_StrictLoader) == {"k": iface}


def test_the_duplicate_key_loader_really_rejects_duplicates():
    with pytest.raises(AssertionError):
        yaml.load("a: 1\na: 2\n", Loader=_StrictLoader)


def test_profile_validation_names_the_bad_field():
    msgs = ddc.profile_problems("x", prof(gpu=1, gpu_ram="24gi", gpu_model="a100", gpu_interface="PCIe"))
    assert any("gpu_ram" in m and "24Gi" in m for m in msgs)
    assert ddc.profile_problems("x", prof(gpu=1, gpu_model="a100", gpu_ram="24Gi", gpu_interface="pcie", gpu_models=["h100"], cpu_arch="x86-64", cpu_vendor="amd", region="eu-west")) == []
    assert ddc.profile_problems("x", prof(gpu_vendor="amd"))                    # a GPU constraint without a GPU
    assert ddc.profile_problems("x", prof(providers=["bad address"])) and ddc.profile_problems("x", prof(gpu=1, gpu_ram="24Gi"))   # VRAM without a model cannot be ordered
    assert ddc.profile_problems("x", prof(gpu=9)) and ddc.profile_problems("x", prof(cpu=129))
    assert ddc.profile_problems("x", prof(gpu=9, gpu_model="a100")) and not ddc.profile_problems("x", prof(gpu_model="a100"))      # an old profile with a model but no GPU stays accepted


def test_the_size_limits_are_configurable_within_the_networks_maxima():
    assert sdlgen.bounds({}) == {"max_cpu": 128, "max_mem_gb": 1024, "max_disk_gb": 4000, "max_gpu": 8}                    # the defaults are what earlier versions allowed
    assert sdlgen.bounds({"max_gpu": 24, "max_cpu": 384})["max_gpu"] == 24 and sdlgen.bounds({"max_gpu": 25})["max_gpu"] == 8   # above the network's own maximum: ignored
    assert sdlgen.bounds({"max_gpu": "many", "max_cpu": True})["max_gpu"] == 8
    assert Spec(cpu=200, gpu=1, max_usd_hr=1).validate() and not Spec(cpu=200, gpu=1, max_usd_hr=1).validate({"max_cpu": 256})
    assert Spec(gpu=16, max_usd_hr=1).validate() and not Spec(gpu=16, max_usd_hr=1).validate({"max_gpu": 16})
    ddc.CFG["max_gpu"] = 16
    assert ddc.profile_problems("x", prof(gpu=16, gpu_model="h100")) == []
    ddc.CFG["max_gpu"] = 99
    assert any("max_gpu" in p for p in ddc.config_problems())


# ------------------------------------------------------------------ the price cap: minimum, floor, never above the request
def test_cap_math_edges():
    assert ddc.uact_block_from_usd_hr(0.5) == 833 and ddc.uact_block_from_usd_hr(0.9) == 1500 and ddc.uact_block_from_usd_hr(2.0) == 3333
    assert ddc.uact_block_from_usd_hr(0.001) == 1 and ddc.uact_block_from_usd_hr(0.0006) in (0, 1)       # right at the minimum: float noise decides, the checks below handle either
    for tiny in (0.0001, 0.0005, 1e-9, 1e-300):
        assert ddc.uact_block_from_usd_hr(tiny) == 0
        assert any("too small" in m for m in ddc.profile_problems("x", prof(max_usd_hr=tiny)))               # refused when the profile is checked ...
        with pytest.raises(ddc.DdcError):
            render(prof(max_usd_hr=tiny))                                                                    # ... and again when the SDL is built
    for bad in (0, -1, float("nan"), float("inf"), "0.5", True, None):
        assert ddc.profile_problems("x", prof(max_usd_hr=bad))


def test_cap_math_random_caps_never_exceed_the_users_cap():
    rng = random.Random(7)
    for _ in range(500):
        cap = rng.choice([rng.uniform(0.0001, 0.01), rng.uniform(0.01, 2.0), round(rng.uniform(0.01, 2.0), 2)])
        amount = ddc.uact_block_from_usd_hr(cap)
        if amount < 1:
            assert ddc.profile_problems("x", prof(max_usd_hr=cap)), cap                              # an unusable cap is refused, never rendered as 0
            continue
        assert amount * 600 / 1e6 <= cap + 1e-12 and cap - amount * 600 / 1e6 < 600 / 1e6 + 1e-12      # floor: at most one uact per block below the request
        pr = prof(max_usd_hr=cap)
        assert doc_of(pr)["profiles"]["placement"]["akash"]["pricing"]["box"]["amount"] == amount


def test_check_sdl_refuses_a_missing_zero_or_doubled_cap():
    good = render(prof())
    sdlgen.check_sdl(good)
    for broken in (good.replace("amount: 1500", "amount: 0"), good.replace("          amount: 1500\n", ""), good.replace("denom: uact", "denom: uakt"),
                   good + "          denom: uact\n          amount: 5\n", good.replace('version: "2.0"', 'version: "3.0"'), good.replace("amount: 1500", "amount: __X__")):
        with pytest.raises(sdlgen.SdlError):
            sdlgen.check_sdl(broken)


# ------------------------------------------------------------------ what is actually sent when an order is created
def spy(monkeypatch):
    sent = []
    real = ddc.api

    def api(method, path, body=None, *a, **k):
        sent.append((method, path.split("?")[0], body))
        return real(method, path, body, *a, **k)
    monkeypatch.setattr(ddc, "api", api)
    return sent


def test_create_box_sends_the_cap_in_the_sdl_and_a_runtime_limit_between_1_and_48(fake, monkeypatch):
    sent = spy(monkeypatch)
    spec = Spec(cpu=8, mem_gb=32, disk_gb=100, gpu=1, gpu_model="rtx4090", gpu_ram="24Gi", region="eu-west", max_usd_hr=0.9, hours=6)
    ddc.save_custom_profile("x1", spec.to_profile())
    ddc.create_box("box", "x1", 0, {}, limit_h=6)
    posts = [b for m, p, b in sent if (m, p) == ("POST", "/v1/deployments")]
    assert len(posts) == 1
    data = posts[0]["data"]
    assert isinstance(data["runtimeLimitHours"], int) and 1 <= data["runtimeLimitHours"] <= 48 and data["runtimeLimitHours"] == 6
    assert data["name"].startswith("ddc-box-x1-")
    sdl = yaml.safe_load(data["sdl"])
    res, placement = invariants(sdl, spec.to_profile())
    assert placement["pricing"]["box"]["amount"] == 1500 and placement["attributes"] == {"location-region": "eu-west"}
    assert res["gpu"]["attributes"]["vendor"]["nvidia"] == [{"model": "rtx4090", "ram": "24Gi"}]


@pytest.mark.parametrize("hours", [0, -5, 49, 1000, 0.5, "x"])
def test_create_box_refuses_an_impossible_runtime_before_any_request(fake, monkeypatch, hours):
    sent = spy(monkeypatch)
    ddc.save_custom_profile("x2", Spec(cpu=4, mem_gb=8, disk_gb=20, max_usd_hr=0.5).to_profile())
    with pytest.raises(ddc.DdcError):
        ddc.create_box("box", "x2", 0, {}, limit_h=hours)
    assert not [1 for m, p, b in sent if m == "POST"] and not fake.deployments
    assert json.dumps(sent, default=str) is not None
