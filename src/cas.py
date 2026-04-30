"""Channel Access Sublayer (STANAG 5066 Annex B, Edition 3) — Type 1 protocol.

Formato C_PDU conforme Annex B Figures B-1 a B-6:
- Byte 0: [TYPE (4 bits, high nibble)][FIELD (4 bits, low nibble)]
- Tipo 0 (DATA): byte 0 = 0x00, seguido de S_PDU
- Tipo 1 (LINK_REQUEST): low nibble bit 0 = LINK (0=Nonexclusive, 1=Exclusive)
- Tipo 2 (LINK_ACCEPTED): low nibble = 0
- Tipo 3 (LINK_REJECTED): low nibble = REASON (4 bits)
- Tipo 4 (LINK_BREAK): low nibble = REASON (4 bits)
- Tipo 5 (LINK_BREAK_CONFIRM): low nibble = 0
- Endereço/Call ID não pertencem ao C_PDU; DTS faz o envelopamento.

Suporta múltiplos Physical Links simultâneos conforme B.3 item 2:
- Múltiplos Nonexclusive links (um por nó remoto)
- No máximo 2 Exclusive links
- Nonexclusive rejeitado se Exclusive ativo/pendente
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from .flow_log import flow_rx, flow_tx
from .non_arq import NonArqEngine
from .stypes import (
    CPDU,
    CAS_LOCAL_TIMEOUT,
    CPDUBreakReason,
    CPDURejectReason,
    CPDUType,
    CasEvent,
    CasLinkState,
    DPDUType,
    NonArqDelivery,
    PhysicalLinkType,
)


@dataclass
class CasConfig:
    """Parâmetros de temporização/retries do CAS."""

    call_timeout_seconds: float = 10.0
    break_timeout_seconds: float = 5.0
    max_retries: int = 3
    called_idle_timeout_seconds: float = 30.0  # B.3.2.1 Step 2d
    max_nonexclusive_links: int = 4            # B.3 item 2d


DEFAULT_CALL_TIMEOUT_MS = 10_000
DEFAULT_BREAK_TIMEOUT_MS = 5_000
DEFAULT_MAX_RETRIES = 3


def encode_cpdu(cpdu: CPDU) -> bytes:
    """Codifica C_PDU conforme Annex B Edition 3. Byte 0: [TYPE(4)][FIELD(4)]."""
    t = int(cpdu.cpdu_type) & 0x0F
    if t == 0:  # DATA: TYPE=0 + S_PDU
        return bytes([0x00]) + (cpdu.payload or b"")
    if t == 1:  # LINK_REQUEST: TYPE=1, bit 0=LINK (exclusive/nonexclusive)
        return bytes([(t << 4) | (cpdu.link_type & 0x01)])
    if t in (2, 5):  # LINK_ACCEPTED, BREAK_CONFIRM: TYPE only
        return bytes([t << 4])
    if t in (3, 4):  # LINK_REJECTED, LINK_BREAK: TYPE + REASON(4 bits)
        return bytes([(t << 4) | (cpdu.reason & 0x0F)])
    raise ValueError(f"CPDU type {t} not in Annex B")


def decode_cpdu(data: bytes, *, strict: bool = False) -> CPDU:
    """Decodifica C_PDU conforme Annex B Edition 3. Byte 0: [TYPE(4)][FIELD(4)].

    ``strict``: quando True, valida que os bits ``NOT_USED`` em LINK_REQUEST
    (bits 1-3 do low nibble — B.3.1.2 §4) e em LINK_ACCEPTED/LINK_BREAK_CONFIRM
    (low nibble inteiro — B.3 §8) estão zerados; caso contrário levanta
    ``ValueError``. Por padrão (modo permissivo) os bits são silenciosamente
    descartados, conforme princípio robust-be-permissive.
    """
    if len(data) < 1:
        raise ValueError("CPDU buffer too short")
    b0 = data[0]
    t = (b0 >> 4) & 0x0F    # HIGH nibble = TYPE
    low = b0 & 0x0F          # LOW nibble = field
    if t == 0:
        return CPDU(cpdu_type=CPDUType.DATA, payload=data[1:])
    if t == 1:
        if strict and (low & 0x0E) != 0:
            raise ValueError(
                "LINK_REQUEST: bits 1-3 do field são NOT_USED (B.3.1.2 §4) "
                f"mas vieram = 0x{low:01X}"
            )
        return CPDU(cpdu_type=CPDUType.LINK_REQUEST, link_type=low & 0x01)
    if t == 2:
        if strict and low != 0:
            raise ValueError(
                "LINK_ACCEPTED: low nibble deve ser 0 (B.3 §8), "
                f"got 0x{low:01X}"
            )
        return CPDU(cpdu_type=CPDUType.LINK_ACCEPTED)
    if t in (3, 4):
        return CPDU(cpdu_type=CPDUType(t), reason=low)
    if t == 5:
        if strict and low != 0:
            raise ValueError(
                "LINK_BREAK_CONFIRM: low nibble deve ser 0 (B.3 §8), "
                f"got 0x{low:01X}"
            )
        return CPDU(cpdu_type=CPDUType.LINK_BREAK_CONFIRM)
    raise ValueError(f"Invalid CPDU type {t}")


# ---------------------------------------------------------------------------
# Per-remote link context
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class _LinkContext:
    """Estado de um Physical Link com um nó remoto específico."""
    remote_address: int
    state: CasLinkState = CasLinkState.IDLE
    link_type: PhysicalLinkType = PhysicalLinkType.NONEXCLUSIVE
    is_called_node: bool = False
    link_made_ms: int = 0
    last_data_rx_ms: int = 0
    deadline_ms: int = 0
    retry_count: int = 0


# ---------------------------------------------------------------------------
# CASEngine — multi-link Type 1 protocol
# ---------------------------------------------------------------------------

class CASEngine:
    """Type 1 Channel Access Protocol (Annex B, Edition 3) state machine.

    Suporta múltiplos Physical Links simultâneos:
    - Múltiplos Nonexclusive links (um por nó remoto, B.3 item 2d)
    - No máximo 2 Exclusive links (B.3 item 2c)
    - Nonexclusive rejeitado se Exclusive ativo ou pendente (B.3.2 criterion 1)
    """

    def __init__(
        self,
        local_node_address: int,
        non_arq: NonArqEngine,
        call_timeout_ms: int = DEFAULT_CALL_TIMEOUT_MS,
        break_timeout_ms: int = DEFAULT_BREAK_TIMEOUT_MS,
        max_retries: int = DEFAULT_MAX_RETRIES,
        max_nonexclusive_links: int = 4,
        called_idle_timeout_ms: int = 30_000,
        allow_incoming_links: bool = True,
        busy: bool = False,
        arq_data_handler: Callable[[int, bytes], None] | None = None,
    ) -> None:
        self.local_node_address = local_node_address
        self.non_arq = non_arq
        self.call_timeout_ms = call_timeout_ms
        self.break_timeout_ms = break_timeout_ms
        self.max_retries = max_retries
        self.max_nonexclusive_links = max_nonexclusive_links
        self.called_idle_timeout_ms = called_idle_timeout_ms
        self.allow_incoming_links = allow_incoming_links
        self.busy = busy
        # B.3.1 §5-7: o serviço de entrega do DATA C_PDU deve seguir o modo
        # solicitado pelo SIS (ARQ ou Non-ARQ). Quando ``use_arq=True`` em
        # ``send_data``, o ``arq_data_handler(dest_addr, encoded_cpdu)`` é
        # chamado para que o orquestrador despache via ARQ.
        self.arq_data_handler = arq_data_handler

        self.event_log: list[CasEvent] = []
        self.received_data_cpdus: list[CPDU] = []
        self.received_control_cpdus: list[CPDU] = []
        self.last_failure_reason: int = 0

        # Per-remote link state
        self._links: dict[int, _LinkContext] = {}
        self._primary_remote: int | None = None

    # -------------------------------------------------------------------
    # Backward-compatible properties
    # -------------------------------------------------------------------

    @property
    def state(self) -> CasLinkState:
        """Estado do link primário (para backward compat com StanagNode)."""
        ctx = self._primary_ctx
        return ctx.state if ctx else CasLinkState.IDLE

    @property
    def remote_node_address(self) -> int | None:
        """Endereço do nó remoto do link primário."""
        return self._primary_remote

    @remote_node_address.setter
    def remote_node_address(self, value: int | None) -> None:
        self._primary_remote = value

    @property
    def link_type(self) -> PhysicalLinkType:
        """Tipo do link primário."""
        ctx = self._primary_ctx
        return ctx.link_type if ctx else PhysicalLinkType.NONEXCLUSIVE

    @property
    def _is_called_node(self) -> bool:
        ctx = self._primary_ctx
        return ctx.is_called_node if ctx else False

    @property
    def _primary_ctx(self) -> _LinkContext | None:
        if self._primary_remote is not None:
            return self._links.get(self._primary_remote)
        return None

    @property
    def _active_exclusive_count(self) -> int:
        return sum(
            1 for c in self._links.values()
            if c.state == CasLinkState.MADE and c.link_type == PhysicalLinkType.EXCLUSIVE
        )

    @property
    def _active_nonexclusive_remotes(self) -> set[int]:
        return {
            addr for addr, c in self._links.items()
            if c.state == CasLinkState.MADE and c.link_type == PhysicalLinkType.NONEXCLUSIVE
        }

    # -------------------------------------------------------------------
    # Query methods
    # -------------------------------------------------------------------

    def get_link_state(self, remote: int) -> CasLinkState:
        """Retorna o estado do link com um nó remoto específico."""
        ctx = self._links.get(remote)
        return ctx.state if ctx else CasLinkState.IDLE

    def is_linked(self, remote: int) -> bool:
        """Verifica se há um link ativo (MADE) com um nó remoto."""
        ctx = self._links.get(remote)
        return ctx is not None and ctx.state == CasLinkState.MADE

    def active_links(self) -> dict[int, PhysicalLinkType]:
        """Retorna mapa {remote_addr: link_type} de todos os links ativos."""
        return {
            addr: ctx.link_type for addr, ctx in self._links.items()
            if ctx.state == CasLinkState.MADE
        }

    # -------------------------------------------------------------------
    # Internal context management
    # -------------------------------------------------------------------

    def _ensure_ctx(self, remote: int) -> _LinkContext:
        if remote not in self._links:
            self._links[remote] = _LinkContext(remote_address=remote)
        return self._links[remote]

    def _remove_link(self, remote: int) -> None:
        self._links.pop(remote, None)
        if self._primary_remote == remote:
            self._primary_remote = None

    # -------------------------------------------------------------------
    # Public API
    # -------------------------------------------------------------------

    def reset(self, remote: int | None = None) -> None:
        """Remove o link com o nó especificado (ou o primário se None)."""
        target = remote if remote is not None else self._primary_remote
        if target is not None:
            self._remove_link(target)
        self.last_failure_reason = 0

    def make_link(self, remote_node_address: int, current_time_ms: int,
                  link_type: PhysicalLinkType = PhysicalLinkType.NONEXCLUSIVE) -> None:
        """Inicia protocolo de estabelecimento de link (Caller).

        B.3.2 (4) caller-side: rejeita iniciar Nonexclusive enquanto há um
        Exclusive ativo ou pendente — caso contrário o nó pode emitir
        LINK_REQUESTs incoerentes com sua própria política. Para iniciar um
        Nonexclusive nesse cenário, o caller deve primeiro fazer break_link
        no Exclusive existente.
        """
        # Verificar se já existe um outgoing call pendente (CALLING/BREAKING)
        pctx = self._primary_ctx
        if pctx and pctx.state in (CasLinkState.CALLING, CasLinkState.BREAKING):
            raise RuntimeError("CAS link is already active or pending")
        # B.3.2 (4): Nonexclusive não pode coexistir com Exclusive (ativo ou
        # pendente). Bloqueia o caller antes de emitir o LINK_REQUEST.
        if link_type == PhysicalLinkType.NONEXCLUSIVE:
            for c in self._links.values():
                if (c.link_type == PhysicalLinkType.EXCLUSIVE
                        and c.state in (CasLinkState.CALLING, CasLinkState.MADE)):
                    raise RuntimeError(
                        "Cannot start Nonexclusive link while Exclusive link is "
                        "active or pending (B.3.2 (4))"
                    )
        # Limpar link primário antigo se em IDLE/FAILED (não remover links MADE)
        if pctx and pctx.state in (CasLinkState.IDLE, CasLinkState.FAILED):
            self._remove_link(self._primary_remote)  # type: ignore

        ctx = self._ensure_ctx(remote_node_address)
        ctx.state = CasLinkState.CALLING
        ctx.link_type = link_type
        ctx.is_called_node = False
        ctx.retry_count = 0
        ctx.deadline_ms = current_time_ms + self.call_timeout_ms
        self._primary_remote = remote_node_address

        self._emit_event(CasEvent(state=CasLinkState.CALLING, remote=remote_node_address))
        self._send_control_cpdu(
            CPDU(CPDUType.LINK_REQUEST, link_type=int(link_type)),
            remote_node_address)

    def break_link(self, current_time_ms: int, reason: int = 0,
                   remote: int | None = None) -> None:
        """Inicia protocolo de quebra de link (B.3.2.2)."""
        target = remote if remote is not None else self._primary_remote
        if target is None:
            raise RuntimeError("CAS link is not established")
        ctx = self._links.get(target)
        if ctx is None or ctx.state != CasLinkState.MADE:
            raise RuntimeError("CAS link is not established")

        ctx.state = CasLinkState.BREAKING
        ctx.retry_count = 0
        ctx.deadline_ms = current_time_ms + self.break_timeout_ms
        self._emit_event(CasEvent(state=CasLinkState.BREAKING, remote=target))
        self._send_control_cpdu(CPDU(CPDUType.LINK_BREAK, reason=reason), target)

    def send_data(
        self,
        payload: bytes,
        *,
        expedited: bool = False,
        use_arq: bool = False,
    ) -> None:
        """Envia DATA C_PDU no link primário (B.3.1 §5-7).

        Quando ``use_arq=True`` o C_PDU é despachado via ``arq_data_handler``
        registrado pelo orquestrador (tipicamente ``StanagNode.send_data``);
        ``expedited`` é ignorado nesse caso (Expedited é decidido pelo handler
        ARQ ao optar por D_PDU Tipo 4 vs 0). Sem ``use_arq`` mantém-se o
        comportamento Non-ARQ legado.
        """
        if self._primary_remote is None:
            raise RuntimeError("CAS link is not established")
        ctx = self._primary_ctx
        if ctx is None or ctx.state != CasLinkState.MADE:
            raise RuntimeError("CAS link is not established")
        cpdu = CPDU(CPDUType.DATA, payload=payload)
        encoded = encode_cpdu(cpdu)
        if use_arq:
            if self.arq_data_handler is None:
                raise RuntimeError(
                    "send_data(use_arq=True) requer arq_data_handler "
                    "registrado no construtor (B.3.1 §5-7)"
                )
            self.arq_data_handler(self._primary_remote, encoded)
            return
        self.non_arq.queue_cpdu(
            DPDUType.EXPEDITED_NON_ARQ if expedited else DPDUType.NON_ARQ,
            self._primary_remote,
            encoded,
        )

    def process_delivery(self, delivery: NonArqDelivery, current_time_ms: int) -> None:
        """Processa entrega Non-ARQ."""
        if delivery.error or not delivery.complete:
            return
        try:
            cpdu = decode_cpdu(delivery.payload)
        except ValueError:
            return
        flow_rx("CAS", f"node={self.local_node_address} RX {cpdu.cpdu_type.name} from={delivery.source} len={len(cpdu.payload)}")
        self.process_cpdu(cpdu, delivery.source, current_time_ms)

    def process_cpdu(self, cpdu: CPDU, from_node_address: int, current_time_ms: int) -> None:
        """Máquina de estados: processa C_PDU recebido de um nó remoto."""
        if cpdu.cpdu_type is CPDUType.DATA:
            ctx = self._links.get(from_node_address)
            if ctx:
                ctx.last_data_rx_ms = current_time_ms
            self.received_data_cpdus.append(cpdu)
            self._emit_event(
                CasEvent(
                    state=self.state,
                    remote=from_node_address,
                    cpdu=cpdu,
                    reason=cpdu.reason,
                )
            )
            return

        self.received_control_cpdus.append(cpdu)

        if cpdu.cpdu_type is CPDUType.LINK_REQUEST:
            self._handle_link_request(from_node_address, cpdu.link_type, current_time_ms)
            return

        if cpdu.cpdu_type is CPDUType.LINK_ACCEPTED:
            ctx = self._links.get(from_node_address)
            if ctx and ctx.state == CasLinkState.CALLING:
                # B.3.2.1 Step 3a(1): Exclusive accepted → break all nonexclusive
                if ctx.link_type == PhysicalLinkType.EXCLUSIVE:
                    self._break_all_nonexclusive(current_time_ms)
                ctx.state = CasLinkState.MADE
                ctx.link_made_ms = current_time_ms
                ctx.deadline_ms = 0
                self._emit_event(CasEvent(state=CasLinkState.MADE, remote=from_node_address, cpdu=cpdu))
            return

        if cpdu.cpdu_type is CPDUType.LINK_REJECTED:
            ctx = self._links.get(from_node_address)
            if ctx and ctx.state == CasLinkState.CALLING:
                ctx.state = CasLinkState.FAILED
                self.last_failure_reason = cpdu.reason
                ctx.deadline_ms = 0
                self._emit_event(
                    CasEvent(
                        state=CasLinkState.FAILED,
                        remote=from_node_address,
                        cpdu=cpdu,
                        reason=cpdu.reason,
                    )
                )
            return

        if cpdu.cpdu_type is CPDUType.LINK_BREAK:
            ctx = self._links.get(from_node_address)
            # Sempre responde com BREAK_CONFIRM (B.3.1.5 shall(5)).
            self._send_control_cpdu(CPDU(CPDUType.LINK_BREAK_CONFIRM), from_node_address)
            # Só emite o evento de transição IDLE quando havia um link
            # localmente conhecido — caso contrário poluiria o event_log com
            # transições para nós nunca conectados.
            if ctx:
                self._remove_link(from_node_address)
                self._emit_event(
                    CasEvent(state=CasLinkState.IDLE, remote=from_node_address, cpdu=cpdu)
                )
            return

        if cpdu.cpdu_type is CPDUType.LINK_BREAK_CONFIRM:
            ctx = self._links.get(from_node_address)
            if ctx and ctx.state == CasLinkState.BREAKING:
                self._remove_link(from_node_address)
                self._emit_event(CasEvent(state=CasLinkState.IDLE, remote=from_node_address, cpdu=cpdu))

    def tick(self, current_time_ms: int) -> None:
        """Ciclo de temporização — verifica timeouts de todos os links."""
        for addr, ctx in list(self._links.items()):
            if ctx.state == CasLinkState.CALLING:
                if current_time_ms >= ctx.deadline_ms:
                    if ctx.retry_count < self.max_retries:
                        ctx.retry_count += 1
                        ctx.deadline_ms = current_time_ms + self.call_timeout_ms
                        self._send_control_cpdu(
                            CPDU(CPDUType.LINK_REQUEST, link_type=int(ctx.link_type)),
                            addr)
                    else:
                        ctx.state = CasLinkState.FAILED
                        self.last_failure_reason = CAS_LOCAL_TIMEOUT
                        self._emit_event(
                            CasEvent(
                                state=CasLinkState.FAILED,
                                remote=addr,
                                reason=self.last_failure_reason,
                            )
                        )

            elif ctx.state == CasLinkState.BREAKING:
                if current_time_ms >= ctx.deadline_ms:
                    if ctx.retry_count < self.max_retries:
                        ctx.retry_count += 1
                        ctx.deadline_ms = current_time_ms + self.break_timeout_ms
                        self._send_control_cpdu(CPDU(CPDUType.LINK_BREAK), addr)
                    else:
                        self._remove_link(addr)
                        self._emit_event(CasEvent(state=CasLinkState.IDLE, remote=addr))

            # Called-node idle timeout (B.3.2.1 Step 2d shall(16)(17))
            elif (ctx.state == CasLinkState.MADE
                  and ctx.is_called_node
                  and self.called_idle_timeout_ms > 0):
                # Spec: abort if no DATA on newly made link
                baseline = ctx.last_data_rx_ms if ctx.last_data_rx_ms > 0 else ctx.link_made_ms
                if baseline > 0 and current_time_ms - baseline > self.called_idle_timeout_ms:
                    # Notifica o peer com LINK_BREAK reason=NO_MORE_DATA antes
                    # de remover localmente, evitando que ele mantenha um
                    # link fantasma até seu próprio timeout (BAIXA-B3).
                    self._send_control_cpdu(
                        CPDU(CPDUType.LINK_BREAK,
                             reason=int(CPDUBreakReason.NO_MORE_DATA)),
                        addr,
                    )
                    self._remove_link(addr)
                    self._emit_event(CasEvent(state=CasLinkState.IDLE, remote=addr))

    # -------------------------------------------------------------------
    # B.3.2.1 Step 3a(1): break nonexclusive links on exclusive accept
    # -------------------------------------------------------------------

    def _break_all_nonexclusive(self, current_time_ms: int) -> None:
        """Envia LINK_BREAK para todos os links nonexclusive ativos (B.3.2.1 Step 3a(1))."""
        nonexcl = [
            (addr, ctx) for addr, ctx in self._links.items()
            if ctx.link_type == PhysicalLinkType.NONEXCLUSIVE
            and ctx.state == CasLinkState.MADE
        ]
        for addr, ctx in nonexcl:
            ctx.state = CasLinkState.BREAKING
            ctx.retry_count = 0
            ctx.deadline_ms = current_time_ms + self.break_timeout_ms
            self._send_control_cpdu(
                CPDU(CPDUType.LINK_BREAK,
                     reason=int(CPDUBreakReason.HIGHER_PRIORITY_LINK_REQUEST_PENDING)),
                addr)

    # -------------------------------------------------------------------
    # _handle_link_request — multi-link aware
    # -------------------------------------------------------------------

    def _handle_link_request(self, from_node_address: int, link_type: int = 0,
                             current_time_ms: int = 0) -> None:
        if not self.allow_incoming_links:
            self._send_control_cpdu(
                CPDU(CPDUType.LINK_REJECTED, reason=int(CPDURejectReason.BROADCAST_ONLY_NODE)),
                from_node_address,
            )
            return

        if self.busy:
            self._send_control_cpdu(
                CPDU(CPDUType.LINK_REJECTED,
                     reason=int(CPDURejectReason.HIGHER_PRIORITY_LINK_REQUEST_PENDING)),
                from_node_address,
            )
            return

        # B.3.2.1 Step 2c: Re-accept se link já MADE com este remoto
        existing = self._links.get(from_node_address)
        if existing and existing.state == CasLinkState.MADE:
            self._send_control_cpdu(CPDU(CPDUType.LINK_ACCEPTED), from_node_address)
            return

        # Rejeitar se estado transitório com este remoto específico
        if existing and existing.state in (CasLinkState.CALLING, CasLinkState.BREAKING):
            self._send_control_cpdu(
                CPDU(CPDUType.LINK_REJECTED,
                     reason=int(CPDURejectReason.HIGHER_PRIORITY_LINK_REQUEST_PENDING)),
                from_node_address,
            )
            return

        # B.3.2(4): Nonexclusive rejeitado se Exclusive ativo ou pendente
        if link_type == PhysicalLinkType.NONEXCLUSIVE:
            has_exclusive = any(
                c.link_type == PhysicalLinkType.EXCLUSIVE
                and c.state in (CasLinkState.MADE, CasLinkState.CALLING)
                for c in self._links.values()
            )
            if has_exclusive:
                self._send_control_cpdu(
                    CPDU(CPDUType.LINK_REJECTED,
                         reason=int(CPDURejectReason.SUPPORTING_EXCLUSIVE_LINK)),
                    from_node_address)
                return
            nonexcl_count = sum(
                1 for c in self._links.values()
                if c.state == CasLinkState.MADE and c.link_type == PhysicalLinkType.NONEXCLUSIVE
            )
            if nonexcl_count >= self.max_nonexclusive_links:
                self._send_control_cpdu(
                    CPDU(CPDUType.LINK_REJECTED,
                         reason=int(CPDURejectReason.REASON_UNKNOWN)),
                    from_node_address)
                return

        # B.3.2(5): Exclusive rejeitado se 2+ exclusive ativos
        if link_type == PhysicalLinkType.EXCLUSIVE:
            excl_count = sum(
                1 for c in self._links.values()
                if c.state == CasLinkState.MADE
                and c.link_type == PhysicalLinkType.EXCLUSIVE
            )
            if excl_count >= 2:
                self._send_control_cpdu(
                    CPDU(CPDUType.LINK_REJECTED,
                         reason=int(CPDURejectReason.HIGHER_PRIORITY_LINK_REQUEST_PENDING)),
                    from_node_address)
                return

        # Accept
        ctx = self._ensure_ctx(from_node_address)
        ctx.is_called_node = True
        ctx.link_type = PhysicalLinkType(link_type)
        ctx.state = CasLinkState.MADE
        ctx.link_made_ms = current_time_ms
        ctx.last_data_rx_ms = 0
        ctx.retry_count = 0
        ctx.deadline_ms = 0

        # Set as primary se nenhum primário ativo
        if self._primary_remote is None or self._primary_remote not in self._links:
            self._primary_remote = from_node_address

        self._send_control_cpdu(CPDU(CPDUType.LINK_ACCEPTED), from_node_address)
        self._emit_event(CasEvent(state=CasLinkState.MADE, remote=from_node_address))

    # -------------------------------------------------------------------
    # Internals
    # -------------------------------------------------------------------

    def _send_control_cpdu(self, cpdu: CPDU, destination: int) -> None:
        flow_tx("CAS", f"node={self.local_node_address} TX {cpdu.cpdu_type.name} dest={destination} len={len(cpdu.payload)}")
        self.non_arq.queue_cpdu(
            DPDUType.EXPEDITED_NON_ARQ,
            destination,
            encode_cpdu(cpdu),
        )

    def _emit_event(self, event: CasEvent) -> None:
        self.event_log.append(event)

    def on_warning_received(self, from_node: int, reason: int) -> None:
        """Processa a primitiva D_WARNING_RECEIVED vinda do DTS (Annex C.3.1)."""
        flow_rx("CAS", f"node={self.local_node_address} D_WARNING_RECEIVED from={from_node} reason={reason}")

    def on_warning_transmitted(self, to_node: int, reason: int) -> None:
        """Processa a primitiva D_WARNING_TRANSMITTED vinda do DTS (Annex C.3.1)."""
        flow_tx("CAS", f"node={self.local_node_address} D_WARNING_TRANSMITTED to={to_node} reason={reason}")
