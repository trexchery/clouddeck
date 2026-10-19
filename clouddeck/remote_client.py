#!/usr/bin/env python3
"""Stdlib-only client for the CloudDeck controller API.  This single file is what gets copied to a dev box as
`~/.local/bin/clouddeck-remote`; it must stay free of third-party and CloudDeck imports.

  clouddeck-remote status | instances | scale N [preset] | dispatch "cmd" [--on dev|workers] | checkpoint

URL defaults to $CLOUDDECK_URL or http://127.0.0.1:8765 (the end of the SSH reverse tunnel); the token comes from
$CLOUDDECK_TOKEN or ~/.config/clouddeck/token.  The token only unlocks what its scope allows (read: status; fleet: also scale within the
configured limits, dispatch to workers, checkpoint; admin: also dispatch to the dev box), expires, and can be revoked on the PC."""
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path


def token() -> str:
    t = os.environ.get("CLOUDDECK_TOKEN")
    if not t:
        p = Path.home() / ".config" / "clouddeck" / "token"
        t = p.read_text(encoding="utf-8").strip() if p.exists() else ""
    if not t:
        raise SystemExit("no token: set CLOUDDECK_TOKEN or create ~/.config/clouddeck/token (clouddeck serve --tunnel does this)")
    return t


def call(method: str, path: str, body: dict | None = None, url: str | None = None, timeout: int = 1900) -> dict:
    base = (url or os.environ.get("CLOUDDECK_URL") or "http://127.0.0.1:8765").rstrip("/")
    req = urllib.request.Request(base + path, method=method, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Authorization": "Bearer " + token(), "Content-Type": "application/json", "User-Agent": "clouddeck-remote"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        try:
            msg = json.loads(e.read().decode()).get("error", "")
        except Exception:
            msg = ""
        hint = " (the token expired or was replaced, e.g. the dev box was re-created: `clouddeck serve --tunnel` on the PC pushes a new one)" if e.code == 401 else ""
        raise SystemExit(f"controller said {e.code}: {msg or e.reason}{hint}")
    except urllib.error.URLError as e:
        raise SystemExit(f"cannot reach the controller at {base}: {e.reason} (is the tunnel up? run `clouddeck serve --tunnel` on the PC)")


def main(argv: list[str] | None = None, url: str | None = None, as_json: bool = False) -> int:
    a = list(sys.argv[1:] if argv is None else argv)
    as_json = as_json or "--json" in a
    a = [x for x in a if x != "--json"]
    if not a:
        print(__doc__)
        return 2
    act, rest = a[0], a[1:]
    if act in ("status", "instances"):
        r = call("GET", "/v1/status", url=url)
        if as_json:
            print(json.dumps(r, indent=1))
            return 0
        s = r["spend"]
        bal = "unknown" if s.get("balance") is None else f"${s['balance']:.2f}"
        print(f"{r['provider']}: balance {bal} | burn ${s['burn_usd_hr']:.3f}/h | today ${s['today_usd']:.2f}")
        for i in r["instances"]:
            print(f"  {i['role']:8} {i['state']:12} {i['cpu']:>3} vCPU ${i['usd_hr']:.3f}/h up {i['uptime_h']:4.1f}h  {i['name']}" + ("" if i["managed"] else "  (external)"))
        return 0
    if act == "scale" and rest:
        if not rest[0].isdigit():
            raise SystemExit("scale needs a whole number of workers, e.g. `clouddeck-remote scale 2`")
        r = call("POST", "/v1/scale", {"n": int(rest[0]), "preset": rest[1] if len(rest) > 1 else ""}, url=url)
        print(f"workers now: {r['workers']}")
        return 0
    if act == "dispatch" and rest:
        on = rest[rest.index("--on") + 1] if "--on" in rest else "workers"
        cmd = " ".join(x for i, x in enumerate(rest) if x != "--on" and (i == 0 or rest[i - 1] != "--on"))
        r = call("POST", "/v1/dispatch", {"command": cmd, "target": on}, url=url)
        bad = 0
        for res in r["results"]:
            for line in res["lines"]:
                print(f"{res['host']:>5} | {line}")
            bad += res["code"] != 0
        return 1 if bad else 0
    if act == "checkpoint":
        r = call("POST", "/v1/checkpoint", {}, url=url)
        print(f"checkpoint {r['created_at']} verified on the PC ({r['entries']} entries)")
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
