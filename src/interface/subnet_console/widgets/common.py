"""Small reusable building blocks shared by every console screen.

The design is built from a handful of primitives — labels, dots, pills, cards,
KPI tiles, progress bars and dense tables. Reproducing those once here keeps the
screen modules short and visually consistent.
"""
from __future__ import annotations

from typing import Iterable, Optional, Sequence, Union

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (
    QComboBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from .. import theme as T

_WEIGHTS = {
    300: QFont.Weight.Light,
    400: QFont.Weight.Normal,
    500: QFont.Weight.Medium,
    600: QFont.Weight.DemiBold,
    700: QFont.Weight.Bold,
}
_ALIGN = {
    "l": Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
    "r": Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
    "c": Qt.AlignmentFlag.AlignCenter,
}


def _qfont(size: float, weight: int, mono: bool) -> QFont:
    f = QFont()
    f.setFamilies(T.MONO_STACK if mono else T.SANS_STACK)
    f.setPixelSize(round(size))
    f.setWeight(_WEIGHTS.get(weight, QFont.Weight.Normal))
    return f


def lbl(
    text: str = "",
    *,
    size: float = 12.5,
    weight: int = 400,
    color: str = T.FG_BODY,
    mono: bool = False,
    align: str = "l",
    bg: Optional[str] = None,
    radius: int = 0,
    pad: Optional[tuple] = None,
    border: Optional[str] = None,
    letter_spacing: Optional[float] = None,
    upper: bool = False,
    tooltip: Optional[str] = None,
) -> QLabel:
    """The workhorse text factory — plain labels *and* rounded pills/badges."""
    w = QLabel(text.upper() if upper else text)
    w.setFont(_qfont(size, weight, mono))
    w.setAlignment(_ALIGN[align])
    css = [f"color:{color};", "background:transparent;"]
    if bg:
        css[1] = f"background:{bg};"
    if radius:
        css.append(f"border-radius:{radius}px;")
    if border:
        css.append(f"border:1px solid {border};")
    if pad:
        v, h = pad
        css.append(f"padding:{v}px {h}px;")
    if letter_spacing is not None:
        f = w.font()
        f.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, letter_spacing)
        w.setFont(f)
    w.setStyleSheet("QLabel{" + "".join(css) + "}")
    if tooltip:
        w.setToolTip(tooltip)
    hpol = QSizePolicy.Policy.Expanding if align == "r" else QSizePolicy.Policy.Preferred
    # A filled label is a pill/badge — hug its content vertically so it never
    # balloons to fill a tall row.
    vpol = QSizePolicy.Policy.Maximum if bg else QSizePolicy.Policy.Preferred
    w.setSizePolicy(hpol, vpol)
    return w


def scoped(frame: QFrame, css: str) -> QFrame:
    """Style ``frame`` without leaking borders/background onto child widgets.

    QLabel is a QFrame subclass, so a bare ``QFrame{...}`` stylesheet cascades to
    every descendant label. Scoping the rule to this frame's unique objectName
    keeps it on the frame itself. ``css`` is the property body only, e.g.
    ``"background:#fff;border:1px solid #ccc;border-radius:6px;"``.
    """
    name = f"f{id(frame) & 0xFFFFFFFF:x}"
    frame.setObjectName(name)
    frame.setStyleSheet(f"QFrame#{name}{{{css}}}")
    return frame


def clear_layout(layout) -> None:
    """Recursively remove and delete every item in ``layout``.

    Reparent widgets out *before* ``deleteLater`` so they stop painting
    immediately — otherwise a taken-but-not-yet-deleted widget lingers at its old
    geometry and ghosts behind the rebuilt content.
    """
    while layout.count():
        item = layout.takeAt(0)
        w = item.widget()
        if w is not None:
            w.setParent(None)
            w.deleteLater()
        elif item.layout() is not None:
            clear_layout(item.layout())
            item.layout().deleteLater()


class ClickableFrame(QFrame):
    """A frame that emits :attr:`clicked` and hover-highlights (nav rows, pills)."""

    clicked = pyqtSignal()

    def __init__(self, *, hover_bg: Optional[str] = None, base_css: str = "",
                 cursor: bool = True):
        super().__init__()
        # Scope the caller's ``QFrame{...}`` rule to this frame only, so its
        # border/background never bleeds onto child labels (QLabel is-a QFrame).
        self._name = f"clk{id(self) & 0xFFFFFFFF:x}"
        self.setObjectName(self._name)
        self._base_css = base_css.replace("QFrame{", f"QFrame#{self._name}{{")
        self._hover_bg = hover_bg
        self.setStyleSheet(self._base_css)
        if cursor:
            self.setCursor(Qt.CursorShape.PointingHandCursor)

    def enterEvent(self, e):
        if self._hover_bg:
            self.setStyleSheet(self._base_css + "QFrame#%s{background:%s;}" % (self._name, self._hover_bg))
        super().enterEvent(e)

    def leaveEvent(self, e):
        if self._hover_bg:
            self.setStyleSheet(self._base_css)
        super().leaveEvent(e)

    def mouseReleaseEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton and self.rect().contains(e.pos()):
            self.clicked.emit()
        super().mouseReleaseEvent(e)


def caption(text: str, *, color: str = T.FG_FAINT, size: float = 10, weight: int = 600) -> QLabel:
    """Uppercase, letter-spaced section caption (e.g. ``SECTIONS``, ``TX QUEUE``)."""
    return lbl(text, size=size, weight=weight, color=color, upper=True, letter_spacing=0.4)


def dot(color: str, size: int = 8, *, halo: Optional[str] = None) -> QWidget:
    """A status dot. ``halo`` draws a soft ring around it (toolbar modem light)."""
    if halo:
        wrap = QFrame()
        wrap.setFixedSize(size + 6, size + 6)
        wrap.setStyleSheet(f"background:{halo};border-radius:{(size + 6) // 2}px;")
        wl = QHBoxLayout(wrap)
        wl.setContentsMargins(3, 3, 3, 3)
        inner = QLabel()
        inner.setFixedSize(size, size)
        inner.setStyleSheet(f"background:{color};border-radius:{size // 2}px;")
        wl.addWidget(inner)
        return wrap
    d = QLabel()
    d.setFixedSize(size, size)
    d.setStyleSheet(f"QLabel{{background:{color};border-radius:{size // 2}px;}}")
    return d


def pill(text: str, fg: str, bg: str, *, mono: bool = True, size: float = 10, weight: int = 600) -> QLabel:
    return lbl(text, size=size, weight=weight, color=fg, mono=mono, bg=bg, radius=3, pad=(2, 6), align="c")


def hline(color: str = T.HAIRLINE) -> QFrame:
    f = QFrame()
    f.setFixedHeight(1)
    f.setStyleSheet(f"background:{color};border:none;")
    return f


def vline(color: str = T.MENU_BORDER, height: int = 26) -> QFrame:
    f = QFrame()
    f.setFixedSize(1, height)
    f.setStyleSheet(f"background:{color};border:none;")
    return f


def spacer() -> QWidget:
    w = QWidget()
    w.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
    return w


def row(*widgets, spacing: int = 8, margins: tuple = (0, 0, 0, 0), align=None) -> QWidget:
    """Horizontal container. ``None`` entries insert a stretch."""
    w = QWidget()
    lay = QHBoxLayout(w)
    lay.setContentsMargins(*margins)
    lay.setSpacing(spacing)
    for item in widgets:
        if item is None:
            lay.addStretch(1)
        elif isinstance(item, QWidget):
            lay.addWidget(item)
        else:  # (widget, stretch)
            lay.addWidget(item[0], item[1])
    if align is not None:
        lay.setAlignment(align)
    return w


def col(*widgets, spacing: int = 6, margins: tuple = (0, 0, 0, 0)) -> QWidget:
    w = QWidget()
    lay = QVBoxLayout(w)
    lay.setContentsMargins(*margins)
    lay.setSpacing(spacing)
    for item in widgets:
        if item is None:
            lay.addStretch(1)
        elif isinstance(item, QWidget):
            lay.addWidget(item)
        else:
            lay.addWidget(item[0], item[1])
    return w


class Card(QFrame):
    """White rounded panel with an optional grey header strip."""

    def __init__(self, title: Optional[str] = None, right: Optional[QWidget] = None):
        super().__init__()
        self.setObjectName("card")
        self.setStyleSheet(
            "QFrame#card{background:%s;border:1px solid %s;border-radius:6px;}"
            % (T.CARD_BG, T.CARD_BORDER)
        )
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        if title is not None:
            header = QFrame()
            scoped(header,
                   "background:%s;border:none;border-bottom:1px solid %s;"
                   "border-top-left-radius:6px;border-top-right-radius:6px;"
                   % (T.CARD_HEADER_BG, T.CARD_BORDER))
            hl = QHBoxLayout(header)
            hl.setContentsMargins(14, 10, 14, 10)
            hl.setSpacing(8)
            hl.addWidget(lbl(title, size=13, weight=600, color="#2a2d31"))
            if right is not None:
                hl.addStretch(1)
                hl.addWidget(right)
            outer.addWidget(header)
        self._content = QWidget()
        self.body = QVBoxLayout(self._content)
        self.body.setContentsMargins(0, 0, 0, 0)
        self.body.setSpacing(0)
        outer.addWidget(self._content, 1)

    def pad(self, left=14, top=12, right=14, bottom=12) -> "Card":
        self.body.setContentsMargins(left, top, right, bottom)
        return self

    def add(self, w: QWidget) -> "Card":
        self.body.addWidget(w)
        return self

    def add_layout(self, layout) -> "Card":
        self.body.addLayout(layout)
        return self


class KpiTile(QFrame):
    """Label / big-number(+unit) / delta stat tile used across dashboards."""

    def __init__(self, label: str, value: str, unit: str = "", delta: str = "", delta_color: str = T.FG_DIM):
        super().__init__()
        scoped(self, "background:%s;border:1px solid %s;border-radius:6px;" % (T.CARD_BG, T.CARD_BORDER))
        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 12, 14, 12)
        lay.setSpacing(0)
        lay.addWidget(caption(label, size=10.5))
        val = QHBoxLayout()
        val.setContentsMargins(0, 7, 0, 0)
        val.setSpacing(6)
        val.addWidget(lbl(value, size=25, weight=600, color=T.FG, mono=True))
        if unit:
            u = lbl(unit, size=12, color=T.FG_FAINT)
            u.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignBottom)
            val.addWidget(u)
        val.addStretch(1)
        lay.addLayout(val)
        if delta:
            d = lbl(delta, size=11, color=delta_color)
            d.setContentsMargins(0, 3, 0, 0)
            lay.addWidget(d)


def kpi_strip(items: Sequence[dict], cols: int = 4, *, gap: int = 12) -> QWidget:
    w = QWidget()
    grid = QGridLayout(w)
    grid.setContentsMargins(0, 0, 0, 0)
    grid.setHorizontalSpacing(gap)
    grid.setVerticalSpacing(gap)
    for i, it in enumerate(items):
        tile = KpiTile(
            it["label"], it["value"], it.get("unit", ""),
            it.get("delta", ""), it.get("delta_color", T.FG_DIM),
        )
        grid.addWidget(tile, i // cols, i % cols)
    for c in range(cols):
        grid.setColumnStretch(c, 1)
    return w


def status_text(text: str, *, color: str = T.GREEN_DARK, dot_color: Optional[str] = T.GREEN,
                size: float = 11) -> QWidget:
    """Small ``● LABEL`` status indicator used at the top-right of client panes."""
    parts = []
    if dot_color:
        parts.append(dot(dot_color, 8))
    parts.append(lbl(text, size=size, mono=True, color=color))
    return row(*parts, spacing=7)


def max_width(inner: QWidget, width: int) -> QWidget:
    """Left-align ``inner`` capped at ``width`` px (modem/config panes)."""
    inner.setMaximumWidth(width)
    w = QWidget()
    lay = QHBoxLayout(w)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.setSpacing(0)
    lay.addWidget(inner)
    lay.addStretch(1)
    return w


def page_header(
    title: str,
    subtitle: str = "",
    *,
    badges: Optional[Sequence[QWidget]] = None,
    right: Optional[QWidget] = None,
) -> QWidget:
    w = QWidget()
    lay = QHBoxLayout(w)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.setSpacing(16)
    left = QVBoxLayout()
    left.setSpacing(2)
    titlerow = QHBoxLayout()
    titlerow.setSpacing(9)
    titlerow.addWidget(lbl(title, size=19, weight=700, color=T.FG))
    for b in badges or []:
        titlerow.addWidget(b)
    titlerow.addStretch(1)
    left.addLayout(titlerow)
    if subtitle:
        left.addWidget(lbl(subtitle, size=12, color=T.FG_DIM))
    lw = QWidget()
    lw.setLayout(left)
    lay.addWidget(lw, 1)
    if right is not None:
        lay.addWidget(right, 0, Qt.AlignmentFlag.AlignBottom)
    return w


def bar(pct: str, color: str, *, height: int = 7, track: str = "#e7e8eb", radius: int = 4) -> QWidget:
    """Horizontal progress fill. ``pct`` like ``'64%'``."""
    try:
        frac = max(0.0, min(1.0, float(str(pct).rstrip("%")) / 100.0))
    except ValueError:
        frac = 0.0
    outer = QFrame()
    outer.setFixedHeight(height)
    outer.setStyleSheet(f"background:{track};border-radius:{radius}px;")
    lay = QHBoxLayout(outer)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.setSpacing(0)
    fill = QFrame()
    fill.setStyleSheet(f"background:{color};border-radius:{radius}px;")
    lay.addWidget(fill, max(1, round(frac * 1000)))
    if frac < 1.0:
        lay.addWidget(spacer(), max(1, round((1 - frac) * 1000)))
    return outer


def progress_row(label: str, value: str, pct: str, color: str) -> QWidget:
    w = QWidget()
    lay = QVBoxLayout(w)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.setSpacing(5)
    top = QHBoxLayout()
    top.setContentsMargins(0, 0, 0, 0)
    top.addWidget(lbl(label, size=12, color=T.FG_MUTED))
    top.addStretch(1)
    top.addWidget(lbl(value, size=12, weight=600, color=T.FG_BODY, mono=True))
    lay.addLayout(top)
    lay.addWidget(bar(pct, color))
    return w


def button(text: str, *, kind: str = "default", accent: str = "#2f6fb0",
           on_click=None, size: float = 12) -> QPushButton:
    """Styled push button. ``kind``: default | primary | danger | ghost."""
    b = QPushButton(text)
    b.setCursor(Qt.CursorShape.PointingHandCursor)
    b.setFont(_qfont(size, 600 if kind in ("primary", "danger") else 500, False))
    if kind == "primary":
        css = ("QPushButton{background:%s;color:#fff;border:none;border-radius:5px;padding:8px 18px;}"
               "QPushButton:hover{background:%s;}" % (accent, T.tint(accent, 0.12)))
    elif kind == "danger":
        css = ("QPushButton{background:%s;color:#fff;border:none;border-radius:5px;padding:8px 18px;}"
               "QPushButton:hover{background:%s;}" % (T.RED, T.tint(T.RED, 0.10)))
    else:  # default / ghost
        css = ("QPushButton{background:%s;color:#2a2d31;border:1px solid %s;border-radius:4px;"
               "padding:6px 12px;}QPushButton:hover{background:#ffffff;border-color:#9da0a6;}"
               % (T.INPUT_BG_ALT, "#b9bcc1"))
    b.setStyleSheet(css)
    if on_click:
        b.clicked.connect(on_click)
    return b


def line_edit(text: str = "", *, placeholder: str = "", mono: bool = True, accent: str = "#2f6fb0",
              on_change=None, on_return=None) -> QLineEdit:
    e = QLineEdit(text)
    e.setPlaceholderText(placeholder)
    e.setFont(_qfont(13, 400, mono))
    e.setStyleSheet(
        "QLineEdit{color:%s;background:%s;border:1px solid %s;border-radius:5px;padding:8px 11px;}"
        "QLineEdit:focus{border:1px solid %s;background:#ffffff;}"
        % (T.FG_BODY, T.INPUT_BG, T.INPUT_BORDER, accent)
    )
    if on_change:
        e.textEdited.connect(on_change)
    if on_return:
        e.returnPressed.connect(on_return)
    return e


def text_edit(text: str = "", *, placeholder: str = "", accent: str = "#2f6fb0", on_change=None) -> QPlainTextEdit:
    e = QPlainTextEdit(text)
    e.setPlaceholderText(placeholder)
    e.setFont(_qfont(13, 400, False))
    e.setStyleSheet(
        "QPlainTextEdit{color:%s;background:#ffffff;border:1px solid %s;border-radius:6px;padding:8px 10px;}"
        "QPlainTextEdit:focus{border:1px solid %s;}" % (T.FG_BODY, T.INPUT_BORDER, accent)
    )
    if on_change:
        e.textChanged.connect(lambda: on_change(e.toPlainText()))
    return e


def combo(items: Sequence[tuple], current: str, *, accent: str = "#2f6fb0", on_change=None) -> QComboBox:
    """``items``: list of ``(value, label)``; selects ``current`` by value."""
    c = QComboBox()
    c.setFont(_qfont(12, 400, True))
    c.setCursor(Qt.CursorShape.PointingHandCursor)
    for i, (val, label) in enumerate(items):
        c.addItem(label, val)
        if val == current:
            c.setCurrentIndex(i)
    c.setStyleSheet(
        "QComboBox{color:%s;background:%s;border:1px solid %s;border-radius:4px;padding:6px 9px;}"
        "QComboBox:hover{border-color:#9da0a6;}"
        "QComboBox::drop-down{border:none;width:20px;}"
        "QComboBox QAbstractItemView{background:#ffffff;border:1px solid %s;selection-background-color:%s;"
        "selection-color:#fff;outline:none;}"
        % (T.FG_BODY, T.INPUT_BG_ALT, T.INPUT_BORDER, T.INPUT_BORDER, accent)
    )
    if on_change:
        c.currentIndexChanged.connect(lambda i: on_change(c.itemData(i)))
    return c


def read_field(label: str, value: str) -> QWidget:
    """Read-only ``label`` over a boxed monospace ``value`` (config/modem panes)."""
    w = QVBoxLayout()
    box = QFrame()
    box.setStyleSheet(f"QFrame{{background:{T.INPUT_BG};border:1px solid {T.INPUT_BORDER};border-radius:5px;}}")
    bl = QHBoxLayout(box)
    bl.setContentsMargins(11, 8, 11, 8)
    bl.addWidget(lbl(value, mono=True, size=13, color=T.FG_BODY))
    cont = QWidget()
    v = QVBoxLayout(cont)
    v.setContentsMargins(0, 0, 0, 0)
    v.setSpacing(5)
    v.addWidget(lbl(label, size=11, weight=600, color=T.FG_DIM))
    v.addWidget(box)
    return cont


def kv_card(title: str, rows: Sequence[dict], *, width: Optional[int] = None,
            right: Optional[QWidget] = None, upper: bool = True, k_size: float = 10) -> "Card":
    """Card of aligned ``k`` / ``v`` rows (binding panels, server params)."""
    card = Card(title, right=right)
    if width:
        card.setFixedWidth(width)
    tbl = Table([1, 1], aligns=["l", "r"])
    for r in rows:
        tbl.add([
            lbl(r["k"], size=k_size, weight=600, color=T.FG_FAINT, upper=upper, letter_spacing=0.3),
            lbl(r["v"], size=12, mono=True, color=T.FG_BODY, align="r"),
        ])
    card.add(tbl)
    return card


Cell = Union[str, QWidget]


class Table(QFrame):
    """Dense, column-aligned table with per-row styling.

    Each row is its own frame (so it can carry a background + bottom hairline);
    columns line up across rows because every row shares the same stretch/fixed
    width per column. Pass ``scroll_height`` to make the data area scroll while
    the header stays pinned (used by the live log panels).
    """

    def __init__(
        self,
        weights: Sequence[float],
        *,
        fixed: Optional[dict] = None,
        aligns: Optional[Sequence[str]] = None,
        scroll_height: Optional[int] = None,
        pad_h: int = 14,
    ):
        super().__init__()
        self.setStyleSheet("QFrame{background:transparent;border:none;}")
        self._weights = list(weights)
        self._fixed = fixed or {}
        self._aligns = list(aligns) if aligns else ["l"] * len(weights)
        self._pad_h = pad_h
        self._outer = QVBoxLayout(self)
        self._outer.setContentsMargins(0, 0, 0, 0)
        self._outer.setSpacing(0)
        self._body_holder = QVBoxLayout()
        self._body_holder.setContentsMargins(0, 0, 0, 0)
        self._body_holder.setSpacing(0)
        if scroll_height is not None:
            self._body_container = QWidget()
            self._body_container.setLayout(self._body_holder)
            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setFrameShape(QFrame.Shape.NoFrame)
            scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
            scroll.setMaximumHeight(scroll_height)
            scroll.setStyleSheet("QScrollArea{background:transparent;border:none;}")
            scroll.setWidget(self._body_container)
            self._scroll = scroll
        else:
            self._scroll = None

    def _cell_widget(self, c: Cell, col_idx: int, *, header: bool) -> QWidget:
        if isinstance(c, QWidget):
            return c
        if header:
            return lbl(str(c), size=10, weight=600, color=T.FG_FAINT, upper=True,
                       letter_spacing=0.3, align=self._aligns[col_idx])
        return lbl(str(c), size=12.5, color=T.FG_MUTED, align=self._aligns[col_idx])

    def _build_line(self, cells: Sequence[Cell], *, header: bool, bg: Optional[str],
                    divider: str) -> QFrame:
        line = QFrame()
        line.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        scoped(line, "background:%s;border:none;border-bottom:1px solid %s;" % (bg or "transparent", divider))
        lay = QHBoxLayout(line)
        top = 7 if header else 8
        lay.setContentsMargins(self._pad_h, top, self._pad_h, top)
        lay.setSpacing(10)
        for i, c in enumerate(cells):
            wdg = self._cell_widget(c, i, header=header)
            if i in self._fixed:
                wdg.setFixedWidth(self._fixed[i])
                lay.addWidget(wdg)
                continue
            stretch = max(1, round(self._weights[i] * 100))
            # Cells that genuinely want to fill (progress bars, right-aligned
            # values) declare an Expanding policy — let them fill. Everything else
            # (pills, badges, short labels) hugs its content, aligned in-column.
            if wdg.sizePolicy().horizontalPolicy() == QSizePolicy.Policy.Expanding:
                lay.addWidget(wdg, stretch)
            else:
                lay.addWidget(wdg, stretch, _ALIGN[self._aligns[i]])
        return line

    def header(self, titles: Sequence[str]) -> "Table":
        line = self._build_line(titles, header=True, bg=None, divider=T.HAIRLINE)
        self._outer.addWidget(line)
        if self._scroll is not None:
            self._outer.addWidget(self._scroll, 1)
        return self

    def add(self, cells: Sequence[Cell], *, bg: Optional[str] = None,
            divider: str = T.ROW_DIV) -> "Table":
        line = self._build_line(cells, header=False, bg=bg, divider=divider)
        if self._scroll is not None:
            self._body_holder.addWidget(line)
        else:
            self._outer.addWidget(line)
        return line

    def finish(self) -> "Table":
        """Push remaining scroll rows to the top."""
        if self._scroll is not None:
            self._body_holder.addStretch(1)
        return self
