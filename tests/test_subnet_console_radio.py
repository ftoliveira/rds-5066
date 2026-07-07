"""Radio Control (ALE 2G remote-control protocol) — headless end-to-end check.

Drives the live path: an :class:`AleController` (real UDP socket + HELLO/RX
threads) talks to :class:`MockAleBackend` over the loopback. The backend learns
the client, fans out the scene (CHANNELS/SCAN/LQA/SOUND_HIST/STATE) and streams
STATE; the console's ``ConsoleModel`` decodes it and its ``radio_view`` reflects
the telemetry. Commands (CONFIG / CHEDIT / CALL / AMD) round-trip and their effect
comes back in the next STATE — exactly the protocol's fire-and-forget contract.
"""
from __future__ import annotations

import os
import time

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication  # noqa: E402

from src.interface.subnet_console.backend.ale_controller import AleController  # noqa: E402
from src.interface.subnet_console.model import ConsoleModel  # noqa: E402
from src.interface.subnet_console.window import SubnetConsoleWindow  # noqa: E402
from tests.mock_ale_backend import MockAleBackend  # noqa: E402


@pytest.fixture
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app
    app.processEvents()


def _pump(app, pred, timeout: float = 10.0, step: float = 0.01) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if pred():
            return True
        time.sleep(step)
    app.processEvents()
    return pred()


def _bring_up(app):
    backend = MockAleBackend().start()
    ctrl = AleController("127.0.0.1", backend.port)
    model = ConsoleModel(node="A", ale_controller=ctrl)
    ctrl.state_changed.connect(model.on_ale_state)
    ctrl.channels_changed.connect(model.on_ale_channels)
    ctrl.scan_changed.connect(model.on_ale_scan)
    ctrl.lqa_changed.connect(model.on_ale_lqa)
    ctrl.sound_hist_changed.connect(model.on_ale_sound_hist)
    ctrl.log_received.connect(model.on_ale_log)
    ctrl.amd_received.connect(model.on_ale_amd)
    ctrl.connection_changed.connect(model.on_ale_conn)
    ctrl.start()
    return backend, ctrl, model


def test_radio_telemetry_and_commands(qapp):
    backend, ctrl, model = _bring_up(qapp)
    try:
        # scene + first STATE arrive → radio becomes reachable and shows telemetry
        assert _pump(qapp, lambda: model._ale_reachable and model.radio_view()["channels"])
        rv = model.radio_view()
        assert rv["online"] is True and rv["live"] is True
        # current channel 4 → 14.109 MHz surfaces as the frequency + in the toolbar
        kpis = {k["label"]: k["value"] for k in rv["kpis"]}
        assert kpis["FREQUENCY"] == "14.109"
        assert kpis["TX POWER"] == "47"
        assert kpis["SINAD"] == "22"
        assert kpis["RSSI"] == "-71"
        assert model.rf_readouts()["freq"] == "14.109 MHz"
        assert model.rf_readouts()["snr"] == "22 dB"
        # channel table + LQA matrix decoded from the scene
        assert [c["freq"] for c in rv["channels"][:2]] == ["3.596", "5.357"]
        assert [p["addr"] for p in rv["lqa_peers"]] == ["BR2", "BR3"]

        # CONFIG: change TX power → effect comes back in STATE (fire-and-forget)
        model.ale_set_tx_power(60)
        assert _pump(qapp, lambda: model._ale_state.get("tx_power_dbm") == 60)
        assert {k["label"]: k["value"] for k in model.radio_view()["kpis"]}["TX POWER"] == "60"

        # CONFIG: sideband + scan rate
        model.ale_set_sideband(1)   # LSB
        model.ale_set_scan_rate(10)
        assert _pump(qapp, lambda: model._ale_state.get("sideband") == 1
                     and model._ale_state.get("scan_rate") == 10)
        assert model.rf_readouts()["mode"] == "ALE LSB"

        # CHEDIT: edit channel 0 → new CHANNELS event
        model.set_ale_chedit_idx("0")
        model.set_ale_chedit_freq("4.000")
        model.set_ale_chedit_name("EDIT")
        model.ale_chedit_apply()
        assert _pump(qapp, lambda: model._ale_channels
                     and model._ale_channels[0]["freq"] == "4.000")
        assert model._ale_channels[0]["name"] == "EDIT"

        # CALL: establish a link → FSM LINKED with the dialed peer
        model.set_ale_call_addr("BR9")
        model.ale_call()
        assert _pump(qapp, lambda: model._ale_state.get("linked")
                     and model._ale_state.get("link_peer") == "BR9")
        assert "LINKED" in model.radio_view()["status"]["label"]

        # AMD: the mock echoes it back as a received AMD (RX path)
        model.set_ale_amd_text("hello over the air")
        model.ale_send_amd()
        assert _pump(qapp, lambda: any("hello over the air" in m["text"]
                                       for m in model.radio_view()["amd"]))

        # TERM: tear the link down
        model.ale_terminate()
        assert _pump(qapp, lambda: not model._ale_state.get("linked"))
    finally:
        ctrl.stop()
        backend.stop()


def test_radio_screen_renders_live(qapp):
    backend, ctrl, model = _bring_up(qapp)
    win = None
    try:
        assert _pump(qapp, lambda: model._ale_reachable and model.radio_view()["channels"])
        win = SubnetConsoleWindow(model)
        win.show()
        model.set_screen("radio")
        # let a few STATE frames drive radio_tele refreshes through the live screen
        _pump(qapp, lambda: False, timeout=0.6)
        rv = model.radio_view()
        assert rv["status"]["label"].startswith(("AVAILABLE", "SCANNING", "LINKED", "FORCED"))
    finally:
        if win is not None:
            win.close()
        ctrl.stop()
        backend.stop()


def test_radio_offline_when_backend_silent(qapp):
    """No backend: the controller starts, never hears STATE, screen stays offline."""
    ctrl = AleController("127.0.0.1", 54999)   # nothing listening
    model = ConsoleModel(node="A", ale_controller=ctrl)
    ctrl.connection_changed.connect(model.on_ale_conn)
    ctrl.state_changed.connect(model.on_ale_state)
    ctrl.start()
    try:
        _pump(qapp, lambda: False, timeout=0.4)
        rv = model.radio_view()
        assert rv["online"] is False
        assert rv["status"]["label"] == "RADIO OFFLINE"
        assert model.rf_readouts()["freq"] == "— MHz"
    finally:
        ctrl.stop()
