"""Tests for WARNING D_PDU handling in StanagNode and CASEngine (STANAG 5066 Annex C)."""

import pytest
from unittest.mock import MagicMock

from src.modem_if import ModemConfig, ModemInterface
from src.stanag_node import StanagNode
from src.dpdu_frame import build_warning, encode_dpdu, build_data_only, dpdu_set_address
from src.stypes import DPDUType, Address

class _StubModem(ModemInterface):
    def __init__(self):
        super().__init__(config=ModemConfig())
        self.tx_frames: list[bytes] = []

    def modem_rx_read_frame(self):
        return None

    def modem_tx_dpdu(self, dpdu_buffer, length=None):
        self.tx_frames.append(dpdu_buffer)
        return len(dpdu_buffer)

    def modem_tx_burst(self, frames):
        self.tx_frames.extend(frames)
        return sum(len(f) for f in frames)

    def modem_rx_start(self): pass
    def modem_rx_stop(self): pass
    def modem_get_carrier_status(self): return True
    def modem_set_tx_enable(self, enabled): pass

def _make_node(**kwargs):
    modem = _StubModem()
    return StanagNode(local_node_address=1, modem=modem, **kwargs)

class TestStanagNodeWarnings:
    def test_warning_received_dispatches_to_cas(self):
        """Receiving a WARNING D_PDU should invoke cas.on_warning_received."""
        node = _make_node()
        # Mock CAS warning reception
        node.cas.on_warning_received = MagicMock()

        # Build a WARNING D_PDU
        remote_addr = 5
        warning_reason = 3
        received_dpdu = 0 # Type 0 DATA_ONLY
        addr = dpdu_set_address(destination=1, source=remote_addr)
        
        warn_dpdu = build_warning(
            eow=0, eot=0, address=addr, 
            received_dpdu_type=received_dpdu, 
            reason=warning_reason
        )
        encoded_warn = encode_dpdu(warn_dpdu)

        # Inject RX frame
        node._dispatch_rx_frame(encoded_warn)

        # Verify CAS method was called
        node.cas.on_warning_received.assert_called_once_with(remote_addr, warning_reason)

    def test_invalid_dpdu_state_transmits_warning_to_cas(self):
        """Receiving a connection-related D_PDU while unconnected should transmit a WARNING and invoke cas.on_warning_transmitted."""
        node = _make_node()
        # Mock CAS warning transmission
        node.cas.on_warning_transmitted = MagicMock()

        # Node is IDLE_UNCONNECTED. DPDU Type 0 (DATA_ONLY) is not allowed.
        remote_addr = 5
        addr = dpdu_set_address(destination=1, source=remote_addr)
        
        data_dpdu = build_data_only(
            eow=0, eot=0, address=addr,
            user_data=b"test",
            tx_frame_seq=0,
            tx_uwe=False, tx_lwe=False,
            drop_pdu=False,
            pdu_start=True, pdu_end=True
        )
        encoded_data = encode_dpdu(data_dpdu)

        # Inject RX frame
        node._dispatch_rx_frame(encoded_data)

        from src.dts_state import WARNING_REASON_CONN_DPDU_BUT_UNCONNECTED

        # Verify Warning was built and transmitted to modem
        assert len(node.modem.tx_frames) == 1
        
        # Verify CAS method was called tracking the transmitted warning
        node.cas.on_warning_transmitted.assert_called_once_with(remote_addr, WARNING_REASON_CONN_DPDU_BUT_UNCONNECTED)

