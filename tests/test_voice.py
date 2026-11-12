import numpy as np
import pytest

from ema_lightning.voice import (
    DEFAULT_VOICE,
    VOICES,
    model_speed,
    semitones,
    shift,
    voice_by_key,
    voice_by_label,
)

RATE = 48000


def tone(frequency=1000.0, seconds=0.5, rate=RATE):
    t = np.arange(int(seconds * rate), dtype=np.float32) / rate
    return np.sin(2 * np.pi * frequency * t).astype(np.float32)


def dominant(samples, rate=RATE):
    """The strongest frequency in the signal, ignoring the edges the filter rounds off."""
    window = samples[int(0.05 * rate):int(0.05 * rate) + 8192]
    spectrum = np.abs(np.fft.rfft(window * np.hanning(len(window))))
    return float(np.fft.rfftfreq(len(window), 1 / rate)[int(np.argmax(spectrum))])


def test_the_presets_do_not_repeat_a_key_or_a_label():
    assert len({voice.key for voice in VOICES}) == len(VOICES)
    assert len({voice.label for voice in VOICES}) == len(VOICES)


def test_the_menu_keeps_the_model_voice_and_the_lighter_one():
    assert [voice.key for voice in VOICES] == ["ema", "bright"]
    assert voice_by_key("ema").pitch == 1.0
    assert semitones(voice_by_key("bright").pitch) > 1.0


def test_unknown_settings_fall_back_to_the_model_voice():
    assert voice_by_key("nope") is DEFAULT_VOICE
    assert voice_by_label("Ses Yok") is DEFAULT_VOICE


def test_a_slower_beat_compensates_for_the_lighter_voice():
    assert model_speed(1.0, 1.12) == pytest.approx(1.0 / 1.12)
    assert model_speed(2.0, 1.12) == pytest.approx(2.0 / 1.12)
    assert model_speed(1.0, 1.0) == 1.0


def test_the_beat_stays_inside_what_the_model_accepts():
    assert model_speed(0.25, 1.12) == 0.25  # the model would have been asked for 0.223
    assert model_speed(0.5, 1.12) == pytest.approx(0.5 / 1.12)


def test_pitch_must_be_positive():
    with pytest.raises(ValueError):
        model_speed(1.0, 0.0)
    with pytest.raises(ValueError):
        shift(tone(), 0)


def test_the_untouched_voice_is_returned_as_is():
    audio = tone()
    assert shift(audio, 1.0) is audio


def test_a_deeper_voice_lowers_the_frequency_and_lengthens_the_signal():
    """`shift` is general, so the other direction stays covered even though no preset uses it."""
    audio = tone(1000.0)
    deeper = shift(audio, 0.8)
    assert deeper.dtype == np.float32
    assert len(deeper) == pytest.approx(len(audio) / 0.8, rel=0.01)
    assert dominant(deeper) == pytest.approx(800.0, abs=6.0)


def test_a_lighter_voice_raises_the_frequency():
    lighter = shift(tone(1000.0), 1.12)
    assert dominant(lighter) == pytest.approx(1120.0, abs=8.0)


def test_the_lighter_preset_keeps_the_duration_when_the_beat_is_compensated():
    """speak slower, play faster: the length the speed slider promises is what comes out."""
    audio = tone(1000.0, seconds=1.0)
    pitch = voice_by_key("bright").pitch
    longer = np.resize(audio, int(len(audio) * pitch))  # what the model makes at the compensated beat
    assert len(shift(longer, pitch)) == pytest.approx(len(audio), rel=0.005)


def test_a_short_or_empty_signal_survives_the_shift():
    assert len(shift(np.zeros(0, dtype=np.float32), 0.8)) == 0
    assert len(shift(np.zeros(1, dtype=np.float32), 0.8)) == 1
