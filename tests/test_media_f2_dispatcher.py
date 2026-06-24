"""MÉDIA-F2 — AnnexFDispatcher central no Raw SIS Socket.

Substitui a cadeia de callbacks instalada *por bind* por um roteador central
registrado uma única vez no nó. Cobre:

  * roteamento de unidata por SAP destino, com múltiplos clientes;
  * fallback ao callback de host para SAPs sem conexão socket (sem closures
    vazadas apontando para conexões mortas);
  * roteamento de hard-link *established* ao SAP iniciador correto — corrige o
    bug de sobrescrita em que só o ÚLTIMO cliente a dar bind recebia eventos;
  * roteamento de hard-link *indication* ao SAP local destino;
  * roteamento de hard-link *terminated* (per_sap, A.3.2.2.3 §3) ao SAP afetado;
  * callback global de terminação encadeia só ao host (não escreve no socket);
  * unbind para de rotear (tabela viva, sem closure residual);
  * install() idempotente — registra no nó uma única vez.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from src.raw_sis_socket import RawSisSocketServer, _ClientConnection
from src.s_primitive_codec import decode_s_primitive
from src.stypes import SPrimitiveType


class _FakeWriter:
    def __init__(self):
        self.written = b""

    def write(self, data):
        self.written += data

    def close(self):
        pass

    def is_closing(self):
        return False

    def get_extra_info(self, name, default=None):
        return default


class _FakeSession:
    def __init__(self, local_initiator_sap=-1):
        self.local_initiator_sap = local_initiator_sap
        self.sis_hard_link_type = 0
        self.link_priority = 0


def _make_node():
    node = MagicMock()
    node._saps = {}
    node._link_session = _FakeSession()
    cb = MagicMock()
    cb.unidata_indication = None
    cb.hard_link_established = None
    cb.hard_link_rejected = None
    cb.hard_link_indication = None
    cb.hard_link_terminated = None
    cb.hard_link_terminated_per_sap = None
    node._callbacks = cb
    return node


def _attach_conn(server, conn_id, sap_id):
    conn = _ClientConnection(conn_id, reader=object(), writer=_FakeWriter())
    conn.bound_sap = sap_id
    server._connections[conn_id] = conn
    server._sap_to_conn[sap_id] = conn_id
    return conn


def _indication(dest_sap, src_sap=9, src_addr=2, priority=10, updu=b"hi"):
    ind = MagicMock()
    ind.dest_sap = dest_sap
    ind.src_sap = src_sap
    ind.src_addr = src_addr
    ind.priority = priority
    ind.updu = updu
    return ind


# ---------------------------------------------------------------------------
# unidata
# ---------------------------------------------------------------------------


def test_unidata_routed_per_sap_multiclient():
    """Cada cliente recebe APENAS a indicação do seu próprio SAP."""
    server = RawSisSocketServer(_make_node())
    c0 = _attach_conn(server, 1, 0)
    c1 = _attach_conn(server, 2, 1)
    server._dispatcher.install()

    server._dispatcher._on_unidata(_indication(dest_sap=0, updu=b"for-zero"))
    server._dispatcher._on_unidata(_indication(dest_sap=1, updu=b"for-one"))

    pt0, _, _ = decode_s_primitive(c0.writer.written)
    assert pt0 == SPrimitiveType.S_UNIDATA_INDICATION
    assert b"for-zero" in c0.writer.written
    assert b"for-one" not in c0.writer.written

    pt1, _, _ = decode_s_primitive(c1.writer.written)
    assert pt1 == SPrimitiveType.S_UNIDATA_INDICATION
    assert b"for-one" in c1.writer.written
    assert b"for-zero" not in c1.writer.written


def test_unidata_unowned_sap_falls_back_to_host():
    """SAP não pertencente a nenhuma conexão socket → callback de host."""
    node = _make_node()
    calls = []
    node._callbacks.unidata_indication = lambda ind: calls.append(ind)

    server = RawSisSocketServer(node)
    c0 = _attach_conn(server, 1, 0)
    server._dispatcher.install()

    ind = _indication(dest_sap=5)
    server._dispatcher._on_unidata(ind)

    assert calls == [ind]
    assert c0.writer.written == b""


def test_unbind_stops_routing_without_leaked_closure():
    """Após unbind (remoção do mapa), a indicação não chega ao socket morto."""
    node = _make_node()
    calls = []
    node._callbacks.unidata_indication = lambda ind: calls.append(ind)

    server = RawSisSocketServer(node)
    c0 = _attach_conn(server, 1, 0)
    server._dispatcher.install()

    # Simula unbind/disconnect: a única mudança é remover o SAP da tabela.
    server._sap_to_conn.pop(0)

    ind = _indication(dest_sap=0)
    server._dispatcher._on_unidata(ind)

    assert calls == [ind]            # delegou ao host
    assert c0.writer.written == b""  # nada foi escrito na conexão antiga


# ---------------------------------------------------------------------------
# hard link
# ---------------------------------------------------------------------------


def test_hard_link_established_routes_to_initiator_not_last_bound():
    """Regressão MÉDIA-F2: established vai ao SAP iniciador, não ao último bind.

    Sob a implementação antiga, cada bind SOBRESCREVIA os callbacks de hard
    link — só o último cliente a dar bind (SAP 1) receberia o evento. Com o
    dispatcher central, o roteamento usa ``_link_session.local_initiator_sap``
    e entrega ao cliente correto (SAP 0), enquanto o SAP 1 NÃO recebe nada.
    """
    node = _make_node()
    node._link_session = _FakeSession(local_initiator_sap=0)

    server = RawSisSocketServer(node)
    c0 = _attach_conn(server, 1, 0)   # iniciador
    c1 = _attach_conn(server, 2, 1)   # bound depois — não deve receber

    server._dispatcher.install()
    server._dispatcher._on_hard_link_established(0x456, 3)

    pt, _, _ = decode_s_primitive(c0.writer.written)
    assert pt == SPrimitiveType.S_HARD_LINK_ESTABLISHED
    assert c1.writer.written == b""


def test_hard_link_rejected_routes_to_initiator():
    node = _make_node()
    node._link_session = _FakeSession(local_initiator_sap=1)

    server = RawSisSocketServer(node)
    c0 = _attach_conn(server, 1, 0)
    c1 = _attach_conn(server, 2, 1)   # iniciador

    server._dispatcher.install()
    server._dispatcher._on_hard_link_rejected(0x456, 0, reason=2)

    pt, _, _ = decode_s_primitive(c1.writer.written)
    assert pt == SPrimitiveType.S_HARD_LINK_REJECTED
    assert c0.writer.written == b""


def test_hard_link_indication_routes_to_local_dest_sap():
    node = _make_node()
    server = RawSisSocketServer(node)
    c0 = _attach_conn(server, 1, 0)
    c1 = _attach_conn(server, 2, 1)

    server._dispatcher.install()
    # 2º argumento = SAP local destino do pedido (= 1).
    server._dispatcher._on_hard_link_indication(0x456, 1, 5, 2)

    pt, _, _ = decode_s_primitive(c1.writer.written)
    assert pt == SPrimitiveType.S_HARD_LINK_INDICATION
    assert c0.writer.written == b""


def test_hard_link_terminated_per_sap_routes_by_sap():
    node = _make_node()
    server = RawSisSocketServer(node)
    c0 = _attach_conn(server, 1, 0)
    c1 = _attach_conn(server, 2, 1)

    server._dispatcher.install()
    server._dispatcher._on_hard_link_terminated_per_sap(1, 0x456, True)

    pt, _, _ = decode_s_primitive(c1.writer.written)
    assert pt == SPrimitiveType.S_HARD_LINK_TERMINATED
    assert c0.writer.written == b""


def test_global_terminated_chains_to_host_only():
    """O callback global (sem SAP) só encadeia ao host; não escreve no socket."""
    node = _make_node()
    calls = []
    node._callbacks.hard_link_terminated = lambda addr, c: calls.append((addr, c))

    server = RawSisSocketServer(node)
    c0 = _attach_conn(server, 1, 0)
    server._dispatcher.install()

    server._dispatcher._on_hard_link_terminated(0x456, False)

    assert calls == [(0x456, False)]
    assert c0.writer.written == b""


def test_hard_link_event_for_unowned_sap_falls_back_to_host():
    node = _make_node()
    established = []
    node._callbacks.hard_link_established = \
        lambda addr, sap: established.append((addr, sap))
    # iniciador é um SAP que nenhuma conexão socket possui.
    node._link_session = _FakeSession(local_initiator_sap=7)

    server = RawSisSocketServer(node)
    c0 = _attach_conn(server, 1, 0)
    server._dispatcher.install()

    server._dispatcher._on_hard_link_established(0x456, 3)

    assert established == [(0x456, 3)]
    assert c0.writer.written == b""


# ---------------------------------------------------------------------------
# install
# ---------------------------------------------------------------------------


def test_install_is_idempotent():
    node = _make_node()
    server = RawSisSocketServer(node)
    server._dispatcher.install()
    server._dispatcher.install()
    server._dispatcher.install()
    assert node.register_callbacks.call_count == 1


def test_install_captures_host_callbacks_once():
    """Os callbacks de host capturados são os presentes no 1º install."""
    node = _make_node()
    first_host = lambda ind: None
    node._callbacks.unidata_indication = first_host

    server = RawSisSocketServer(node)
    server._dispatcher.install()
    # Uma 2ª chamada de host depois do install não troca o fallback capturado.
    node._callbacks.unidata_indication = lambda ind: None
    server._dispatcher.install()  # no-op

    assert server._dispatcher._host_unidata is first_host
