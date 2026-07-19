"""
Lightweight double-clap detector.

No ML model — this is pure signal energy analysis, deliberately, so it
costs almost nothing on an old machine. A clap is a short, sharp burst of
energy well above the room's noise floor. Two of those within CLAP_WINDOW_MS
of each other = trigger.

Amplitude alone isn't enough to tell a clap apart from a burst of talking
or other room noise, which can regularly hit similar energy levels — the
real distinguishing feature is *duration*: a clap is a ~20-40ms transient,
while speech/ambient noise stays elevated for many consecutive frames.
So loud energy is only treated as a clap candidate once it's confirmed
short (the streak ends within max_clap_frames); anything that stays loud
longer than that is assumed to be talking or other noise and is ignored
outright, not even counted as a "first clap".
"""
import collections
import time
import numpy as np


class DoubleClapDetector:
    def __init__(
        self,
        sample_rate: int = 16000,
        frame_ms: int = 20,
        energy_threshold: float = 0.35,
        clap_window_ms: int = 600,
        refractory_ms: int = 250,
        max_clap_ms: int = 60,
    ):
        self.sample_rate = sample_rate
        self.frame_size = int(sample_rate * frame_ms / 1000)
        self.energy_threshold = energy_threshold
        self.clap_window_ms = clap_window_ms
        self.refractory_ms = refractory_ms
        self.max_clap_frames = max(1, max_clap_ms // frame_ms)

        # rolling noise floor estimate, so the threshold adapts to the room
        self._noise_floor = collections.deque(maxlen=50)
        self._last_clap_ts = 0.0
        self._first_clap_ts = None
        self._loud_streak = 0
        self._streak_start_ts = None

    def _frame_energy(self, frame: np.ndarray) -> float:
        # normalized RMS energy, 0..1 range assuming int16 PCM input
        rms = np.sqrt(np.mean(frame.astype(np.float64) ** 2))
        return rms / 32768.0

    def _is_loud(self, energy: float) -> bool:
        if len(self._noise_floor) < 10:
            return energy > self.energy_threshold
        floor = float(np.median(self._noise_floor))
        return energy > max(self.energy_threshold, floor * 6.0)

    def _handle_clap_candidate(self, ts: float) -> bool:
        """A short loud transient just ended at `ts` — run it through the
        existing refractory/double-clap-window state machine."""
        if (ts - self._last_clap_ts) * 1000 < self.refractory_ms:
            return False
        self._last_clap_ts = ts

        if self._first_clap_ts is None:
            self._first_clap_ts = ts
            return False

        gap_ms = (ts - self._first_clap_ts) * 1000
        self._first_clap_ts = None
        if gap_ms <= self.clap_window_ms:
            return True
        # too slow to be a "double" clap — treat this one as a fresh first clap
        self._first_clap_ts = ts
        return False

    def process_frame(self, frame: np.ndarray) -> bool:
        """
        Feed one audio frame (int16 numpy array of length self.frame_size).
        Returns True exactly when a double-clap trigger fires.
        """
        energy = self._frame_energy(frame)
        now = time.monotonic()

        if self._is_loud(energy):
            if self._loud_streak == 0:
                self._streak_start_ts = now
            self._loud_streak += 1
            return False  # decided on the falling edge, once we know how long this lasted

        if self._loud_streak > 0:
            streak_len = self._loud_streak
            streak_ts = self._streak_start_ts
            self._loud_streak = 0
            self._streak_start_ts = None

            if streak_len <= self.max_clap_frames:
                return self._handle_clap_candidate(streak_ts)
            # sustained loud sound (talking, background noise, etc.) —
            # not a clap; fall through to noise-floor bookkeeping below

        self._noise_floor.append(energy)
        # expire a stale "waiting for second clap" state
        if self._first_clap_ts is not None:
            if (now - self._first_clap_ts) * 1000 > self.clap_window_ms:
                self._first_clap_ts = None
        return False
