"""Shared helpers for the hardware tests: the saved public-API payloads (tests/fixtures/hardware) and a fake network for AkashProvider.

Nothing here (or in any hardware test) touches the network: `FakeNet` replaces `ddc.public_get` and `AkashProvider._post_public`, and
anything it has no payload for raises, exactly like an unreachable network."""
from __future__ import annotations

import copy
import json
import urllib.error
from pathlib import Path
from typing import Any

from clouddeck.engine import ddc
from clouddeck.providers.akash import AkashProvider
from clouddeck.providers.base import ProviderError

FIX = Path(__file__).parent / "fixtures" / "hardware"


def load(name: str) -> Any:
    return json.loads((FIX / name).read_text(encoding="utf-8"))


def providers() -> list[dict[str, Any]]:
    return copy.deepcopy(load("providers_sample.json"))


def future_providers() -> list[dict[str, Any]]:
    """Two synthetic online providers advertising what no list knows yet: an AMD GPU and a model that does not exist today."""
    base = [p for p in providers() if p["isOnline"] and p["isAudited"] and p.get("gpuModels")][0]
    out = []
    for i, (vendor, model, ram, iface) in enumerate((("amd", "mi300x", "192Gi", "OAM"), ("zzvendor", "zz9000", "48Gi", "PCIe"))):
        p = copy.deepcopy(base)
        p.update(owner=f"akash1FUTURE{i:03d}", name=f"future{i}.example", hostUri=f"https://future{i}.example:8443", gpuModels=[{"vendor": vendor, "model": model, "ram": ram, "interface": iface}])
        p["stats"]["gpu"] = {"active": 0, "available": 4, "pending": 0, "total": 4}
        p["attributes"] = [a for a in p["attributes"] if not a["key"].startswith("capabilities/gpu")]
        out.append(p)
    return out


def payloads(extra: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {"/v1/providers": providers() + (extra or []), "/v1/gpu-models": load("gpu_models_sample.json"), "/v1/placement-options": load("placement_options_sample.json"),
            "/v1/gpu-prices": load("gpu_prices_sample.json")}


class FakeNet:
    """Serves the saved payloads for the public GETs and a pricing / bid-screening POST; records every request; `down` makes everything fail."""

    def __init__(self, mp: Any, extra: list[dict[str, Any]] | None = None) -> None:
        self.data = payloads(extra)
        self.down = False
        self.fail: set[str] = set()
        self.gets: list[str] = []
        self.posts: list[tuple[str, Any]] = []
        self.screening: Any = {"providers": []}
        mp.setattr(ddc, "public_get", self.get)
        mp.setattr(AkashProvider, "_post_public", staticmethod(self.post))

    def get(self, path: str) -> Any:
        self.gets.append(path)
        if self.down or path in self.fail or path not in self.data:
            raise urllib.error.URLError(f"network unreachable (test): {path}")
        return copy.deepcopy(self.data[path])

    def post(self, path: str, body: Any) -> Any:
        self.posts.append((path, body))
        if self.down or path in self.fail:
            raise ProviderError(f"request to {path} failed: network unreachable (test)")
        if path == "/v1/pricing":
            return [{"akash": 123.54, "aws": 560.0, "gcp": 610.0, "azure": 650.0}]
        if path == "/v1/bid-screening":
            return copy.deepcopy(self.screening)
        raise AssertionError(f"unfaked POST {path}")

    def count(self, path: str) -> int:
        return self.gets.count(path)


def screening_reply(owners: list[str], gpus: int = 0, incidents: int = 0) -> dict[str, Any]:
    return {"providers": [{"owner": o, "hostUri": f"https://{o}.example:8443", "isAudited": True, "location": "eu-west", "organization": f"org-{o}", "availableGpus": gpus,
                           "incidents": [{"date": "2026-10-16", "hasOpenIncident": False, "incidentCount": incidents, "downtimeSeconds": 0}]} for o in owners]}
