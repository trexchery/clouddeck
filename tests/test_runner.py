"""`ddc runner`: the PC side, with `gh` and `ssh` faked at subprocess level (no GitHub, no lease, no token).
The box side (lease/runner.sh, the ci idle rule, setup.sh's ci user) is in tests/test_lease_scripts.py."""
import json
import types

import pytest

from clouddeck import runner
from clouddeck.engine import ddc

JIT = "SyntheticJitConfig" + "A" * 40
BOX = {"dseq": "4242", "host": "box.example", "port": 22, "role": "dev", "status": "ready", "name": "ddc-dev"}


@pytest.fixture
def world(monkeypatch):
    w = types.SimpleNamespace(calls=[], errors={}, private=True, alive="", runners=[], box=dict(BOX))

    def fake_run(args, input=None, **k):
        args = list(args)
        w.calls.append((args, input))
        if args[0] == "gh":
            path = args[4]
            errs = w.errors.get(path)
            if errs and (err := errs.pop(0)):   # queued per path: None = answer normally, a string = gh fails with it
                return types.SimpleNamespace(returncode=1, stdout=b"", stderr=err.encode())
            if path.endswith("generate-jitconfig"):
                out = {"encoded_jit_config": JIT, "runner": {"id": 7}}
            elif "/actions/runners?" in path:
                out = {"runners": w.runners}
            elif path.count("/") == 2:
                out = {"private": w.private}
            else:
                out = None
            return types.SimpleNamespace(returncode=0, stdout=json.dumps(out).encode() if out is not None else b"", stderr=b"")
        cmd = args[-1]
        out = "ok\n" if cmd.startswith("id ci") else w.alive if cmd.endswith("runner.sh list") else "fine\n"
        return types.SimpleNamespace(returncode=0, stdout=out.encode(), stderr=b"")

    monkeypatch.setattr(runner.subprocess, "run", fake_run)   # the same module object the controller's ssh() uses
    monkeypatch.setattr(ddc, "dev_box", lambda: w.box and dict(w.box))
    for f in ("create_box", "launch_lease", "cmd_start", "cmd_scale"):
        monkeypatch.setattr(ddc, f, lambda *a, **k: pytest.fail("ddc runner must never start a lease"))
    return w


def gh_calls(w, needle=""):
    return [(a, i) for a, i in w.calls if a[0] == "gh" and needle in a[4]]


def ssh_cmds(w):
    return [(a[-1], a[-2].split("@")[0], i) for a, i in w.calls if a[0] != "gh"]


def test_up_refuses_without_a_running_dev_box_and_touches_nothing(world):
    world.box = None
    with pytest.raises(ddc.DdcError, match="never starts a lease"):
        runner.cmd_up(["me/app"], 1, False)
    assert world.calls == []
    assert ddc.main(["runner", "up", "me/app"]) == 1


def test_jit_config_travels_only_on_ssh_stdin(world):
    runner.cmd_up(["me/app"], 2, False)
    for args, _ in world.calls:
        assert not any(JIT in a for a in args), args
    starts = [(c, u, i) for c, u, i in ssh_cmds(world) if "runner.sh start" in c]
    assert [(c, u) for c, u, _ in starts] == [("bash /opt/ddc/runner.sh start me.app.1", "root"), ("bash /opt/ddc/runner.sh start me.app.2", "root")]
    assert all(i == JIT.encode() + b"\n" for _, _, i in starts)
    assert [i for a, i in world.calls if i and JIT.encode() in i] == [i for _, _, i in starts], "nothing else receives the JIT config"
    bodies = [json.loads(i) for _, i in gh_calls(world, "generate-jitconfig")]
    assert [b["name"] for b in bodies] == ["ddc-4242-1", "ddc-4242-2"]
    assert bodies[0]["labels"] == ["self-hosted", "Linux", "X64", "proj-ephemeral", "ddc"] and bodies[0]["runner_group_id"] == 1
    assert ddc.read_json(ddc.STATE_DIR / "runners.json", {}) == {"me/app": 2}


def test_live_slots_are_not_refilled(world):
    world.alive = "me.app.1\n"
    runner.cmd_up(["me/app"], 2, False)
    assert [c for c, _, _ in ssh_cmds(world) if "start" in c] == ["bash /opt/ddc/runner.sh start me.app.2"]


def test_public_repo_is_refused_before_any_runner_exists(world):
    world.private = False
    with pytest.raises(ddc.DdcError, match="public"):
        runner.cmd_up(["me/site"], 1, False)
    assert not gh_calls(world, "generate-jitconfig") and not ssh_cmds(world)


def test_bad_repo_names_are_refused(world):
    for bad in ("me", "me/app;rm -rf /", "../x/y"):
        with pytest.raises(ddc.DdcError, match="owner/repo"):
            runner.cmd_up([bad], 1, False)
    assert not gh_calls(world)


def test_down_removes_only_our_idle_runners(world):
    ddc.write_json(ddc.STATE_DIR / "runners.json", {"me/app": 1})
    lab = [{"name": n} for n in ("self-hosted", "ddc")]
    world.runners = [{"id": 1, "name": "ddc-4242-1", "busy": False, "labels": lab},
                     {"id": 2, "name": "ddc-4242-2", "busy": True, "labels": lab},
                     {"id": 3, "name": "laptop", "busy": False, "labels": [{"name": "self-hosted"}]}]
    runner.cmd_down([], False)
    assert [a[4] for a, _ in world.calls if a[0] == "gh" and a[3] == "DELETE"] == ["repos/me/app/actions/runners/1"]
    assert [c for c, _, _ in ssh_cmds(world)] == ["bash /opt/ddc/runner.sh stop"]
    assert ddc.read_json(ddc.STATE_DIR / "runners.json", {}) == {}


LAB = [{"name": n} for n in ("self-hosted", "ddc")]


def deletes(w):
    return [a[4] for a, _ in w.calls if a[0] == "gh" and a[3] == "DELETE"]


def test_watch_drops_this_boxs_idle_registrations_when_the_box_is_gone(world, monkeypatch):
    world.runners = [{"id": 1, "name": "ddc-4242-1", "busy": False, "labels": LAB},
                     {"id": 2, "name": "ddc-4242-2", "busy": True, "labels": LAB},    # mid-job: left alone
                     {"id": 3, "name": "ddc-9999-1", "busy": False, "labels": LAB},   # another box's
                     {"id": 4, "name": "laptop", "busy": False, "labels": [{"name": "self-hosted"}]}]
    monkeypatch.setattr(runner.time, "sleep", lambda s: setattr(world, "box", None))
    runner.cmd_up(["me/app"], 1, True)
    assert deletes(world) == ["repos/me/app/actions/runners/1"]


def test_watch_rechecks_private_on_every_pass(world, monkeypatch):
    world.runners = [{"id": 1, "name": "ddc-4242-1", "busy": False, "labels": LAB}]
    naps = []

    def sleep(s):
        naps.append(s)
        assert len(naps) == 1, "the loop kept running after the repo went public"
        world.private = False
    monkeypatch.setattr(runner.time, "sleep", sleep)
    with pytest.raises(ddc.DdcError, match="public"):
        runner.cmd_up(["me/app"], 1, True)
    assert len(gh_calls(world, "generate-jitconfig")) == 1, "no runner after the repo went public"
    assert deletes(world) == ["repos/me/app/actions/runners/1"]


GH_502 = "gh: Server Error (HTTP 502)"


def test_watch_survives_a_transient_github_error_in_the_private_recheck(world, monkeypatch):
    world.errors["repos/me/app"] = [None, GH_502]   # the first check (before the loop) answers; the first recheck fails
    world.runners = [{"id": 1, "name": "ddc-4242-1", "busy": False, "labels": LAB}]
    naps = []

    def sleep(s):
        naps.append(s)
        if len(naps) == 2:
            world.box = None
    monkeypatch.setattr(runner.time, "sleep", sleep)
    runner.cmd_up(["me/app"], 1, True)   # ends because the box is gone, not on the 502
    assert len(naps) == 2 and deletes(world) == ["repos/me/app/actions/runners/1"]


def test_watch_stops_after_three_failed_private_rechecks_in_a_row(world, monkeypatch):
    world.errors["repos/me/app"] = [None, GH_502, GH_502, GH_502]
    naps = []
    monkeypatch.setattr(runner.time, "sleep", naps.append)
    with pytest.raises(ddc.DdcError, match="three times"):
        runner.cmd_up(["me/app"], 1, True)
    assert len(naps) == 3 and not deletes(world), "a failed request is not a public repo: registrations stay"


def test_drop_idle_skips_a_runner_that_took_a_job_and_removes_the_rest(world):
    world.runners = [{"id": 1, "name": "ddc-4242-1", "busy": False, "labels": LAB},
                     {"id": 2, "name": "ddc-4242-2", "busy": False, "labels": LAB}]
    world.errors["repos/me/app/actions/runners/1"] = ["gh: Validation Failed (HTTP 422)"]
    runner._drop_idle("me/app", "4242")
    assert deletes(world) == ["repos/me/app/actions/runners/1", "repos/me/app/actions/runners/2"]
