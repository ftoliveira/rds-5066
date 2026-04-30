"""StanagNode — Nó unificado STANAG 5066 (DTS + SIS).

Combina multiplexação DTS (CAS + Non-ARQ controle + ARQ dados) com a
camada SIS (SAPs, filas de prioridade, sessões soft/hard link, TTL/TTD)
em uma única classe.
"""

from __future__ import annotations

import struct
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Callable, Optional

from src.arq import ArqEngine
from src.cas import CasConfig, CASEngine, decode_cpdu, encode_cpdu
from src.dpdu_frame import build_warning, decode_dpdu, encode_dpdu
from src.dts_state import DTSState, DTSStateMachine
from src.expedited_arq import ExpeditedArqEngine
from src.management import ManagementEngine
from src.flow_log import dpdu_wire_hint, flow_rx, flow_tx, payload_hint
from src.modem_if import ModemConfig, ModemInterface
from src.non_arq import NonArqEngine
from src.sis import (
    _LinkSession,
    _PendingHardLinkIndication,
    _SapContext,
    _SisCallbacks,
    _TxEntry,
    decode_spdu,
    decode_spdu_data_delivery_confirm,
    decode_spdu_data_delivery_fail,
    decode_spdu_hard_link_rejected,
    decode_spdu_hard_link_request,
    decode_spdu_hard_link_terminate,
    encode_spdu,
    encode_spdu_data_delivery_confirm,
    encode_spdu_data_delivery_confirm_from,
    encode_spdu_data_delivery_fail,
    encode_spdu_data_delivery_fail_from,
    encode_spdu_hard_link_confirm,
    encode_spdu_hard_link_rejected,
    encode_spdu_hard_link_request,
    encode_spdu_hard_link_terminate,
    encode_spdu_hard_link_terminate_confirm,
    spdu_type,
)
from src.stypes import (
    CPDU,
    CPDUType,
    CasLinkState,
    DeliveryMode,
    DPDUType,
    LinkType,
    NonArqDelivery,
    PhysicalLinkType,
    ServiceType,
    SisBindRejectReason,
    SisDataDeliveryFailReason,
    SisHardLinkRejectReason,
    SisHardLinkTerminateReason,
    SisLinkSessionState,
    SisRejectReason,
    SisUnbindIndicationReason,
    SisUnidataIndication,
    SPDU,
    SPDU_TYPE_DATA,
    SPDU_TYPE_DATA_DELIVERY_CONFIRM,
    SPDU_TYPE_DATA_DELIVERY_FAIL,
    SPDU_TYPE_HARD_LINK_ESTABLISH_CONFIRM,
    SPDU_TYPE_HARD_LINK_ESTABLISH_REJECTED,
    SPDU_TYPE_HARD_LINK_ESTABLISH_REQUEST,
    SPDU_TYPE_HARD_LINK_TERMINATE,
    SPDU_TYPE_HARD_LINK_TERMINATE_CONFIRM,
    TxMode,
)


# ---------------------------------------------------------------------------
# Modem demux — filtra frames RX por tipo (Non-ARQ vs ARQ)
# ---------------------------------------------------------------------------


class _ModemDemux(ModemInterface):
    """Modem que lê de uma fila RX (só frames 7/8) e envia para o modem real."""

    def __init__(self, real_modem: ModemInterface) -> None:
        super().__init__(config=real_modem.config)
        self._real = real_modem
        self._rx_queue: deque[bytes] = deque()

    def modem_rx_read_frame(self) -> bytes | None:
        if not self._rx_queue:
            return None
        return self._rx_queue.popleft()

    def modem_tx_dpdu(self, dpdu_buffer: bytes, length: Optional[int] = None) -> int:
        return self._real.modem_tx_dpdu(dpdu_buffer, length)

    def modem_tx_burst(self, frames: list[bytes]) -> int:
        return self._real.modem_tx_burst(frames)

    def modem_rx_start(self) -> None:
        self._real.modem_rx_start()

    def modem_rx_stop(self) -> None:
        self._real.modem_rx_stop()

    def modem_get_carrier_status(self) -> bool:
        return self._real.modem_get_carrier_status()

    def modem_set_tx_enable(self, enabled: bool) -> None:
        self._real.modem_set_tx_enable(enabled)

    def push_frame(self, frame: bytes) -> None:
        self._rx_queue.append(frame)


# ---------------------------------------------------------------------------
# StanagNode — classe unificada
# ---------------------------------------------------------------------------


class StanagNode:
    """Nó unificado STANAG 5066 — DTS (Phase 3) + SIS (Phase 4)."""

    MAX_SAPS = 16
    MTU_DEFAULT = 2048
    SOFT_LINK_IDLE_TIMEOUT_MS = 30_000
    HARD_LINK_ESTABLISH_TIMEOUT_MS = 60_000  # A.3.2.2.2§12
    HARD_LINK_TERMINATE_TIMEOUT_MS = 30_000  # A.3.2.2.3§4
    MANAGEMENT_MSG_REQUIRED_RANK = 15  # A.2.1.15§3
    KEEP_ALIVE_MIN_INTERVAL_MS = 120_000  # A.2.1.17§3
    KEEP_ALIVE_RESPONSE_TIMEOUT_MS = 10_000  # A.2.1.17§1

    def __init__(
        self,
        local_node_address: int,
        modem: ModemInterface,
        *,
        cas_config: CasConfig | None = None,
        max_user_data_bytes: int = 1023,
        soft_link_idle_timeout_ms: int = 30_000,
        half_duplex: bool = True,
        use_arq_data: bool = True,
        arq_reset_retransmit_ms: int | None = None,
        arq_retx_timeout_ms: int | None = None,
        arq_max_retries: int | None = None,
        hard_link_establish_timeout_ms: int = 60_000,
        hard_link_terminate_timeout_ms: int = 30_000,
        max_expedited_per_client: int = 0,
        allow_management_rank: bool = False,
    ) -> None:
        self.local_node_address = local_node_address
        self.modem = modem
        self.use_arq_data = use_arq_data
        self._soft_link_idle_timeout_ms = soft_link_idle_timeout_ms
        self._max_user_data_bytes = max_user_data_bytes
        self._current_time_ms = 0

        # --- DTS state ---
        self._dts = DTSStateMachine()
        self._arq_pending_tx: list[bytes] = []
        self._pending_primitives: list[tuple] = []  # GAP-04: queued primitives for state transitions
        self._we_initiated_link = False
        self._prev_cas_state_dts: Optional[CasLinkState] = None  # DTS-level

        # --- Modem demux ---
        self._demux = _ModemDemux(modem)

        # --- Non-ARQ engine ---
        self.non_arq = NonArqEngine(
            local_node_address,
            self._demux,
            max_user_data_bytes=max_user_data_bytes,
            half_duplex=half_duplex,
            delivery_handler=self._on_non_arq_delivery,
        )

        # --- CAS engine ---
        cfg = cas_config or CasConfig()
        self.cas = CASEngine(
            local_node_address,
            self.non_arq,
            call_timeout_ms=int(cfg.call_timeout_seconds * 1000),
            break_timeout_ms=int(cfg.break_timeout_seconds * 1000),
            max_retries=cfg.max_retries,
            max_nonexclusive_links=cfg.max_nonexclusive_links,
            called_idle_timeout_ms=int(cfg.called_idle_timeout_seconds * 1000),
            arq_data_handler=self._cas_arq_data_handler,
        )

        # --- ARQ engine ---
        arq_kw: dict = dict(
            local_node_address=local_node_address,
            remote_node_address=0,
            delivery_callback=self._on_arq_delivery,
        )
        if arq_reset_retransmit_ms is not None:
            arq_kw["reset_retransmit_ms"] = int(arq_reset_retransmit_ms)
        if arq_retx_timeout_ms is not None:
            arq_kw["retx_timeout_ms"] = int(arq_retx_timeout_ms)
        if arq_max_retries is not None:
            arq_kw["max_retries"] = int(arq_max_retries)
        self.arq = ArqEngine(**arq_kw)

        # --- Expedited ARQ engine ---
        expedited_arq_kw = {k: v for k, v in arq_kw.items() if k != "reset_retransmit_ms"}
        expedited_arq_kw["delivery_callback"] = self._on_expedited_delivery
        self.expedited_arq = ExpeditedArqEngine(**expedited_arq_kw)

        # --- Management engine ---
        self._mgmt_engine: ManagementEngine | None = None

        # --- DTS internal state ---
        self.received_cpdus: list = []
        self.received_deliveries: list[NonArqDelivery] = []

        # --- SIS internal state ---
        self._saps: dict[int, _SapContext] = {}
        self._tx_queue: list[_TxEntry] = []
        self._flow_on: bool = True
        self._link_session = _LinkSession()
        self._callbacks = _SisCallbacks()
        self._rx_cursor: int = 0
        self._prev_cas_state_sis: CasLinkState = self.cas.state  # SIS-level
        self._deferred_hard_link_request: bool = False
        self._hard_link_establish_timeout_ms = hard_link_establish_timeout_ms
        self._hard_link_terminate_timeout_ms = hard_link_terminate_timeout_ms
        self._max_expedited_per_client = max_expedited_per_client
        self._expedited_counts: dict[int, int] = {}  # sap_id -> count

        # A.2.1.1 §6: rank 15 (gerência) só pode ser concedido a clientes
        # autorizados. O nó decide a política via flag de construção.
        self._allow_management_rank: bool = allow_management_rank

        # Rank inferido para clientes remotos (A.3.2.2.1 §1). O S_PDU tipo 3
        # não carrega o campo Rank, portanto cada nó mantém uma tabela local
        # mapeando (remote_node_addr, remote_sap) -> rank conhecido. Se a
        # entrada não existir, usa-se ``_default_remote_rank``.
        self._remote_rank_table: dict[tuple[int, int], int] = {}
        self._default_remote_rank: int = 0

        # Último reason recebido em S_PDU TERMINATE (Annex A A.3.1.7); pode ser
        # consultado por testes/observadores. Não é exposto via callback para
        # preservar compatibilidade da API.
        self._last_terminate_reason: int = 0

        # Start modem RX
        modem.modem_rx_start()

    # -------------------------------------------------------------------
    # SIS: Configuração de Rank (A.2.1.1 / A.3.2.2.1)
    # -------------------------------------------------------------------

    def set_remote_rank(self, remote_addr: int, remote_sap: int, rank: int) -> None:
        """Declara o rank conhecido de um cliente remoto.

        Usado pelas regras de precedência de Hard Link (A.3.2.2.1 §1) já que o
        S_PDU tipo 3 não carrega o campo Rank na rede.
        """
        if not (0 <= rank <= 15):
            raise ValueError(f"rank deve estar em 0-15, got {rank}")
        self._remote_rank_table[(remote_addr, remote_sap)] = rank

    def set_default_remote_rank(self, rank: int) -> None:
        """Define o rank assumido para clientes remotos sem entrada na tabela."""
        if not (0 <= rank <= 15):
            raise ValueError(f"rank deve estar em 0-15, got {rank}")
        self._default_remote_rank = rank

    def _resolve_remote_rank(self, remote_addr: int, remote_sap: int) -> int:
        return self._remote_rank_table.get(
            (remote_addr, remote_sap), self._default_remote_rank
        )

    # -------------------------------------------------------------------
    # Propriedades públicas (acesso direto)
    # -------------------------------------------------------------------

    @property
    def dts(self) -> DTSStateMachine:
        return self._dts

    # -------------------------------------------------------------------
    # Backward-compat: node.sis.X -> node.X, node.sis._node.Y -> node.Y
    # -------------------------------------------------------------------

    @property
    def sis(self) -> StanagNode:
        return self

    @property
    def _node(self) -> StanagNode:
        return self

    # -------------------------------------------------------------------
    # SIS: Ciclo de vida do cliente
    # -------------------------------------------------------------------

    def bind(self, sap_id: int, rank: int = 0, service: ServiceType | None = None) -> int:
        """Vincula um SAP (0-15). Retorna sap_id. MTU=2048 (Annex A).

        A.2.1.1 §5-6: ``rank`` deve estar em 0-15 (4 bits). O valor 15 é
        reservado a clientes de gerência e só é aceito quando o nó foi
        construído com ``allow_management_rank=True``; caso contrário a
        primitiva é rejeitada com ``NOT_ENOUGH_RESOURCES`` (sem privilégio).
        """
        if not (0 <= sap_id < self.MAX_SAPS):
            if self._callbacks.bind_rejected is not None:
                self._callbacks.bind_rejected(SisBindRejectReason.INVALID_SAP_ID)
            else:
                raise ValueError(f"SAP id deve ser 0-{self.MAX_SAPS - 1}, got {sap_id}")
            return -1
        if not (0 <= rank <= 15):
            if self._callbacks.bind_rejected is not None:
                self._callbacks.bind_rejected(SisBindRejectReason.NOT_ENOUGH_RESOURCES)
            else:
                raise ValueError(
                    f"rank deve estar em 0-15 (A.2.1.1 §5), got {rank}"
                )
            return -1
        if rank == self.MANAGEMENT_MSG_REQUIRED_RANK and not self._allow_management_rank:
            if self._callbacks.bind_rejected is not None:
                self._callbacks.bind_rejected(SisBindRejectReason.NOT_ENOUGH_RESOURCES)
            else:
                raise ValueError(
                    "rank=15 (gerência) requer allow_management_rank=True"
                )
            return -1
        # Tabela F-1: SAP 0 é reservado ao Subnet Management Client; só é
        # acessível a clientes autorizados via ``allow_management_rank``.
        if sap_id == 0 and not self._allow_management_rank:
            if self._callbacks.bind_rejected is not None:
                self._callbacks.bind_rejected(SisBindRejectReason.NOT_ENOUGH_RESOURCES)
            else:
                raise ValueError(
                    "SAP 0 (Subnet Management) requer allow_management_rank=True"
                )
            return -1
        if sap_id in self._saps:
            if self._callbacks.bind_rejected is not None:
                self._callbacks.bind_rejected(SisBindRejectReason.SAP_ALREADY_ALLOCATED)
            else:
                raise ValueError(f"SAP {sap_id} já está vinculado")
            return -1
        if len(self._saps) >= self.MAX_SAPS:
            if self._callbacks.bind_rejected is not None:
                self._callbacks.bind_rejected(SisBindRejectReason.NOT_ENOUGH_RESOURCES)
            else:
                raise ValueError("Máximo de SAPs atingido")
            return -1
        self._saps[sap_id] = _SapContext(
            sap_id=sap_id,
            rank=rank,
            service=service or ServiceType(),
        )
        return sap_id

    def unbind(self, sap_id: int) -> None:
        """Desvincula um SAP."""
        self._saps.pop(sap_id, None)

    # -------------------------------------------------------------------
    # SIS: Callbacks
    # -------------------------------------------------------------------

    def register_callbacks(
        self,
        *,
        unidata_indication: Callable | None = None,
        request_confirm: Callable | None = None,
        request_rejected: Callable | None = None,
        bind_rejected: Callable | None = None,
        unbind_indication: Callable | None = None,
        hard_link_established: Callable | None = None,
        hard_link_indication: Callable | None = None,
        hard_link_rejected: Callable | None = None,
        hard_link_terminated: Callable | None = None,
        hard_link_terminated_per_sap: Callable | None = None,
    ) -> None:
        """Registra callbacks para primitivas SIS."""
        if unidata_indication is not None:
            self._callbacks.unidata_indication = unidata_indication
        if request_confirm is not None:
            self._callbacks.request_confirm = request_confirm
        if request_rejected is not None:
            self._callbacks.request_rejected = request_rejected
        if bind_rejected is not None:
            self._callbacks.bind_rejected = bind_rejected
        if unbind_indication is not None:
            self._callbacks.unbind_indication = unbind_indication
        if hard_link_established is not None:
            self._callbacks.hard_link_established = hard_link_established
        if hard_link_indication is not None:
            self._callbacks.hard_link_indication = hard_link_indication
        if hard_link_rejected is not None:
            self._callbacks.hard_link_rejected = hard_link_rejected
        if hard_link_terminated is not None:
            self._callbacks.hard_link_terminated = hard_link_terminated
        if hard_link_terminated_per_sap is not None:
            self._callbacks.hard_link_terminated_per_sap = hard_link_terminated_per_sap

    # -------------------------------------------------------------------
    # SIS: Submissão de dados
    # -------------------------------------------------------------------

    def unidata_request(
        self,
        sap_id: int,
        dest_addr: int,
        dest_sap: int,
        priority: int,
        ttl_seconds: float,
        mode: DeliveryMode | None = None,
        updu: bytes = b"",
    ) -> None:
        """Submete um U_PDU para transmissão."""
        if sap_id not in self._saps:
            self._fire_rejected(sap_id, SisRejectReason.SAP_NOT_BOUND)
            return
        if len(updu) > self.MTU_DEFAULT:
            self._fire_rejected(sap_id, SisRejectReason.MTU_EXCEEDED)
            return
        if not self._flow_on:
            self._fire_rejected(sap_id, SisRejectReason.FLOW_CONTROL)
            return

        dm = mode or DeliveryMode()
        # A.2.1.5 §8: TTL = 0 significa "infinito" — usa float('inf') como
        # sentinela para sinalizar que a U_PDU nunca expira via TTD/purge.
        ttd = (
            float("inf") if ttl_seconds <= 0
            else time.time() + ttl_seconds
        )
        tx_mode = TxMode.ARQ if dm.arq_mode else TxMode.NON_ARQ
        if dm.expedited:
            tx_mode = TxMode.EXPEDITED_NON_ARQ

        spdu = SPDU(
            version=1,
            src_sap=sap_id,
            dest_sap=dest_sap,
            priority=priority,
            ttd=ttd,
            tx_mode=int(tx_mode),
            node_delivery_confirm_required=dm.node_delivery_confirm,
            client_delivery_confirm_required=dm.client_delivery_confirm,
            deliver_in_order=dm.in_order,
            updu=updu,
        )
        entry = _TxEntry(
            spdu=spdu,
            dest_addr=dest_addr,
            src_sap=sap_id,
            enqueued_at_ms=self._current_time_ms,
            delivery_mode=dm,
        )
        self._tx_queue.append(entry)

    def expedited_unidata_request(
        self,
        sap_id: int,
        dest_addr: int,
        dest_sap: int,
        ttl_seconds: float,
        mode: DeliveryMode | None = None,
        updu: bytes = b"",
    ) -> None:
        """Submete U_PDU expedited ARQ (tipos 4/5, stop-and-wait).

        A.2.1.10§3-4: rastreia o número de expedited requests por cliente; se
        exceder ``max_expedited_per_client`` o nó desliga unilateralmente o
        cliente via S_UNBIND_INDICATION reason=4.
        """
        if sap_id not in self._saps:
            self._fire_rejected(sap_id, SisRejectReason.SAP_NOT_BOUND)
            return
        if not self.track_expedited_request(sap_id):
            # SAP foi desligado por exceder o limite; nada mais a fazer.
            return
        dm = mode or DeliveryMode(arq_mode=True, expedited=True)
        self.unidata_request(
            sap_id, dest_addr, dest_sap,
            priority=0,  # A.3.1.1: S_EXPEDITED_UNIDATA_REQUEST has no priority; use 0
            ttl_seconds=ttl_seconds,
            mode=dm,
            updu=updu,
        )

    # -------------------------------------------------------------------
    # SIS: Controle de enlace
    # -------------------------------------------------------------------

    def hard_link_establish(
        self,
        sap_id: int,
        link_priority: int,
        remote_addr: int,
        remote_sap: int,
        link_type: int = 0,
    ) -> None:
        """Solicita estabelecimento de hard link (Annex A). Envia S_PDU tipo 3 após CAS MADE."""
        if sap_id not in self._saps:
            return
        self._link_session.state = SisLinkSessionState.ESTABLISHING
        self._link_session.link_type = LinkType.HARD
        self._link_session.remote_addr = remote_addr
        self._link_session.remote_sap = remote_sap
        self._link_session.hard_link_owner = sap_id
        self._link_session.hard_link_owner_rank = self._saps[sap_id].rank
        self._link_session.local_initiator_sap = sap_id
        # A.2.1.11 §3: Link Priority limitado a 0-3 (S_PDU tipo 3 carrega 2 bits).
        self._link_session.link_priority = min(3, max(0, link_priority))
        self._link_session.sis_hard_link_type = link_type & 0x03
        self._link_session.is_calling = True
        self._we_initiated_link = True
        self.make_link(remote_addr, link_type=PhysicalLinkType.EXCLUSIVE)

    def hard_link_terminate(
        self,
        sap_id: int,
        remote_addr: int,
        reason: int = int(SisHardLinkTerminateReason.LINK_TERMINATED_BY_REMOTE),
    ) -> None:
        """Solicita terminação de hard link.

        A.2.1.12 §2: a sub-rede só termina o link se o cliente que pediu a
        terminação foi quem o estabeleceu localmente. Se esse nó é apenas o
        solicitado (Type 0/1, sem dono local), nenhum SAP local pode invocar
        terminate — só o remoto via TERMINATE recebido.

        ``reason`` (4 bits, ver ``SisHardLinkTerminateReason``) é encodado no
        S_PDU tipo 6 enviado ao peer.
        """
        if self._link_session.link_type != LinkType.HARD:
            return
        if sap_id not in self._saps:
            return
        local_initiator = self._link_session.local_initiator_sap
        if local_initiator < 0 or local_initiator != sap_id:
            return
        self._link_session.state = SisLinkSessionState.TERMINATING
        self._link_session.awaiting_terminate_confirm = True
        self._link_session.terminate_confirm_timeout_ms = (
            self._current_time_ms + self._hard_link_terminate_timeout_ms
        )
        self._send_control_expedited(
            remote_addr, encode_spdu_hard_link_terminate(reason & 0x0F)
        )

    def hard_link_accept(
        self,
        link_priority: int,
        link_type: int,
        remote_addr: int,
        remote_sap: int,
        local_sap: int | None = None,
    ) -> None:
        """Aceita hard link tipo 2 indicado via S_HARD_LINK_INDICATION (Annex A).

        Quando ``local_sap`` é fornecido (SAP local que aceitou a indicação),
        ele é registrado como ``local_initiator_sap`` da sessão para que
        ``hard_link_terminate`` reconheça o originador.
        """
        p = self._link_session.pending_indication
        if p is None or p.src_addr != remote_addr or p.remote_sap != remote_sap:
            return
        self._link_session.state = SisLinkSessionState.ACTIVE
        self._link_session.link_type = LinkType.HARD
        self._link_session.remote_addr = p.src_addr
        self._link_session.remote_sap = p.remote_sap
        self._link_session.hard_link_owner = p.remote_sap
        # Captura o tipo do hard link aceito (Type 2 atualmente, mas o
        # helper per-sap depende deste campo para distinguir notificações).
        self._link_session.sis_hard_link_type = p.link_type & 0x03
        self._link_session.link_priority = p.link_priority & 0x03
        self._link_session.local_initiator_sap = (
            local_sap if local_sap is not None and local_sap in self._saps else -1
        )
        self._link_session.pending_indication = None
        self._send_control_expedited(p.src_addr, encode_spdu_hard_link_confirm())
        if self._callbacks.hard_link_established is not None:
            self._callbacks.hard_link_established(p.src_addr, p.remote_sap)
        # Indicações ainda em backlog ficam pendentes para o próximo
        # accept/reject. Como já há link ativo, novas que cheguem entram em
        # precedência via ``_handle_hard_link_request``.
        self._promote_next_pending_indication()

    def hard_link_reject(
        self,
        reason: int,
        link_priority: int,
        link_type: int,
        remote_addr: int,
        remote_sap: int,
    ) -> None:
        """Rejeita hard link tipo 2/3 indicado via S_HARD_LINK_INDICATION (Annex A)."""
        p = self._link_session.pending_indication
        if p is None or p.src_addr != remote_addr or p.remote_sap != remote_sap:
            return
        self._link_session.pending_indication = None
        self._send_control_expedited(p.src_addr, encode_spdu_hard_link_rejected(reason))
        self._promote_next_pending_indication()

    # -------------------------------------------------------------------
    # SIS: Flow control
    # -------------------------------------------------------------------

    def data_flow_on(self) -> None:
        """Habilita despacho de dados."""
        self._flow_on = True

    def data_flow_off(self) -> None:
        """Desabilita despacho de dados."""
        self._flow_on = False

    # -------------------------------------------------------------------
    # Métodos de enlace (CAS / Hard Link API)
    # -------------------------------------------------------------------

    def make_link(self, remote_node: int, current_time_ms: int | None = None,
                  link_type: PhysicalLinkType = PhysicalLinkType.NONEXCLUSIVE) -> None:
        self._we_initiated_link = True
        t = current_time_ms if current_time_ms is not None else self._current_time_ms
        self.cas.make_link(remote_node, t, link_type=link_type)

    def break_link(self) -> None:
        self.cas.break_link(self._current_time_ms)

    def send_data(self, payload: bytes, deliver_in_order: bool = True) -> None:
        if self.use_arq_data:
            if self.cas.state != CasLinkState.MADE or self.cas.remote_node_address is None:
                return
            # B.3.1.1/B.3: S_PDU DEVE ser encapsulado em DATA C_PDU (0x00 + S_PDU)
            cpdu_bytes = self._wrap_in_cpdu(payload)
            self._dts.enter_data()
            self.arq.submit_cpdu(cpdu_bytes, deliver_in_order=deliver_in_order)
        else:
            self.cas.send_data(payload)

    # -------------------------------------------------------------------
    # tick() unificado
    # -------------------------------------------------------------------

    def tick(self, current_time_ms: int) -> None:
        """Ciclo principal unificado — chamar periodicamente."""
        self._current_time_ms = current_time_ms

        # 1. Ler frames do modem -> _dispatch_rx_frame()
        while True:
            frame = self.modem.modem_rx_read_frame()
            if frame is None:
                break
            self._dispatch_rx_frame(frame)

        # 2. non_arq.process_rx()
        deliveries = self.non_arq.process_rx(current_time_ms)

        # 3. Transições CAS internas (FULL_RESET, DTS on_link_made/broken)
        self._dts_cas_transitions(current_time_ms)

        # 4. cas.tick()
        self.cas.tick(current_time_ms)

        # 5. TX prioritário (ARQ pending, non_arq, expedited, regular ARQ)
        self._dts_transmit(current_time_ms)

        # 6. _process_rx() (S_PDUs recebidos via ARQ)
        self._process_rx()

        # 7. _process_non_arq_deliveries()
        self._process_non_arq_deliveries(deliveries)

        # 8. _monitor_cas_transitions() (sessão SIS)
        self._monitor_cas_transitions()

        # 9. Enviar HARD_LINK_REQUEST adiado
        if self._deferred_hard_link_request and not self.arq.reset_pending:
            self._deferred_hard_link_request = False
            self._link_session.awaiting_hard_link_response = True
            self._link_session.hard_link_response_timeout_ms = (
                current_time_ms + self._hard_link_establish_timeout_ms
            )
            payload = encode_spdu_hard_link_request(
                self._link_session.sis_hard_link_type,
                self._link_session.link_priority,
                self._link_session.hard_link_owner if self._link_session.hard_link_owner >= 0 else 0,
                self._link_session.remote_sap,
            )
            self._send_control_expedited(self._link_session.remote_addr, payload)

        # 10. Check hard link timeouts (A.3.2.2.2§12, A.3.2.2.3§4)
        self._check_hard_link_timeouts(current_time_ms)

        # 11. _purge_expired()
        self._purge_expired()

        # 12. _dispatch_tx() (fila SIS)
        if self._flow_on:
            self._dispatch_tx()

        # 13. _manage_soft_link()
        self._manage_soft_link(current_time_ms)

    # ===================================================================
    # DTS internals
    # ===================================================================

    def _on_non_arq_delivery(self, delivery: NonArqDelivery) -> None:
        self.received_deliveries.append(delivery)
        if delivery.error or not delivery.complete:
            return
        hint = payload_hint(delivery.payload)
        try:
            cpdu = decode_cpdu(delivery.payload)
        except ValueError:
            flow_rx(
                "DTS",
                f"node={self.local_node_address} NonARQ reassembly completo -> SIS | "
                f"{hint} source={delivery.source} payload_len={len(delivery.payload)}",
            )
            return
        tipo = cpdu.cpdu_type.name if hasattr(cpdu.cpdu_type, "name") else str(cpdu.cpdu_type)
        flow_rx(
            "DTS",
            f"node={self.local_node_address} NonARQ reassembly completo -> CAS process_cpdu | "
            f"{hint} {tipo} source={delivery.source} payload_len={len(cpdu.payload)}",
        )
        self.cas.process_cpdu(cpdu, delivery.source, self._current_time_ms)
        if cpdu.cpdu_type is not CPDUType.DATA:
            self.received_cpdus.append(cpdu)

    def _on_arq_delivery(self, payload: bytes) -> None:
        if self.cas.remote_node_address is None:
            return
        # B.3.1.1/B.3: payload contém DATA C_PDU (0x00 + S_PDU), decodificar
        try:
            cpdu = decode_cpdu(payload)
        except ValueError:
            return
        if cpdu.cpdu_type != CPDUType.DATA:
            return
        flow_rx("DTS", f"node={self.local_node_address} ARQ DATA len={len(payload)} remote={self.cas.remote_node_address}")
        self.received_cpdus.append(cpdu)

    def _on_expedited_delivery(self, payload: bytes) -> None:
        if self.cas.remote_node_address is None:
            return
        # B.3.1.1/B.3: payload contém DATA C_PDU (0x00 + S_PDU), decodificar
        try:
            cpdu = decode_cpdu(payload)
        except ValueError:
            return
        if cpdu.cpdu_type != CPDUType.DATA:
            return
        flow_rx("DTS", f"node={self.local_node_address} EXPEDITED DATA len={len(payload)} remote={self.cas.remote_node_address}")
        self.received_cpdus.append(cpdu)

    def _dispatch_rx_frame(self, frame: bytes) -> None:
        try:
            dpdu = decode_dpdu(frame)
        except ValueError:
            flow_rx("DTS", f"node={self.local_node_address} frame descartado (decode falhou) len={len(frame)}")
            return
        tipo = dpdu.dpdu_type.name if hasattr(dpdu.dpdu_type, "name") else str(dpdu.dpdu_type)
        # C.3.12 §10: nunca responder a um WARNING com outro WARNING. O Tipo 15
        # não é "esperado" em qualquer estado, mas o tratamento dele é direto
        # mais abaixo — pular a verificação evita loops de WARNING ⇄ WARNING.
        warn_reason = (
            None
            if dpdu.dpdu_type is DPDUType.WARNING
            else self._dts.warning_reason(dpdu.dpdu_type)
        )
        if warn_reason is not None:
            flow_rx(
                "DTS",
                f"node={self.local_node_address} {tipo} não permitido no estado "
                f"{self._dts.state.value} -> WARNING(reason={warn_reason})",
            )
            from src.dpdu_frame import dpdu_set_address
            warn_addr = dpdu_set_address(
                destination=dpdu.address.source,
                source=self.local_node_address,
            )
            warn_dpdu = build_warning(0, 0, warn_addr, dpdu.dpdu_type.value, warn_reason)
            self.modem.modem_tx_dpdu(encode_dpdu(warn_dpdu))
            self.cas.on_warning_transmitted(dpdu.address.source, warn_reason)
            return
        if dpdu.dpdu_type is DPDUType.WARNING:
            warn_reason = dpdu.warning.reason if dpdu.warning else 0
            flow_rx(
                "DTS",
                f"node={self.local_node_address} WARNING D_PDU recebido de {dpdu.address.source} reason={warn_reason}",
            )
            self.cas.on_warning_received(dpdu.address.source, warn_reason)
            return
        if dpdu.dpdu_type in (DPDUType.NON_ARQ, DPDUType.EXPEDITED_NON_ARQ):
            flow_rx(
                "DTS",
                f"node={self.local_node_address} {tipo} dest={dpdu.address.destination} -> fila NonARQ (demux)",
            )
            self._demux.push_frame(frame)
            return
        if dpdu.dpdu_type is DPDUType.RESETWIN_RESYNC and dpdu.reset is not None:
            flow_rx(
                "DTS",
                f"node={self.local_node_address} RESETWIN_RESYNC full_reset_cmd={dpdu.reset.full_reset_cmd} "
                f"reset_ack={dpdu.reset.reset_ack} -> ARQ process_rx_reset",
            )
            enc = self.arq.process_rx_reset(dpdu)
            if enc:
                self._arq_pending_tx.append(enc)
                if dpdu.reset.full_reset_cmd and not dpdu.reset.reset_ack:
                    self._dts.on_link_made()
                    self._dts.enter_data()
                flow_tx(
                    "DTS",
                    f"node={self.local_node_address} RESET_ACK enfileirado para TX prioritário",
                )
            return
        if dpdu.dpdu_type in (
            DPDUType.DATA_ONLY,
            DPDUType.ACK_ONLY,
            DPDUType.DATA_ACK,
            DPDUType.EXPEDITED_DATA_ONLY,
            DPDUType.EXPEDITED_ACK_ONLY,
        ):
            if dpdu.address.destination == self.local_node_address:
                if self.cas.state != CasLinkState.MADE:
                    from src.dts_state import WARNING_REASON_CONN_DPDU_BUT_UNCONNECTED
                    flow_rx(
                        "DTS",
                        f"node={self.local_node_address} {tipo} sem enlace ativo -> WARNING(reason={WARNING_REASON_CONN_DPDU_BUT_UNCONNECTED})",
                    )
                    from src.dpdu_frame import dpdu_set_address
                    warn_addr = dpdu_set_address(
                        destination=dpdu.address.source,
                        source=self.local_node_address,
                    )
                    warn_dpdu = build_warning(0, 0, warn_addr, dpdu.dpdu_type.value, WARNING_REASON_CONN_DPDU_BUT_UNCONNECTED)
                    self.modem.modem_tx_dpdu(encode_dpdu(warn_dpdu))
                    self.cas.on_warning_transmitted(dpdu.address.source, WARNING_REASON_CONN_DPDU_BUT_UNCONNECTED)
                    return
                if dpdu.dpdu_type in (DPDUType.EXPEDITED_DATA_ONLY, DPDUType.EXPEDITED_ACK_ONLY):
                    flow_rx(
                        "DTS",
                        f"node={self.local_node_address} {tipo} src={dpdu.address.source} -> ExpeditedARQ",
                    )
                    self._dts.enter_expedited()
                    self.expedited_arq.process_rx_dpdu(dpdu)
                    if not self.expedited_arq.has_pending_tx():
                        self._dts.exit_expedited()
                else:
                    flow_rx(
                        "DTS",
                        f"node={self.local_node_address} {tipo} src={dpdu.address.source} -> ARQ process_rx_dpdu",
                    )
                    self.arq.process_rx_dpdu(dpdu)
            else:
                flow_rx(
                    "DTS",
                    f"node={self.local_node_address} {tipo} dest={dpdu.address.destination} != local, ignorado",
                )
            return
        if dpdu.dpdu_type is DPDUType.MANAGEMENT:
            flow_rx(
                "DTS",
                f"node={self.local_node_address} MANAGEMENT -> enter_management / process",
            )
            self._dts.enter_management()
            # Ensure ManagementEngine exists for this peer
            if self._mgmt_engine is None and dpdu.address.source != 0:
                self._mgmt_engine = ManagementEngine(
                    self.local_node_address,
                    dpdu.address.source,
                    data_rate_bps=self.modem.config.data_rate_bps,
                )
            if self._mgmt_engine is not None:
                responses = self._mgmt_engine.process_rx(dpdu)
                for resp in responses:
                    self.modem.modem_tx_dpdu(resp)
            self._dts.exit_management()
            return
        flow_rx("DTS", f"node={self.local_node_address} {tipo} sem despacho explicito (ignorado)")

    def _dts_cas_transitions(self, current_time_ms: int) -> None:
        """Transições CAS internas: FULL_RESET, DTS on_link_made/broken."""
        if self._prev_cas_state_dts is not None and self.cas.state == CasLinkState.MADE:
            if self._prev_cas_state_dts != CasLinkState.MADE:
                self._dts.on_link_made()
                self._dts.enter_data()
                self.arq.reset_full()
                if self._prev_cas_state_dts == CasLinkState.CALLING:
                    self._we_initiated_link = True
                    enc = self.arq.start_full_reset(current_time_ms)
                    if enc:
                        flow_tx(
                            "DTS",
                            f"node={self.local_node_address} MADE (caller) -> FULL_RESET ARQ enfileirado | "
                            f"{dpdu_wire_hint(enc)}",
                        )
                        self._arq_pending_tx.append(enc)
        if (
            self._prev_cas_state_dts == CasLinkState.MADE
            and self.cas.state != CasLinkState.MADE
        ):
            self._dts.on_link_broken()
        self._prev_cas_state_dts = self.cas.state

        if self.cas.state == CasLinkState.MADE and self.cas.remote_node_address is not None:
            self.arq.remote_node_address = self.cas.remote_node_address
            self.expedited_arq.remote_node_address = self.cas.remote_node_address

    def _dts_transmit(self, current_time_ms: int) -> None:
        """TX prioritário: ARQ pending, non_arq, expedited, regular ARQ."""
        if self._arq_pending_tx:
            enc = self._arq_pending_tx.pop(0)
            flow_tx(
                "DTS",
                f"node={self.local_node_address} TX prioritário ARQ pending -> modem | {dpdu_wire_hint(enc)}",
            )
            self.modem.modem_tx_dpdu(enc)
            return

        non_arq_sent = self.non_arq.process_tx(current_time_ms)
        if non_arq_sent:
            d0 = non_arq_sent[0]
            flow_tx(
                "DTS",
                f"node={self.local_node_address} NonARQ burst {len(non_arq_sent)} D_PDU(s) "
                f"type={d0.dpdu_type.name} dest={d0.address.destination} eot={d0.eot}",
            )
            return

        # Expedited ARQ TX (prioridade sobre ARQ regular)
        if self.expedited_arq.has_pending_tx() and self._dts.state.is_data:
            self._dts.enter_expedited()
        exp_burst = self.expedited_arq.process_tx(current_time_ms)
        if exp_burst:
            flow_tx("DTS", f"node={self.local_node_address} ExpARQ burst {len(exp_burst)} frame(s) | {dpdu_wire_hint(exp_burst[0])}")
            self.modem.modem_tx_burst(exp_burst)
            return
        if self._dts.state.is_expedited and not self.expedited_arq.has_pending_tx():
            self._dts.exit_expedited()

        arq_burst = self.arq.process_tx(current_time_ms)
        if arq_burst:
            flow_tx("DTS", f"node={self.local_node_address} ARQ burst {len(arq_burst)} frame(s) | {dpdu_wire_hint(arq_burst[0])}")
            self.modem.modem_tx_burst(arq_burst)

    # ===================================================================
    # SIS internals (ex-SIS class)
    # ===================================================================

    def _process_rx(self) -> None:
        """Processa CPDUs recebidas (ARQ DATA) — contêm S_PDU codificado."""
        cpdus = self.received_cpdus
        while self._rx_cursor < len(cpdus):
            cpdu = cpdus[self._rx_cursor]
            self._rx_cursor += 1
            if cpdu.cpdu_type != CPDUType.DATA:
                continue
            if not cpdu.payload:
                continue
            try:
                t = spdu_type(cpdu.payload)
            except ValueError:
                continue
            if t in (SPDU_TYPE_HARD_LINK_ESTABLISH_REQUEST, SPDU_TYPE_HARD_LINK_ESTABLISH_CONFIRM,
                     SPDU_TYPE_HARD_LINK_ESTABLISH_REJECTED, SPDU_TYPE_HARD_LINK_TERMINATE,
                     SPDU_TYPE_HARD_LINK_TERMINATE_CONFIRM):
                self._process_spdu_control(cpdu.payload, self._get_remote_addr())
            elif t in (SPDU_TYPE_DATA_DELIVERY_CONFIRM, SPDU_TYPE_DATA_DELIVERY_FAIL):
                self._process_delivery_confirm_or_fail(cpdu.payload, self._get_remote_addr(), t)
            elif t == SPDU_TYPE_DATA:
                try:
                    spdu = decode_spdu(cpdu.payload)
                except ValueError:
                    continue
                self._deliver_to_sap(spdu, src_addr=self._get_remote_addr(), via_arq=True)

    def _process_non_arq_deliveries(self, deliveries) -> None:
        """Processa entregas non-ARQ — payload contém C_PDU encapsulando S_PDU."""
        for delivery in deliveries:
            if delivery.error or not delivery.complete:
                continue
            if not delivery.payload:
                continue
            try:
                cpdu = decode_cpdu(delivery.payload)
            except ValueError:
                continue
            if cpdu.cpdu_type is not CPDUType.DATA:
                continue
            spdu_payload = cpdu.payload
            if not spdu_payload:
                continue
            try:
                t = spdu_type(spdu_payload)
            except ValueError:
                continue
            if t in (SPDU_TYPE_HARD_LINK_ESTABLISH_REQUEST, SPDU_TYPE_HARD_LINK_ESTABLISH_CONFIRM,
                     SPDU_TYPE_HARD_LINK_ESTABLISH_REJECTED, SPDU_TYPE_HARD_LINK_TERMINATE,
                     SPDU_TYPE_HARD_LINK_TERMINATE_CONFIRM):
                self._process_spdu_control(spdu_payload, delivery.source)
            elif t in (SPDU_TYPE_DATA_DELIVERY_CONFIRM, SPDU_TYPE_DATA_DELIVERY_FAIL):
                self._process_delivery_confirm_or_fail(spdu_payload, delivery.source, t)
            elif t == SPDU_TYPE_DATA:
                try:
                    spdu = decode_spdu(spdu_payload)
                except ValueError:
                    continue
                self._deliver_to_sap(spdu, src_addr=delivery.source, via_arq=False)

    def _process_spdu_control(self, payload: bytes, src_addr: int) -> None:
        """Processa S_PDUs de controle (tipos 3-7)."""
        t = spdu_type(payload)
        if t == SPDU_TYPE_HARD_LINK_ESTABLISH_REQUEST:
            try:
                link_type, link_pri, req_sap, remote_sap = decode_spdu_hard_link_request(payload)
            except ValueError:
                return
            self._handle_hard_link_request(
                src_addr=src_addr,
                req_sap=req_sap,
                remote_sap=remote_sap,
                link_pri=link_pri,
                link_type=link_type,
            )
        elif t == SPDU_TYPE_HARD_LINK_ESTABLISH_CONFIRM:
            if self._link_session.awaiting_hard_link_response:
                self._link_session.awaiting_hard_link_response = False
                self._link_session.state = SisLinkSessionState.ACTIVE
                if self._callbacks.hard_link_established is not None:
                    self._callbacks.hard_link_established(
                        self._link_session.remote_addr,
                        self._link_session.remote_sap,
                    )
        elif t == SPDU_TYPE_HARD_LINK_ESTABLISH_REJECTED:
            if self._link_session.awaiting_hard_link_response:
                self._link_session.awaiting_hard_link_response = False
                self._link_session.state = SisLinkSessionState.IDLE
                self._link_session.link_type = LinkType.SOFT
                reason = decode_spdu_hard_link_rejected(payload)
                if self._callbacks.hard_link_rejected is not None:
                    self._callbacks.hard_link_rejected(
                        self._link_session.remote_addr,
                        self._link_session.remote_sap,
                        reason,
                    )
        elif t == SPDU_TYPE_HARD_LINK_TERMINATE:
            self._last_terminate_reason = decode_spdu_hard_link_terminate(payload)
            # Captura per-sap notifications antes do reset zerar o tipo/owner.
            self._notify_hard_link_terminated_per_sap(
                src_addr, initiator_received_confirm=False,
            )
            self._reset_link_session_to_idle()
            self._send_control_expedited(src_addr, encode_spdu_hard_link_terminate_confirm())
            if self._callbacks.hard_link_terminated is not None:
                self._callbacks.hard_link_terminated(
                    src_addr,
                    initiator_received_confirm=False,
                )
        elif t == SPDU_TYPE_HARD_LINK_TERMINATE_CONFIRM:
            if self._link_session.awaiting_terminate_confirm:
                self._link_session.awaiting_terminate_confirm = False
                remote = self._link_session.remote_addr
                self._notify_hard_link_terminated_per_sap(
                    remote, initiator_received_confirm=True,
                )
                self._reset_link_session_to_idle()
                if self._callbacks.hard_link_terminated is not None:
                    self._callbacks.hard_link_terminated(
                        remote,
                        initiator_received_confirm=True,
                    )

    # -------------------------------------------------------------------
    # Hard Link helpers (A.3.2.2.1 / A.3.2.2.2)
    # -------------------------------------------------------------------

    def _reset_link_session_to_idle(self) -> None:
        """Devolve a sessão de enlace ao estado IDLE/SOFT (sem afetar CAS)."""
        self._link_session.state = SisLinkSessionState.IDLE
        self._link_session.link_type = LinkType.SOFT
        self._link_session.hard_link_owner = -1
        self._link_session.hard_link_owner_rank = 0
        self._link_session.link_priority = 0
        self._link_session.sis_hard_link_type = 0
        self._link_session.pending_indication = None
        self._link_session.pending_indications.clear()
        self._link_session.awaiting_hard_link_response = False
        self._link_session.awaiting_terminate_confirm = False
        self._link_session.is_calling = False
        self._link_session.local_initiator_sap = -1

    def _notify_hard_link_terminated_per_sap(
        self, remote_addr: int, initiator_received_confirm: bool,
    ) -> None:
        """A.3.2.2.3 §3: dispara callback granular para cada SAP afetado.

        Type 0: todos os SAPs locais bound.
        Type 1/2: apenas o ``local_initiator_sap`` (se conhecido).
        """
        cb = self._callbacks.hard_link_terminated_per_sap
        if cb is None:
            return
        link_type = self._link_session.sis_hard_link_type
        local_initiator = self._link_session.local_initiator_sap
        if link_type == 0:
            sap_ids = list(self._saps)
        elif local_initiator >= 0:
            sap_ids = [local_initiator]
        else:
            sap_ids = []
        for sap_id in sap_ids:
            cb(sap_id, remote_addr, initiator_received_confirm)

    def _promote_next_pending_indication(self) -> None:
        """Promove a próxima indicação Type 2 da fila para ``pending_indication``
        e dispara ``hard_link_indication`` para o cliente."""
        if self._link_session.pending_indication is not None:
            return
        if not self._link_session.pending_indications:
            return
        nxt = self._link_session.pending_indications.pop(0)
        self._link_session.pending_indication = nxt
        if self._callbacks.hard_link_indication is not None:
            self._callbacks.hard_link_indication(
                nxt.src_addr, nxt.remote_sap, nxt.link_priority, nxt.link_type,
            )

    def _evaluate_hard_link_precedence(
        self,
        *,
        requester_rank: int,
        requester_priority: int,
        requester_link_type: int,
    ) -> bool:
        """Aplica regras A.3.2.2.1 §1-§4. Retorna True se o novo pedido vence.

        Regras:
          1. Maior Rank vence.
          2. Mesmo Rank: maior Link Priority vence.
          3. Mesmo Rank + Priority: ``link_type`` mais alto (0 < 1 < 2) vence.
          4. Empate total: o existente prevalece (first-come).
        """
        existing_rank = self._link_session.hard_link_owner_rank
        if requester_rank > existing_rank:
            return True
        if requester_rank < existing_rank:
            return False
        if requester_priority > self._link_session.link_priority:
            return True
        if requester_priority < self._link_session.link_priority:
            return False
        return requester_link_type > self._link_session.sis_hard_link_type

    def _terminate_existing_hard_link(self, reason: int) -> None:
        """A.3.2.2.2 §8: encerra o Hard Link prévio antes de aceitar novo.

        Envia TERMINATE ao peer corrente, dispara callback ``hard_link_terminated``
        ao owner local com o reason indicado e zera o estado da sessão.
        """
        prev_remote_addr = self._link_session.remote_addr
        prev_owner = self._link_session.hard_link_owner
        if prev_remote_addr:
            self._send_control_expedited(
                prev_remote_addr,
                encode_spdu_hard_link_terminate(reason),
            )
        self._reset_link_session_to_idle()
        if self._callbacks.hard_link_terminated is not None:
            self._callbacks.hard_link_terminated(
                prev_remote_addr,
                initiator_received_confirm=False,
            )
        # Suprime variáveis não usadas só para clareza.
        del prev_owner

    def _handle_hard_link_request(
        self,
        *,
        src_addr: int,
        req_sap: int,
        remote_sap: int,
        link_pri: int,
        link_type: int,
    ) -> None:
        """Trata REQUEST recebido conforme A.3.2.2.1 / A.3.2.2.2.

        Sempre envia RESPONSE explícita: CONFIRM (tipo 4) ou REJECTED (tipo 5)
        com o reason adequado. Type 2 dispara S_HARD_LINK_INDICATION para o
        SAP destino quando registrado.
        """
        requester_rank = self._resolve_remote_rank(src_addr, req_sap)
        existing_active = (
            self._link_session.link_type == LinkType.HARD
            and self._link_session.state == SisLinkSessionState.ACTIVE
        )

        if existing_active:
            new_wins = self._evaluate_hard_link_precedence(
                requester_rank=requester_rank,
                requester_priority=link_pri,
                requester_link_type=link_type,
            )
            if not new_wins:
                # A.3.2.2.1 §6: se já existe Hard Link Type 0 prevalece.
                if self._link_session.sis_hard_link_type == 0:
                    reason = int(SisHardLinkRejectReason.REQUESTED_TYPE0_EXISTS)
                else:
                    reason = int(SisHardLinkRejectReason.HIGHER_PRIORITY_LINK_EXISTING)
                self._send_control_expedited(
                    src_addr,
                    encode_spdu_hard_link_rejected(reason),
                )
                return
            # A.3.2.2.2 §8: novo pedido vence — encerra o existente antes.
            self._terminate_existing_hard_link(
                int(SisHardLinkTerminateReason.HIGHER_PRIORITY_LINK_REQUESTED),
            )

        if link_type == 2 and self._callbacks.hard_link_indication is not None:
            new_indication = _PendingHardLinkIndication(
                src_addr=src_addr,
                remote_sap=remote_sap,
                link_priority=link_pri,
                link_type=link_type,
                requesting_sap=req_sap,
            )
            # A.2.1.13: indicações Type 2 podem chegar simultaneamente. A
            # primeira fica em ``pending_indication`` aguardando accept/reject;
            # subsequentes vão para ``pending_indications`` (FIFO) até serem
            # promovidas após resposta.
            if self._link_session.pending_indication is None:
                self._link_session.pending_indication = new_indication
            else:
                self._link_session.pending_indications.append(new_indication)
            self._callbacks.hard_link_indication(
                src_addr, remote_sap, link_pri, link_type,
            )
            return

        if link_type == 2 and remote_sap not in self._saps:
            self._send_control_expedited(
                src_addr,
                encode_spdu_hard_link_rejected(
                    int(SisHardLinkRejectReason.DEST_SAP_NOT_BOUND),
                ),
            )
            return

        # Aceita.
        self._link_session.state = SisLinkSessionState.ACTIVE
        self._link_session.link_type = LinkType.HARD
        self._link_session.remote_addr = src_addr
        self._link_session.remote_sap = remote_sap
        self._link_session.hard_link_owner = remote_sap if link_type == 2 else -1
        self._link_session.hard_link_owner_rank = requester_rank
        self._link_session.link_priority = link_pri & 0x03
        self._link_session.sis_hard_link_type = link_type & 0x03
        # Lado solicitado (called): nenhum SAP local iniciou. Apenas em Type 2
        # com hard_link_accept() o cliente local opt-in vira "initiator local".
        self._link_session.local_initiator_sap = -1
        self._send_control_expedited(src_addr, encode_spdu_hard_link_confirm())
        if self._callbacks.hard_link_established is not None:
            self._callbacks.hard_link_established(src_addr, remote_sap)

    def _wrap_in_cpdu(self, spdu_payload: bytes) -> bytes:
        """Encapsula S_PDU em C_PDU DATA (tipo 0) para transporte non-ARQ."""
        return encode_cpdu(CPDU(cpdu_type=CPDUType.DATA, payload=spdu_payload))

    def _cas_arq_data_handler(self, dest_addr: int, encoded_cpdu: bytes) -> None:
        """Despacha DATA C_PDU via ARQ a pedido do CAS (B.3.1 §5-7).

        Invocado por ``CASEngine.send_data(use_arq=True)``. Sincroniza o
        endereço remoto da engine ARQ e envia o C_PDU já encapsulado para a
        fila ARQ regular.
        """
        if self.arq.remote_node_address != dest_addr:
            self.arq.remote_node_address = dest_addr
        self._dts.enter_data()
        self.arq.submit_cpdu(encoded_cpdu)

    def _send_control_expedited(self, dest_addr: int, payload: bytes) -> None:
        """Envia S_PDU de controle Hard Link.

        A.3.2.2.2 §11 exige Expedited ARQ (D_PDU tipo 4) com confirmação
        stop-and-wait quando o Physical Link já está MADE. Caso o CAS ainda
        não esteja MADE (ex.: REJECT enviado pré-handshake), recorre a
        Expedited Non-ARQ (D_PDU tipo 8) como fallback.
        """
        cpdu_bytes = self._wrap_in_cpdu(payload)
        if (
            self.cas.state == CasLinkState.MADE
            and self.cas.remote_node_address is not None
            and self.cas.remote_node_address == dest_addr
        ):
            self.expedited_arq.remote_node_address = dest_addr
            self.expedited_arq.submit_cpdu(cpdu_bytes)
            self._dts.enter_expedited()
            return
        self.non_arq.queue_cpdu(DPDUType.EXPEDITED_NON_ARQ, dest_addr, cpdu_bytes)

    def _process_delivery_confirm_or_fail(self, payload: bytes, src_addr: int, spdu_t: int) -> None:
        """Processa S_PDU tipo 1 (CONFIRM) ou 2 (FAIL)."""
        if spdu_t == SPDU_TYPE_DATA_DELIVERY_CONFIRM:
            try:
                src_sap, dest_sap, updu_partial = decode_spdu_data_delivery_confirm(payload)
            except ValueError:
                return
            if self._callbacks.request_confirm is not None:
                self._callbacks.request_confirm(src_sap, src_addr, dest_sap, updu_partial)
        elif spdu_t == SPDU_TYPE_DATA_DELIVERY_FAIL:
            try:
                src_sap, dest_sap, reason, updu_partial = decode_spdu_data_delivery_fail(payload)
            except ValueError:
                return
            if self._callbacks.request_rejected is not None:
                self._callbacks.request_rejected(
                    src_sap,
                    SisRejectReason.DEST_SAP_NOT_BOUND if reason == 1 else SisRejectReason.DEST_UNKNOWN,
                )

    def _deliver_to_sap(self, spdu: SPDU, src_addr: int, via_arq: bool = True) -> None:
        """Entrega U_PDU ao SAP destino via callback."""
        dest_sap = spdu.dest_sap
        success = dest_sap in self._saps
        if success and self._callbacks.unidata_indication is not None:
            indication = SisUnidataIndication(
                dest_sap=dest_sap,
                src_addr=src_addr,
                src_sap=spdu.src_sap,
                priority=spdu.priority,
                updu=spdu.updu,
            )
            self._callbacks.unidata_indication(indication)
        if spdu.client_delivery_confirm_required:
            updu_partial = spdu.updu[:32] if spdu.updu else b""
            # A.3.1.2 §7 / A.3.1.3: CONFIRM/FAIL replicam todos os campos
            # S_PCI do DATA original (PRIORITY, VALID_TTD, JULIAN, GMT, ...).
            if success:
                payload = encode_spdu_data_delivery_confirm_from(spdu, updu_partial)
            else:
                payload = encode_spdu_data_delivery_fail_from(
                    spdu,
                    int(SisDataDeliveryFailReason.DEST_SAP_NOT_BOUND),
                    updu_partial,
                )
            if via_arq and self.cas.state == CasLinkState.MADE and self.cas.remote_node_address is not None:
                self.send_data(payload)
            else:
                self.non_arq.queue_cpdu(DPDUType.EXPEDITED_NON_ARQ, src_addr, self._wrap_in_cpdu(payload))

    def _get_remote_addr(self) -> int:
        if self.cas.remote_node_address is not None:
            return self.cas.remote_node_address
        return 0

    # -------------------------------------------------------------------
    # Monitoramento CAS (SIS-level)
    # -------------------------------------------------------------------

    def _monitor_cas_transitions(self) -> None:
        """Detecta transições de estado CAS e atualiza sessão de enlace."""
        cas_state = self.cas.state
        prev = self._prev_cas_state_sis
        self._prev_cas_state_sis = cas_state

        if cas_state == CasLinkState.MADE and prev != CasLinkState.MADE:
            self._link_session.last_activity_ms = self._current_time_ms
            if self._link_session.link_type == LinkType.HARD and self._we_initiated_link:
                self._deferred_hard_link_request = True
            else:
                self._link_session.state = SisLinkSessionState.ACTIVE
                if self._link_session.link_type == LinkType.HARD and not self._we_initiated_link:
                    if self._callbacks.hard_link_established is not None:
                        self._callbacks.hard_link_established(
                            self._link_session.remote_addr,
                            self._link_session.remote_sap,
                        )

        if prev in (CasLinkState.MADE, CasLinkState.BREAKING) and cas_state in (CasLinkState.IDLE, CasLinkState.FAILED):
            was_hard = self._link_session.link_type == LinkType.HARD
            self._link_session.state = SisLinkSessionState.IDLE
            self._link_session.link_type = LinkType.SOFT
            self._link_session.hard_link_owner = -1
            self._link_session.pending_indication = None
            self._we_initiated_link = False
            self._deferred_hard_link_request = False
            if cas_state == CasLinkState.FAILED:
                self._reject_all_pending(SisRejectReason.LINK_FAILED)
            if was_hard and self._callbacks.hard_link_terminated is not None:
                self._callbacks.hard_link_terminated(
                    self._link_session.remote_addr,
                    initiator_received_confirm=False,
                )

        if prev in (CasLinkState.CALLING,) and cas_state in (CasLinkState.IDLE, CasLinkState.FAILED):
            if self._link_session.state == SisLinkSessionState.ESTABLISHING:
                self._link_session.state = SisLinkSessionState.IDLE
                self._reject_all_pending(SisRejectReason.LINK_FAILED)

    # -------------------------------------------------------------------
    # TTL / Purge
    # -------------------------------------------------------------------

    def _purge_expired(self) -> None:
        now = time.time()
        expired = [e for e in self._tx_queue if now >= e.spdu.ttd]
        for entry in expired:
            self._tx_queue.remove(entry)
            self._fire_rejected(entry.src_sap, SisRejectReason.TTL_EXPIRED)

    # -------------------------------------------------------------------
    # Despacho TX (SIS)
    # -------------------------------------------------------------------

    def _dispatch_tx(self) -> None:
        if not self._tx_queue:
            return

        arq_entries = [e for e in self._tx_queue if e.delivery_mode.arq_mode and not e.delivery_mode.expedited]
        exp_arq_entries = [e for e in self._tx_queue if e.delivery_mode.arq_mode and e.delivery_mode.expedited]
        non_arq_entries = [e for e in self._tx_queue if not e.delivery_mode.arq_mode]

        for entry in non_arq_entries:
            self._tx_queue.remove(entry)
            encoded = encode_spdu(entry.spdu)
            dpdu_type = DPDUType.EXPEDITED_NON_ARQ if entry.delivery_mode.expedited else DPDUType.NON_ARQ
            self.non_arq.queue_cpdu(dpdu_type, entry.dest_addr, self._wrap_in_cpdu(encoded))
            self._link_session.last_activity_ms = self._current_time_ms

        if exp_arq_entries and self._link_session.state == SisLinkSessionState.ACTIVE:
            for entry in exp_arq_entries:
                self._tx_queue.remove(entry)
                encoded = encode_spdu(entry.spdu)
                # B.3.1.1/B.3: encapsular S_PDU em DATA C_PDU
                self.expedited_arq.submit_cpdu(self._wrap_in_cpdu(encoded))
                self._link_session.last_activity_ms = self._current_time_ms

        if not arq_entries:
            return
        if self._link_session.state != SisLinkSessionState.ACTIVE:
            return
        if self.cas.state != CasLinkState.MADE:
            return

        entry = self._pick_next_tx(arq_entries)
        if entry is None:
            return
        self._tx_queue.remove(entry)
        encoded = encode_spdu(entry.spdu)
        self.send_data(encoded, deliver_in_order=entry.spdu.deliver_in_order)
        self._link_session.last_activity_ms = self._current_time_ms

    def _pick_next_tx(self, entries: list[_TxEntry] | None = None) -> _TxEntry | None:
        pool = entries if entries else self._tx_queue
        if not pool:
            return None
        return max(
            pool,
            key=lambda e: (
                e.delivery_mode.expedited,
                e.spdu.priority,
                self._saps.get(e.src_sap, _SapContext(sap_id=e.src_sap)).rank,
                -e.enqueued_at_ms,
            ),
        )

    # -------------------------------------------------------------------
    # Soft/Hard link management
    # -------------------------------------------------------------------

    def _manage_soft_link(self, current_time_ms: int) -> None:
        arq_pending = any(
            e.delivery_mode.arq_mode and not e.delivery_mode.expedited
            for e in self._tx_queue
        )

        if arq_pending and self._link_session.state == SisLinkSessionState.IDLE:
            if self.cas.state not in (CasLinkState.IDLE, CasLinkState.FAILED):
                return
            first_arq = next(
                e for e in self._tx_queue
                if e.delivery_mode.arq_mode and not e.delivery_mode.expedited
            )
            self._link_session.state = SisLinkSessionState.ESTABLISHING
            self._link_session.link_type = LinkType.SOFT
            self._link_session.remote_addr = first_arq.dest_addr
            self._we_initiated_link = True
            self.make_link(first_arq.dest_addr, link_type=PhysicalLinkType.NONEXCLUSIVE)
            return

        if (
            self._link_session.state == SisLinkSessionState.ACTIVE
            and self._link_session.link_type == LinkType.SOFT
            and self._we_initiated_link
            and not arq_pending
            and (current_time_ms - self._link_session.last_activity_ms) > self._soft_link_idle_timeout_ms
        ):
            self._link_session.state = SisLinkSessionState.TERMINATING
            self.break_link()

    # -------------------------------------------------------------------
    # Hard link timeouts (A.3.2.2.2§12, A.3.2.2.3§4)
    # -------------------------------------------------------------------

    def _check_hard_link_timeouts(self, current_time_ms: int) -> None:
        """Check and handle hard link establish/terminate timeouts."""
        # Establish timeout
        if (
            self._link_session.awaiting_hard_link_response
            and self._link_session.hard_link_response_timeout_ms > 0
            and current_time_ms >= self._link_session.hard_link_response_timeout_ms
        ):
            self._link_session.awaiting_hard_link_response = False
            self._link_session.hard_link_response_timeout_ms = 0
            self._link_session.state = SisLinkSessionState.IDLE
            self._link_session.link_type = LinkType.SOFT
            if self._callbacks.hard_link_rejected is not None:
                self._callbacks.hard_link_rejected(
                    self._link_session.remote_addr,
                    self._link_session.remote_sap,
                    int(SisHardLinkRejectReason.REMOTE_NODE_NOT_RESPONDING),
                )

        # Terminate timeout
        if (
            self._link_session.awaiting_terminate_confirm
            and self._link_session.terminate_confirm_timeout_ms > 0
            and current_time_ms >= self._link_session.terminate_confirm_timeout_ms
        ):
            self._link_session.awaiting_terminate_confirm = False
            self._link_session.terminate_confirm_timeout_ms = 0
            self._link_session.state = SisLinkSessionState.IDLE
            self._link_session.link_type = LinkType.SOFT
            self._link_session.hard_link_owner = -1
            if self._callbacks.hard_link_terminated is not None:
                self._callbacks.hard_link_terminated(
                    self._link_session.remote_addr,
                    initiator_received_confirm=False,
                )

    # -------------------------------------------------------------------
    # Management message rank validation (A.2.1.15§3)
    # -------------------------------------------------------------------

    def validate_management_msg_rank(self, sap_id: int) -> bool:
        """Returns True if the SAP has rank 15 (required for management msgs)."""
        ctx = self._saps.get(sap_id)
        if ctx is None:
            return False
        return ctx.rank == self.MANAGEMENT_MSG_REQUIRED_RANK

    # -------------------------------------------------------------------
    # Expedited request tracking (A.2.1.10§3-4)
    # -------------------------------------------------------------------

    def track_expedited_request(self, sap_id: int) -> bool:
        """Track expedited request count. Returns False if limit exceeded.

        A.2.1.10§3-4: ao exceder o limite, o nó desliga o SAP via
        S_UNBIND_INDICATION com REASON=4 ("Too many expedited-data request
        primitives") e retorna False.
        """
        if self._max_expedited_per_client <= 0:
            return True  # tracking disabled
        count = self._expedited_counts.get(sap_id, 0) + 1
        self._expedited_counts[sap_id] = count
        if count > self._max_expedited_per_client:
            self.unbind(sap_id)
            self._expedited_counts.pop(sap_id, None)
            if self._callbacks.unbind_indication is not None:
                self._callbacks.unbind_indication(
                    sap_id,
                    SisUnbindIndicationReason.TOO_MANY_EXPEDITED_REQUESTS,
                )
            return False
        return True

    # -------------------------------------------------------------------
    # Helpers
    # -------------------------------------------------------------------

    def _fire_rejected(self, sap_id: int, reason: SisRejectReason) -> None:
        if self._callbacks.request_rejected is not None:
            self._callbacks.request_rejected(sap_id, reason)

    def _reject_all_pending(self, reason: SisRejectReason) -> None:
        arq_entries = [
            e for e in self._tx_queue
            if e.delivery_mode.arq_mode and not e.delivery_mode.expedited
        ]
        for entry in arq_entries:
            self._tx_queue.remove(entry)
            self._fire_rejected(entry.src_sap, reason)
