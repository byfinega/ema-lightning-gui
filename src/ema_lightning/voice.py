"""Voice presets for the desktop app, and the pitch shift behind them.

The model has one speaker: the training corpus is a single synthetic voice and the
weights carry no speaker input, so there is no second voice to choose from. What the
preset below does is resample the finished audio, which moves pitch and formants
together and makes the same voice sound lighter. Nothing extra is generated: the model
is asked for slightly slower speech and the result is played back faster, so the
duration stays exactly what the speed setting says.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

SPEED_LIMITS = (0.25, 4.0)  # what EMA.stream() accepts


@dataclass(frozen=True)
class Voice:
    """One entry of the voice menu: a label, and the playback rate factor it implies."""

    key: str
    label: str
    pitch: float  # below 1 is deeper, above 1 is lighter
    note: str


VOICES = (
    Voice("ema", "EMA · özgün", 1.0, "Modelin kendi sesi."),
    Voice("bright", "İnce", 1.12, "Aynı sesin perdesi yükseltilmiş, daha tiz hâli."),
)
DEFAULT_VOICE = VOICES[0]
_BY_KEY = {voice.key: voice for voice in VOICES}
_BY_LABEL = {voice.label: voice for voice in VOICES}


def voice_by_key(key: str) -> Voice:
    """The preset with this key, or the model's own voice when the key is unknown."""
    return _BY_KEY.get(key, DEFAULT_VOICE)


def voice_by_label(label: str) -> Voice:
    """The preset a combo box shows, or the model's own voice when it no longer matches."""
    return _BY_LABEL.get(label, DEFAULT_VOICE)


def model_speed(speed: float, pitch: float) -> float:
    """The speed to synthesize at so the shifted audio lasts as long as `speed` asks for.

    Resampling by `pitch` shortens the audio by `pitch`, so a lighter voice needs slower
    speech behind it. The result is clamped to what the model accepts; at the very bottom
    of the speed slider a light preset can therefore come out slightly shorter.
    """
    low, high = SPEED_LIMITS
    if pitch <= 0:
        raise ValueError("pitch must be positive")
    return min(high, max(low, float(speed) / float(pitch)))


def semitones(pitch: float) -> float:
    """How far the preset moves the voice, in semitones, for logs and documentation."""
    return 12.0 * float(np.log2(pitch)) if pitch > 0 else 0.0


def shift(audio: np.ndarray, pitch: float) -> np.ndarray:
    """Resample `audio` by `pitch`, returning float32 at the same nominal rate.

    A deeper voice means the samples are read more slowly: the signal gets longer and its
    frequency content moves down by `pitch`. The model is asked for faster speech so that,
    after this stretch, the duration still matches the speed setting. Above 1.0 the signal
    is low-passed first, so decimation cannot fold the sibilants back into the audible range.
    """
    if pitch <= 0:
        raise ValueError("pitch must be positive")
    samples = np.asarray(audio, dtype=np.float32)
    if abs(pitch - 1.0) < 1e-4 or samples.size < 2:
        return samples
    if pitch > 1.0:
        samples = _lowpass(samples, cutoff=0.5 / pitch)
    count = max(1, int(round(samples.size / pitch)))
    positions = np.arange(count, dtype=np.float64) * pitch
    left = np.clip(np.floor(positions).astype(np.int64), 0, samples.size - 1)
    right = np.clip(left + 1, 0, samples.size - 1)
    weight = (positions - left).astype(np.float32)
    return (samples[left] * (1.0 - weight) + samples[right] * weight).astype(np.float32)


def _lowpass(samples: np.ndarray, cutoff: float, taps: int = 63) -> np.ndarray:
    """Kaiser-windowed sinc low-pass at `cutoff` cycles per sample."""
    offsets = np.arange(taps) - (taps - 1) / 2.0
    kernel = 2.0 * cutoff * np.sinc(2.0 * cutoff * offsets) * np.kaiser(taps, 8.0)
    kernel /= kernel.sum()
    return np.convolve(samples, kernel, mode="same").astype(np.float32)
