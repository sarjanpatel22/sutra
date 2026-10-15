"""Wake-word detection — the "say the word, then talk" front-end for the voice loop.

Opt-in (WAKEWORD_ENABLED / `sutra voice --mode wake`): Sutra sits dormant until a
wake word fires, then runs one normal VAD turn. Runs openWakeWord on the EXISTING
onnxruntime (no torch); the pretrained models ship bundled with the package, so the
default "hey_jarvis" works with zero downloads. A custom "Hey Sutra" model needs a
training pipeline — a documented follow-on.

Design mirrors audio.BargeInDetector: the firing decision (threshold + consecutive-
frame streak + cooldown) is a PURE dataclass, unit-tested with synthetic scores; the
onnx model and the mic are lazy and isolated so neither the tests nor importing this
module need them. Live-mic accuracy is Sarjan's to validate (far-field SNR caveat).
"""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

_log = logging.getLogger("sutra.voice.wake")


def wake_enabled() -> bool:
    """Whether wake-word mode is switched on. Off by default (opt-in)."""
    return os.getenv("WAKEWORD_ENABLED", "0").strip().lower() not in ("", "0", "false", "no")


@dataclass
class WakeGate:
    """Pure firing logic over a stream of wake-model scores — no model, no mic.

    Fires when the score has been ≥ threshold for `trigger_frames` consecutive frames
    AND at least `cooldown_s` has passed since the last fire (so one spoken wake word
    triggers exactly one turn, not a burst). This is the unit-tested core.
    """

    threshold: float = 0.5
    cooldown_s: float = 2.0
    trigger_frames: int = 1
    _streak: int = field(default=0, init=False, repr=False)
    _last_fire: float = field(default=-1e9, init=False, repr=False)

    @classmethod
    def from_env(cls) -> WakeGate:
        """Build from WAKEWORD_* knobs."""
        return cls(
            threshold=float(os.getenv("WAKEWORD_THRESHOLD", "0.5")),
            cooldown_s=float(os.getenv("WAKEWORD_COOLDOWN_S", "2.0")),
            trigger_frames=max(1, int(os.getenv("WAKEWORD_TRIGGER_FRAMES", "1"))),
        )

    def feed(self, score: float, *, now: float | None = None) -> bool:
        """Consume one wake-model score; return True the moment a wake is confirmed."""
        now = time.monotonic() if now is None else now
        self._streak = self._streak + 1 if score >= self.threshold else 0
        if self._streak >= self.trigger_frames and (now - self._last_fire) >= self.cooldown_s:
            self._last_fire = now
            self._streak = 0
            return True
        return False

    def reset(self) -> None:
        """Clear the streak (cooldown persists across resets within a session)."""
        self._streak = 0


class WakeWord:
    """openWakeWord model wrapper — scores 16k int16 mic frames. Model is lazy.

    The pretrained models ship inside the openWakeWord package; `WAKEWORD_MODEL`
    selects one by substring (default "hey_jarvis"; also bundled: "alexa",
    "hey_mycroft", "hey_marvin", "timer", "weather").
    """

    def __init__(self, model_name: str | None = None) -> None:
        self.model_name = (model_name or os.getenv("WAKEWORD_MODEL") or "hey_jarvis").strip()
        self._model: Any = None

    def _resolve_path(self) -> str:
        import openwakeword

        paths: list[str] = []
        try:
            paths = list(openwakeword.get_pretrained_model_paths())
        except Exception:  # noqa: BLE001 — fall back to scanning the bundled models dir
            paths = []
        if not paths:
            d = os.path.join(os.path.dirname(openwakeword.__file__), "resources", "models")
            if os.path.isdir(d):
                paths = [os.path.join(d, f) for f in os.listdir(d)]
        cands = [p for p in paths if p.endswith(".onnx") and self.model_name in os.path.basename(p)]
        if not cands:
            raise FileNotFoundError(
                f"wake model {self.model_name!r} not found among bundled openWakeWord "
                f"models: {[os.path.basename(p) for p in paths if p.endswith('.onnx')]}"
            )
        return cands[0]

    def _ensure(self) -> Any:
        if self._model is None:
            from openwakeword.model import Model

            path = self._resolve_path()
            try:
                self._model = Model(wakeword_model_paths=[path], inference_framework="onnx")
            except TypeError:  # signature without the kwarg — extension already implies onnx
                self._model = Model(wakeword_model_paths=[path])
            _log.info("wake model loaded: %s", os.path.basename(path))
        return self._model

    def score(self, frame: Any) -> float:
        """Top wake-model probability for one int16 frame (0 if the model is silent)."""
        preds = self._ensure().predict(frame)
        return max(preds.values()) if preds else 0.0

    def warm(self) -> None:
        """Load the model now so the first real wake isn't slowed by a cold load."""
        import numpy as np

        try:
            self.score(np.zeros(1280, dtype=np.int16))
        except Exception:  # noqa: BLE001 — surfaced again at real use; warming is best-effort
            _log.exception("wake model warm failed (will retry on first frame)")


def make_detector(
    score_fn: Callable[[Any], float] | None = None,
    gate: WakeGate | None = None,
) -> Callable[[Any], bool]:
    """Return a `feed(frame) -> bool` that scores a frame and runs it through the gate.

    `score_fn` defaults to a real WakeWord model; inject a fake one in tests so the
    gate logic is exercised without onnx or a mic.
    """
    if score_fn is None:
        score_fn = WakeWord().score
    gate = gate or WakeGate.from_env()

    def feed(frame: Any) -> bool:
        return gate.feed(score_fn(frame))

    return feed
