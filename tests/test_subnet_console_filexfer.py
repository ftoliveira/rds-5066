"""Fatia 4 (File Transfer RCOP/UDOP) do Subnet Console — verificação headless.

Exercita o caminho de ficheiros ao vivo: fatiar em blocos MTU com o protocolo
`FILE:/FCON:/FEND:/FALL:`, enviar via SAP 6 (RCOP/ARQ) ou 7 (UDOP/non-ARQ), e
reassemblar do outro lado — dois nós reais cruzados por `MockAir`, offscreen.
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


def test_udop_file_transfer_end_to_end(qapp):
    air, ctrl_a, ctrl_b, model_a, model_b = _bring_up(qapp)
    try:
        payload = bytes(range(256)) * 3          # 768 B binários, inclui \x00
        model_a.set_ft_proto("UDOP")             # SAP 7 · non-ARQ (sem link)
        model_a.stage_files([("wx_grib.bin", payload)])
        n_chunks = len(model_a._chunk_file("wx_grib.bin", payload))
        assert n_chunks > 1                       # ficheiro realmente fatiado
        model_a.send_ft()
        assert model_a.ft_staged == []

        assert _pump(qapp, lambda: model_b.ft_received
                     and model_b.ft_received[-1]["name"] == "wx_grib.bin", 25.0), \
            "ficheiro UDOP não chegou ao nó B"
        rx = model_b.ft_received[-1]
        assert rx["data"] == payload              # bytes reassemblados idênticos
        assert rx["from"] == 1 and rx["proto"] == "UDOP"

        # Job TX do A conclui (non-ARQ → SENT 100%).
        assert _pump(qapp, lambda: model_a.ft_active is None, 15.0)
        job = model_a.ft_queue[0]
        assert job["st"] == "SENT" and job["pct"] == 100

        # Ficheiro recebido aparece na fila do B como RECEIVED.
        rxjob = next(j for j in model_b.ft_queue if j["st"] == "RECEIVED")
        assert rxjob["name"] == "wx_grib.bin" and rxjob["proto"] == "UDOP"
        assert any(n == "FEND" for _, d, n, _, _ in model_b.ft_events)
    finally:
        ctrl_a.stop()
        ctrl_b.stop()
        air.stop()
        qapp.processEvents()


def test_rcop_file_transfer_end_to_end(qapp):
    air, ctrl_a, ctrl_b, model_a, model_b = _bring_up(qapp)
    try:
        # RCOP é ARQ → precisa de link; estabelece o hard link (CAS) primeiro.
        model_a.toggle_chat_link()
        assert _pump(qapp, lambda: model_a.chat_link_up and model_b.chat_link_up, 20.0), \
            "hard link não estabeleceu"

        payload = bytes(range(200)) * 2          # 400 B
        model_a.set_ft_proto("RCOP")             # SAP 6 · ARQ
        model_a.stage_files([("sitrep.pdf", payload)])
        model_a.send_ft()

        assert _pump(qapp, lambda: model_b.ft_received
                     and model_b.ft_received[-1]["data"] == payload, 30.0), \
            "ficheiro RCOP não chegou íntegro ao nó B"
        assert model_b.ft_received[-1]["proto"] == "RCOP"

        # ARQ confirma → job do A fica DELIVERED 100%.
        assert _pump(qapp, lambda: model_a.ft_active is None, 20.0)
        job = model_a.ft_queue[0]
        assert job["st"] == "DELIVERED" and job["pct"] == 100

        assert model_b.ft_kpis()[2]["value"] == "1"   # Files Received
    finally:
        ctrl_a.stop()
        ctrl_b.stop()
        air.stop()
        qapp.processEvents()


def test_chunk_reassembly_roundtrip(qapp):
    """Fatiar e reassemblar (sem rede) preserva os bytes — inclui casos-limite."""
    m = ConsoleModel(node="A")   # demo: _chunk_file usa MTU default 128
    cases = {
        "empty.bin": b"",
        "tiny.txt": b"hi",
        "onechunk.bin": bytes(range(100)),      # < MTU → FALL
        "multi.bin": bytes(range(256)) * 5,      # 1280 B, muitos blocos, inclui \x00
    }
    for name, data in cases.items():
        m.ft_received.clear()
        m.ft_rx_buffers.clear()
        chunks = m._chunk_file(name, data)
        assert chunks[0][:5] in (b"FILE:", b"FALL:")
        for ch in chunks:
            m._handle_ft_rx(ch, src=2, sap=6)
        assert len(m.ft_received) == 1, name
        assert m.ft_received[0]["name"] == name
        assert m.ft_received[0]["data"] == data, name


def test_demo_filexfer_unaffected(qapp):
    """Sem controller a fila arranca com o seed de demonstração e send_ft é demo."""
    m = ConsoleModel(node="A")
    assert not m.live
    assert len(m.ft_queue) == 4                  # seed de demonstração
    m.stage_files([("a.txt", b"hello")])
    assert m.ft_staged[0]["bytes"] == 5
    m.send_ft()
    assert m.ft_queue[0]["name"] == "a.txt"      # novo job à frente
    assert m.ft_received == []                    # nada foi para o caminho live
