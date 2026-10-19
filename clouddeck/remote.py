"""Controller API for remote terminals (e.g. the Akash dev box, or Akash Console's shell into it).

Design: the controller (and therefore the Akash API key, wallet material and SSH keys) stays on this PC.
  * The server listens on loopback only (enforced when it binds and again on every request).  A remote terminal reaches it through an
    SSH *reverse* tunnel that this PC opens to the dev box (`clouddeck serve --tunnel`), so nothing is exposed to the internet.
    THE TUNNEL GIVES EVERY PROCESS ON THE RENTED BOX A PATH TO THIS API: treat the box as untrusted, so a token only unlocks the minimum.
  * Every request needs a bearer token.  Only its SHA-256 is stored here (file mode 0600); the token itself is shown once and pushed to
    the box.  Tokens expire (default 24 h; the tunnel's token lives as long as the dev box can) and are compared in constant time.
  * A token has a scope: `read` (status) < `fleet` (also scale workers, run commands on the workers, checkpoint) < `admin` (also run
    commands on the dev box).  No scope can deploy a dev box, terminate anything, read secrets or change configuration.
  * Scaling is bounded by `remote_max_workers` (default 4), `max_workers` and the budget guard, and is refused outright when the budget
    guard is switched off.  Dispatch and checkpoint cannot add spend.  Mutating calls are serialized, rate limited, size limited
    (16 KiB body, 30 s socket timeout) and written to remote-audit.log.  Error replies never contain secrets, tracebacks or this PC's folder names, and `status` lists only the instances this tool created."""
from __future__ import annotations

import argparse
import hashlib
import hmac
import ipaddress
import json
import math
import os
import re
import secrets
import socket
import subprocess
import sys
import threading
import time
from collections import deque
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from .engine import ddc
from .providers.base import Provider, ProviderError, Spec
from .service import Service

SCOPES = {"read": 1, "fleet": 2, "admin": 3}
LABEL_RE = re.compile(r"^[A-Za-z0-9._-]{1,40}$")
MAX_BODY = 16 * 1024
SOCKET_TIMEOUT = 30                # seconds a client may stall while sending or reading
DEFAULT_TOKEN_HOURS = 24
LIMITS = {"auth_fail": (10, 60.0), "read": (120, 60.0), "write": (12, 60.0)}   # (requests, per seconds); the box is one client, so keyed by token
MAX_REPLY_LINES, MAX_LINE = 200, 2000
TOKENS = ddc.STATE_DIR / "clouddeck-tokens.json"
AUDIT = ddc.STATE_DIR / "remote-audit.log"


class Refused(ProviderError):
    """A limit or the spend guard says no (HTTP 403): the reason is meant to be read."""


LIMIT_ERRORS = getattr(ddc, "LimitExceeded", Refused)   # the controller's own "a limit says no"


# ---------------------------------------------------------------------------------------------- tokens
def _load() -> dict[str, dict[str, Any]]:
    db = ddc.read_json(TOKENS, {})   # unreadable or malformed: no tokens, so every request is refused
    if not isinstance(db, dict):
        return {}
    return {k: v for k, v in db.items() if isinstance(v, dict) and isinstance(v.get("sha256"), str)}


def _save(db: dict[str, dict[str, Any]]) -> None:
    """Write the token store owner-only (created 0600 on POSIX, so there is never a readable moment)."""
    TOKENS.parent.mkdir(parents=True, exist_ok=True)
    tmp = TOKENS.with_suffix(".json.tmp")
    with os.fdopen(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w", encoding="utf-8") as f:
        json.dump(db, f, indent=1)
    os.replace(tmp, TOKENS)
    if os.name != "nt":
        TOKENS.chmod(0o600)


def create_token(label: str, scope: str, hours: float | None = DEFAULT_TOKEN_HOURS) -> str:
    """A new token (the only time it exists in clear).  `hours` = lifetime; None or 0 = never expires."""
    if scope not in SCOPES:
        raise ProviderError(f"scope must be one of {', '.join(SCOPES)}")
    if not LABEL_RE.match(label):
        raise ProviderError("label: letters, digits, '.', '_' or '-', up to 40 characters")
    if hours is not None and not (math.isfinite(hours) and hours >= 0):
        raise ProviderError("expiry hours must be a number >= 0")
    tok = "cdk_" + secrets.token_urlsafe(32)
    db = _load()
    now = time.time()
    db[label] = {"sha256": hashlib.sha256(tok.encode()).hexdigest(), "scope": scope, "created": now}
    if hours:
        db[label]["expires"] = now + hours * 3600
    _save(db)
    return tok


def revoke_token(label: str) -> bool:
    db = _load()
    gone = db.pop(label, None) is not None
    _save(db)
    return gone


def authenticate(header: str) -> tuple[str, str] | None:
    """Authorization header -> (label, scope), or None.  Compares every stored hash in constant time; expired tokens never match."""
    if not header.startswith("Bearer ") or len(header) > 200:
        return None
    digest = hashlib.sha256(header[7:].strip().encode()).hexdigest()
    found = None
    now = time.time()
    for label, rec in _load().items():
        same = hmac.compare_digest(rec["sha256"], digest)
        live = rec.get("scope") in SCOPES and not (rec.get("expires") and now >= float(rec["expires"]))
        if same and live:
            found = (label, rec["scope"])
    return found


def redact(text: str, *extra: str) -> str:
    """Strip every configured secret value (and `extra`) from a message before it leaves this PC."""
    try:
        values = [v for v in ddc.load_secrets().values() if len(v) >= 6]
    except Exception:
        values = []
    for v in (*values, *(e for e in extra if len(e) >= 6)):
        text = text.replace(v, "***")
    return text


def scrub(text: str) -> str:
    """`redact` plus this PC's own paths (state, secrets, config, home): the box is untrusted and has no use for the user name or folder layout."""
    text = redact(text)
    spots = [(ddc.STATE_DIR, "<state dir>"), (ddc.SECRETS_DIR, "<secrets dir>"), (Path(ddc.CONFIG_FILE).parent if ddc.CONFIG_FILE else None, "<config dir>"),
             (Path.home(), "<home>")]
    flags = re.IGNORECASE if os.name == "nt" else 0
    for path, label in sorted(((p, lb) for p, lb in spots if p), key=lambda x: -len(str(x[0]))):   # longest first: the state dir before the home it lives in
        for form in {str(path), Path(path).as_posix()}:
            if len(form) > 3:
                text = re.sub(re.escape(form), label, text, flags=flags)
    return text


class RateLimiter:
    """Sliding window per key.  ponytail: in memory in one process; the API is a single loopback server, so that is enough."""

    def __init__(self) -> None:
        self.hits: dict[str, deque[float]] = {}
        self.lock = threading.Lock()

    def allow(self, key: str, kind: str) -> bool:
        limit, per = LIMITS[kind]
        now = time.monotonic()
        with self.lock:
            q = self.hits.setdefault(f"{kind}:{key}", deque())
            while q and q[0] <= now - per:
                q.popleft()
            if len(q) >= limit:
                return False
            q.append(now)
            return True


def is_loopback(addr: str) -> bool:
    try:
        return ipaddress.ip_address(addr.split("%")[0]).is_loopback
    except ValueError:
        return False


# ---------------------------------------------------------------------------------------------- server
class Api:
    def __init__(self, provider: Provider):
        self.provider = provider
        self.service = Service(provider)
        self.write_lock = threading.Lock()
        self.limiter = RateLimiter()

    def status(self) -> dict[str, Any]:
        s = self.provider.spend()
        insts = self.provider.instances(False)
        insts = [i for i in insts if i.managed]   # other deployments on the account are the owner's business, not the untrusted box's
        return {"provider": self.provider.id, "spend": {"balance": s.balance, "burn_usd_hr": s.burn_hr, "today_usd": s.today, "closed_total_usd": s.closed_total},
                "instances": [{"id": i.id, "name": i.name, "role": i.role, "state": i.state, "cpu": i.cpu, "mem_gb": i.mem_gb, "gpu": i.gpu, "usd_hr": i.usd_hr,
                               "uptime_h": i.uptime_h, "managed": i.managed} for i in insts]}

    @staticmethod
    def worker_cap() -> int:
        """The most workers a remote caller may ask for: remote_max_workers AND the controller's max_workers, whichever is lower."""
        caps = []
        for key, default in (("remote_max_workers", 4), ("max_workers", 4)):
            try:
                caps.append(int(ddc.CFG.get(key, default)))
            except (TypeError, ValueError):
                caps.append(0)   # a malformed limit allows nothing
        return max(0, min(caps))

    def scale(self, body: dict[str, Any]) -> dict[str, Any]:
        n = body.get("n", -1)
        cap = self.worker_cap()
        if isinstance(n, bool) or not isinstance(n, int) or not 0 <= n <= cap:
            raise ProviderError(f"n must be a whole number 0–{cap} (remote_max_workers / max_workers in config.toml)")
        presets = self.provider.presets()
        workers = [w for w in self.provider.instances(False) if w.role == "worker" and w.managed]
        name = body.get("preset") or next((w.meta.get("profile") for w in workers if w.meta.get("profile") in presets), "") or "32"
        if not isinstance(name, str) or name not in presets:
            raise ProviderError(f"unknown preset (have: {', '.join(presets)})")
        spec: Spec = presets[name].with_(role="worker", hours=int(ddc.CFG.get("worker_runtime_hours", 4)))
        if n > len(workers):   # only growing can add spend
            v = self.provider.guard(spec, n - len(workers))
            if not v.ok:
                raise Refused(v.text)
            if v.guard_on is False:
                raise Refused("the budget guard is off (budget_guard = false): a remote terminal cannot add workers without it")
        got = self.provider.scale(n, spec, lambda m: None)
        return {"workers": len(got), "preset": name}

    def dispatch(self, body: dict[str, Any], scope: str = "fleet") -> dict[str, Any]:
        cmd = body.get("command")
        if not isinstance(cmd, str) or not cmd.strip() or len(cmd) > 4000 or "\x00" in cmd:
            raise ProviderError("command must be 1–4000 characters")
        target = body.get("target", "workers")
        if target not in ("workers", "dev"):
            raise ProviderError("target must be 'workers' or 'dev'")
        if target == "dev" and SCOPES[scope] < SCOPES["admin"]:
            raise Refused(f"running commands on the dev box needs scope 'admin' (this token has '{scope}')")
        cwd = body.get("cwd") or ""
        if not isinstance(cwd, str) or len(cwd) > 300 or "\x00" in cwd:
            raise ProviderError("cwd must be a path of up to 300 characters")
        nodes = [i for i in self.provider.instances(False) if i.reachable and i.role == ("dev" if target == "dev" else "worker")]
        if not nodes:
            raise ProviderError(f"no running {target} to run on")
        res = self.service.dispatch(cmd.strip(), nodes, lambda h, l: None, cwd=cwd)
        return {"results": [{"host": r.host, "code": r.code, "lines": [x[:MAX_LINE] for x in r.lines[-MAX_REPLY_LINES:]]} for r in res]}

    def checkpoint(self) -> dict[str, Any]:
        c = self.provider.checkpoint(lambda m: None)
        return {"created_at": c.created_at, "entries": sum(c.entries.values())}


def make_handler(api: Api):
    class Handler(BaseHTTPRequestHandler):
        server_version = "CloudDeck"
        protocol_version = "HTTP/1.1"
        timeout = SOCKET_TIMEOUT        # a client that stalls mid-request cannot hold a thread

        def log_message(self, *a: Any) -> None:   # the audit log replaces the default stderr noise
            pass

        def _send(self, code: int, obj: dict[str, Any], close: bool = False) -> None:
            data = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            if close:
                self.send_header("Connection", "close")
                self.close_connection = True
            self.end_headers()
            self.wfile.write(data)

        def _audit(self, who: str, code: int, note: str = "") -> None:
            try:
                AUDIT.parent.mkdir(parents=True, exist_ok=True)
                def one_line(t: str) -> str:   # no control character can start a second, forged log line
                    return re.sub(r"[^\x21-\x7e]", "?", t)
                with AUDIT.open("a", encoding="utf-8") as f:
                    f.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {one_line(who)} {self.command} {one_line(self.path[:120])} -> {code} from {self.client_address[0]}"
                            + (f" ({one_line(redact(note))})" if note else "") + "\n")
            except OSError:
                pass

        def _handle(self) -> None:
            if not is_loopback(self.client_address[0]):   # belt and braces: the socket is loopback-bound anyway
                self._audit("anonymous", 403, "non-loopback peer")
                return self._send(403, {"error": "loopback only"}, close=True)
            host = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]").lower()
            if host not in ("127.0.0.1", "localhost", "::1"):   # a web page cannot reach this API through DNS rebinding
                self._audit("anonymous", 421, "bad Host header")
                return self._send(421, {"error": "unexpected Host header"}, close=True)
            auth = authenticate(self.headers.get("Authorization", ""))
            if not auth:
                if not api.limiter.allow("anonymous", "auth_fail"):
                    self._audit("anonymous", 429)
                    return self._send(429, {"error": "too many failed attempts; wait a minute"}, close=True)
                self._audit("anonymous", 401)
                return self._send(401, {"error": "missing, invalid or expired token"})
            label, scope = auth
            need = "read" if self.command == "GET" else "fleet"
            if SCOPES[scope] < SCOPES[need]:
                self._audit(label, 403)
                return self._send(403, {"error": f"token '{label}' has scope '{scope}'; this needs '{need}'"})
            if not api.limiter.allow(label, "read" if self.command == "GET" else "write"):
                self._audit(label, 429)
                return self._send(429, {"error": "rate limit reached; slow down"}, close=True)
            try:
                if self.headers.get("Transfer-Encoding"):
                    self._audit(label, 400, "chunked body")
                    return self._send(400, {"error": "send a Content-Length body"}, close=True)
                n = int(self.headers.get("Content-Length") or 0)
                if not 0 <= n <= MAX_BODY:
                    self._audit(label, 413)
                    return self._send(413, {"error": f"body must be 0–{MAX_BODY} bytes"}, close=True)   # the body is never read: drop the connection
                body = json.loads(self.rfile.read(n) or b"{}") if n else {}
                if not isinstance(body, dict):
                    raise ValueError("the request body must be a JSON object")
                route = (self.command, self.path.split("?")[0])
                if route == ("GET", "/v1/status"):
                    out = api.status()
                elif route in (("POST", "/v1/scale"), ("POST", "/v1/dispatch"), ("POST", "/v1/checkpoint")):
                    if not api.write_lock.acquire(blocking=False):
                        self._audit(label, 409)
                        return self._send(409, {"error": "another remote operation is running"})
                    try:
                        out = {"/v1/scale": api.scale, "/v1/dispatch": lambda b: api.dispatch(b, scope), "/v1/checkpoint": lambda b: api.checkpoint()}[route[1]](body)
                    finally:
                        api.write_lock.release()
                else:
                    self._audit(label, 404)
                    return self._send(404, {"error": "no such endpoint"})
            except (Refused, LIMIT_ERRORS) as e:
                self._audit(label, 403, str(e)[:120])
                return self._send(403, {"error": scrub(str(e))[:400]})
            except (ProviderError, ddc.DdcError) as e:   # our own, human-readable errors
                self._audit(label, 400, str(e)[:120])
                return self._send(400, {"error": scrub(str(e))[:400]})
            except (ValueError, KeyError, TypeError):   # includes bad JSON
                self._audit(label, 400, "malformed request")
                return self._send(400, {"error": "malformed request"})
            except Exception as e:   # never leak a traceback or an unexpected message to the remote side
                self._audit(label, 500, type(e).__name__)
                return self._send(500, {"error": "internal error (see remote-audit.log on the controller PC)"})
            self._audit(label, 200)
            self._send(200, out)

        do_GET = do_POST = _handle

    return Handler


class Server(ThreadingHTTPServer):
    # On Windows SO_REUSEADDR lets a second process bind a port that is already being served (the first binder wins every connection), so another
    # local program could sit in front of `clouddeck serve` and read the bearer tokens.  There the port is taken exclusively instead.
    allow_reuse_address = os.name != "nt"

    def server_bind(self) -> None:
        if os.name == "nt":
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def make_server(provider: Provider, port: int = 8765) -> ThreadingHTTPServer:
    """Always bound to loopback: remote terminals come in through the SSH reverse tunnel.  There is deliberately no way to pick another address."""
    try:
        srv = Server(("127.0.0.1", port), make_handler(Api(provider)))
    except OSError as e:
        raise ProviderError(f"cannot listen on 127.0.0.1:{port} ({e.strerror or e}): is another `clouddeck serve` running? Pick another --port") from e
    if not is_loopback(srv.server_address[0]):   # cannot happen with the literal above; the check keeps a future edit honest
        srv.server_close()
        raise ProviderError(f"refusing to serve on {srv.server_address[0]}: the controller API is loopback only")
    srv.daemon_threads = True
    return srv


# ---------------------------------------------------------------------------------------------- tunnel to the dev box
def push_client(provider: Provider, dev: Any, token: str) -> None:
    client = (Path(__file__).with_name("remote_client.py")).read_bytes().replace(b"\r\n", b"\n")
    if provider.id != "akash":
        raise ProviderError("pushing the client is implemented for Akash")
    d = {"host": dev.host, "port": dev.port}
    ddc.put_file(d, "/home/dev/.config/clouddeck/token", token.encode(), mode="600")
    ddc.put_file(d, "/home/dev/.local/bin/clouddeck-remote", client, mode="755")


def run_tunnel(provider: Provider, port: int, stop: threading.Event, on_new_dev: Callable[[Any], None] | None = None) -> None:
    """Keep `ssh -N -R 127.0.0.1:PORT:127.0.0.1:PORT dev@box` alive; the box can then reach this PC's loopback API.
    `on_new_dev(dev)` runs before the tunnel to every dev box not seen before (first start, or a box that was re-created while `serve` kept
    running): that is where it gets its fresh token.  If it fails, the tunnel is not opened and it is tried again."""
    pushed = None
    while not stop.is_set():
        dev = next((i for i in provider.instances(True) if i.role == "dev" and i.reachable), None)
        if not dev:
            print("waiting for a running dev box…", flush=True)
            stop.wait(15)
            continue
        if on_new_dev and dev.id != pushed:
            try:
                on_new_dev(dev)
            except Exception as e:
                print(f"could not give the dev box its token ({scrub(str(e))[:200]}); retrying in 15 s", file=sys.stderr, flush=True)
                stop.wait(15)
                continue
            pushed = dev.id
        base = provider.ssh_command(dev)   # [..., "dev@host"]: options must go before the destination
        argv = base[:-1] + ["-N", "-o", "ExitOnForwardFailure=yes", "-R", f"127.0.0.1:{port}:127.0.0.1:{port}", base[-1]]
        print(f"tunnel up: box:127.0.0.1:{port} → this PC (Ctrl+C to stop)", flush=True)
        p = subprocess.Popen(argv, creationflags=ddc.NOWIN)
        while p.poll() is None and not stop.is_set():
            stop.wait(2)
        if p.poll() is None:
            p.terminate()
        if not stop.is_set():
            print("tunnel dropped; retrying in 5 s", flush=True)
            stop.wait(5)


# ---------------------------------------------------------------------------------------------- CLI
def serve_cli(a: argparse.Namespace) -> int:
    from . import providers
    if a.list_tokens:
        for label, rec in _load().items():
            exp = f"expires {time.strftime('%Y-%m-%d %H:%M', time.localtime(rec['expires']))}" if rec.get("expires") else "never expires"
            print(f"{label:20} scope={rec.get('scope', '?'):6} created {time.strftime('%Y-%m-%d %H:%M', time.localtime(rec.get('created', 0)))}  {exp}")
        return 0
    if a.revoke:
        print("revoked" if revoke_token(a.revoke) else "no such token")
        return 0
    hours = DEFAULT_TOKEN_HOURS if a.expires_hours is None else a.expires_hours
    if a.new_token:
        print(create_token(a.new_token, a.scope, hours))
        print(f"(shown once; scope {a.scope}, " + (f"valid {hours:g} h" if hours else "never expires") + f". Revoke with: clouddeck serve --revoke {a.new_token})", file=sys.stderr)
        return 0
    provider = providers.get(a.provider or "akash")
    if a.tunnel and provider.id != "akash":
        raise ProviderError("--tunnel is implemented for Akash only")
    srv = make_server(provider, a.port)
    stop = threading.Event()
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    print(f"CloudDeck controller API on http://127.0.0.1:{a.port}  (loopback only; audit log: {AUDIT})")
    if a.tunnel:
        label = "devbox"
        life = a.expires_hours if a.expires_hours is not None else float(ddc.CFG.get("max_runtime_hours", 8)) + 1   # the dev box cannot outlive its platform limit

        def give_token(dev: Any) -> None:
            push_client(provider, dev, create_token(label, a.scope, life))   # replaces the previous devbox token, so an old box's token dies with it
            print(f"pushed a '{a.scope}' token (" + (f"valid {life:g} h" if life else "never expires") + f") and clouddeck-remote to the dev box {dev.name} (never any key or Akash credential)", flush=True)
        threading.Thread(target=run_tunnel, args=(provider, a.port, stop, give_token), daemon=True).start()
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        stop.set()
        srv.shutdown()
    return 0


def client_cli(a: argparse.Namespace) -> int:
    from . import remote_client
    argv = [a.action, *a.arg]
    return remote_client.main(argv, url=a.url, as_json=a.json)
