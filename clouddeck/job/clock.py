"""Injectable clock so every wait loop can run on virtual time in tests and simulations."""
import time as _time


class RealClock:
    time = staticmethod(_time.time)
    sleep = staticmethod(_time.sleep)


class FakeClock:
    """Single-threaded virtual time: sleep() just advances it. Also quacks like the `time` module (the controller uses `time.time` / `time.sleep`)."""

    def __init__(self, t: float = 1_800_000_000.0):
        self.t = t

    def time(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.t += max(0.0, s)
