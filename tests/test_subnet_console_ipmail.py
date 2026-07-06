"""Fatia 6 (IP Client SAP 9 + HF Mail HMTP 3/HFPOP 4) — verificação headless.

Exercita os clientes ``annex_f`` ao vivo através da consola: o nó A envia um
datagrama IPv4 (IPClient · SAP 9) e submete um mail-object (HMTP · SAP 3); o nó
B decodifica e mostra ambos — dois nós reais cruzados por ``MockAir``, offscreen.
"""
from __future__ import annotations

import os
import time

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication  # noqa: E402

from src.interface.subnet_console.backend.node_controller import NodeController  # noqa: E402
from src.interface.subnet_console.model import ConsoleModel  # noqa: E402
from tests.mock_110d_modem import MockAir  # noqa: E402


@pytest.fixture
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app
    app.processEvents()


def _wire(controller: NodeController, model: ConsoleModel) -> None:
    controller.status_changed.connect(model.apply_live_status)
    controller.unidata_received.connect(model.on_rx)
    controller.link_established.connect(model.on_link_up)
    controller.link_terminated.connect(model.on_link_down)
    controller.request_rejected.connect(model.on_rejected)


def _pump(app, pred, timeout: float = 30.0, step: float = 0.01) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if pred():
            return True
        time.sleep(step)
    app.processEvents()
    return pred()


def _bring_up(app):
    air = MockAir(keepalive_period=2.0).start()
    ctrl_a = NodeController(1, 2, "127.0.0.1", air.modem_a.port)
    ctrl_b = NodeController(2, 1, "127.0.0.1", air.modem_b.port)
    model_a = ConsoleModel(node="A", controller=ctrl_a)
    model_b = ConsoleModel(node="B", controller=ctrl_b)
    _wire(ctrl_a, model_a)
    _wire(ctrl_b, model_b)
    ctrl_a.start()
    ctrl_b.start()
    assert _pump(app, lambda: model_a._live_status.get("connected")
                 and model_b._live_status.get("connected"), 10.0), "modems não conectaram"
    return air, ctrl_a, ctrl_b, model_a, model_b


def _hard_link(app, model_a, model_b):
    """ARQ (unicast IP / HMTP) precisa de enlace — estabelece o hard link SAP 5."""
    model_a.toggle_chat_link()
    assert _pump(app, lambda: model_a.chat_link_up and model_b.chat_link_up, 20.0), \
        "hard link não estabeleceu"


def test_ip_datagram_end_to_end(qapp):
    air, ctrl_a, ctrl_b, model_a, model_b = _bring_up(qapp)
    try:
        assert ctrl_b.bound_saps == (3, 4, 5, 6, 7, 9)   # SAP 9 (IP) ligado
        _hard_link(qapp, model_a, model_b)

        model_a.send_ip_test()                            # IPClient · unicast → ARQ
        assert model_a.ip_tx == 1
        assert model_a.ip_events[0]["result"] == "SENT"
        assert model_a.ip_events[0]["dst"] == "10.66.0.2"

        assert _pump(qapp, lambda: model_b.ip_rx >= 1, 25.0), \
            "datagrama IP não chegou ao nó B"
        rx = model_b.ip_events[0]
        assert rx["result"] == "RECV"
        assert rx["src"] == "10.66.0.1" and rx["dst"] == "10.66.0.2"
        assert rx["proto"] == "TCP"

        # KPIs ao vivo refletem o tráfego.
        assert model_b.ip_kpis()[1]["value"] == "1"       # Datagrams RX
        assert model_a.ip_kpis()[0]["value"] == "1"       # Datagrams TX
        # Log ao vivo aparece via o accessor colorido.
        assert model_b.ip_log()[0]["src"] == "10.66.0.1"
    finally:
        ctrl_a.stop()
        ctrl_b.stop()
        air.stop()
        qapp.processEvents()


def test_mail_submit_end_to_end(qapp):
    air, ctrl_a, ctrl_b, model_a, model_b = _bring_up(qapp)
    try:
        assert ctrl_b.bound_saps == (3, 4, 5, 6, 7, 9)   # SAP 3 (HMTP) + 4 (HFPOP)
        _hard_link(qapp, model_a, model_b)

        model_a.compose_new()
        model_a.set_compose_to("duty@corvus-06.s5066")
        model_a.set_compose_subj("Relay window 1500Z")
        model_a.set_compose_body("Confirm hold ARQ for the bulletin push.\nStanding by.")
        model_a.send_mail()                               # HMTP submit · SAP 3 → B

        assert model_a.live_sent and model_a.live_sent[0]["subj"] == "Relay window 1500Z"
        assert model_a.mail_folder == "sent"

        assert _pump(qapp, lambda: len(model_b.live_inbox) >= 1, 25.0), \
            "mail-object HMTP não chegou ao nó B"
        obj = model_b.live_inbox[0]
        assert obj["subj"] == "Relay window 1500Z"
        assert "bulletin push" in obj["body"]
        assert obj["unread"] is True

        # A caixa de entrada ao vivo do B expõe o objeto via mail_view.
        v = model_b.mail_view()
        assert v["kpis"][0]["value"] == "1"               # Inbox unread
        assert any(r["subj"] == "Relay window 1500Z" for r in v["rows"])
    finally:
        ctrl_a.stop()
        ctrl_b.stop()
        air.stop()
        qapp.processEvents()


def test_demo_ipmail_unaffected(qapp):
    """Sem controller os ecrãs IP/Mail ficam em demo e os comandos live são no-op."""
    m = ConsoleModel(node="A")
    assert not m.live
    # IP: demo tiles/log, e send_ip_test não faz nada (sem nó).
    assert m.ip_kpis()[0]["value"] == "5 902"
    assert len(m.ip_log()) == 9
    m.send_ip_test()
    assert m.ip_tx == 0 and m.ip_events == []
    # Mail: send_mail segue o caminho demo (vai para o outbox), não o HMTP live.
    m.compose_new()
    m.set_compose_subj("hello")
    m.set_compose_body("world")
    m.send_mail()
    assert m.live_sent == [] and m.live_inbox == []
    assert m.outbox[0]["subj"] == "hello"                 # seed demo cresceu
    assert m.mail_folder == "outbox"
