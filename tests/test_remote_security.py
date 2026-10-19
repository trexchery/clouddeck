"""The controller API is reachable from a rented, untrusted box through the reverse tunnel: these tests pin down what that box can and cannot do.
Offline: the simulated provider, a loopback server on a free port, fake secret values."""
import argparse
import hashlib
import http.client
import json
import os
import socket
import threading
import time

import pytest

from clouddeck import providers, remote
from clouddeck.engine import ddc
from clouddeck.providers.base import ProviderError

FAKE_SECRET = "sk-fake-secret-value-123456"


@pytest.fixture
def api():
    p = providers.get("demo")
    srv = remote.make_server(p, 0)
    threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()
    yield p, srv.server_address[1]
    srv.shutdown()
    srv.server_close()


def call(port, path, token=None, method="GET", body=None, headers=None, raw=None):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=20)
    h = dict(headers or {})
    if token:
        h["Authorization"] = f"Bearer {token}"
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    try:
        c.request(method, path, body=data, headers=h)
        r = c.getresponse()
        return r.status, json.loads(r.read() or b"{}")
    finally:
        c.close()


# ------------------------------------------------------------------ loopback only
def test_is_loopback_and_the_server_cannot_be_bound_elsewhere(monkeypatch):
    assert remote.is_loopback("127.0.0.1") and remote.is_loopback("::1")
    for bad in ("0.0.0.0", "192.168.1.5", "10.0.0.2", "::", "example.com", ""):
        assert not remote.is_loopback(bad), bad
    monkeypatch.setattr(remote, "is_loopback", lambda a: False)
    with pytest.raises(ProviderError, match="loopback only"):
        remote.make_server(providers.get("demo"), 0)


def test_a_non_loopback_peer_or_a_foreign_host_header_is_refused_even_with_a_valid_token(api, monkeypatch):
    p, port = api
    tok = remote.create_token("t", "read")
    assert call(port, "/v1/status", tok)[0] == 200
    assert call(port, "/v1/status", tok, headers={"Host": "evil.example"})[0] == 421          # DNS rebinding from a web page
    assert call(port, "/v1/status", tok, headers={"Host": f"localhost:{port}"})[0] == 200
    monkeypatch.setattr(remote, "is_loopback", lambda a: False)
    assert call(port, "/v1/status", tok)[0] == 403


# ------------------------------------------------------------------ tokens
def test_token_store_holds_only_hashes_is_private_and_tokens_expire(api):
    p, port = api
    tok = remote.create_token("laptop", "fleet", hours=1)
    text = remote.TOKENS.read_text()
    assert tok not in text and hashlib.sha256(tok.encode()).hexdigest() in text
    if os.name != "nt":
        assert remote.TOKENS.stat().st_mode & 0o777 == 0o600
    rec = json.loads(text)["laptop"]
    assert rec["expires"] - rec["created"] == pytest.approx(3600, abs=2)
    assert call(port, "/v1/status", tok)[0] == 200
    db = json.loads(text)
    db["laptop"]["expires"] = time.time() - 1                                                # now in the past
    remote.TOKENS.write_text(json.dumps(db))
    assert call(port, "/v1/status", tok)[0] == 401
    forever = remote.create_token("old", "read", hours=0)
    assert "expires" not in json.loads(remote.TOKENS.read_text())["old"] and call(port, "/v1/status", forever)[0] == 200


def test_a_token_store_from_before_expiry_existed_keeps_working(api):
    p, port = api
    remote.TOKENS.write_text(json.dumps({"legacy": {"sha256": hashlib.sha256(b"cdk_legacy").hexdigest(), "scope": "fleet", "created": 1.0}}))
    assert call(port, "/v1/status", "cdk_legacy")[0] == 200


@pytest.mark.parametrize("junk", ["not json", "[1, 2]", '{"a": 5}', '{"a": {"scope": "read"}}', '{"a": {"sha256": 1}}'])
def test_a_damaged_token_store_refuses_everyone_instead_of_crashing(api, junk):
    p, port = api
    remote.TOKENS.write_text(junk)
    assert call(port, "/v1/status", "cdk_anything")[0] == 401
    assert remote.authenticate("Bearer cdk_anything") is None


def test_an_unknown_scope_in_the_store_never_authenticates(api):
    p, port = api
    remote.TOKENS.write_text(json.dumps({"x": {"sha256": hashlib.sha256(b"cdk_x").hexdigest(), "scope": "root", "created": 1.0}}))
    assert call(port, "/v1/status", "cdk_x")[0] == 401


def test_token_comparison_is_constant_time_over_every_stored_token(monkeypatch):
    toks = [remote.create_token(f"t{i}", "read") for i in range(3)]
    seen = []
    real = remote.hmac.compare_digest
    monkeypatch.setattr(remote.hmac, "compare_digest", lambda a, b: (seen.append(1), real(a, b))[1])
    assert remote.authenticate("Bearer " + toks[0])[0] == "t0"                              # the first token matches ...
    assert len(seen) == 3                                                                   # ... and the others are still compared
    seen.clear()
    assert remote.authenticate("Bearer cdk_nope") is None and len(seen) == 3
    assert remote.authenticate("Basic abc") is None and remote.authenticate("Bearer " + "x" * 400) is None


def test_label_scope_and_expiry_are_validated():
    for bad in ("", "a b", "x" * 41, "a\nb", "../x"):
        with pytest.raises(ProviderError):
            remote.create_token(bad, "read")
    with pytest.raises(ProviderError):
        remote.create_token("ok", "root")
    with pytest.raises(ProviderError):
        remote.create_token("ok", "read", hours=float("nan"))
    with pytest.raises(ProviderError):
        remote.create_token("ok", "read", hours=-1)


def test_new_token_cli_prints_the_token_once_and_lists_without_it(capsys):
    a = argparse.Namespace(list_tokens=False, revoke=None, new_token="box", scope="admin", expires_hours=2.0, provider=None, port=0, tunnel=False)
    assert remote.serve_cli(a) == 0
    cap = capsys.readouterr()
    tok = cap.out.strip()
    assert tok.startswith("cdk_") and "valid 2 h" in cap.err and tok not in cap.err
    rec = json.loads(remote.TOKENS.read_text())["box"]
    assert rec["scope"] == "admin" and rec["expires"] > time.time()
    assert remote.serve_cli(argparse.Namespace(list_tokens=True, revoke=None, new_token=None)) == 0
    listing = capsys.readouterr().out
    assert "box" in listing and "admin" in listing and "expires" in listing and tok not in listing
    a.expires_hours = None                                                                  # default: 24 h, never "forever" by accident
    remote.serve_cli(a)
    assert json.loads(remote.TOKENS.read_text())["box"]["expires"] - time.time() == pytest.approx(24 * 3600, abs=60)


# ------------------------------------------------------------------ scopes: the box gets the minimum
def test_fleet_cannot_run_commands_on_the_dev_box_but_admin_can(api):
    p, port = api
    read, fleet, admin = (remote.create_token(n, n) for n in ("read", "fleet", "admin"))
    assert call(port, "/v1/dispatch", read, "POST", {"command": "id"})[0] == 403
    code, out = call(port, "/v1/dispatch", fleet, "POST", {"command": "id", "target": "dev"})
    assert code == 403 and "admin" in out["error"] and not [c for c in p.calls if c[0] == "stream"]
    code, out = call(port, "/v1/dispatch", fleet, "POST", {"command": "id"})
    assert code == 200 and len(out["results"]) == 3
    code, out = call(port, "/v1/dispatch", admin, "POST", {"command": "id", "target": "dev"})
    assert code == 200 and len(out["results"]) == 1


@pytest.mark.parametrize("body", [{"command": 5}, {"command": ""}, {"command": "x" * 4001}, {"command": "a\x00b"}, {"command": "ok", "target": "everything"},
                                  {"command": "ok", "cwd": 5}, {"command": "ok", "cwd": "x" * 301}, {"command": ["a"]}])
def test_dispatch_input_is_validated(api, body):
    p, port = api
    fleet = remote.create_token("f", "fleet")
    assert call(port, "/v1/dispatch", fleet, "POST", body)[0] == 400
    assert not [c for c in p.calls if c[0] == "stream"]


def test_dispatch_with_no_running_node_is_a_clear_error(api):
    p, port = api
    fleet = remote.create_token("f", "fleet")
    for w in [i for i in p.instances() if i.role == "worker"]:
        p.terminate(w.id)
    code, out = call(port, "/v1/dispatch", fleet, "POST", {"command": "id"})
    assert code == 400 and "no running workers" in out["error"]


# ------------------------------------------------------------------ scaling is bounded by the limits and the guard
def test_scale_cap_is_the_lower_of_remote_max_workers_and_max_workers(api):
    p, port = api
    fleet = remote.create_token("f", "fleet")
    ddc.CFG["remote_max_workers"], ddc.CFG["max_workers"] = 6, 5
    assert remote.Api.worker_cap() == 5
    assert call(port, "/v1/scale", fleet, "POST", {"n": 6})[0] == 400
    ddc.CFG["remote_max_workers"] = 4
    assert remote.Api.worker_cap() == 4
    ddc.CFG.pop("remote_max_workers")
    ddc.CFG.pop("max_workers")
    assert remote.Api.worker_cap() == 4                                                     # defaults are small, never 16
    ddc.CFG["max_workers"] = "lots"
    assert remote.Api.worker_cap() == 0                                                     # a malformed limit allows nothing
    assert call(port, "/v1/scale", fleet, "POST", {"n": 1})[0] == 400
    assert not [c for c in p.calls if c[0] == "scale"]


@pytest.mark.parametrize("n", [-1, 1.5, "2", True, None, [2]])
def test_scale_n_must_be_a_whole_number(api, n):
    p, port = api
    assert call(port, "/v1/scale", remote.create_token("f", "fleet"), "POST", {"n": n})[0] == 400
    assert not [c for c in p.calls if c[0] == "scale"]


def test_scale_up_is_refused_by_the_budget_guard_and_when_the_guard_is_off(api):
    p, port = api
    fleet = remote.create_token("f", "fleet")
    p.limits["max_workers"] = 4
    p.limits["budget_usd"] = 3.0                                                            # below what is already committed
    code, out = call(port, "/v1/scale", fleet, "POST", {"n": 4})
    assert code == 403 and "would exceed budget" in out["error"]
    p.limits["budget_usd"] = 85.0
    p.limits["max_total_leases"] = 4
    code, out = call(port, "/v1/scale", fleet, "POST", {"n": 4})
    assert code == 403 and "max_total_leases" in out["error"]
    p.limits["max_total_leases"] = 8
    p.limits["budget_guard"] = False
    code, out = call(port, "/v1/scale", fleet, "POST", {"n": 4})
    assert code == 403 and "budget guard is off" in out["error"]
    assert not [c for c in p.calls if c[0] == "scale"]                                      # every refusal happened before the provider was asked
    assert call(port, "/v1/scale", fleet, "POST", {"n": 1})[0] == 200                       # shrinking never needs the guard
    p.limits["budget_guard"] = True
    assert call(port, "/v1/scale", fleet, "POST", {"n": 4})[0] == 200


def test_scale_preset_must_be_a_known_preset_name(api):
    p, port = api
    fleet = remote.create_token("f", "fleet")
    for preset in ("nope", 5, ["32"]):
        assert call(port, "/v1/scale", fleet, "POST", {"n": 4, "preset": preset})[0] == 400
    assert not [c for c in p.calls if c[0] == "scale"]


# ------------------------------------------------------------------ request size, timeouts, rate limits
def test_oversized_bad_and_chunked_bodies_are_rejected(api):
    p, port = api
    fleet = remote.create_token("f", "fleet")
    code, out = call(port, "/v1/dispatch", fleet, "POST", raw=b"{" + b" " * remote.MAX_BODY)
    assert code == 413
    assert call(port, "/v1/dispatch", fleet, "POST", raw=b"not json")[0] == 400
    assert call(port, "/v1/dispatch", fleet, "POST", raw=b"[1,2]")[0] == 400
    assert call(port, "/v1/dispatch", fleet, "POST", headers={"Content-Length": "-5"})[0] == 413       # a negative length would block a read forever
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    c.putrequest("POST", "/v1/dispatch")
    c.putheader("Authorization", "Bearer " + fleet)
    c.putheader("Transfer-Encoding", "chunked")
    c.endheaders()
    r = c.getresponse()
    assert r.status == 400
    c.close()
    assert not [x for x in p.calls if x[0] == "stream"]


def test_a_stalled_client_cannot_hold_a_connection_open(monkeypatch):
    monkeypatch.setattr(remote, "SOCKET_TIMEOUT", 0.4)
    srv = remote.make_server(providers.get("demo"), 0)
    threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()
    try:
        s = socket.create_connection(("127.0.0.1", srv.server_address[1]), timeout=10)
        s.sendall(b"POST /v1/scale HTTP/1.1\r\nHost: 127.0.0.1\r\nContent-Le")        # never finishes the request
        t0 = time.time()
        assert s.recv(100) == b"" and time.time() - t0 < 5                           # the server hung up on us
        s.close()
    finally:
        srv.shutdown()
        srv.server_close()


def test_failed_logins_and_writes_are_rate_limited_per_token(api, monkeypatch):
    p, port = api
    monkeypatch.setitem(remote.LIMITS, "auth_fail", (3, 60.0))
    monkeypatch.setitem(remote.LIMITS, "write", (2, 60.0))
    good = remote.create_token("g", "fleet")
    assert [call(port, "/v1/status", "cdk_bad")[0] for _ in range(5)] == [401, 401, 401, 429, 429]
    assert call(port, "/v1/status", good)[0] == 200                                         # a flood of bad guesses does not lock out the real token
    assert [call(port, "/v1/dispatch", good, "POST", {"command": "id"})[0] for _ in range(3)] == [200, 200, 429]
    assert call(port, "/v1/status", good)[0] == 200                                         # reads have their own budget


# ------------------------------------------------------------------ no secret echo
def test_errors_never_carry_secrets_or_tracebacks(api, monkeypatch):
    p, port = api
    fleet = remote.create_token("f", "fleet")
    monkeypatch.setattr(ddc, "load_secrets", lambda: {"AKASH_API_KEY": FAKE_SECRET, "short": "abc"})

    def leaky(*a, **k):
        raise ProviderError(f"ssh failed: key {FAKE_SECRET} rejected")
    monkeypatch.setattr(type(p), "checkpoint", leaky)
    code, out = call(port, "/v1/checkpoint", fleet, "POST", {})
    assert code == 400 and FAKE_SECRET not in json.dumps(out) and "***" in out["error"]

    def bug(*a, **k):
        raise RuntimeError(f"internal path /home/user/.config/secrets.env {FAKE_SECRET}")
    monkeypatch.setattr(type(p), "checkpoint", bug)
    code, out = call(port, "/v1/checkpoint", fleet, "POST", {})
    assert code == 500 and out == {"error": "internal error (see remote-audit.log on the controller PC)"}
    audit = remote.AUDIT.read_text()
    assert "RuntimeError" in audit and FAKE_SECRET not in audit and fleet not in audit and "Traceback" not in audit
    assert call(port, "/v1/scale", fleet, "POST", {"n": {"x": 1}})[0] == 400


def test_the_token_never_appears_in_replies_the_audit_log_or_the_store(api):
    p, port = api
    tok = remote.create_token("secretive", "fleet")
    replies = [call(port, "/v1/status", tok), call(port, "/v1/nothing", tok, "POST", {}), call(port, "/v1/status", tok + "x")]
    assert tok not in json.dumps(replies) and tok not in remote.AUDIT.read_text() and tok not in remote.TOKENS.read_text()
    assert " secretive GET /v1/status -> 200 " in remote.AUDIT.read_text()


def test_audit_log_lines_cannot_be_forged_through_the_request_path(api):
    p, port = api
    tok = remote.create_token("a", "read")
    s = socket.create_connection(("127.0.0.1", port), timeout=10)
    s.sendall(b"GET /v1/status\x01\x1b[2J%0aforged HTTP/1.1\r\nHost: 127.0.0.1\r\nAuthorization: Bearer " + tok.encode() + b"\r\nConnection: close\r\n\r\n")
    while s.recv(4096):
        pass
    s.close()
    line = remote.AUDIT.read_text().splitlines()[-1]
    assert "\x01" not in line and "\x1b" not in line and "/v1/status??[2J%0aforged" in line


# ------------------------------------------------------------------ the tunnel itself
def test_the_reverse_tunnel_only_opens_a_loopback_to_loopback_forward(monkeypatch):
    p = providers.get("demo")
    argv_seen = []

    class FakeProc:
        def __init__(self, argv, **kw):
            argv_seen.append(argv)
            stop.set()

        def poll(self):
            return 0

        def terminate(self):
            pass
    stop = threading.Event()
    monkeypatch.setattr(remote.subprocess, "Popen", FakeProc)
    remote.run_tunnel(p, 8765, stop)
    argv = argv_seen[0]
    assert argv[argv.index("-R") + 1] == "127.0.0.1:8765:127.0.0.1:8765" and "ExitOnForwardFailure=yes" in argv and "-N" in argv
    assert argv[-1] == p.ssh_command(p.instances()[0])[-1] and argv.index("-R") < len(argv) - 1       # options come before the destination


# ------------------------------------------------------------------ what the untrusted box learns from replies
def test_status_lists_only_what_this_tool_created(api):
    p, port = api
    assert any(not i.managed for i in p.instances(True))                                    # the account has someone else's deployment ...
    code, out = call(port, "/v1/status", remote.create_token("r", "read"))
    assert code == 200 and out["instances"] and all(i["managed"] for i in out["instances"])  # ... and the box never hears of it


def test_error_replies_do_not_carry_this_pcs_folders(api, monkeypatch):
    p, port = api
    fleet = remote.create_token("f", "fleet")
    state = str(ddc.STATE_DIR)

    def damaged(*a, **k):
        raise ddc.DdcError(f"{ddc.STATE_FILE} is unusable. Your data is kept in {state}/state.json.bak and {ddc.SECRETS_DIR}")
    monkeypatch.setattr(type(p), "checkpoint", damaged)
    code, out = call(port, "/v1/checkpoint", fleet, "POST", {})
    assert code == 400 and state not in out["error"] and str(ddc.SECRETS_DIR) not in out["error"] and "<state dir>" in out["error"]
    home = remote.Path.home()
    assert str(home) not in remote.scrub(f"{home}/x and {home.as_posix()}/y")
    assert remote.scrub("nothing to hide") == "nothing to hide"

    def refused(*a, **k):
        raise remote.Refused(f"limit in {ddc.CONFIG_FILE}")
    monkeypatch.setattr(type(p), "checkpoint", refused)
    code, out = call(port, "/v1/checkpoint", fleet, "POST", {})
    assert code == 403 and str(ddc.CONFIG_FILE.parent) not in out["error"] and "<config dir>" in out["error"]


def test_a_multi_line_error_cannot_forge_an_audit_line(api, monkeypatch):
    p, port = api
    fleet = remote.create_token("f", "fleet")

    def nasty(*a, **k):
        raise ProviderError("first\n2099-01-01T00:00:00 admin POST /v1/scale -> 200 from 127.0.0.1\x1b[2J")
    monkeypatch.setattr(type(p), "checkpoint", nasty)
    assert call(port, "/v1/checkpoint", fleet, "POST", {})[0] == 400
    lines = remote.AUDIT.read_text().splitlines()
    assert len([ln for ln in lines if "/v1/checkpoint" in ln]) == 1 and not any(ln.startswith("2099") for ln in lines)
    assert "\x1b" not in remote.AUDIT.read_text()


# ------------------------------------------------------------------ the port cannot be shared with another local program
def test_a_second_server_cannot_take_over_the_same_port(api):
    p, port = api
    with pytest.raises(ProviderError, match="another `clouddeck serve`"):               # on Windows plain SO_REUSEADDR would let it bind and steal connections
        remote.make_server(p, port)
    c = socket.create_connection(("127.0.0.1", port), timeout=10)                         # the first server still owns it
    c.close()


def test_the_listening_socket_is_exclusive_on_windows_and_reusable_on_posix():
    srv = remote.make_server(providers.get("demo"), 0)
    try:
        sock = srv.socket
        if os.name == "nt":
            assert sock.getsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE) == 1      # nobody else can bind this port, not even with SO_REUSEADDR
            assert not remote.Server.allow_reuse_address
        else:
            assert sock.getsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR) and remote.Server.allow_reuse_address   # POSIX: restart without TIME_WAIT trouble, still one listener
    finally:
        srv.server_close()


# ------------------------------------------------------------------ the dev box gets a token again when it is re-created while `serve` keeps running
class _Stop:
    """A stop event that never really waits: `wait` returns at once and the loop ends after `rounds` tunnel launches."""

    def __init__(self, rounds):
        self.rounds, self.launched, self.waits = rounds, 0, []

    def is_set(self):
        return self.launched >= self.rounds

    def wait(self, t=None):
        self.waits.append(t)
        return self.is_set()

    def set(self):
        self.launched = self.rounds


def _tunnel_harness(monkeypatch, dev_ids, rounds):
    """run_tunnel against the demo provider whose dev box has the id given for each round; every launched ssh 'drops' at once."""
    from dataclasses import replace
    p = providers.get("demo")
    dev = next(i for i in p.instances() if i.role == "dev")
    ids = iter(dev_ids)
    current = {"id": None}

    def instances(refresh=False):
        current["id"] = next(ids, current["id"])
        return [replace(dev, id=current["id"])]
    monkeypatch.setattr(p, "instances", instances)
    stop = _Stop(rounds)
    launches = []

    class FakeProc:
        def __init__(self, argv, **kw):
            launches.append(current["id"])
            stop.launched += 1

        def poll(self):
            return 0

        def terminate(self):
            pass
    monkeypatch.setattr(remote.subprocess, "Popen", FakeProc)
    return p, stop, launches


def test_a_re_created_dev_box_gets_a_fresh_token_while_the_tunnel_keeps_running(monkeypatch):
    p, stop, launches = _tunnel_harness(monkeypatch, ["box-A", "box-A", "box-B"], rounds=3)
    pushed = []
    remote.run_tunnel(p, 8765, stop, lambda d: pushed.append(d.id))
    assert launches == ["box-A", "box-A", "box-B"]                                          # the tunnel was re-opened every time ssh dropped ...
    assert pushed == ["box-A", "box-B"]                                                     # ... but a token is pushed once per box, and again for the new one


def test_a_failed_token_push_is_retried_before_any_tunnel_is_opened(monkeypatch, capsys):
    p, stop, launches = _tunnel_harness(monkeypatch, ["box-A"], rounds=1)
    tries = []

    def flaky(dev):
        tries.append(dev.id)
        if len(tries) == 1:
            raise ProviderError(f"ssh refused at {ddc.STATE_DIR}")
    remote.run_tunnel(p, 8765, stop, flaky)
    assert tries == ["box-A", "box-A"] and launches == ["box-A"]                            # first try failed: no tunnel; second worked: tunnel
    err = capsys.readouterr().err
    assert "could not give the dev box its token" in err and str(ddc.STATE_DIR) not in err and 15 in stop.waits


def test_serve_with_a_tunnel_mints_a_new_devbox_token_for_every_new_dev_box(monkeypatch):
    """serve_cli's callback: create_token('devbox') replaces the old token (so the old box's token dies) and pushes it with the client."""
    import types
    real_time = remote.time
    done = threading.Event()
    p = providers.get("demo")
    monkeypatch.setattr(p, "id", "akash", raising=False)
    monkeypatch.setattr(providers, "get", lambda name=None: p)
    dev = next(i for i in p.instances() if i.role == "dev")

    def fake_tunnel(provider, port, stop, on_new_dev):
        on_new_dev(dev)
        on_new_dev(dev)                                                                     # a second box
        done.set()

    def interrupt(seconds):                                                                 # serve_cli's idle loop: wait for the tunnel thread, then Ctrl+C
        done.wait(30)
        raise KeyboardInterrupt

    class Clock(types.SimpleNamespace):
        def __getattr__(self, name):
            return getattr(real_time, name)
    monkeypatch.setattr(remote, "time", Clock(sleep=interrupt))
    monkeypatch.setattr(remote, "run_tunnel", fake_tunnel)
    sent = []
    monkeypatch.setattr(remote, "push_client", lambda prov, d, tok: sent.append(tok))
    a = argparse.Namespace(list_tokens=False, revoke=None, new_token=None, scope="fleet", expires_hours=None, provider=None, port=0, tunnel=True)
    assert remote.serve_cli(a) == 0
    assert len(sent) == 2 and sent[0] != sent[1] and all(t.startswith("cdk_") for t in sent)
    db = json.loads(remote.TOKENS.read_text())
    assert list(db) == ["devbox"] and db["devbox"]["scope"] == "fleet" and db["devbox"]["sha256"] == hashlib.sha256(sent[1].encode()).hexdigest()
    assert remote.authenticate("Bearer " + sent[0]) is None and remote.authenticate("Bearer " + sent[1])[0] == "devbox"     # the first box's token is dead


def test_a_tunnel_needs_the_akash_provider():
    a = argparse.Namespace(list_tokens=False, revoke=None, new_token=None, scope="read", expires_hours=None, provider="demo", port=0, tunnel=True)
    with pytest.raises(ProviderError, match="Akash only"):
        remote.serve_cli(a)


def test_the_client_explains_a_dead_token_and_a_bad_scale_argument(api, monkeypatch):
    from clouddeck import remote_client
    p, port = api
    monkeypatch.setenv("CLOUDDECK_TOKEN", "cdk_dead")
    with pytest.raises(SystemExit, match="401.*re-created.*serve --tunnel"):
        remote_client.main(["status"], url=f"http://127.0.0.1:{port}")
    with pytest.raises(SystemExit, match="whole number"):
        remote_client.main(["scale", "lots"], url=f"http://127.0.0.1:{port}")


# ------------------------------------------------------------------ table: everything a token can do, per scope; nothing destructive exists as a route
DESTRUCTIVE_POSTS = ("/v1/terminate", "/v1/stop", "/v1/stop-all", "/v1/resize", "/v1/deploy", "/v1/heal", "/v1/config", "/v1/secrets")
ROUTES = [   # (method, path, body, expected status for a read / fleet / admin token)
    ("GET", "/v1/status", None, (200, 200, 200)),
    ("POST", "/v1/scale", {"n": 2}, (403, 200, 200)),
    ("POST", "/v1/dispatch", {"command": "id"}, (403, 200, 200)),
    ("POST", "/v1/dispatch", {"command": "id", "target": "dev"}, (403, 403, 200)),      # a shell on the dev box is the admin scope's
    ("POST", "/v1/checkpoint", {}, (403, 200, 200)),
    *[("POST", path, {}, (403, 404, 404)) for path in DESTRUCTIVE_POSTS],               # a read token is stopped at the door; for the others there is no such route
    *[("GET", path, None, (404, 404, 404)) for path in ("/v1/config", "/v1/secrets", "/v1/tokens", "/v1/instances/1/terminate")],
]


@pytest.mark.parametrize("method,path,body,expected", ROUTES, ids=[f"{m} {p} {(b or {}).get('target', '')}".strip() for m, p, b, _ in ROUTES])
def test_every_route_and_scope_behaves_as_documented(api, method, path, body, expected):
    p, port = api
    for scope, want in zip(("read", "fleet", "admin"), expected):
        assert call(port, path, remote.create_token(f"t-{scope}", scope), method, body)[0] == want, scope
    assert not [c for c in p.calls if c[0] in ("deploy", "resize", "discard")]               # whatever was asked, nothing was deployed or resized


def test_a_token_can_never_close_the_dev_box_or_anything_but_workers(api):
    p, port = api
    admin = remote.create_token("a", "admin")
    before = {i.id: i.role for i in p.instances() if i.managed}
    assert call(port, "/v1/scale", admin, "POST", {"n": 0})[0] == 200                       # the one mutating route that can close things ...
    closed = [c[1] for c in p.calls if c[0] == "terminate"]
    assert closed and all(before[i] == "worker" for i in closed)                            # ... closes workers only (they hold no data) ...
    assert any(i.role == "dev" for i in p.instances())                                      # ... and the dev box is still there
    for verb in ("DELETE", "PUT", "PATCH"):
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=20)
        c.request(verb, "/v1/status", headers={"Authorization": "Bearer " + admin})
        assert c.getresponse().status == 501                                                # no other verb is even routed
        c.close()


# ------------------------------------------------------------------ a job dispatched through the API keeps the workers from being closed as idle (SAF-24)
def test_a_dispatch_through_the_api_tells_the_controller_the_workers_are_busy(api, monkeypatch):
    p, port = api
    touched = []
    monkeypatch.setattr(ddc, "touch_worker_activity", lambda d: touched.append(d))
    monkeypatch.setattr(p, "id", "akash", raising=False)
    fleet = remote.create_token("f", "fleet")
    assert call(port, "/v1/dispatch", fleet, "POST", {"command": "echo hi"})[0] == 200
    dev = next(i for i in p.instances() if i.role == "dev")
    deadline = time.monotonic() + 30
    while len(touched) < 2 and time.monotonic() < deadline:                                 # the beat at the start and the one at the end
        time.sleep(0.01)
    assert len(touched) >= 2 and touched[0] == {"host": dev.host, "port": dev.port}
