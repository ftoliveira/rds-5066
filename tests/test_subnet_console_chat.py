"""Fatia 2 (HFCHAT ao vivo) do Subnet Console — verificação headless ponta-a-ponta.

Dois `NodeController` reais cruzados por `MockAir`; um `ConsoleModel(live)` por
nó, ligado exatamente como em ``app.run()``. Estabelece o hard link SAP 5, envia
uma mensagem de chat de A→B e confirma que ela chega à *thread* ao vivo do modelo
B, e que os *feeds* de S-primitives dos dois lados registam os eventos.

Corre offscreen (``QT_QPA_PLATFORM=offscreen``), sem display — é a mesma malha
que o ``run_110d_real.sh`` exercita, mas com o mock-modem em vez de rádio real.
"""
from __future__ import annotations

import os
import time

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication  # noqa: E402 (após definir a plataforma)

from src.interface.subnet_console.backend.node_controller import NodeController  # noqa: E402
from src.interface.subnet_console.model import ConsoleModel  # noqa: E402
from src.interface.subnet_console.window import SubnetConsoleWindow  # noqa: E402
from tests.mock_110d_modem import MockAir  # noqa: E402


@pytest.fixture
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app
    app.processEvents()


def _wire(controller: NodeController, model: ConsoleModel) -> None:
    """Liga os sinais do controller ao modelo, como faz ``app.run()``."""
    controller.status_changed.connect(model.apply_live_status)
    controller.unidata_received.connect(model.on_rx)
    controller.link_established.connect(model.on_link_up)
    controller.link_terminated.connect(model.on_link_down)
    controller.request_rejected.connect(model.on_rejected)


def _pump(app: QApplication, pred, timeout: float = 20.0, step: float = 0.01) -> bool:
    """Bombeia o event loop Qt até ``pred()`` ou até esgotar ``timeout``."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if pred():
            return True
        time.sleep(step)
    app.processEvents()
    return pred()


def test_hfchat_live_end_to_end(qapp):
    air = MockAir(keepalive_period=2.0).start()
    ctrl_a = NodeController(1, 2, "127.0.0.1", air.modem_a.port)
    ctrl_b = NodeController(2, 1, "127.0.0.1", air.modem_b.port)
    model_a = ConsoleModel(node="A", controller=ctrl_a)
    model_b = ConsoleModel(node="B", controller=ctrl_b)
    _wire(ctrl_a, model_a)
    _wire(ctrl_b, model_b)
    try:
        ctrl_a.start()
        ctrl_b.start()
        assert _pump(qapp, lambda: model_a._live_status.get("connected")
                     and model_b._live_status.get("connected"), 10.0), \
            "adaptadores não conectaram ao mock-modem"

        # Hard link SAP 5 (A inicia; link_type 0 → B auto-aceita).
        model_a.toggle_chat_link()
        assert _pump(qapp, lambda: model_a.chat_link_up and model_b.chat_link_up, 20.0), \
            "hard link SAP 5 não estabeleceu nos dois nós"

        # Envia uma mensagem de chat A→B.
        text = "FALCON to NODE 2, live chat check."
        model_a.set_draft(text)
        model_a.send_msg()
        assert model_a.draft == "" and model_a.live_messages[-1]["text"] == text
        assert _pump(qapp, lambda: any(m["dir"] == "in" for m in model_b.live_messages), 20.0), \
            "mensagem HFCHAT não chegou ao nó B"

        rx = next(m for m in model_b.live_messages if m["dir"] == "in")
        assert rx["text"] == text          # ASCII + CRLF removido
        assert rx["from"] == "NODE 1"      # src_addr do nó A
        assert rx["conf"] == "RECEIVED"

        # Os dois feeds do chat registaram o caminho SIS (projeção pública).
        feed_a = [p["name"] for p in model_a.chat_prims()]
        feed_b = [p["name"] for p in model_b.chat_prims()]
        assert "S_UNIDATA_INDICATION" in feed_b
        assert "S_UNIDATA_REQUEST" in feed_a
        assert any("ESTABLISH_CONFIRM" in n for n in feed_a)

        # A thread ao vivo do B alimenta o accessor que o ecrã desenha.
        drawn = model_b.chat_messages()
        assert drawn[-1]["text"] == text and drawn[-1]["align"] == "l"

        # --- Fatia 3: monitor / dashboard / barra de estado ao vivo ---
        # O event log do monitor mostra a mesma atividade (SAP 5).
        prims_b = [e["prim"] for e in model_b.event_log()]
        assert "S_UNIDATA_INDICATION" in prims_b
        assert model_b.counters()[0]["value"] == str(model_b.live_rx)  # U-PDUs RX
        assert model_a.counters()[1]["value"] == str(model_a.live_tx)  # U-PDUs TX
        assert model_a.live_tx >= 1 and model_b.live_rx >= 1

        # SAP table ao vivo: SAP 5 BOUND com contagem de RX no nó B.
        row5_b = next(r for r in model_b.sap_table() if r["sap"] == "5")
        assert row5_b["state"] == "BOUND" and int(row5_b["rx"]) >= 1
        row6_b = next(r for r in model_b.sap_table() if r["sap"] == "6")
        assert row6_b["state"] == "UNBOUND"

        # KPIs e barra de estado refletem o snapshot de status().
        assert model_a.dashboard_kpis()[0]["value"] == "UP"          # Modem Link
        sb = model_a.statusbar_view()
        assert sb["traffic"] == f"TX {model_a.live_tx} · RX {model_a.live_rx}"
        assert "LISTENING" in sb["sis_label"]
    finally:
        ctrl_a.stop()
        ctrl_b.stop()
        air.stop()
        qapp.processEvents()


def test_modem_starts_disconnected(qapp):
    """Um console live arranca OFFLINE — só liga em Connect Modem (nunca .start())."""
    ctrl = NodeController(1, 2, "127.0.0.1", 3000)
    model = ConsoleModel(node="A", controller=ctrl)
    assert model.live and not ctrl.running
    mv = model.modem_view()
    assert mv["top_label"] == "MODEM OFFLINE" and mv["btn_label"] == "Connect Modem"
    assert model.statusbar_view()["sis_label"] == "SIS OFFLINE"


def test_hfchat_config_wires_send(qapp):
    """As configurações HFCHAT (Config) alimentam os argumentos do S_UNIDATA."""
    air = MockAir(keepalive_period=2.0).start()
    ctrl_a = NodeController(1, 2, "127.0.0.1", air.modem_a.port)
    ctrl_b = NodeController(2, 1, "127.0.0.1", air.modem_b.port)
    model_a = ConsoleModel(node="A", controller=ctrl_a)
    model_b = ConsoleModel(node="B", controller=ctrl_b)
    _wire(ctrl_a, model_a)
    _wire(ctrl_b, model_b)
    try:
        ctrl_a.start()
        ctrl_b.start()
        assert _pump(qapp, lambda: model_a._live_status.get("connected")
                     and model_b._live_status.get("connected"), 10.0)

        # Edita o rascunho: non-ARQ · prio 6 · sem in-order · CLIENT DELIVERY.
        model_a.set_chat_arq(False)
        model_a.set_chat_priority(6)
        model_a.toggle_chat_in_order()
        model_a.cycle_chat_confirm()
        assert model_a.config_view()["dirty"] is True
        assert model_a.chat_cfg["arq"] is True          # rascunho não afeta o envio

        # Espia os argumentos que o modelo passa ao nó.
        captured: dict = {}
        real = ctrl_a.send_unidata

        def spy(sap, dest_sap, payload, **kw):
            captured.update(kw)
            real(sap, dest_sap, payload, **kw)

        ctrl_a.send_unidata = spy

        model_a.apply_chat_cfg()
        assert model_a.config_view()["dirty"] is False
        model_a.set_draft("param check")
        model_a.send_msg()

        assert captured["priority"] == 6
        dm = captured["mode"]
        assert dm.arq_mode is False and dm.in_order is False
        assert dm.client_delivery_confirm is True and dm.node_delivery_confirm is False

        # Non-ARQ ainda entrega ponta a ponta ao nó B.
        assert _pump(qapp, lambda: any(m["dir"] == "in" for m in model_b.live_messages), 20.0), \
            "mensagem non-ARQ não chegou ao nó B"
        assert model_b.live_messages[-1]["text"] == "param check"
    finally:
        ctrl_a.stop()
        ctrl_b.stop()
        air.stop()
        qapp.processEvents()


def test_scroll_screen_click_rebuild_survives(qapp):
    """Regressão: um ClickableFrame de um ecrã com scroll cujo clique reconstrói o
    ecrã não pode apagar-se a meio do próprio mouseReleaseEvent (crash RuntimeError).
    """
    from PyQt6.QtCore import Qt, QPoint
    from PyQt6.QtTest import QTest
    from PyQt6.QtWidgets import QVBoxLayout
    from src.interface.subnet_console.widgets import common as C
    from src.interface.subnet_console.widgets.screens.base import Screen

    model = ConsoleModel(node="A")

    class Clicky(Screen):
        topics = {"config"}

        def build(self, lay: QVBoxLayout) -> None:
            f = C.ClickableFrame(base_css="QFrame{background:#fff;}")
            f.setFixedSize(60, 24)
            f.clicked.connect(lambda: model.changed.emit("config"))  # rebuilds this screen
            lay.addWidget(f)
            self.btn = f

    scr = Clicky(model)
    scr.resize(200, 200)
    scr.show()
    qapp.processEvents()
    first = scr.btn
    QTest.mouseClick(first, Qt.MouseButton.LeftButton, pos=QPoint(10, 10))
    qapp.processEvents()          # deixa o deleteLater diferido correr
    assert scr.btn is not first   # o rebuild trocou o widget — e não houve crash
    scr.close()


def test_config_controls_clickable_live(qapp):
    """Clicar nos controlos HFCHAT (Config) muda o rascunho sem crashar."""
    from PyQt6.QtCore import Qt, QPoint
    from PyQt6.QtTest import QTest
    from src.interface.subnet_console.widgets.common import ClickableFrame

    ctrl = NodeController(1, 2, "127.0.0.1", 3000)   # não arranca
    model = ConsoleModel(node="A", controller=ctrl)
    win = SubnetConsoleWindow(model)
    model.set_screen("config")
    qapp.processEvents()

    before = dict(model.chat_cfg_draft)
    screen = win._screens["config"]
    # Clica no primeiro chip de prioridade que difere da prioridade atual.
    for cf in screen.findChildren(ClickableFrame):
        if cf.width() < 60 and cf.height() < 40 and cf.isVisible():
            QTest.mouseClick(cf, Qt.MouseButton.LeftButton, pos=QPoint(5, 5))
            qapp.processEvents()
            break
    # Não crashou; o rascunho continua consultável.
    assert isinstance(model.config_view()["dirty"], bool)
    assert model.chat_cfg_draft.keys() == before.keys()
    win.close()


def test_demo_chat_unaffected(qapp):
    """Sem controller o modelo continua em modo demo (seam intacto)."""
    model = ConsoleModel(node="A")
    assert not model.live
    assert len(model.chat_messages()) == 6                 # seed de demonstração
    assert model.chat_header()["name"] == "CORVUS-06"
    assert model.chat_prims()[-1]["name"] == "S_BIND_REQUEST"

    model.set_draft("hello demo")
    model.send_msg()
    assert model.chat_messages()[-1]["text"] == "hello demo"
    assert model.live_messages == []                       # nada foi para o caminho live
