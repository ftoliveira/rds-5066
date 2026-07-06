"""Application menu bar, built from the model's menu definitions."""
from __future__ import annotations

from PyQt6.QtGui import QAction
from PyQt6.QtWidgets import QMenu, QMenuBar

from .. import theme as T
from ..model import ConsoleModel

_QSS = """
QMenuBar{background:%(bar)s;border:none;border-bottom:1px solid %(border)s;
    color:#34373c;padding:2px 6px;font-size:13px;}
QMenuBar::item{background:transparent;padding:4px 9px;margin:0 1px;border-radius:3px;}
QMenuBar::item:selected{background:%(hover)s;}
QMenuBar::item:pressed{background:%(hover)s;}
QMenu{background:#fdfdfe;border:1px solid %(border)s;border-radius:6px;padding:4px;}
QMenu::item{padding:6px 26px 6px 10px;border-radius:4px;color:#2a2d31;font-size:13px;}
QMenu::item:selected{background:#eef0f3;}
QMenu::separator{height:1px;background:#e4e6e9;margin:4px 8px;}
""" % {"bar": T.MENU_BG, "border": T.MENU_BORDER, "hover": T.MENU_HOVER}


def build_menubar(window, model: ConsoleModel) -> QMenuBar:
    mb = QMenuBar(window)
    mb.setNativeMenuBar(False)
    mb.setStyleSheet(_QSS)

    def make_handler(target):
        if target == "__quit__":
            return window.close
        if target:
            return lambda: model.set_screen(target)
        return lambda: None

    for label, items in model.menu_defs():
        menu = QMenu(label, mb)
        menu.setStyleSheet(_QSS)
        for item_label, shortcut, target in items:
            if item_label == "---":
                menu.addSeparator()
                continue
            act = QAction(item_label, menu)
            if shortcut:
                act.setShortcut(shortcut)
            act.triggered.connect(make_handler(target))
            menu.addAction(act)
        mb.addMenu(menu)
    return mb
