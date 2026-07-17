"""
Lightweight double-clap detector.

No ML model — this is pure signal energy analysis, deliberately, so it
costs almost nothing on an old machine. A clap is a short, sharp burst of
energy well above the room's noise floor. Two of those within CLAP_WINDOW_MS
of each other = trigger.
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
    ):
        self.sample_rate = sample_rate
        self.frame_size = int(sample_rate * frame_ms / 1000)
        self.energy_threshold = energy_threshold
        self.clap_window_ms = clap_window_ms
        self.refractory_ms = refractory_ms

        # rolling noise floor estimate, so the threshold adapts to the room
        self._noise_floor = collections.deque(maxlen=50)
        self._last_clap_ts = 0.0
        self._first_clap_ts = None

    def _frame_energy(self, frame: np.ndarray) -> float:
        # normalized RMS energy, 0..1 range assuming int16 PCM input
        rms = np.sqrt(np.mean(frame.astype(np.float64) ** 2))
        return rms / 32768.0

    def _is_clap(self, energy: float) -> bool:
        if len(self._noise_floor) < 10:
            return energy > self.energy_threshold
        floor = float(np.median(self._noise_floor))
        return energy > max(self.energy_threshold, floor * 6.0)

    def process_frame(self, frame: np.ndarray) -> bool:
        """
        Feed one audio frame (int16 numpy array of length self.frame_size).
        Returns True exactly when a double-clap trigger fires.
        """
        energy = self._frame_energy(frame)
        now = time.monotonic()

        if self._is_clap(energy):
            # ignore claps that arrive too close to the previous one (echo/ringing)
            if (now - self._last_clap_ts) * 1000 < self.refractory_ms:
                return False

            self._last_clap_ts = now

            if self._first_clap_ts is None:
                self._first_clap_ts = now
                return False

            gap_ms = (now - self._first_clap_ts) * 1000
            self._first_clap_ts = None  # reset regardless of outcome
            if gap_ms <= self.clap_window_ms:
                return True
            else:
                # too slow to be a "double" clap — treat this one as a fresh first clap
                self._first_clap_ts = now
                return False
        else:
            self._noise_floor.append(energy)
            # expire a stale "waiting for second clap" state
            if self._first_clap_ts is not None:
                if (now - self._first_clap_ts) * 1000 > self.clap_window_ms:
                    self._first_clap_ts = None
            return False
