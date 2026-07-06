"""Base class for the content screens.

A :class:`Screen` owns a content widget that it rebuilds from the model whenever
a relevant ``changed`` topic fires or the accent changes. Scrolling screens wrap
the content in a themed scroll area with a top-aligned column; full-height
screens (chat) fill the content area directly.
"""
from __future__ import annotations

from typing import Set

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QFrame, QScrollArea, QVBoxLayout, QWidget

from ... import theme as T
from ...model import ConsoleModel

_SCROLLBAR_QSS = """
QScrollArea{background:%s;border:none;}
QScrollBar:vertical{background:#e4e5e8;width:11px;margin:0;}
QScrollBar::handle:vertical{background:#b2b5ba;border:2px solid #e4e5e8;border-radius:5px;min-height:30px;}
QScrollBar::handle:vertical:hover{background:#9da0a6;}
QScrollBar::add-line:vertical,QScrollBar::sub-line:vertical{height:0;}
QScrollBar::add-page:vertical,QScrollBar::sub-page:vertical{background:transparent;}
""" % T.CONTENT_BG


class Screen(QWidget):
    topics: Set[str] = set()
    scroll: bool = True
    full_height: bool = False
    padding = (18, 18, 20, 18)   # l, t, r, b
    spacing = 14

    def __init__(self, model: ConsoleModel):
        super().__init__()
        self.model = model
        self.setStyleSheet(f"background:{T.CONTENT_BG};")
        self._root = QVBoxLayout(self)
        self._root.setContentsMargins(0, 0, 0, 0)
        self._root.setSpacing(0)
        if self.scroll:
            self._area = QScrollArea()
            self._area.setWidgetResizable(True)
            self._area.setFrameShape(QFrame.Shape.NoFrame)
            self._area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
            self._area.setStyleSheet(_SCROLLBAR_QSS)
            self._root.addWidget(self._area)
        else:
            self._area = None
        model.changed.connect(self._on_changed)
        model.accent_changed.connect(self.rebuild)
        self.rebuild()

    def _on_changed(self, topic: str) -> None:
        if topic in self.topics:
            self.rebuild()

    def rebuild(self) -> None:
        content = QWidget()
        content.setStyleSheet(f"background:{T.CONTENT_BG};")
        lay = QVBoxLayout(content)
        if self.full_height:
            lay.setContentsMargins(0, 0, 0, 0)
            lay.setSpacing(0)
        else:
            l, t, r, b = self.padding
            lay.setContentsMargins(l, t, r, b)
            lay.setSpacing(self.spacing)
        self.build(lay)
        if self.scroll and not self.full_height:
            lay.addStretch(1)
        if self._area is not None:
            # Defer deletion of the outgoing content: a rebuild is often driven by
            # a click on a widget *inside* that content (e.g. a config toggle), and
            # QScrollArea.setWidget() would delete it synchronously — mid-event —
            # crashing the click handler. takeWidget() detaches without deleting;
            # deleteLater() then frees it safely once the event unwinds.
            old = self._area.takeWidget()
            self._area.setWidget(content)
            if old is not None:
                old.deleteLater()
        else:
            # replace the single direct child
            while self._root.count():
                old = self._root.takeAt(0).widget()
                if old is not None:
                    old.setParent(None)
                    old.deleteLater()
            self._root.addWidget(content)

    # subclasses override
    def build(self, lay: QVBoxLayout) -> None:  # pragma: no cover - abstract
        raise NotImplementedError

    # convenience: current theme accent
    @property
    def accent(self) -> str:
        return self.model.theme.accent


class Placeholder(Screen):
    """Temporary screen used until the real view module is filled in."""

    def __init__(self, model, title: str, subtitle: str = ""):
        self._title = title
        self._subtitle = subtitle
        super().__init__(model)

    def build(self, lay) -> None:
        from .. import common as C
        lay.addWidget(C.page_header(self._title, self._subtitle))
        note = C.Card().pad(18, 18, 18, 18)
        note.add(C.lbl("This screen is part of the console shell and will render here.",
                       size=12, color=T.FG_DIM))
        lay.addWidget(note)
