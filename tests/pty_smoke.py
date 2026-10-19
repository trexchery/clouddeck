"""Real-terminal smoke test (Windows ConPTY): launches `clouddeck --provider demo` in a pseudo-console, sends real key
presses and reads the screen through a terminal emulator.  Run manually: python tests/pty_smoke.py
Needs: pip install pyte (and pywinpty on Windows).  Uses the simulated provider and an isolated state dir, so nothing real is touched."""
import atexit
import os
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path

import pyte

ROOT = Path(__file__).resolve().parent.parent
state = tempfile.mkdtemp(prefix="cd-pty-")
atexit.register(shutil.rmtree, state, ignore_errors=True)   # leave nothing behind
shutil.copy(ROOT / "tests" / "fixtures" / "config.toml", Path(state) / "config.toml")  # a copy: the TUI can edit its config
env = {**os.environ, "DDC_STATE_DIR": state, "DDC_SECRETS_DIR": state, "DDC_CONFIG": str(Path(state) / "config.toml"), "CLOUDDECK_DEMO_LATENCY": "0.05", "CLOUDDECK_DEMO_SSH_FAIL": "1", "PYTHONPATH": os.pathsep.join(x for x in (str(ROOT), os.environ.get("PYTHONPATH", "")) if x), "PYTHONIOENCODING": "utf-8"}
COLS, ROWS = 140, 40
ARGV = [sys.executable, "-m", "clouddeck", "--provider", "demo"]


class Proc:
    """A child process on a real pseudo-terminal: ConPTY on Windows, a pty on Linux/macOS."""

    def __init__(self) -> None:
        if os.name == "nt":
            from winpty import PtyProcess
            self.p = PtyProcess.spawn(ARGV, dimensions=(ROWS, COLS), env=env, cwd=str(ROOT))
        else:
            import fcntl
            import pty
            import struct
            import subprocess
            import termios
            self.master, slave = pty.openpty()
            fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", ROWS, COLS, 0, 0))
            self.p = subprocess.Popen(ARGV, stdin=slave, stdout=slave, stderr=slave, env={**env, "TERM": "xterm-256color", "COLORTERM": "truecolor"}, cwd=str(ROOT),
                                      close_fds=True, start_new_session=True)
            os.close(slave)

    def read(self, n: int) -> str:
        if os.name == "nt":
            return self.p.read(n)
        try:
            return os.read(self.master, n).decode("utf-8", "replace")
        except OSError:
            raise EOFError

    def write(self, data: str) -> None:
        self.p.write(data) if os.name == "nt" else os.write(self.master, data.encode())

    def isalive(self) -> bool:
        return self.p.isalive() if os.name == "nt" else self.p.poll() is None


proc = Proc()
screen = pyte.Screen(COLS, ROWS)
stream = pyte.Stream(screen)
alive = True


def pump() -> None:
    while alive:
        try:
            data = proc.read(65536)
        except EOFError:
            return
        if data:
            stream.feed(data)


threading.Thread(target=pump, daemon=True).start()


def text() -> str:
    return "\n".join(line.rstrip() for line in screen.display)


def wait_for(needle: str, timeout: float = 60) -> None:
    """Wait for a condition on the emulated screen (never a fixed sleep: a loaded machine is just slower)."""
    end = time.time() + timeout
    while time.time() < end:
        if needle in text():
            return
        time.sleep(0.1)
    print(text())
    raise SystemExit(f"FAIL: never saw {needle!r}")


def wait_gone(needle: str, timeout: float = 60) -> None:
    end = time.time() + timeout
    while time.time() < end:
        if needle not in text():
            return
        time.sleep(0.1)
    print(text())
    raise SystemExit(f"FAIL: {needle!r} never went away")


def press(keys: str) -> None:
    proc.write(keys)


wait_for("CloudDeck")
wait_for("ddc-dev")                      # instances loaded
print("PASS boot: header, summary and instance table rendered through the real console driver")
for key, needle in [("2", "workers"), ("3", "Projects"), ("4", "Balance"), ("5", "Idle timer"), ("1", "Instances")]:
    press(key)
    wait_for(needle)
    time.sleep(0.3)                      # the new tab takes the focus a moment after it shows; keys sent before that are not for it
print("PASS tabs 1-5 switch with real key presses")
press("\r")                              # Enter on the dev box = attach Claude; the demo's fake ssh program does not exist (CLOUDDECK_DEMO_SSH_FAIL)
wait_for("Cannot open")                  # ...so the TUI must come back and say why, not freeze
press("2")
wait_for("workers")
press("1")
wait_for("Instances")
time.sleep(0.3)
print("PASS a failing interactive handoff returns to the TUI with a clear message")
press("d")
wait_for("Deploy instance")
wait_for("Expected", 10)
wait_for("Spend guard", 10)              # the guard's verdict and budget ceiling are part of the estimate
print("PASS deploy dialog opens with a live price estimate and the spend guard verdict")
press("\x1b")                            # Esc
wait_gone("Deploy instance")
press("x")                               # terminate the selected (dev) instance: details first, then a typed phrase
wait_for("Destroys")
wait_for("stop dev")
press("\x1b")
wait_gone("Destroys")
print("PASS terminate lists what is destroyed and asks for a typed phrase")
press("5")                               # Stop everything is typed too
wait_for("Idle timer")
time.sleep(0.3)
press("X")
wait_for("Destroys")
wait_for("stop all")
press("\x1b")
wait_gone("Destroys")
print("PASS stop everything lists what is destroyed and asks for a typed phrase")
press("\x10")                            # Ctrl+P command palette
wait_for("Deploy instance")
press("\x1b")
wait_gone("Deploy instance")
press("q")
wait_for("Quit CloudDeck?")
press("\r")                              # the Quit button has focus
for _ in range(50):
    if not proc.isalive():
        break
    time.sleep(0.2)
alive = False
print("PASS quit confirm exits cleanly" if not proc.isalive() else "FAIL still running")
sys.exit(0 if not proc.isalive() else 1)
