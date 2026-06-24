"""
F.16 — Raw SIS Socket Server (MANDATORY, Edition 3).

TCP socket server na porta 5066 que expoe a interface SIS via protocolo
binário A.2.2. Clientes externos conectam via TCP, enviam S_PRIMITIVEs
codificadas e recebem respostas codificadas.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from src.s_primitive_codec import (
    PREAMBLE, HEADER_SIZE,
    decode_s_primitive, encode_s_primitive,
    decode_bind_request, encode_bind_accepted, encode_bind_rejected,
    decode_unbind_request,
    decode_unidata_request, encode_unidata_indication,
    decode_hard_link_establish, decode_hard_link_terminate,
    decode_hard_link_accept, decode_hard_link_reject,
    encode_hard_link_established, encode_hard_link_rejected,
    encode_hard_link_terminated, encode_hard_link_indication,
    encode_unbind_indication,
    encode_unidata_request_confirm, encode_unidata_request_rejected,
    encode_subnet_availability, encode_data_flow_on, encode_data_flow_off,
    encode_keep_alive,
    encode_management_msg_indication, decode_management_msg_request,
    DECODERS,
)
from src.stypes import (
    SPrimitiveType, DeliveryMode, ServiceType,
    SisBindRejectReason, SisUnidataIndication,
    TxMode,
)

if TYPE_CHECKING:
    from src.stanag_node import StanagNode

logger = logging.getLogger(__name__)

DEFAULT_PORT = 5066
MAX_CONNECTIONS = 5
READ_BUFFER_SIZE = 65536


class _ClientConnection:
    """State for one TCP client connected to the Raw SIS Socket."""

    __slots__ = ('conn_id', 'reader', 'writer', 'bound_sap', 'buffer',
                 'peername', 'service_type', 'rank', 'last_keep_alive_sent_ms')

    def __init__(self, conn_id: int, reader: asyncio.StreamReader,
                 writer: asyncio.StreamWriter):
        self.conn_id = conn_id
        self.reader = reader
        self.writer = writer
        self.bound_sap: int | None = None
        self.buffer = bytearray()
        self.peername = writer.get_extra_info('peername', ('?', 0))
        self.service_type: ServiceType | None = None
        self.rank: int = 0
        self.last_keep_alive_sent_ms: float = 0.0


class AnnexFDispatcher:
    """Roteador central de indicações SIS → conexões TCP (F.16 / MÉDIA-F2).

    Substitui a antiga cadeia de callbacks instalada *por bind*, que tinha
    dois defeitos:

      * ``unidata_indication`` era reembrulhada a cada bind, formando uma
        cadeia de closures que crescia indefinidamente e nunca era desfeita
        no unbind/disconnect — vazando referências a conexões mortas;
      * os quatro callbacks de hard link eram **sobrescritos** a cada bind:
        apenas o último cliente recebia eventos e os callbacks do host eram
        perdidos.

    O dispatcher é registrado **uma única vez** no nó (idempotente). O
    roteamento consulta a tabela viva ``server._sap_to_conn``; quando um SAP
    não pertence a nenhuma conexão socket, a indicação é delegada ao callback
    de host capturado no momento da instalação. Adicionar/remover clientes
    passa a ser apenas atualizar ``_sap_to_conn`` — sem (des)instalar nada.

    Roteamento por evento:
      * ``unidata_indication``        → SAP destino (``indication.dest_sap``);
      * ``hard_link_established``     → SAP iniciador (``_link_session
        .local_initiator_sap``, A.2.1.12 §2);
      * ``hard_link_rejected``        → idem (só o caller aguarda resposta);
      * ``hard_link_indication``      → SAP local destino (2º argumento);
      * ``hard_link_terminated_per_sap`` → SAP afetado (A.3.2.2.3 §3);
      * ``hard_link_terminated`` (global, sem SAP) → apenas encadeia ao host;
        clientes socket são notificados pela variante *per_sap*.
    """

    __slots__ = (
        '_server', '_installed',
        '_host_unidata', '_host_hl_established', '_host_hl_rejected',
        '_host_hl_indication', '_host_hl_terminated',
        '_host_hl_terminated_per_sap',
    )

    def __init__(self, server: RawSisSocketServer):
        self._server = server
        self._installed = False
        self._host_unidata = None
        self._host_hl_established = None
        self._host_hl_rejected = None
        self._host_hl_indication = None
        self._host_hl_terminated = None
        self._host_hl_terminated_per_sap = None

    def install(self) -> None:
        """Registra o dispatcher no nó (idempotente).

        Captura os callbacks de host pré-existentes como fallback para SAPs
        que não pertençam a nenhuma conexão socket.
        """
        if self._installed:
            return
        cb = self._server.node._callbacks
        self._host_unidata = cb.unidata_indication
        self._host_hl_established = cb.hard_link_established
        self._host_hl_rejected = cb.hard_link_rejected
        self._host_hl_indication = cb.hard_link_indication
        self._host_hl_terminated = cb.hard_link_terminated
        self._host_hl_terminated_per_sap = cb.hard_link_terminated_per_sap
        self._server.node.register_callbacks(
            unidata_indication=self._on_unidata,
            hard_link_established=self._on_hard_link_established,
            hard_link_rejected=self._on_hard_link_rejected,
            hard_link_indication=self._on_hard_link_indication,
            hard_link_terminated=self._on_hard_link_terminated,
            hard_link_terminated_per_sap=self._on_hard_link_terminated_per_sap,
        )
        self._installed = True

    # ------------------------------------------------------------------
    # Resolução de destino
    # ------------------------------------------------------------------

    def _conn_for_sap(self, sap_id) -> _ClientConnection | None:
        """Conexão TCP viva dona de ``sap_id``, ou None."""
        if sap_id is None or sap_id < 0:
            return None
        conn_id = self._server._sap_to_conn.get(sap_id)
        if conn_id is None:
            return None
        conn = self._server._connections.get(conn_id)
        if conn is None or conn.writer.is_closing():
            return None
        return conn

    def _initiator_sap(self) -> int:
        """SAP local que iniciou o hard link corrente (A.2.1.12 §2)."""
        session = getattr(self._server.node, '_link_session', None)
        if session is None:
            return -1
        try:
            return int(getattr(session, 'local_initiator_sap', -1))
        except (TypeError, ValueError):
            return -1

    # ------------------------------------------------------------------
    # Handlers registrados no nó
    # ------------------------------------------------------------------

    def _on_unidata(self, indication: SisUnidataIndication):
        conn = self._conn_for_sap(indication.dest_sap)
        if conn is None:
            if self._host_unidata is not None:
                self._host_unidata(indication)
            return
        self._server._send_raw(conn, encode_unidata_indication(
            priority=indication.priority,
            dest_sap=indication.dest_sap,
            dest_addr=0,  # nó local
            tx_mode=TxMode.ARQ,
            src_sap=indication.src_sap,
            src_addr=indication.src_addr,
            updu=indication.updu,
        ))

    def _on_hard_link_established(self, remote_addr, remote_sap):
        conn = self._conn_for_sap(self._initiator_sap())
        if conn is None:
            if self._host_hl_established is not None:
                self._host_hl_established(remote_addr, remote_sap)
            return
        # Lê link_type/link_priority realmente negociados em ``_link_session``
        # (MÉDIA-F3); 0 é default seguro para os nibbles do S_PDU tipo 3.
        session = getattr(self._server.node, '_link_session', None)
        link_type = (
            int(getattr(session, 'sis_hard_link_type', 0)) & 0x03
            if session is not None else 0
        )
        link_priority = (
            int(getattr(session, 'link_priority', 0)) & 0x03
            if session is not None else 0
        )
        self._server._send_raw(conn, encode_hard_link_established(
            remote_node_status=0,
            link_type=link_type,
            link_priority=link_priority,
            remote_sap=remote_sap,
            remote_node=remote_addr,
        ))

    def _on_hard_link_rejected(self, remote_addr, remote_sap, reason):
        conn = self._conn_for_sap(self._initiator_sap())
        if conn is None:
            if self._host_hl_rejected is not None:
                self._host_hl_rejected(remote_addr, remote_sap, reason)
            return
        self._server._send_raw(conn, encode_hard_link_rejected(
            reason=reason,
            link_type=0,
            link_priority=0,
            remote_sap=remote_sap,
            remote_node=remote_addr,
        ))

    def _on_hard_link_indication(self, remote_addr, local_sap, link_priority, link_type):
        # A 2ª posição carrega o SAP local destino do pedido (deve estar bound).
        conn = self._conn_for_sap(local_sap)
        if conn is None:
            if self._host_hl_indication is not None:
                self._host_hl_indication(remote_addr, local_sap, link_priority, link_type)
            return
        self._server._send_raw(conn, encode_hard_link_indication(
            remote_node_status=0,
            link_type=link_type,
            link_priority=link_priority,
            remote_sap=local_sap,
            remote_node=remote_addr,
        ))

    def _on_hard_link_terminated_per_sap(self, sap_id, remote_addr, initiator_received_confirm):
        # A.3.2.2.3 §3: callback granular carrega o SAP afetado → roteamento exato.
        conn = self._conn_for_sap(sap_id)
        if conn is None:
            if self._host_hl_terminated_per_sap is not None:
                self._host_hl_terminated_per_sap(
                    sap_id, remote_addr, initiator_received_confirm)
            return
        self._server._send_raw(conn, encode_hard_link_terminated(
            reason=0,
            link_type=0,
            link_priority=0,
            remote_sap=0,
            remote_node=remote_addr,
        ))

    def _on_hard_link_terminated(self, remote_addr, initiator_received_confirm):
        # O callback global não carrega SAP: clientes socket são notificados via
        # _on_hard_link_terminated_per_sap. Aqui só encadeamos ao host.
        if self._host_hl_terminated is not None:
            self._host_hl_terminated(remote_addr, initiator_received_confirm)


class RawSisSocketServer:
    """TCP server que expoe a interface SIS via socket (F.16)."""

    def __init__(self, node: StanagNode, host: str = '0.0.0.0', port: int = DEFAULT_PORT,
                 max_connections: int = MAX_CONNECTIONS):
        self.node = node
        self.host = host
        self.port = port
        self.max_connections = max_connections
        self._server: asyncio.Server | None = None
        self._connections: dict[int, _ClientConnection] = {}
        self._conn_counter = 0
        self._sap_to_conn: dict[int, int] = {}  # sap_id -> conn_id
        # Roteador central de indicações SIS → conexões TCP (MÉDIA-F2).
        # Registrado no nó no primeiro bind; roteia por ``_sap_to_conn``.
        self._dispatcher = AnnexFDispatcher(self)

    async def start(self):
        """Inicia o server socket assincrono."""
        self._server = await asyncio.start_server(
            self._handle_client, self.host, self.port,
        )
        addr = self._server.sockets[0].getsockname()
        logger.info("Raw SIS Socket Server listening on %s:%d", addr[0], addr[1])

    async def stop(self):
        """Para o server e fecha todas as conexoes."""
        if self._server:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        # Close all client connections
        for conn in list(self._connections.values()):
            conn.writer.close()
        self._connections.clear()
        self._sap_to_conn.clear()

    async def _handle_client(self, reader: asyncio.StreamReader,
                             writer: asyncio.StreamWriter):
        """Loop de leitura para um cliente TCP conectado."""
        if len(self._connections) >= self.max_connections:
            logger.warning("Max connections reached, rejecting new client")
            writer.close()
            return

        self._conn_counter += 1
        conn = _ClientConnection(self._conn_counter, reader, writer)
        self._connections[conn.conn_id] = conn
        logger.info("Client %d connected from %s", conn.conn_id, conn.peername)

        try:
            while True:
                data = await reader.read(READ_BUFFER_SIZE)
                if not data:
                    break
                conn.buffer.extend(data)
                self._process_buffer(conn)
        except (asyncio.CancelledError, ConnectionResetError, BrokenPipeError):
            pass
        except Exception as e:
            logger.error("Client %d error: %s", conn.conn_id, e)
        finally:
            self._cleanup_client(conn)

    def _process_buffer(self, conn: _ClientConnection):
        """Tenta decodificar S_PRIMITIVEs do buffer do cliente."""
        while True:
            # Need at least preamble to try
            idx = conn.buffer.find(PREAMBLE)
            if idx == -1:
                conn.buffer.clear()
                return
            if idx > 0:
                conn.buffer = conn.buffer[idx:]

            if len(conn.buffer) < HEADER_SIZE:
                return

            try:
                prim_type, payload, consumed = decode_s_primitive(bytes(conn.buffer))
            except ValueError:
                return  # incomplete, wait for more data

            conn.buffer = conn.buffer[consumed:]
            self._dispatch_primitive(conn, prim_type, payload)

    def _dispatch_primitive(self, conn: _ClientConnection, prim_type: int,
                            payload: bytes):
        """Mapeia S_PRIMITIVE recebida para chamadas SIS."""
        try:
            if prim_type == SPrimitiveType.S_BIND_REQUEST:
                self._handle_bind_request(conn, payload)
            elif prim_type == SPrimitiveType.S_UNBIND_REQUEST:
                self._handle_unbind_request(conn)
            elif prim_type == SPrimitiveType.S_UNIDATA_REQUEST:
                self._handle_unidata_request(conn, payload)
            elif prim_type == SPrimitiveType.S_HARD_LINK_ESTABLISH:
                self._handle_hard_link_establish(conn, payload)
            elif prim_type == SPrimitiveType.S_HARD_LINK_TERMINATE:
                self._handle_hard_link_terminate(conn, payload)
            elif prim_type == SPrimitiveType.S_HARD_LINK_ACCEPT:
                self._handle_hard_link_accept(conn, payload)
            elif prim_type == SPrimitiveType.S_HARD_LINK_REJECT:
                self._handle_hard_link_reject(conn, payload)
            elif prim_type == SPrimitiveType.S_KEEP_ALIVE:
                self._handle_keep_alive(conn)
            elif prim_type == SPrimitiveType.S_MANAGEMENT_MSG_REQUEST:
                self._handle_management_msg(conn, payload)
            else:
                logger.warning("Client %d: unhandled primitive type %d",
                               conn.conn_id, prim_type)
        except Exception as e:
            logger.error("Client %d: error dispatching type %d: %s",
                         conn.conn_id, prim_type, e)

    def _handle_bind_request(self, conn: _ClientConnection, payload: bytes):
        """Processa S_BIND_REQUEST do cliente."""
        req = decode_bind_request(payload)
        sap_id = req['sap_id']

        # Check if SAP already bound by another connection
        if sap_id in self._sap_to_conn:
            self._send_raw(conn, encode_bind_rejected(
                SisBindRejectReason.SAP_ALREADY_ALLOCATED))
            return

        # Try to bind on the SIS
        try:
            st = req['service_type']  # dict from decode_service_type
            service = ServiceType(
                transmission_mode=st['transmission_mode'],
                delivery_confirmation=st['delivery_confirmation'],
                delivery_order=st['delivery_order'],
                extended=st['extended'],
                min_retransmissions=st['min_retransmissions'],
            )
            try:
                self.node.bind(sap_id, rank=req['rank'], service=service)
            except Exception as bind_err:
                # SAP may already be bound by the host application (e.g. pre-bind in ChatApp).
                # If the node already holds it, allow the external socket client to register
                # against the existing binding rather than rejecting outright.
                if sap_id not in self.node._saps:
                    raise
                logger.info(
                    "Client %d: SAP %d already bound by host node; "
                    "accepting external client bind on existing SAP (%s)",
                    conn.conn_id, sap_id, bind_err,
                )

            conn.bound_sap = sap_id
            conn.service_type = service
            conn.rank = req['rank']
            self._sap_to_conn[sap_id] = conn.conn_id

            # Roteador central registrado uma única vez; roteia indicações
            # deste e dos demais SAPs via ``_sap_to_conn`` (MÉDIA-F2).
            self._dispatcher.install()

            mtu = 2048  # default MTU
            self._send_raw(conn, encode_bind_accepted(sap_id, mtu))
            logger.info("Client %d bound to SAP %d", conn.conn_id, sap_id)

        except Exception as e:
            logger.error("Bind failed for client %d SAP %d: %s",
                         conn.conn_id, sap_id, e)
            self._send_raw(conn, encode_bind_rejected(
                SisBindRejectReason.NOT_ENOUGH_RESOURCES))

    def _handle_unbind_request(self, conn: _ClientConnection):
        """Processa S_UNBIND_REQUEST."""
        if conn.bound_sap is not None:
            self._sap_to_conn.pop(conn.bound_sap, None)
            logger.info("Client %d unbound from SAP %d", conn.conn_id, conn.bound_sap)
            conn.bound_sap = None

    def _handle_unidata_request(self, conn: _ClientConnection, payload: bytes):
        """Processa S_UNIDATA_REQUEST — envia dados via SIS."""
        if conn.bound_sap is None:
            logger.warning("Client %d: UNIDATA_REQUEST without bind", conn.conn_id)
            return

        req = decode_unidata_request(payload)
        dm = req['delivery_mode']
        tx_mode_val = dm.get('tx_mode', 0)
        arq = (tx_mode_val == 0)  # 0=ARQ, 1=NON_ARQ, 2=EXP_NON_ARQ

        # A.2.2.28.2 bits [3:2]: 0=NONE, 1=NODE DELIVERY, 2=CLIENT DELIVERY, 3=BOTH
        dm_confirm = dm.get('delivery_confirm', 0)
        mode = DeliveryMode(
            arq_mode=arq,
            node_delivery_confirm=(dm_confirm in (1, 3)),
            client_delivery_confirm=(dm_confirm in (2, 3)),
            expedited=(tx_mode_val == 2),
        )

        self.node.unidata_request(
            sap_id=conn.bound_sap,
            dest_addr=req['dest_addr'],
            dest_sap=req['dest_sap'],
            priority=req['priority'],
            ttl_seconds=req['ttl'],
            mode=mode,
            updu=req['updu'],
        )

    def _handle_hard_link_establish(self, conn: _ClientConnection, payload: bytes):
        """Processa S_HARD_LINK_ESTABLISH."""
        if conn.bound_sap is None:
            logger.warning("Client %d: HARD_LINK_ESTABLISH without bind", conn.conn_id)
            return
        req = decode_hard_link_establish(payload)
        self.node.hard_link_establish(
            sap_id=conn.bound_sap,
            link_priority=req['link_priority'],
            remote_addr=req['remote_node'],
            remote_sap=req['remote_sap'],
            link_type=req['link_type'],
        )

    def _handle_hard_link_terminate(self, conn: _ClientConnection, payload: bytes):
        """Processa S_HARD_LINK_TERMINATE."""
        if conn.bound_sap is None:
            return
        req = decode_hard_link_terminate(payload)
        self.node.hard_link_terminate(
            sap_id=conn.bound_sap,
            remote_addr=req['remote_node'],
        )

    def _handle_hard_link_accept(self, conn: _ClientConnection, payload: bytes):
        """Processa S_HARD_LINK_ACCEPT."""
        if conn.bound_sap is None:
            logger.warning("Client %d: HARD_LINK_ACCEPT without bind", conn.conn_id)
            return
        req = decode_hard_link_accept(payload)
        self.node.hard_link_accept(
            link_priority=req['link_priority'],
            link_type=req['link_type'],
            remote_addr=req['remote_node'],
            remote_sap=req['remote_sap'],
        )

    def _handle_hard_link_reject(self, conn: _ClientConnection, payload: bytes):
        """Processa S_HARD_LINK_REJECT."""
        if conn.bound_sap is None:
            logger.warning("Client %d: HARD_LINK_REJECT without bind", conn.conn_id)
            return
        req = decode_hard_link_reject(payload)
        self.node.hard_link_reject(
            reason=req['reason'],
            link_priority=req['link_priority'],
            link_type=req['link_type'],
            remote_addr=req['remote_node'],
            remote_sap=req['remote_sap'],
        )

    def _handle_keep_alive(self, conn: _ClientConnection):
        """Processa S_KEEP_ALIVE — respond within 10s, no more than 1x/120s (A.2.1.17)."""
        import time
        now_ms = time.monotonic() * 1000
        if (now_ms - conn.last_keep_alive_sent_ms) >= 120_000:
            self._send_raw(conn, encode_keep_alive())
            conn.last_keep_alive_sent_ms = now_ms
        else:
            logger.debug("Client %d: keep-alive throttled (120s interval)",
                         conn.conn_id)

    def _handle_management_msg(self, conn: _ClientConnection, payload: bytes):
        """Processa S_MANAGEMENT_MSG_REQUEST (A.2.1.15§3: requires rank 15)."""
        if conn.rank != 15:
            logger.warning("Client %d: management msg rejected (rank=%d, need 15)",
                           conn.conn_id, conn.rank)
            return
        req = decode_management_msg_request(payload)
        logger.debug("Client %d: management msg: %d bytes",
                     conn.conn_id, len(req['message']))

    def send_to_client(self, sap_id: int, prim_type: int, payload: bytes):
        """Envia S_PRIMITIVE codificada a um cliente via SAP ID."""
        conn_id = self._sap_to_conn.get(sap_id)
        if conn_id is None:
            return
        conn = self._connections.get(conn_id)
        if conn is None or conn.writer.is_closing():
            return
        raw = encode_s_primitive(prim_type, payload)
        self._send_raw(conn, raw)

    def _send_raw(self, conn: _ClientConnection, data: bytes):
        """Envia bytes raw ao cliente TCP."""
        try:
            conn.writer.write(data)
        except (ConnectionResetError, BrokenPipeError):
            logger.warning("Client %d: write failed, connection lost", conn.conn_id)

    def _cleanup_client(self, conn: _ClientConnection):
        """Limpa estado do cliente desconectado.

        F.16 / A.2.1: ao desconectar um cliente TCP que ainda tinha SAP
        vinculado, emitimos ``S_UNBIND_INDICATION`` para o cliente (best
        effort — pode falhar se o socket já fechou) e fazemos ``unbind``
        no nó para liberar o SAP de imediato.
        """
        if conn.bound_sap is not None:
            sap_id = conn.bound_sap
            self._sap_to_conn.pop(sap_id, None)
            try:
                # PEER_DISCONNECT (=2): cliente fechou inesperadamente.
                self._send_raw(conn, encode_unbind_indication(reason=2))
            except Exception:
                # Socket pode estar fechado; não propaga.
                pass
            try:
                self.node.unbind(sap_id)
            except Exception:
                pass
            logger.info("Client %d disconnected (was SAP %d)", conn.conn_id, sap_id)
        else:
            logger.info("Client %d disconnected (unbound)", conn.conn_id)

        self._connections.pop(conn.conn_id, None)
        try:
            conn.writer.close()
        except Exception:
            pass
