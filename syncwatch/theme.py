"""The look of the app: colours, fonts and images from the ``assets`` folder.

Designers only touch ``assets/``: replace a PNG with one of the same name, or
change colours in ``assets/theme.json``. Anything missing or broken falls back
to built-in defaults (SW-111 / SW-112), so a bad file can never stop the app.
This module is Qt-free; the UI turns the result into a Qt stylesheet.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Tuple

DEFAULT_COLORS: Dict[str, str] = {
    "window": "#101318", "panel": "#121620E6", "panel_border": "#2A3140", "text": "#E8EAED",
    "text_dim": "#9AA3B2", "accent": "#E5484D", "accent_text": "#FFFFFF", "button": "#262C38",
    "button_hover": "#323A4A", "button_pressed": "#3C4658", "button_text": "#E8EAED",
    "input": "#1A1F29", "good": "#3CCB7F", "warn": "#F5A524", "bad": "#F0506E",
    "seek_track": "#3A4150", "seek_fill": "#E5484D", "marker": "#4DA3FF", "video_background": "#000000",
    "check_box": "#F2F4F7", "check_border": "#9AA3B2", "check_mark": "#000000",
}
BACKGROUND_MODES = ("cover", "stretch", "tile", "center")
ICON_NAMES = ("play", "pause", "back", "forward", "sync_up", "sync_me", "subtitles", "volume", "mute",
              "fullscreen", "exit_fullscreen", "sidebar", "settings", "open", "copy", "host", "join",
              "leave", "help")
_COLOR = re.compile(r"^#[0-9A-Fa-f]{6}([0-9A-Fa-f]{2})?$")


@dataclass
class Theme:
    colors: Dict[str, str] = field(default_factory=lambda: dict(DEFAULT_COLORS))
    font_family: str = ""
    font_size_pt: float = 10.0
    radius: int = 6
    background_image: str = "background.png"
    background_mode: str = "cover"
    logo: str = "logo.png"
    default_avatar: str = "default_avatar.png"

    def css(self, name: str) -> str:
        """A colour as Qt stylesheet text (rgba() when it has opacity)."""
        value = self.colors.get(name, DEFAULT_COLORS.get(name, "#FF00FF"))
        if len(value) == 9:
            r, g, b, a = (int(value[i:i + 2], 16) for i in (1, 3, 5, 7))
            return f"rgba({r},{g},{b},{a})"
        return value


def load(assets_dir: Path) -> Tuple[Theme, List[str]]:
    """Read theme.json. Returns (theme, problem codes)."""
    theme = Theme()
    path = assets_dir / "theme.json"
    if not path.exists():
        return theme, ["SW-112"]
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("not an object")
    except (OSError, ValueError):
        return theme, ["SW-112"]
    problems: List[str] = []
    for name, value in (raw.get("colors") or {}).items():
        if name in DEFAULT_COLORS and isinstance(value, str) and _COLOR.match(value):
            theme.colors[name] = value.upper()
        elif name in DEFAULT_COLORS:
            problems = ["SW-112"]
    font = raw.get("font") or {}
    if isinstance(font.get("family"), str):
        theme.font_family = font["family"][:64]
    if isinstance(font.get("size_pt"), (int, float)) and 6 <= font["size_pt"] <= 30:
        theme.font_size_pt = float(font["size_pt"])
    if isinstance(raw.get("corner_radius_px"), int) and 0 <= raw["corner_radius_px"] <= 30:
        theme.radius = raw["corner_radius_px"]
    background = raw.get("background") or {}
    if isinstance(background.get("image"), str):
        theme.background_image = Path(background["image"]).name  # file names only, no paths
    if background.get("mode") in BACKGROUND_MODES:
        theme.background_mode = background["mode"]
    for key in ("logo", "default_avatar"):
        if isinstance(raw.get(key), str):
            setattr(theme, key, Path(raw[key]).name)
    return theme, problems


def stylesheet(theme: Theme) -> str:
    """The Qt stylesheet for the whole app."""
    c = theme.css
    r = theme.radius
    font = f"font-family: '{theme.font_family}';" if theme.font_family else ""
    return f"""
* {{ color: {c('text')}; font-size: {theme.font_size_pt}pt; {font} }}
QToolTip {{ background: {c('input')}; color: {c('text')}; border: 1px solid {c('panel_border')}; padding: 4px; }}
#Panel, #ControlBar {{ background: {c('panel')}; border: 1px solid {c('panel_border')}; border-radius: {r}px; }}
#PanelTitle {{ color: {c('text_dim')}; font-weight: bold; }}
#Card {{ background: {c('panel')}; border: 1px solid {c('panel_border')}; border-radius: {r + 4}px; }}
#LibraryTitle {{ font-size: {theme.font_size_pt + 4}pt; font-weight: bold; }}
#LibraryEmptyIcon {{ font-size: {theme.font_size_pt * 3}pt; }}
#LibraryEmptyTitle {{ font-size: {theme.font_size_pt + 2}pt; font-weight: bold; }}
#Dim, #Hint {{ color: {c('text_dim')}; }}
#Hint {{ font-size: {max(7, theme.font_size_pt - 1.5)}pt; }}
#Good {{ color: {c('good')}; }} #Warn {{ color: {c('warn')}; }} #Bad {{ color: {c('bad')}; }}
QPushButton, QToolButton {{ background: {c('button')}; color: {c('button_text')}; border: 1px solid {c('panel_border')};
    border-radius: {r}px; padding: 5px 10px; }}
QPushButton:hover, QToolButton:hover {{ background: {c('button_hover')}; }}
QPushButton:pressed, QToolButton:pressed, QToolButton:checked {{ background: {c('button_pressed')}; }}
QPushButton:disabled, QToolButton:disabled {{ color: {c('text_dim')}; }}
QToolButton::menu-indicator {{ image: none; width: 0px; }}
QPushButton#Accent, QToolButton#Accent {{ background: {c('accent')}; color: {c('accent_text')}; border-color: {c('accent')}; }}
QPushButton#Attention, QToolButton#Attention {{ border: 2px solid {c('warn')}; }}
QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox, QKeySequenceEdit, QPlainTextEdit, QTextEdit, QListWidget, QTreeWidget {{
    background: {c('input')}; border: 1px solid {c('panel_border')}; border-radius: {r}px; padding: 3px; }}
QTreeWidget::item {{ padding: 4px 2px; }}
QTreeWidget::item:selected {{ background: {c('button_hover')}; }}
QHeaderView::section {{ background: {c('input')}; color: {c('text_dim')}; border: none; padding: 3px 6px; }}
QProgressBar {{ background: {c('input')}; border: 1px solid {c('panel_border')}; border-radius: {r}px;
    text-align: center; }}
QProgressBar::chunk {{ background: {c('accent')}; border-radius: {r}px; }}
QComboBox QAbstractItemView {{ background: {c('input')}; selection-background-color: {c('button_hover')}; }}
QMenu {{ background: {c('input')}; border: 1px solid {c('panel_border')}; padding: 4px; }}
QMenu::item {{ padding: 5px 18px; border-radius: {r}px; }}
QMenu::item:selected {{ background: {c('button_hover')}; }}
QMenu::item:disabled {{ color: {c('text_dim')}; }}
QSlider::groove:horizontal {{ height: 6px; background: {c('seek_track')}; border-radius: 3px; }}
QSlider::sub-page:horizontal {{ background: {c('seek_fill')}; border-radius: 3px; }}
QSlider::handle:horizontal {{ width: 14px; margin: -5px 0; background: {c('text')}; border-radius: 7px; }}
QScrollArea, QScrollArea > QWidget > QWidget {{ background: transparent; border: none; }}
QScrollBar:vertical {{ background: transparent; width: 10px; margin: 2px; }}
QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 2px; }}
QScrollBar::handle:vertical {{ background: {c('seek_track')}; border-radius: 3px; min-height: 24px; }}
QScrollBar::handle:horizontal {{ background: {c('seek_track')}; border-radius: 3px; min-width: 24px; }}
QScrollBar::handle:hover {{ background: {c('text_dim')}; }}
QScrollBar::add-line, QScrollBar::sub-line {{ width: 0px; height: 0px; border: none; background: none; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: none; }}
QTabWidget::pane {{ border: 1px solid {c('panel_border')}; border-radius: {r}px; }}
QTabBar::tab {{ background: {c('button')}; padding: 6px 12px; border-top-left-radius: {r}px; border-top-right-radius: {r}px; }}
QTabBar::tab:selected {{ background: {c('button_pressed')}; }}
QDialog {{ background: {c('window')}; }}
QSplitter::handle {{ background: transparent; }}
"""
