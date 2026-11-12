import json
import shutil
import sys
import time
import types
import wave

import numpy as np
import pytest

tk = pytest.importorskip("tkinter")

from ema_lightning.gui import (  # noqa: E402
    APP_NAME,
    App,
    Settings,
    estimate_seconds,
    find_weights,
    format_count,
    format_seconds,
    load_settings,
    parse_seed,
    save_settings,
)
from ema_lightning.voice import VOICES, model_speed, voice_by_key  # noqa: E402


def test_estimate_seconds_uses_the_chunker_pace():
    assert estimate_seconds("abc", 1.0) == pytest.approx(3 / 18)
    assert estimate_seconds("abc", 2.0) == pytest.approx(3 / 36)
    assert estimate_seconds("", 1.0) == 0.0


def test_format_seconds():
    assert format_seconds(0.2) == "<1 sn"
    assert format_seconds(3.3) == "3,3 sn"
    assert format_seconds(65) == "1 dk 05 sn"


def test_format_count_uses_turkish_separators():
    assert format_count(1234567) == "1.234.567"


@pytest.mark.parametrize("value, expected", [("", None), ("0", 0), ("42", 42), (" 7 ", 7)])
def test_parse_seed(value, expected):
    assert parse_seed(value) == expected


@pytest.mark.parametrize("value", ["-1", "abc", "1.5", str(2**63)])
def test_parse_seed_rejects_bad_values(value):
    with pytest.raises(ValueError):
        parse_seed(value)


def test_settings_round_trip(tmp_path):
    path = tmp_path / "settings.json"
    settings = Settings(speed=1.25, sample_rate=16000, seed="99", autoplay=False, theme="light",
                        last_dir=str(tmp_path), geometry="1000x720+10+20")
    save_settings(settings, path)
    assert load_settings(path) == settings
    assert json.loads(path.read_text(encoding="utf-8"))["speed"] == 1.25


def test_damaged_settings_fall_back_to_defaults(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text("{not json", encoding="utf-8")
    assert load_settings(path) == Settings()


def test_settings_are_sanitized(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"speed": 99, "sample_rate": 44100, "theme": "neon", "geometry": "bad"}),
                    encoding="utf-8")
    settings = load_settings(path)
    assert settings.speed == 2.0
    assert settings.sample_rate == 48000
    assert settings.theme == "dark"
    assert settings.geometry == ""


def test_find_weights_prefers_the_bundle(tmp_path, monkeypatch):
    folder = tmp_path / "weights"
    folder.mkdir()
    (folder / "ema.pt").write_bytes(b"ema")
    (folder / "decoder.pt").write_bytes(b"decoder")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)
    assert find_weights() == folder


def test_window_builds_without_loading_the_model():
    try:
        root = tk.Tk()
    except tk.TclError:
        pytest.skip("no display available")
    root.withdraw()
    app = App(root, autoload=False, settings=Settings())
    try:
        root.update_idletasks()
        assert app.speak_button["state"] == "disabled"
        assert app.stop_button["state"] == "disabled"
        assert app.save_button["state"] == "disabled"
        assert app.text.get("1.0", "end-1c") == ""
        app.text.insert("1.0", "Merhaba dünya.")
        app._update_stats()
        assert "2 kelime" in app.stats["text"]
    finally:
        root.destroy()
        shutil.rmtree(app._temp_dir, ignore_errors=True)


def test_the_window_is_named_after_the_desktop_gui():
    assert APP_NAME == "EMA Lightning Desktop GUI"


def test_playback_starts_async_and_stop_returns_at_once(monkeypatch, tmp_path):
    try:
        root = tk.Tk()
    except tk.TclError:
        pytest.skip("no display available")
    root.withdraw()
    calls = []

    class FakeWinsound(types.ModuleType):
        SND_FILENAME = 0x20000
        SND_ASYNC = 0x1
        SND_PURGE = 0x40

        @staticmethod
        def PlaySound(sound, flags):
            calls.append((sound, flags))

    monkeypatch.setitem(sys.modules, "winsound", FakeWinsound("winsound"))
    monkeypatch.setattr(sys, "platform", "win32")
    app = App(root, autoload=False, settings=Settings())
    wav = tmp_path / "preview.wav"
    with wave.open(str(wav), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(48000)
        handle.writeframes(b"\x00\x00" * 48000 * 5)
    try:
        app._play(wav, 5.0)
        # Oynatma iki aşamalıdır: önce sessizlik-ısıtma sesi, ~350 ms sonra gerçek ses.
        expected = (str(wav), FakeWinsound.SND_FILENAME | FakeWinsound.SND_ASYNC)
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and calls[-1] != expected:
            root.update()
            time.sleep(0.02)
        assert calls[-1] == expected
        assert app._playing
        assert app.stop_button["state"] == "normal"

        started = time.monotonic()
        app.stop_button.invoke()
        root.update()
        assert time.monotonic() - started < 0.5, "Stop must not wait for the file to end"
        assert calls[-1] == (None, 0)
        assert not app._playing
        assert app.stop_button["state"] == "disabled"
    finally:
        root.destroy()
        shutil.rmtree(app._temp_dir, ignore_errors=True)


def test_stop_cancels_a_running_synthesis():
    try:
        root = tk.Tk()
    except tk.TclError:
        pytest.skip("no display available")
    root.withdraw()

    class SlowTTS:
        def stream(self, text, speed, seed, sample_rate):
            for _ in range(200):
                time.sleep(0.01)
                yield np.zeros(480, dtype=np.float32)

    app = App(root, autoload=False, settings=Settings(autoplay=False))
    app.tts = SlowTTS()
    app.model_state = "ready"
    app.text.insert("1.0", "Merhaba dünya.")
    app._refresh_actions()
    try:
        app._on_speak()
        for _ in range(20):
            root.update()
            time.sleep(0.01)
        assert app._busy and app.stop_button["state"] == "normal"

        app.stop_button.invoke()
        deadline = time.monotonic() + 5
        while app._busy and time.monotonic() < deadline:
            root.update()
            time.sleep(0.01)
        root.update()
        assert not app._busy
        assert app.status_label["text"] == "Durduruldu"
    finally:
        root.destroy()
        shutil.rmtree(app._temp_dir, ignore_errors=True)


def test_a_window_saved_on_a_removed_monitor_is_recentred():
    try:
        root = tk.Tk()
    except tk.TclError:
        pytest.skip("no display available")
    root.withdraw()
    app = App(root, autoload=False, settings=Settings(geometry="1000x720+99999+99999"))
    try:
        assert not app._restore_geometry()
        app.settings.geometry = "1000x720+10+10"
        assert app._restore_geometry()
        app.settings.geometry = "not a geometry"
        assert not app._restore_geometry()
    finally:
        root.destroy()
        shutil.rmtree(app._temp_dir, ignore_errors=True)


def window_or_skip():
    try:
        root = tk.Tk()
    except tk.TclError:
        pytest.skip("no display available")
    root.withdraw()
    return root


def test_the_voice_menu_lightens_the_voice_and_opens_on_the_saved_one():
    root = window_or_skip()
    app = App(root, autoload=False, settings=Settings(voice="bright"))
    try:
        assert list(app.voice["values"]) == [voice.label for voice in VOICES]
        assert app.voice_var.get() == voice_by_key("bright").label
        assert voice_by_key("bright").pitch > 1.0
    finally:
        root.destroy()
        shutil.rmtree(app._temp_dir, ignore_errors=True)


def test_the_chosen_voice_is_written_to_the_settings():
    root = window_or_skip()
    app = App(root, autoload=False, settings=Settings())
    try:
        app.voice_var.set(voice_by_key("bright").label)
        app._sync_settings()
        assert app.settings.voice == "bright"
    finally:
        root.destroy()
        shutil.rmtree(app._temp_dir, ignore_errors=True)


@pytest.mark.parametrize("saved", ["robot", "male", "deep"])
def test_an_unknown_saved_voice_falls_back_to_the_model_voice(tmp_path, saved):
    """Old settings files may name presets that no longer exist; they must still open."""
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"voice": saved}), encoding="utf-8")
    assert load_settings(path).voice == "ema"


def test_synthesis_applies_the_selected_voice_without_changing_the_duration():
    root = window_or_skip()

    class ToneTTS:
        """A one-second 1 kHz tone, shortened by the speed it is asked for."""

        def stream(self, text, speed, seed, sample_rate):
            seconds = 1.0 / speed
            t = np.arange(int(seconds * sample_rate), dtype=np.float32) / sample_rate
            yield np.sin(2 * np.pi * 1000.0 * t).astype(np.float32)

    app = App(root, autoload=False, settings=Settings())
    app.tts = ToneTTS()
    pitch = voice_by_key("bright").pitch
    pace = model_speed(1.0, pitch)
    try:
        app._synthesize(1, "deneme", pace, 0, 48000, pitch)
        done = None
        while not app.events.empty():
            event = app.events.get_nowait()
            if event[0] == "done":
                done = event
        assert done is not None, "synthesis never reported a result"
        _kind, _job, path, duration, _seed = done
        assert duration == pytest.approx(1.0, abs=0.02)
        with wave.open(str(path), "rb") as handle:
            assert handle.getframerate() == 48000
            samples = np.frombuffer(handle.readframes(handle.getnframes()), dtype=np.int16)
        window = samples[4800:4800 + 8192] * np.hanning(8192)
        spectrum = np.abs(np.fft.rfft(window))
        found = np.fft.rfftfreq(8192, 1 / 48000)[int(np.argmax(spectrum))]
        assert found == pytest.approx(1000.0 * pitch, abs=8.0)
    finally:
        root.destroy()
        shutil.rmtree(app._temp_dir, ignore_errors=True)
