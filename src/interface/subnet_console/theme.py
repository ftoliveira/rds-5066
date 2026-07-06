"""Design tokens for the S5066 Subnet Console.

Colours, fonts and helpers reproduced from the ``S5066 Subnet Console`` design
mockup. Everything visual funnels through :class:`Theme` so the accent colour is
swappable at runtime (matching the mockup's ``accentColor`` prop) and so widgets
never hard-code a hex string that should track the theme.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

# ---------------------------------------------------------------------------
# Static neutral palette (accent-independent). Names mirror the mockup usage.
# ---------------------------------------------------------------------------
DESKTOP_BG = "#b9bcc1"        # gray desktop behind the window
WINDOW_BG = "#d4d6d9"         # window body
WINDOW_BORDER = "#9b9ea3"

TITLE_TOP = "#dfe1e4"         # title bar gradient stops
TITLE_BOTTOM = "#cdd0d4"
TITLE_BORDER = "#aeb1b6"
TITLE_FG = "#45484d"

TL_RED = "#e0564b"            # traffic-light dots (close / min / max)
TL_RED_BORDER = "#c2473d"
TL_AMBER = "#e6b23c"
TL_AMBER_BORDER = "#c9991f"
TL_GREEN = "#56b46a"
TL_GREEN_BORDER = "#459a57"

MENU_BG = "#e9eaed"
MENU_BORDER = "#c4c6cb"
MENU_HOVER = "#d6dadf"

TOOLBAR_TOP = "#f0f1f3"
TOOLBAR_BOTTOM = "#e3e5e8"

SIDEBAR_BG = "#e7e8eb"
SIDEBAR_DIV = "#d3d5d9"
SIDEBAR_HOVER = "#dcdee2"
SIDEBAR_CARD = "#dcdee2"
SIDEBAR_CARD_BORDER = "#cccfd4"

CONTENT_BG = "#f2f3f5"

CARD_BG = "#ffffff"
CARD_BORDER = "#d3d5d9"
CARD_HEADER_BG = "#eceef1"

INPUT_BG = "#f7f8f9"
INPUT_BG_ALT = "#fbfbfc"
INPUT_BORDER = "#c4c6cb"

ROW_DIV = "#eef0f2"
ROW_DIV_FAINT = "#f1f2f4"
HAIRLINE = "#e4e6e9"

# Text
FG = "#1c1e22"                # strongest heading
FG_BODY = "#25282c"          # body text
FG_MUTED = "#5a5e64"         # secondary
FG_DIM = "#6b6f75"           # tertiary
FG_FAINT = "#898d93"         # labels / captions
FG_GHOST = "#9a9ea4"         # disabled-ish
FG_GHOST2 = "#a0a4aa"

STATUS_TOP = "#e3e5e8"
STATUS_BOTTOM = "#d4d6d9"
STATUS_BORDER = "#b4b7bc"
STATUS_FG = "#4a4d52"

# Semantic (accent-independent)
GREEN = "#2f8f5b"
GREEN_DARK = "#1f6e43"
GREEN_BG = "#e3f0e8"
GREEN_BORDER = "#bcd9c8"
GREEN_HALO = "#cfe8d9"
AMBER = "#b9821a"
AMBER_BG = "#f6eddb"
RED = "#c2473d"
RED_DARK = "#a0463c"
RED_BG = "#f6e1de"
RED_BORDER = "#e2b7b0"
PURPLE = "#7a5ea8"

# Accent options exactly as offered by the mockup's ``accentColor`` prop.
ACCENT_OPTIONS = {
    "blue": "#2f6fb0",
    "green": "#3a7a52",
    "purple": "#7a5ea8",
    "orange": "#b06a2f",
    "gray": "#46494e",
}
DEFAULT_ACCENT = ACCENT_OPTIONS["blue"]

# ---------------------------------------------------------------------------
# Fonts. IBM Plex is the design font; fall back cleanly when it is not
# installed. ``addApplicationFont`` picks up any *.ttf dropped in assets/fonts.
# ---------------------------------------------------------------------------
SANS_STACK = ["IBM Plex Sans", "DejaVu Sans", "Segoe UI", "Ubuntu", "sans-serif"]
MONO_STACK = ["IBM Plex Mono", "DejaVu Sans Mono", "Consolas", "Ubuntu Mono", "monospace"]

SANS = ", ".join(f"'{f}'" if " " in f else f for f in SANS_STACK)
MONO = ", ".join(f"'{f}'" if " " in f else f for f in MONO_STACK)

FONTS_DIR = Path(__file__).resolve().parent / "assets" / "fonts"


def tint(hex_color: str, amt: float) -> str:
    """Mix ``hex_color`` toward white by ``amt`` in ``[0, 1]`` (mockup ``_tint``)."""
    h = hex_color.lstrip("#")
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    mix = lambda c: round(c + (255 - c) * amt)
    return f"#{mix(r):02x}{mix(g):02x}{mix(b):02x}"


@dataclass
class Theme:
    """Runtime theme. Only :attr:`accent` varies; everything else is constant."""

    accent: str = DEFAULT_ACCENT

    # convenience passthroughs so screens can read ``t.green`` etc.
    green: str = field(default=GREEN, init=False)
    green_dark: str = field(default=GREEN_DARK, init=False)
    green_bg: str = field(default=GREEN_BG, init=False)
    amber: str = field(default=AMBER, init=False)
    amber_bg: str = field(default=AMBER_BG, init=False)
    red: str = field(default=RED, init=False)
    red_bg: str = field(default=RED_BG, init=False)
    purple: str = field(default=PURPLE, init=False)

    def tint(self, amt: float) -> str:
        """Accent tinted toward white by ``amt`` (the mockup's most common op)."""
        return tint(self.accent, amt)

    # Frequently reused accent tints (named for readability at call sites).
    @property
    def accent_soft(self) -> str:   # selected-row background
        return tint(self.accent, 0.90)

    @property
    def accent_note_bg(self) -> str:
        return tint(self.accent, 0.93)

    @property
    def accent_note_border(self) -> str:
        return tint(self.accent, 0.78)

    # SAP id -> chip colour (mockup ``sapColors``)
    def sap_color(self, sap) -> str:
        return {
            "0": PURPLE, 0: PURPLE,
            "5": self.accent, 5: self.accent,
            "9": GREEN, 9: GREEN,
        }.get(sap, "#5a5e64")


def load_fonts() -> None:
    """Register any bundled IBM Plex TTFs so the design font is used when present.

    Safe to call after the QApplication exists; a no-op when the directory or the
    font files are absent (the fallback stack then takes over).
    """
    if not FONTS_DIR.is_dir():
        return
    from PyQt6.QtGui import QFontDatabase

    for ttf in FONTS_DIR.glob("*.ttf"):
        QFontDatabase.addApplicationFont(str(ttf))
