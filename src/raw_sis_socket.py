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

            # Install callback for this SAP
            self._install_sap_callback(conn)
            self._install_hard_link_callbacks(conn)

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

    def _install_sap_callback(self, conn: _ClientConnection):
        """Instala callback SIS para despachar indicacoes ao cliente TCP."""
        sap_id = conn.bound_sap
        conn_id = conn.conn_id

        original_unidata = self.node._callbacks.unidata_indication

        def on_unidata(indication: SisUnidataIndication):
            # If this indication is for our SAP, send to TCP client
            if indication.dest_sap == sap_id:
                target_conn = self._connections.get(conn_id)
                if target_conn and not target_conn.writer.is_closing():
                    encoded = encode_unidata_indication(
                        priority=indication.priority,
                        dest_sap=indication.dest_sap,
                        dest_addr=0,  # local node
                        tx_mode=TxMode.ARQ,
                        src_sap=indication.src_sap,
                        src_addr=indication.src_addr,
                        updu=indication.updu,
                    )
                    self._send_raw(target_conn, encoded)
                return
            # Pass to original handler if not ours
            if original_unidata:
                original_unidata(indication)

        self.node.register_callbacks(unidata_indication=on_unidata)

    def _install_hard_link_callbacks(self, conn: _ClientConnection):
        """Instala callbacks de hard link para enviar S_PRIMITIVEs ao cliente TCP."""
        sap_id = conn.bound_sap
        conn_id = conn.conn_id

        def _send_to_conn(data):
            target = self._connections.get(conn_id)
            if target and not target.writer.is_closing():
                self._send_raw(target, data)

        def on_established(remote_addr, remote_sap):
            if self._sap_to_conn.get(sap_id) == conn_id:
                # NOTE: StanagNode does not expose link_type/link_priority in this callback;
                # using session defaults. link_priority=5 is a default, not the negotiated value.
                _send_to_conn(encode_hard_link_established(
                    remote_node_status=0,
                    link_type=0,
                    link_priority=5,
                    remote_sap=remote_sap,
                    remote_node=remote_addr,
                ))

        def on_rejected(remote_addr, remote_sap, reason):
            if self._sap_to_conn.get(sap_id) == conn_id:
                _send_to_conn(encode_hard_link_rejected(
                    reason=reason,
                    link_type=0,
                    link_priority=0,
                    remote_sap=remote_sap,
                    remote_node=remote_addr,
                ))

        def on_terminated(remote_addr, _initiator_received_confirm):
            if self._sap_to_conn.get(sap_id) == conn_id:
                _send_to_conn(encode_hard_link_terminated(
                    reason=0,
                    link_type=0,
                    link_priority=0,
                    remote_sap=0,
                    remote_node=remote_addr,
                ))

        def on_indication(remote_addr, remote_sap, link_priority, link_type):
            if self._sap_to_conn.get(sap_id) == conn_id:
                _send_to_conn(encode_hard_link_indication(
                    remote_node_status=0,
                    link_type=link_type,
                    link_priority=link_priority,
                    remote_sap=remote_sap,
                    remote_node=remote_addr,
                ))

        self.node.register_callbacks(
            hard_link_established=on_established,
            hard_link_rejected=on_rejected,
            hard_link_terminated=on_terminated,
            hard_link_indication=on_indication,
        )

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
        """Limpa estado do cliente desconectado."""
        if conn.bound_sap is not None:
            self._sap_to_conn.pop(conn.bound_sap, None)
            logger.info("Client %d disconnected (was SAP %d)", conn.conn_id, conn.bound_sap)
        else:
            logger.info("Client %d disconnected (unbound)", conn.conn_id)

        self._connections.pop(conn.conn_id, None)
        try:
            conn.writer.close()
        except Exception:
            pass
