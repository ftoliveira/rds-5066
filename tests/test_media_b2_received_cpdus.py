"""MÉDIA-B2 — separação semântica de ``received_cpdus`` em ``StanagNode``.

Antes da Sprint 6, ``received_cpdus`` era uma única lista que misturava DATA
C_PDUs (ARQ/Expedited, consumidos por ``_process_rx``) com C_PDUs de controle
(Non-ARQ, apenas arquivados). Agora há ``received_data_cpdus`` e
``received_control_cpdus``; ``received_cpdus`` vira visão combinada
somente-leitura (compat).
"""

from __future__ import annotations

from src.cas import encode_cpdu
from src.modem_if import ModemConfig, ModemInterface
from src.non_arq import NonArqDelivery
from src.stanag_node import StanagNode
from src.stypes import CPDU, CPDUType, DPDUType, NonArqDeliveryKind


class _StubModem(ModemInterface):
    def __init__(self):
        super().__init__(config=ModemConfig())

    def modem_rx_read_frame(self): return None
    def modem_tx_dpdu(self, b, length=None): return len(b)
    def modem_tx_burst(self, frames): return sum(len(f) for f in frames)
    def modem_rx_start(self): pass
    def modem_rx_stop(self): pass
    def modem_get_carrier_status(self): return True
    def modem_set_tx_enable(self, e): pass


def _make_node(**kwargs) -> StanagNode:
    return StanagNode(local_node_address=1, modem=_StubModem(), **kwargs)


def _data_cpdu(payload=b"hello"):
    return encode_cpdu(CPDU(cpdu_type=CPDUType.DATA, payload=payload))


def _control_delivery(source=2, cpdu_type=CPDUType.LINK_BREAK):
    payload = encode_cpdu(CPDU(cpdu_type=cpdu_type, payload=b""))
    return NonArqDelivery(
        dpdu_type=DPDUType.NON_ARQ,
        source=source,
        destination=1,
        cpdu_id=0,
        payload=payload,
        complete=True,
        error=False,
        kind=NonArqDeliveryKind.COMPLETE,
    )


def test_initial_lists_are_separate_and_empty():
    node = _make_node()
    assert node.received_data_cpdus == []
    assert node.received_control_cpdus == []
    assert node.received_cpdus == []


def test_arq_data_goes_to_data_list():
    node = _make_node()
    node.cas.remote_node_address = 2
    node._on_arq_delivery(_data_cpdu(b"arq-payload"))

    assert len(node.received_data_cpdus) == 1
    assert node.received_data_cpdus[0].cpdu_type is CPDUType.DATA
    assert node.received_control_cpdus == []


def test_expedited_data_goes_to_data_list():
    node = _make_node()
    node.cas.remote_node_address = 2
    node._on_expedited_delivery(_data_cpdu(b"exp-payload"))

    assert len(node.received_data_cpdus) == 1
    assert node.received_control_cpdus == []


def test_non_arq_control_goes_to_control_list():
    node = _make_node()
    node._on_non_arq_delivery(_control_delivery(cpdu_type=CPDUType.LINK_BREAK))

    assert node.received_data_cpdus == []
    assert len(node.received_control_cpdus) == 1
    assert node.received_control_cpdus[0].cpdu_type is not CPDUType.DATA


def test_received_cpdus_property_combines_both():
    node = _make_node()
    node.cas.remote_node_address = 2
    node._on_arq_delivery(_data_cpdu(b"d1"))
    node._on_expedited_delivery(_data_cpdu(b"d2"))
    node._on_non_arq_delivery(_control_delivery())

    assert len(node.received_data_cpdus) == 2
    assert len(node.received_control_cpdus) == 1
    # compat: visão combinada = data + control
    combined = node.received_cpdus
    assert len(combined) == 3
    assert combined == node.received_data_cpdus + node.received_control_cpdus


def test_process_rx_consumes_only_data_list():
    """``_process_rx`` itera apenas ``received_data_cpdus``; o cursor não é
    afetado por C_PDUs de controle arquivados."""
    node = _make_node()
    node.cas.remote_node_address = 2
    # Arquiva controle ANTES de qualquer DATA: não deve mover o cursor.
    node._on_non_arq_delivery(_control_delivery())
    node._on_arq_delivery(_data_cpdu(b"payload"))

    assert node._rx_cursor == 0
    node._process_rx()
    # Processou exatamente o único DATA C_PDU da lista de dados.
    assert node._rx_cursor == 1
