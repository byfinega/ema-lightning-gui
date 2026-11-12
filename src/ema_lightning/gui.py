"""EMA Lightning Desktop GUI: a small Tkinter window for Turkish text to speech.

Run it from a checkout with ``python -m ema_lightning`` or, after installation,
with the ``ema-lightning-desktop`` entry point. The window loads the same model
as the library, synthesizes in a worker thread and plays the result through the
Windows WAV player, so the interface never blocks.

Arayüz tasarımı ve geliştirmesi: @byfinega
"""
from __future__ import annotations

import json
import logging
import os
import queue
import random
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import tkinter as tk
import wave
from dataclasses import asdict, dataclass
from importlib.metadata import version as _dist_version
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from tkinter import font as tkfont
from typing import Any

from .voice import DEFAULT_VOICE, VOICES, model_speed, shift, voice_by_key, voice_by_label

try:
    APP_VERSION = _dist_version("ema-lightning")
except Exception:  # running from a source tree or a bundled app without metadata
    APP_VERSION = "1.1.2"

APP_NAME = "EMA Lightning Desktop GUI"
APP_TITLE = "EMA Lightning"
APP_KIND = "Desktop GUI"
SAMPLE_RATES = (48000, 24000, 16000, 8000)
RATE_LABELS = {48000: "48 kHz", 24000: "24 kHz", 16000: "16 kHz", 8000: "8 kHz"}
LETTERS_PER_SECOND = 18.0
EXAMPLE_TEXT = (
    "Merhaba! Bugün hava çok güzel. Toplantı 14:30'da başlıyor ve bütçe 1.250.000 TL olarak onaylandı. "
    "Kargonuz yarın öğlene kadar adresinize teslim edilecek."
)

logger = logging.getLogger("ema_lightning.gui")

PALETTES: dict[str, dict[str, str]] = {
    "dark": {
        "bg": "#0B0D12",
        "surface": "#12151C",
        "surface2": "#1A1F2A",
        "border": "#252C3A",
        "input": "#0E1218",
        "text": "#E8EAF0",
        "muted": "#8D97AA",
        "accent": "#7C5CFF",
        "accent_hover": "#8F74FF",
        "accent_text": "#FFFFFF",
        "selection": "#312A5C",
        "success": "#34D399",
        "warning": "#FBBF24",
        "danger": "#F87171",
    },
    "light": {
        "bg": "#F4F5F9",
        "surface": "#FFFFFF",
        "surface2": "#EEF1F7",
        "border": "#DDE2EC",
        "input": "#F8F9FC",
        "text": "#171B24",
        "muted": "#64708A",
        "accent": "#5B4BDB",
        "accent_hover": "#6D5EEA",
        "accent_text": "#FFFFFF",
        "selection": "#DDD8FF",
        "success": "#0F9F6E",
        "warning": "#B45309",
        "danger": "#D92D20",
    },
}

GEOMETRY = re.compile(r"^\d{3,4}x\d{3,4}(?:[+-]\d{1,5}){0,2}$")


@dataclass
class Settings:
    """Everything the app remembers between launches. User text is never stored."""

    speed: float = 1.0
    sample_rate: int = 48000
    voice: str = DEFAULT_VOICE.key
    seed: str = ""
    autoplay: bool = True
    theme: str = "dark"
    last_dir: str = ""
    geometry: str = ""


def settings_path() -> Path:
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / "EMALightning" / "settings.json"


def cache_dir() -> Path:
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    else:
        base = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
    return base / "EMALightning"


def load_settings(path: Path | None = None) -> Settings:
    """Read settings, falling back to defaults on a missing or damaged file."""
    path = Path(path) if path is not None else settings_path()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return Settings()
    if not isinstance(raw, dict):
        return Settings()
    defaults = Settings()
    speed = _number(raw.get("speed"), 0.5, 2.0, defaults.speed)
    rate = raw.get("sample_rate")
    seed = raw.get("seed")
    theme = raw.get("theme")
    geometry = raw.get("geometry")
    return Settings(
        speed=speed,
        sample_rate=rate if rate in SAMPLE_RATES else defaults.sample_rate,
        voice=voice_by_key(raw.get("voice", "")).key,
        seed=seed if isinstance(seed, str) and seed.isdigit() and len(seed) <= 19 else "",
        autoplay=raw["autoplay"] if isinstance(raw.get("autoplay"), bool) else defaults.autoplay,
        theme=theme if theme in PALETTES else defaults.theme,
        last_dir=raw["last_dir"] if isinstance(raw.get("last_dir"), str) else "",
        geometry=geometry if isinstance(geometry, str) and GEOMETRY.match(geometry) else "",
    )


def save_settings(settings: Settings, path: Path | None = None) -> None:
    """Write settings atomically; a failure only loses the preferences, never the app."""
    path = Path(path) if path is not None else settings_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(asdict(settings), ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, path)
    except OSError:
        logger.warning("Could not save settings to %s", path, exc_info=True)


def estimate_seconds(text: str, speed: float = 1.0) -> float:
    """A rough duration for the status line, using the chunker's 18 letters per second."""
    letters = len("".join(text.split()))
    return letters / (LETTERS_PER_SECOND * max(float(speed), 0.01))


def format_seconds(seconds: float) -> str:
    seconds = max(0.0, float(seconds))
    if seconds < 1.0:
        return "<1 sn"
    if seconds < 60.0:
        return f"{seconds:.1f} sn".replace(".", ",")
    minutes, rest = divmod(int(round(seconds)), 60)
    return f"{minutes} dk {rest:02d} sn"


def format_count(value: int) -> str:
    return f"{value:,}".replace(",", ".")


def wav_seconds(path: Path) -> float:
    """Duration of a WAV file, used only when the synthesis length is not at hand."""
    try:
        with wave.open(str(path), "rb") as handle:
            rate = handle.getframerate() or 1
            return handle.getnframes() / rate
    except (OSError, EOFError, wave.Error):
        return 0.0


def parse_seed(value: str) -> int | None:
    """An empty field means a fresh random seed; anything else must be a non-negative integer."""
    value = value.strip()
    if not value:
        return None
    if not value.isdigit():
        raise ValueError("Seed negatif olmayan bir tam sayı olmalı.")
    seed = int(value)
    if seed >= 2**63:
        raise ValueError("Seed çok büyük.")
    return seed


def new_seed() -> int:
    return random.SystemRandom().randrange(2**31)


def resource_path(relative: str) -> Path:
    """A bundled file in a PyInstaller build, or a file next to this module in a checkout."""
    if getattr(sys, "frozen", False):
        base = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    else:
        base = Path(__file__).resolve().parent
    return base / relative


def find_weights() -> Path | None:
    """A folder with both weight files: next to the executable, bundled, or in the checkout."""
    candidates: list[Path] = []
    if getattr(sys, "frozen", False):
        candidates.append(Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent)) / "weights")
        candidates.append(Path(sys.executable).resolve().parent / "weights")
    else:
        candidates.append(Path(__file__).resolve().parents[2] / "weights")
    for folder in candidates:
        if (folder / "ema.pt").is_file() and (folder / "decoder.pt").is_file():
            return folder
    return None


def _number(value: Any, low: float, high: float, default: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):  # noqa: UP038 - keep 3.8 importable
        return default
    value = float(value)
    if value != value:  # NaN from a hand-edited JSON file
        return default
    return min(high, max(low, value))


def _prepare_streams() -> None:
    """A windowed PyInstaller build has no console; give ``print`` somewhere harmless to go."""
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding="utf-8")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w", encoding="utf-8")


def configure_logging() -> None:
    _prepare_streams()
    folder = cache_dir()
    try:
        folder.mkdir(parents=True, exist_ok=True)
        from logging.handlers import RotatingFileHandler

        handler = RotatingFileHandler(folder / "ema-lightning.log", maxBytes=1_000_000, backupCount=2,
                                      encoding="utf-8")
    except OSError:
        handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logger.handlers.clear()
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False


def _set_dpi_awareness() -> None:
    if sys.platform != "win32":
        return
    try:
        import ctypes

        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass


class App:
    """The single window. All model work happens off the Tk thread; events come back through a queue."""

    def __init__(self, root: tk.Tk, autoload: bool = True, settings: Settings | None = None):
        self.root = root
        self.settings = settings if settings is not None else load_settings()
        self.events: queue.Queue = queue.Queue()
        self.tts = None
        self.model_state = "idle"
        self._loading = False
        self._busy = False
        self._playing = False
        self._cancel = threading.Event()
        self._job_id = 0
        self._play_token = 0
        self._play_after: str | None = None
        self._silence_path: Path | None = None
        self._player: subprocess.Popen | None = None
        self._last_wav: Path | None = None
        self._temp_dir = Path(tempfile.mkdtemp(prefix="ema-lightning-"))
        self._themed: list[tuple[tk.Widget, str, str | None]] = []
        self._buttons: list[tk.Button] = []
        self._palette = PALETTES[self.settings.theme]

        self._setup_window()
        self._scale_for_dpi()
        self._setup_fonts()
        self._setup_styles()
        self._build_ui()
        self._apply_theme()
        self._refresh_actions()
        self._update_stats()
        self.root.after_idle(self._draw_scrollbar)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.bind("<Control-Return>", self._on_shortcut_speak)
        self.root.bind("<Control-s>", self._on_shortcut_save)
        self.root.bind("<Escape>", self._on_shortcut_stop)
        self.root.after(50, self._poll_events)
        if autoload:
            self.root.after(180, self._start_model_load)

    # ---------------------------------------------------------------- window and theme

    def _setup_window(self) -> None:
        self.root.title(APP_NAME)
        self.root.minsize(900, 680)
        if not self._restore_geometry():
            self._center(1000, 720)
        icon = resource_path("assets/ema-lightning.ico")
        if icon.is_file():
            try:
                self.root.iconbitmap(default=str(icon))
            except tk.TclError:
                pass

    def _restore_geometry(self) -> bool:
        """Restore the saved window box, but only if it still lands on a visible screen."""
        match = re.match(r"^(\d{3,4})x(\d{3,4})([+-]\d{1,5})([+-]\d{1,5})$", self.settings.geometry)
        if not match:
            return False
        width, height = int(match.group(1)), int(match.group(2))
        x, y = int(match.group(3)), int(match.group(4))
        screen_width, screen_height = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        # A window saved on a monitor that is no longer attached must not open off screen.
        if x + width < 120 or y + height < 120 or x > screen_width - 120 or y > screen_height - 120:
            return False
        self.root.geometry(f"{width}x{height}{x:+d}{y:+d}")
        return True

    def _center(self, width: int, height: int) -> None:
        self.root.update_idletasks()
        screen_width, screen_height = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        width, height = min(width, max(900, screen_width - 40)), min(height, max(680, screen_height - 60))
        x = max(0, (screen_width - width) // 2)
        y = max(0, (screen_height - height) // 3)
        self.root.geometry(f"{width}x{height}+{x}+{y}")

    def _scale_for_dpi(self) -> None:
        if sys.platform != "win32":
            return
        try:
            self.root.tk.call("tk", "scaling", self.root.winfo_fpixels("1i") / 72.0)
        except tk.TclError:
            pass

    def _setup_fonts(self) -> None:
        available = set(tkfont.families(self.root))
        family = next((name for name in ("Segoe UI", "Inter", "DejaVu Sans", "Helvetica") if name in available),
                      "TkDefaultFont")
        self.font_family = family
        self.font_title = tkfont.Font(family=family, size=19, weight="bold")
        self.font_subtitle = tkfont.Font(family=family, size=10)
        self.font_heading = tkfont.Font(family=family, size=12, weight="bold")
        self.font_body = tkfont.Font(family=family, size=11)
        self.font_editor = tkfont.Font(family=family, size=13)
        self.font_button = tkfont.Font(family=family, size=10, weight="bold")
        self.font_small = tkfont.Font(family=family, size=9)

    def _setup_styles(self) -> None:
        self.style = ttk.Style(self.root)
        try:
            self.style.theme_use("clam")
        except tk.TclError:
            pass

    def _register(self, widget: tk.Widget, background: str, foreground: str | None = None) -> tk.Widget:
        self._themed.append((widget, background, foreground))
        return widget

    def _apply_theme(self) -> None:
        p = PALETTES[self.settings.theme]
        self._palette = p
        self.root.configure(bg=p["bg"])
        for widget, background, foreground in self._themed:
            if isinstance(widget, tk.Menu):
                widget.configure(bg=p["surface"], fg=p["text"], activebackground=p["accent"],
                                 activeforeground=p["accent_text"])
                continue
            options: dict[str, Any] = {"bg": p[background]}
            if foreground:
                options["fg"] = p[foreground]
            if isinstance(widget, tk.Text):
                options.update(insertbackground=p["accent"], selectbackground=p["selection"],
                               selectforeground=p["text"], highlightbackground=p["border"],
                               highlightcolor=p["accent"])
            elif isinstance(widget, tk.Entry):
                options.update(insertbackground=p["accent"], selectbackground=p["selection"],
                               selectforeground=p["text"], highlightbackground=p["border"],
                               highlightcolor=p["accent"])
            elif isinstance(widget, tk.Checkbutton):
                options.update(selectcolor=p["surface2"], activebackground=p["surface"],
                               activeforeground=p["text"], highlightbackground=p["surface"])
            elif isinstance(widget, tk.Scrollbar):
                options.update(troughcolor=p["surface"], activebackground=p["border"],
                               borderwidth=0, relief="flat")
            elif isinstance(widget, tk.Frame) and int(widget.cget("highlightthickness") or 0):
                options["highlightbackground"] = p["border"]
            widget.configure(**options)
        self.root.option_add("*TCombobox*Listbox.background", p["surface"])
        self.root.option_add("*TCombobox*Listbox.foreground", p["text"])
        self.root.option_add("*TCombobox*Listbox.selectBackground", p["accent"])
        self.root.option_add("*TCombobox*Listbox.selectForeground", p["accent_text"])
        self.style.configure("EMA.TCombobox", fieldbackground=p["input"], background=p["surface2"],
                             foreground=p["text"], arrowcolor=p["muted"], bordercolor=p["border"],
                             lightcolor=p["border"], darkcolor=p["border"], padding=6)
        self.style.map("EMA.TCombobox", fieldbackground=[("readonly", p["input"])],
                       selectbackground=[("readonly", p["selection"])],
                       selectforeground=[("readonly", p["text"])],
                       background=[("active", p["surface2"])])
        self.style.configure("EMA.Horizontal.TScale", background=p["surface"], troughcolor=p["surface2"],
                             bordercolor=p["surface"], lightcolor=p["accent"], darkcolor=p["accent"],
                             sliderlength=18)
        self.style.configure("EMA.Horizontal.TProgressbar", background=p["accent"], troughcolor=p["surface2"],
                             bordercolor=p["surface2"], lightcolor=p["accent"], darkcolor=p["accent"],
                             thickness=6)
        self.style.configure("EMA.Vertical.TScrollbar", background=p["surface2"], troughcolor=p["surface"],
                             bordercolor=p["surface"], arrowcolor=p["muted"])
        self.style.map("EMA.Vertical.TScrollbar", background=[("active", p["border"])])
        for button in self._buttons:
            self._paint_button(button)
        if self.placeholder.winfo_exists():
            self.placeholder.configure(bg=p["input"], fg=p["muted"])
        if self.logo.winfo_exists():
            self._draw_logo()
        self._draw_scrollbar()

    def _draw_logo(self) -> None:
        p = self._palette
        self.logo.delete("all")
        self.logo.create_rectangle(0, 0, 44, 44, fill=p["accent"], outline="")
        self.logo.create_polygon(24, 7, 13, 25, 21, 25, 18, 37, 31, 19, 23, 19, fill=p["accent_text"],
                                 outline="")

    def _make_button(self, parent: tk.Widget, text: str, command: Any, kind: str = "secondary") -> tk.Button:
        button = tk.Button(parent, text=text, command=command, bd=0, relief="flat", highlightthickness=1,
                           takefocus=True, cursor="hand2", font=self.font_button, padx=14, pady=9)
        button._ema_kind = kind  # type: ignore[attr-defined]
        button._ema_enabled = True  # type: ignore[attr-defined]
        button._ema_hover = False  # type: ignore[attr-defined]
        button.bind("<Enter>", lambda _event: self._hover(button, True))
        button.bind("<Leave>", lambda _event: self._hover(button, False))
        self._register(button, "surface2", "text")
        self._buttons.append(button)
        self._paint_button(button)
        return button

    def _hover(self, button: tk.Button, entering: bool) -> None:
        button._ema_hover = entering  # type: ignore[attr-defined]
        self._paint_button(button)

    def _paint_button(self, button: tk.Button) -> None:
        p = self._palette
        kind = getattr(button, "_ema_kind", "secondary")
        enabled = getattr(button, "_ema_enabled", True)
        hover = getattr(button, "_ema_hover", False) and enabled
        if kind == "primary":
            background = p["accent_hover"] if hover else p["accent"]
            foreground = p["accent_text"]
            active = p["accent_hover"]
        elif kind == "danger":
            background = p["surface2"]
            foreground = p["danger"] if enabled else p["muted"]
            active = p["danger"]
        elif kind == "ghost":
            background = p["border"] if hover else p["surface2"]
            foreground = p["text"]
            active = p["border"]
        else:
            background = p["border"] if hover else p["surface2"]
            foreground = p["text"]
            active = p["border"]
        if not enabled:
            background = p["surface2"]
            foreground = p["muted"]
            active = background
        button.configure(bg=background, fg=foreground, activebackground=active, activeforeground=foreground,
                         disabledforeground=p["muted"], highlightbackground=p["surface"],
                         highlightcolor=p["accent"])

    def _set_enabled(self, button: tk.Button, enabled: bool) -> None:
        button._ema_enabled = enabled  # type: ignore[attr-defined]
        button.configure(state="normal" if enabled else "disabled")
        self._paint_button(button)

    # ---------------------------------------------------------------- layout

    def _build_ui(self) -> None:
        self.root.grid_rowconfigure(0, weight=0)
        self.root.grid_rowconfigure(1, weight=1)
        self.root.grid_rowconfigure(2, weight=0)
        self.root.grid_columnconfigure(0, weight=1)
        self._build_header()
        self._build_body()
        self._build_footer()

    def _build_header(self) -> None:
        header = tk.Frame(self.root, bd=0, highlightthickness=0)
        self._register(header, "bg")
        header.grid(row=0, column=0, sticky="ew", padx=22, pady=(16, 10))
        header.grid_columnconfigure(1, weight=1)
        self.logo = tk.Canvas(header, width=44, height=44, bd=0, highlightthickness=0)
        self._register(self.logo, "bg")
        self.logo.grid(row=0, column=0, rowspan=2, sticky="w")
        heading = tk.Frame(header, bd=0, highlightthickness=0)
        self._register(heading, "bg")
        heading.grid(row=0, column=1, sticky="sw", padx=(12, 0))
        title = tk.Label(heading, text=APP_TITLE, font=self.font_title, anchor="w")
        self._register(title, "bg", "text")
        title.pack(side="left")
        badge = tk.Label(heading, text=APP_KIND.upper(), font=self.font_small, padx=8, pady=2)
        self._register(badge, "surface2", "muted")
        badge.pack(side="left", padx=(10, 0), pady=(0, 3))
        subtitle = tk.Label(header, text="Türkçe metinden sese · çevrimdışı", font=self.font_subtitle, anchor="w")
        self._register(subtitle, "bg", "muted")
        subtitle.grid(row=1, column=1, sticky="nw", padx=(12, 0))
        about = self._make_button(header, "Hakkında", self._on_about, kind="ghost")
        about.grid(row=0, column=2, rowspan=2, padx=(8, 0))
        theme = self._make_button(header, "Tema", self._toggle_theme, kind="ghost")
        theme.grid(row=0, column=3, rowspan=2, padx=(8, 0))
        self._draw_logo()

    def _build_body(self) -> None:
        body = tk.Frame(self.root, bd=0, highlightthickness=0)
        self._register(body, "bg")
        body.grid(row=1, column=0, sticky="nsew", padx=22, pady=(0, 12))
        body.grid_rowconfigure(0, weight=1)
        body.grid_columnconfigure(0, weight=1)
        body.grid_columnconfigure(1, weight=0, minsize=300)
        self._build_editor(body)
        self._build_controls(body)

    def _card(self, parent: tk.Widget) -> tk.Frame:
        card = tk.Frame(parent, bd=0, highlightthickness=1, highlightbackground=self._palette["border"])
        self._register(card, "surface")
        return card

    def _build_editor(self, parent: tk.Widget) -> None:
        card = self._card(parent)
        card.grid(row=0, column=0, sticky="nsew", padx=(0, 14))
        card.grid_rowconfigure(1, weight=1)
        card.grid_columnconfigure(0, weight=1)
        top = tk.Frame(card, bd=0, highlightthickness=0)
        self._register(top, "surface")
        top.grid(row=0, column=0, sticky="ew", padx=18, pady=(16, 10))
        top.grid_columnconfigure(1, weight=1)
        heading = tk.Label(top, text="Metin", font=self.font_heading, anchor="w")
        self._register(heading, "surface", "text")
        heading.grid(row=0, column=0, sticky="w")
        badge = tk.Label(top, text="TÜRKÇE", font=self.font_small, padx=8, pady=2)
        self._register(badge, "surface2", "muted")
        badge.grid(row=0, column=1, sticky="w", padx=(10, 0))
        example = self._make_button(top, "Örnek metin", self._on_example, kind="ghost")
        example.configure(padx=8, pady=3, font=self.font_small)
        example.grid(row=0, column=2, padx=(8, 0))
        clear = self._make_button(top, "Temizle", self._on_clear, kind="ghost")
        clear.configure(padx=8, pady=3, font=self.font_small)
        clear.grid(row=0, column=3, padx=(8, 0))

        holder = tk.Frame(card, bd=0, highlightthickness=0)
        self._register(holder, "surface")
        holder.grid(row=1, column=0, sticky="nsew", padx=18)
        holder.grid_rowconfigure(0, weight=1)
        holder.grid_columnconfigure(0, weight=1)
        self.text = tk.Text(holder, wrap="word", undo=True, maxundo=50, relief="flat", bd=0,
                            highlightthickness=1, padx=14, pady=12, font=self.font_editor,
                            spacing1=2, spacing3=4)
        self._register(self.text, "input", "text")
        self.text.grid(row=0, column=0, sticky="nsew")
        self.scroll_canvas = tk.Canvas(holder, width=10, bd=0, highlightthickness=0)
        self._register(self.scroll_canvas, "surface")
        self.scroll_canvas.grid(row=0, column=1, sticky="ns", padx=(6, 0))
        self.scroll_canvas.bind("<Configure>", lambda _event: self.root.after_idle(self._draw_scrollbar))
        self.scroll_canvas.bind("<Button-1>", self._scroll_drag)
        self.scroll_canvas.bind("<B1-Motion>", self._scroll_drag)
        self.text.configure(yscrollcommand=self._on_text_scroll)
        self.text.bind("<KeyRelease>", lambda _event: self.root.after_idle(self._draw_scrollbar))
        self.text.bind("<MouseWheel>", lambda _event: self.root.after_idle(self._draw_scrollbar))
        self.text.bind("<Button-4>", lambda _event: self.root.after_idle(self._draw_scrollbar))
        self.text.bind("<Button-5>", lambda _event: self.root.after_idle(self._draw_scrollbar))
        self.placeholder = tk.Label(self.text, text="Türkçe metninizi buraya yazın…", font=self.font_editor)
        self._register(self.placeholder, "input", "muted")
        self.placeholder.place(x=16, y=14)
        self.placeholder.bind("<Button-1>", lambda _event: self.text.focus_set())
        self.text.bind("<FocusIn>", lambda _event: self._update_placeholder())
        self.text.bind("<FocusOut>", lambda _event: self._update_placeholder())
        self.text.bind("<<Modified>>", self._on_text_modified)
        self.text.bind("<Button-3>", self._show_context_menu)
        self.text.bind("<Control-Return>", self._on_shortcut_speak)
        self.text.bind("<Control-s>", self._on_shortcut_save)
        self.text.bind("<Escape>", self._on_shortcut_stop)
        self.text.edit_modified(False)
        self._build_context_menu()

        stats = tk.Label(card, text="", font=self.font_small, anchor="w")
        self._register(stats, "surface", "muted")
        stats.grid(row=2, column=0, sticky="ew", padx=20, pady=(10, 14))
        self.stats = stats

    def _build_controls(self, parent: tk.Widget) -> None:
        column = tk.Frame(parent, bd=0, highlightthickness=0)
        self._register(column, "bg")
        column.grid(row=0, column=1, sticky="nsew")
        column.grid_columnconfigure(0, weight=1)
        column.grid_rowconfigure(1, weight=1)
        settings_card = self._card(column)
        settings_card.grid(row=0, column=0, sticky="ew")
        settings_card.grid_columnconfigure(0, weight=1)
        heading = tk.Label(settings_card, text="Ses ayarları", font=self.font_heading, anchor="w")
        self._register(heading, "surface", "text")
        heading.grid(row=0, column=0, sticky="ew", padx=18, pady=(16, 4))

        speed_row = tk.Frame(settings_card, bd=0, highlightthickness=0)
        self._register(speed_row, "surface")
        speed_row.grid(row=1, column=0, sticky="ew", padx=18, pady=(6, 0))
        speed_row.grid_columnconfigure(1, weight=1)
        speed_label = tk.Label(speed_row, text="Hız", font=self.font_body, anchor="w")
        self._register(speed_label, "surface", "muted")
        speed_label.grid(row=0, column=0, sticky="w")
        self.speed_value = tk.Label(speed_row, text="1,00×", font=self.font_body, anchor="e")
        self._register(self.speed_value, "surface", "text")
        self.speed_value.grid(row=0, column=2, sticky="e")
        self.speed_var = tk.DoubleVar(value=self.settings.speed)
        self.speed = ttk.Scale(settings_card, from_=0.5, to=2.0, variable=self.speed_var,
                               command=self._on_speed, style="EMA.Horizontal.TScale")
        self.speed.grid(row=2, column=0, sticky="ew", padx=18, pady=(2, 6))

        voice_row = tk.Frame(settings_card, bd=0, highlightthickness=0)
        self._register(voice_row, "surface")
        voice_row.grid(row=3, column=0, sticky="ew", padx=18, pady=(2, 0))
        voice_row.grid_columnconfigure(1, weight=1)
        voice_label = tk.Label(voice_row, text="Ses", font=self.font_body, anchor="w")
        self._register(voice_label, "surface", "muted")
        voice_label.grid(row=0, column=0, sticky="w")
        self.voice_var = tk.StringVar(value=voice_by_key(self.settings.voice).label)
        self.voice = ttk.Combobox(voice_row, textvariable=self.voice_var, state="readonly", width=14,
                                  values=[voice.label for voice in VOICES], style="EMA.TCombobox")
        self.voice.grid(row=0, column=1, sticky="e")

        rate_row = tk.Frame(settings_card, bd=0, highlightthickness=0)
        self._register(rate_row, "surface")
        rate_row.grid(row=4, column=0, sticky="ew", padx=18, pady=(6, 0))
        rate_row.grid_columnconfigure(1, weight=1)
        rate_label = tk.Label(rate_row, text="Örnekleme", font=self.font_body, anchor="w")
        self._register(rate_label, "surface", "muted")
        rate_label.grid(row=0, column=0, sticky="w")
        self.rate_var = tk.StringVar(value=RATE_LABELS[self.settings.sample_rate])
        self.rate = ttk.Combobox(rate_row, textvariable=self.rate_var, values=list(RATE_LABELS.values()),
                                 state="readonly", width=14, style="EMA.TCombobox")
        self.rate.grid(row=0, column=1, sticky="e")

        seed_row = tk.Frame(settings_card, bd=0, highlightthickness=0)
        self._register(seed_row, "surface")
        seed_row.grid(row=5, column=0, sticky="ew", padx=18, pady=(8, 0))
        seed_row.grid_columnconfigure(1, weight=1)
        seed_label = tk.Label(seed_row, text="Seed", font=self.font_body, anchor="w")
        self._register(seed_label, "surface", "muted")
        seed_label.grid(row=0, column=0, sticky="w")
        validator = (self.root.register(self._validate_seed), "%P")
        self.seed_var = tk.StringVar(value=self.settings.seed)
        self.seed = tk.Entry(seed_row, textvariable=self.seed_var, width=12, relief="flat", bd=0,
                             highlightthickness=1, font=self.font_body, validate="key",
                             validatecommand=validator)
        self._register(self.seed, "input", "text")
        self.seed.grid(row=0, column=1, sticky="ew", padx=(10, 8), ipady=5)
        random_button = self._make_button(seed_row, "Rastgele", self._random_seed, kind="ghost")
        random_button.configure(padx=8, pady=4, font=self.font_small)
        random_button.grid(row=0, column=2)
        hint = tk.Label(settings_card, text="İnce seçeneği aynı sesin perdesini yükseltir.",
                        font=self.font_small, anchor="w", wraplength=260, justify="left")
        self._register(hint, "surface", "muted")
        hint.grid(row=6, column=0, sticky="ew", padx=18, pady=(4, 8))

        self.autoplay_var = tk.BooleanVar(value=self.settings.autoplay)
        autoplay = tk.Checkbutton(settings_card, text="Üretince otomatik oynat", variable=self.autoplay_var,
                                  font=self.font_body, anchor="w", bd=0, highlightthickness=0,
                                  command=self._refresh_actions)
        self._register(autoplay, "surface", "text")
        autoplay.grid(row=7, column=0, sticky="ew", padx=18, pady=(0, 12))

        actions = self._card(column)
        actions.grid(row=1, column=0, sticky="nsew", pady=(14, 0))
        actions.grid_columnconfigure(0, weight=1)
        actions.grid_rowconfigure(4, weight=1)
        heading = tk.Label(actions, text="Çıktı", font=self.font_heading, anchor="w")
        self._register(heading, "surface", "text")
        heading.grid(row=0, column=0, sticky="ew", padx=18, pady=(14, 8))
        self.speak_button = self._make_button(actions, "▶  Seslendir", self._on_speak, kind="primary")
        self.speak_button.grid(row=1, column=0, sticky="ew", padx=18, pady=(0, 7))
        self.stop_button = self._make_button(actions, "■  Durdur", self._on_stop, kind="danger")
        self.stop_button.grid(row=2, column=0, sticky="ew", padx=18, pady=(0, 7))
        self.save_button = self._make_button(actions, "⤓  WAV Kaydet", self._on_save, kind="secondary")
        self.save_button.grid(row=3, column=0, sticky="ew", padx=18, pady=(0, 7))
        self.last_label = tk.Label(actions, text="Henüz ses üretilmedi.", font=self.font_small, anchor="w",
                                   wraplength=260, justify="left")
        self._register(self.last_label, "surface", "muted")
        self.last_label.grid(row=5, column=0, sticky="ew", padx=18, pady=(4, 12))

    def _build_footer(self) -> None:
        footer = tk.Frame(self.root, bd=0, highlightthickness=0)
        self._register(footer, "bg")
        footer.grid(row=2, column=0, sticky="ew", padx=22, pady=(0, 14))
        footer.grid_columnconfigure(1, weight=1)
        self.status_dot = tk.Canvas(footer, width=12, height=12, bd=0, highlightthickness=0)
        self._register(self.status_dot, "bg")
        self.status_dot.grid(row=0, column=0, sticky="w")
        self.status_label = tk.Label(footer, text="Başlatılıyor…", font=self.font_small, anchor="w")
        self._register(self.status_label, "bg", "muted")
        self.status_label.grid(row=0, column=1, sticky="w", padx=(7, 0))
        self.progress = ttk.Progressbar(footer, mode="indeterminate", length=120,
                                        style="EMA.Horizontal.TProgressbar")
        self.progress.grid(row=0, column=2, padx=(12, 12))
        self.progress.grid_remove()
        self.retry_button = self._make_button(footer, "Yeniden dene", self._start_model_load, kind="ghost")
        self.retry_button.configure(padx=8, pady=3, font=self.font_small)
        self.retry_button.grid(row=0, column=3, padx=(0, 12))
        self.retry_button.grid_remove()
        hint = tk.Label(footer, text="Ctrl+Enter seslendir · Esc durdur · Ctrl+S kaydet",
                        font=self.font_small, anchor="e")
        self._register(hint, "bg", "muted")
        hint.grid(row=0, column=4, sticky="e")
        self._set_status("Başlatılıyor…", "muted")

    def _build_context_menu(self) -> None:
        self.menu = tk.Menu(self.root, tearoff=0, bd=0)
        self.menu.add_command(label="Kes", command=lambda: self.text.event_generate("<<Cut>>"))
        self.menu.add_command(label="Kopyala", command=lambda: self.text.event_generate("<<Copy>>"))
        self.menu.add_command(label="Yapıştır", command=lambda: self.text.event_generate("<<Paste>>"))
        self.menu.add_separator()
        self.menu.add_command(label="Tümünü seç", command=lambda: self.text.tag_add("sel", "1.0", "end"))
        self._register(self.menu, "surface", "text")

    # ---------------------------------------------------------------- small callbacks

    def _validate_seed(self, value: str) -> bool:
        return value == "" or (value.isdigit() and len(value) <= 19)

    def _on_speed(self, _value: str | None = None) -> None:
        speed = float(self.speed_var.get())
        self.speed_value.configure(text=f"{speed:.2f}×".replace(".", ","))
        self._update_stats()

    def _on_text_modified(self, _event: Any = None) -> None:
        if self.text.edit_modified():
            self.text.edit_modified(False)
            self._update_placeholder()
            self._update_stats()
            self.root.after_idle(self._draw_scrollbar)

    def _update_placeholder(self) -> None:
        empty = not self.text.get("1.0", "end-1c")
        focused = self.root.focus_get() is self.text
        if empty and not focused:
            self.placeholder.place(x=16, y=14)
        else:
            self.placeholder.place_forget()

    def _update_stats(self) -> None:
        text = self.text.get("1.0", "end-1c")
        speed = float(self.speed_var.get()) if hasattr(self, "speed_var") else 1.0
        chars, words = len(text), len(text.split())
        self.stats.configure(text=f"{format_count(chars)} karakter · {format_count(words)} kelime · "
                                  f"~{format_seconds(estimate_seconds(text, speed))}")

    def _on_text_scroll(self, _first: str, _last: str) -> None:
        self._draw_scrollbar()

    def _draw_scrollbar(self) -> None:
        if not hasattr(self, "scroll_canvas") or not self.scroll_canvas.winfo_exists():
            return
        p = self._palette
        width, height = self.scroll_canvas.winfo_width(), self.scroll_canvas.winfo_height()
        self.scroll_canvas.delete("all")
        first, last = self.text.yview()
        if height <= 1 or last - first >= 0.999:
            return
        thumb = max(22, int((last - first) * height))
        thumb = min(thumb, height)
        top = min(int(first * height), height - thumb)
        left, right = 2, max(3, width - 2)
        self.scroll_canvas.create_oval(left, top, right, top + 6, fill=p["muted"], outline="")
        self.scroll_canvas.create_rectangle(left, top + 3, right, top + thumb - 3, fill=p["muted"], outline="")
        self.scroll_canvas.create_oval(left, top + thumb - 6, right, top + thumb, fill=p["muted"], outline="")

    def _scroll_drag(self, event: tk.Event) -> None:
        height = max(1, self.scroll_canvas.winfo_height())
        self.text.yview_moveto(max(0.0, min(1.0, event.y / height)))
        self.root.after_idle(self._draw_scrollbar)

    def _show_context_menu(self, event: tk.Event) -> None:
        try:
            self.menu.tk_popup(event.x_root, event.y_root)
        finally:
            self.menu.grab_release()

    def _on_shortcut_speak(self, _event: Any = None) -> str:
        self._on_speak()
        return "break"

    def _on_shortcut_stop(self, _event: Any = None) -> str:
        self._on_stop()
        return "break"

    def _on_shortcut_save(self, _event: Any = None) -> str:
        self._on_save()
        return "break"

    def _random_seed(self) -> None:
        self.seed_var.set(str(new_seed()))

    def _on_clear(self) -> None:
        self.text.delete("1.0", "end")
        self._update_placeholder()
        self._update_stats()
        self.text.focus_set()

    def _on_example(self) -> None:
        if self.text.get("1.0", "end-1c").strip():
            if not messagebox.askyesno(APP_NAME, "Mevcut metnin yerine örnek metin yazılsın mı?", parent=self.root):
                return
        self.text.delete("1.0", "end")
        self.text.insert("1.0", EXAMPLE_TEXT)
        self.text.focus_set()
        self._update_placeholder()
        self._update_stats()

    def _on_about(self) -> None:
        messagebox.showinfo(
            f"{APP_NAME} hakkında",
            f"{APP_NAME} {APP_VERSION}\n\n"
            "EMA Lightning motoru üzerinde Tkinter tabanlı masaüstü arayüzü.\n"
            "Türkçe metinden sese, tamamen çevrimdışı.\n"
            "Model: 8,6M parametre, Apache-2.0.\n\n"
            "Ses seçenekleri\n"
            "Model tek konuşmacılıdır. 'İnce' seçeneği aynı sesin perdesini ve\n"
            "formantlarını dönüştürür; ayrı bir konuşmacı kaydı değildir.\n\n"
            "Kısayollar:\n"
            "Ctrl+Enter  Seslendir\n"
            "Esc  Durdur\n"
            "Ctrl+S  WAV kaydet\n\n"
            "Metniniz kaydedilmez; yalnızca ses dosyası oluşturulur.",
            parent=self.root,
        )

    def _toggle_theme(self) -> None:
        self.settings.theme = "light" if self.settings.theme == "dark" else "dark"
        self._apply_theme()
        self._sync_settings()
        save_settings(self.settings)

    def _sync_settings(self) -> None:
        """Copy the widgets' current values into the settings that are written to disk."""
        self.settings.speed = round(float(self.speed_var.get()), 2)
        self.settings.sample_rate = next(
            (value for value, label in RATE_LABELS.items() if label == self.rate_var.get()), 48000)
        self.settings.voice = voice_by_label(self.voice_var.get()).key
        seed = self.seed_var.get().strip()
        self.settings.seed = seed if seed.isdigit() else ""
        self.settings.autoplay = bool(self.autoplay_var.get())
        geometry = self.root.geometry()
        if GEOMETRY.match(geometry):
            self.settings.geometry = geometry

    # ---------------------------------------------------------------- status and actions

    def _set_status(self, text: str, tone: str = "muted") -> None:
        self.status_label.configure(text=text)
        colors = {"muted": self._palette["muted"], "accent": self._palette["accent"],
                  "success": self._palette["success"], "warning": self._palette["warning"],
                  "danger": self._palette["danger"]}
        self.status_dot.delete("all")
        self.status_dot.create_oval(2, 2, 10, 10, fill=colors.get(tone, self._palette["muted"]), outline="")

    def _start_progress(self) -> None:
        self.progress.grid()
        self.progress.start(12)

    def _stop_progress(self) -> None:
        self.progress.stop()
        self.progress.grid_remove()

    def _refresh_actions(self) -> None:
        ready = self.model_state == "ready"
        self._set_enabled(self.speak_button, ready and not self._busy)
        self._set_enabled(self.stop_button, self._busy or self._playing)
        self._set_enabled(self.save_button, self._last_wav is not None and not self._busy)

    def _start_model_load(self) -> None:
        if self._loading:
            return
        self._loading = True
        self.model_state = "loading"
        self.retry_button.grid_remove()
        self._set_status("Model hazırlanıyor… ilk çalıştırmada indirilebilir.", "warning")
        self._start_progress()
        self._refresh_actions()
        threading.Thread(target=self._load_worker, name="ema-gui-load", daemon=True).start()

    def _load_worker(self) -> None:
        try:
            from ema_lightning import EMA

            weights = find_weights()
            logger.info("Loading model (bundled weights: %s)", bool(weights))
            tts = EMA(weights=weights) if weights is not None else EMA()
            if tts.device.type == "cuda":
                self.events.put(("model_stage", "GPU hızlandırması hazırlanıyor…"))
                try:
                    tts.lightning()
                except Exception:
                    logger.warning("lightning() failed; continuing on the plain path", exc_info=True)
                import torch

                device = f"CUDA · {torch.cuda.get_device_name(0)}"
            else:
                device = "CPU"
            self.events.put(("model_ready", tts, device))
        except Exception as error:
            logger.exception("Model could not be loaded")
            self.events.put(("model_error", str(error)))

    def _on_speak(self) -> None:
        if self.model_state != "ready" or self.tts is None:
            self._set_status("Model henüz hazır değil.", "warning")
            return
        if self._busy:
            return
        text = self.text.get("1.0", "end-1c").strip()
        if not text:
            self._set_status("Önce bir metin yazın.", "warning")
            self.text.focus_set()
            return
        try:
            seed = parse_seed(self.seed_var.get())
        except ValueError as error:
            messagebox.showerror(APP_NAME, str(error), parent=self.root)
            self.seed.focus_set()
            return
        if seed is None:
            seed = new_seed()
            self.seed_var.set(str(seed))
        speed = round(float(self.speed_var.get()), 2)
        rate = next((value for value, label in RATE_LABELS.items() if label == self.rate_var.get()), 48000)
        voice = voice_by_label(self.voice_var.get())
        # A lighter voice comes from resampling, which shortens the audio, so the model
        # has to speak slower to keep the length the speed slider promises.
        model_pace = round(model_speed(speed, voice.pitch), 3)
        self._stop_playback()
        self._cancel.clear()
        self._job_id += 1
        job = self._job_id
        self._busy = True
        if self._last_wav is not None:
            try:
                self._last_wav.unlink()
            except OSError:
                pass
        self._last_wav = None
        self.last_label.configure(text="Ses üretiliyor…")
        self._set_status("Ses üretiliyor…", "accent")
        self._start_progress()
        self._refresh_actions()
        logger.info("Synthesis: chars=%d speed=%.2f voice=%s rate=%d seed=%d",
                    len(text), speed, voice.key, rate, seed)
        threading.Thread(target=self._synthesize, args=(job, text, model_pace, seed, rate, voice.pitch),
                         name="ema-gui-synthesize", daemon=True).start()

    def _synthesize(self, job: int, text: str, speed: float, seed: int, rate: int, pitch: float) -> None:
        stream = None
        try:
            import numpy as np

            from ema_lightning.audio import write_wav

            chunks = []
            produced = 0
            stream = self.tts.stream(text, speed=speed, seed=seed, sample_rate=rate)
            for chunk in stream:
                if self._cancel.is_set():
                    break
                chunks.append(chunk)
                produced += len(chunk)
                self.events.put(("progress", job, produced / rate))
            if self._cancel.is_set():
                self.events.put(("cancelled", job))
                return
            if not chunks:
                self.events.put(("empty", job))
                return
            audio = np.concatenate(chunks)
            if abs(pitch - 1.0) > 1e-4:
                audio = shift(audio, pitch)
            path = self._temp_dir / f"preview-{job}.wav"
            write_wav(path, audio, rate)
            self.events.put(("done", job, path, len(audio) / rate, seed))
        except Exception as error:
            logger.exception("Synthesis failed")
            self.events.put(("error", job, str(error)))
        finally:
            if stream is not None:
                try:
                    stream.close()
                except Exception:
                    pass

    def _on_stop(self) -> None:
        was_busy, was_playing = self._busy, self._playing
        if was_busy:
            self._cancel.set()
        if was_playing:
            self._stop_playback()
        if was_busy or was_playing:
            self._set_status("Durduruluyor…" if was_busy else "Durduruldu", "warning")
            self._refresh_actions()

    def _stop_playback(self) -> None:
        """Silence the player immediately.

        Blocking ``winsound.PlaySound(..., SND_FILENAME)`` in a worker thread could not be
        interrupted: the stop call waited until the file ended, which made the Stop button
        look broken. Playback is asynchronous now, so this returns at once.
        """
        self._play_token += 1
        self._playing = False
        if self._play_after is not None:
            try:
                self.root.after_cancel(self._play_after)
            except (tk.TclError, ValueError):
                pass
            self._play_after = None
        if sys.platform == "win32":
            try:
                import winsound

                winsound.PlaySound(None, 0)
            except Exception:
                logger.debug("winsound could not stop the sound", exc_info=True)
        if self._player is not None:
            try:
                self._player.terminate()
            except Exception:
                pass
            self._player = None
        self._refresh_actions()

    def _ensure_silence_wav(self) -> Path:
        """Create a short silence WAV to wake up the audio device before real playback."""
        if self._silence_path is not None and self._silence_path.is_file():
            return self._silence_path
        silence_path = self._temp_dir / "wakeup-silence.wav"
        rate = 48000
        duration_ms = 300
        frames = int(rate * duration_ms / 1000)
        with wave.open(str(silence_path), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(rate)
            w.writeframes(b"\x00\x00" * frames)
        self._silence_path = silence_path
        return silence_path

    def _play_real(self, path: Path, token: int, seconds: float) -> None:
        """Actually play the WAV file after the device has been warmed up."""
        if token != self._play_token or not self._playing:
            return
        try:
            import winsound

            winsound.PlaySound(str(path), winsound.SND_FILENAME | winsound.SND_ASYNC)
        except Exception as error:
            logger.warning("Playback failed", exc_info=True)
            self._playing = False
            self._set_status("Ses oynatılamadı", "danger")
            self._refresh_actions()
            messagebox.showerror(APP_NAME, f"Ses oynatılamadı:\n{error}", parent=self.root)
            return
        self._play_after = self.root.after(max(250, int(seconds * 1000) + 120),
                                           self._finish_playback, token)

    def _play(self, path: Path, duration: float = 0.0) -> None:
        """Play a preview. Windows plays asynchronously and schedules its own end callback."""
        self._stop_playback()
        self._playing = True
        self._play_token += 1
        token = self._play_token
        self._set_status("Oynatılıyor…", "accent")
        self._refresh_actions()
        if sys.platform == "win32":
            try:
                import winsound

                # Warm up the audio device with a short silence so the first
                # word of the real audio is not cut off by device startup latency.
                silence_path = self._ensure_silence_wav()
                winsound.PlaySound(str(silence_path), winsound.SND_FILENAME | winsound.SND_ASYNC)

                seconds = duration if duration > 0 else wav_seconds(path)
                self._play_after = self.root.after(350, lambda: self._play_real(path, token, seconds))
            except Exception as error:
                logger.warning("Playback failed", exc_info=True)
                self._playing = False
                self._set_status("Ses oynatılamadı", "danger")
                self._refresh_actions()
                messagebox.showerror(APP_NAME, f"Ses oynatılamadı:\n{error}", parent=self.root)
                return
        else:
            threading.Thread(target=self._play_worker, args=(path, token), name="ema-gui-play",
                             daemon=True).start()

    def _finish_playback(self, token: int) -> None:
        self._play_after = None
        if token != self._play_token or not self._playing:
            return
        self._playing = False
        self._set_status("Hazır", "success")
        self._refresh_actions()

    def _play_worker(self, path: Path, token: int) -> None:
        try:
            player = next((command for command in (
                ["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet", str(path)],
                ["aplay", "-q", str(path)],
            ) if shutil.which(command[0])), None)
            if player is None:
                raise RuntimeError("Ses oynatıcı bulunamadı. WAV dosyasını kaydedip açabilirsiniz.")
            self._player = subprocess.Popen(player, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self._player.wait()
            self._player = None
            self.events.put(("played", token))
        except Exception as error:
            logger.warning("Playback failed", exc_info=True)
            self.events.put(("play_error", token, str(error)))

    def _on_save(self) -> None:
        if self._last_wav is None or not self._last_wav.is_file():
            return
        initial_dir = self.settings.last_dir
        if not initial_dir or not Path(initial_dir).is_dir():
            initial_dir = str(Path.home() / "Music")
            if not Path(initial_dir).is_dir():
                initial_dir = str(Path.home())
        initial = f"ema-lightning-{time.strftime('%Y%m%d-%H%M%S')}.wav"
        target = filedialog.asksaveasfilename(
            parent=self.root, title="WAV olarak kaydet", defaultextension=".wav",
            initialdir=initial_dir, initialfile=initial,
            filetypes=[("WAV ses dosyası", "*.wav"), ("Tüm dosyalar", "*.*")],
        )
        if not target:
            return
        try:
            shutil.copyfile(self._last_wav, target)
        except OSError as error:
            logger.warning("Could not save WAV", exc_info=True)
            messagebox.showerror(APP_NAME, f"Dosya kaydedilemedi:\n{error}", parent=self.root)
            return
        self.settings.last_dir = str(Path(target).parent)
        save_settings(self.settings)
        self._set_status(f"Kaydedildi: {Path(target).name}", "success")

    # ---------------------------------------------------------------- events

    def _poll_events(self) -> None:
        try:
            while True:
                event = self.events.get_nowait()
                try:
                    self._handle_event(event)
                except Exception:  # one bad event must not stop the queue for good
                    logger.exception("Could not handle the %s event", event[0] if event else "?")
        except queue.Empty:
            pass
        self.root.after(50, self._poll_events)

    def _handle_event(self, event: tuple[Any, ...]) -> None:
        kind = event[0]
        if kind == "model_stage":
            self._set_status(event[1], "warning")
        elif kind == "model_ready":
            self._on_model_ready(event[1], event[2])
        elif kind == "model_error":
            self._on_model_error(event[1])
        elif kind == "progress":
            if event[1] == self._job_id:
                self._set_status(f"Ses üretiliyor… {format_seconds(event[2])}", "accent")
        elif kind == "done":
            self._on_done(event[1], event[2], event[3], event[4])
        elif kind == "cancelled":
            self._on_cancelled(event[1])
        elif kind == "empty":
            self._on_empty(event[1])
        elif kind == "error":
            self._on_error(event[1], event[2])
        elif kind == "played":
            if event[1] == self._play_token:
                self._playing = False
                self._set_status("Hazır", "success")
                self._refresh_actions()
        elif kind == "play_error":
            if event[1] == self._play_token:
                self._playing = False
                self._set_status("Ses oynatılamadı", "danger")
                messagebox.showerror(APP_NAME, event[2], parent=self.root)
                self._refresh_actions()

    def _on_model_ready(self, tts: Any, device: str) -> None:
        self.tts = tts
        self._loading = False
        self.model_state = "ready"
        self._stop_progress()
        self._set_status(f"Hazır · {device}", "success")
        self._refresh_actions()
        logger.info("Model ready on %s", device)

    def _on_model_error(self, message: str) -> None:
        self._loading = False
        self.model_state = "error"
        self._stop_progress()
        self._set_status("Model yüklenemedi", "danger")
        self.retry_button.grid()
        self._refresh_actions()
        messagebox.showerror(
            APP_NAME,
            "Model yüklenemedi.\n\n"
            "İlk çalıştırmada internet bağlantısı ve yaklaşık 34 MB indirme gerekir. "
            "Bağlantınızı kontrol edip 'Yeniden dene' düğmesini kullanın.\n\n"
            f"Ayrıntı: {message[:400]}",
            parent=self.root,
        )

    def _on_done(self, job: int, path: Path, duration: float, seed: int) -> None:
        if job != self._job_id:
            try:
                Path(path).unlink()
            except OSError:
                pass
            return
        if self._last_wav is not None and self._last_wav != path:
            try:
                self._last_wav.unlink()
            except OSError:
                pass
        self._last_wav = Path(path)
        self._busy = False
        self._stop_progress()
        self._set_status(f"Hazır · {format_seconds(duration)}", "success")
        self.last_label.configure(text=f"Son üretim: {format_seconds(duration)} · seed {seed}")
        self._refresh_actions()
        logger.info("Synthesis done: %.2f s, seed=%d", duration, seed)
        if self.autoplay_var.get():
            self._play(self._last_wav, duration)

    def _on_cancelled(self, job: int) -> None:
        if job != self._job_id:
            return
        self._busy = False
        self._stop_progress()
        self._set_status("Durduruldu", "warning")
        self.last_label.configure(text="Üretim durduruldu.")
        self._refresh_actions()

    def _on_empty(self, job: int) -> None:
        if job != self._job_id:
            return
        self._busy = False
        self._stop_progress()
        self._set_status("Okunacak metin bulunamadı", "warning")
        self.last_label.configure(text="Metin yalnızca noktalama veya boşluk içeriyor.")
        self._refresh_actions()

    def _on_error(self, job: int, message: str) -> None:
        if job != self._job_id:
            return
        self._busy = False
        self._stop_progress()
        self._set_status("Ses üretilemedi", "danger")
        self.last_label.configure(text="Ses üretilemedi. Ayrıntılar günlük dosyasında.")
        self._refresh_actions()
        messagebox.showerror(APP_NAME, f"Ses üretilemedi:\n{message[:400]}", parent=self.root)

    # ---------------------------------------------------------------- shutdown

    def _on_close(self) -> None:
        if self._busy or self._playing:
            if not messagebox.askyesno(APP_NAME, "Ses işlemi sürüyor. Yine de çıkılsın mı?", parent=self.root):
                return
        self._cancel.set()
        self._stop_playback()
        self._sync_settings()
        save_settings(self.settings)
        shutil.rmtree(self._temp_dir, ignore_errors=True)
        self.root.destroy()


def main() -> None:
    """Start the desktop app."""
    configure_logging()
    _set_dpi_awareness()
    root = tk.Tk()
    root.ema_app = App(root)  # keep a strong reference for the whole session
    root.mainloop()


if __name__ == "__main__":
    main()
