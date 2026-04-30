"""
Nó A — Testes Anexo F sobre SIS com UDPModemAdapter (sem modem MIL-STD-110C).
Escuta na porta 9000 e envia para o par na 9001.

Papel: caller — registra clientes F.1–F.4, F.7, FAB, F.12(IP),
estabelece hard link, executa testes sequenciais de cada protocolo.
Comunica com annex_f_udp_node_B.py via UDP direto (D_PDUs crus).
"""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.modem.udp_modem_adapter import UDPModemAdapter
from src.stanag_node import StanagNode
from src.cas import CasConfig
from src.stypes import (
    CasLinkState,
    DeliveryMode,
    ServiceType,
    SisHardLinkType,
    SisLinkSessionState,
)
from src.annex_f import (
    AnnexFDispatcher,
    AckMessageClient,
    SendMessage,
    UnackMessageClient,
    HMTPClient,
    MailMessage,
    HFPOP3Client,
    OrderwireClient,
    FABGenerator,
    IPClient,
)


def _ts() -> str:
    return time.strftime("%H:%M:%S")


# Intervalo de tick (ms) — rápido pois UDP direto não tem latência de waveform
TICK_MS = 10


def _tick_n(node, n: int):
    """Executa n ticks com intervalo TICK_MS."""
    for _ in range(n):
        node.tick(int(time.monotonic() * 1000))
        time.sleep(TICK_MS / 1000.0)


def main():
    peer_ip = "127.0.0.1"
    listen_port = 9000
    target_port = 9001

    adapter = UDPModemAdapter(
        listen_port=listen_port,
        target_address=(peer_ip, target_port),
    )

    # --- StanagNode (SIS) ---
    cas_config = CasConfig(call_timeout_seconds=5.0, break_timeout_seconds=5.0, max_retries=5)
    node = StanagNode(
        1, adapter,
        cas_config=cas_config,
        max_user_data_bytes=128,
        use_arq_data=True,
        soft_link_idle_timeout_ms=60_000,
        arq_reset_retransmit_ms=3000,
        arq_retx_timeout_ms=3000,
        arq_max_retries=5,
    )

    # data_rate / interleave
    node.arq.data_rate_bps = 2400
    node.arq.long_interleave = False

    # --- Registrar clientes Anexo F ---
    dispatcher = AnnexFDispatcher(node)

    # F.1 — Mensagem Reconhecida
    ack_client = AckMessageClient(node, connection_id=0)
    f1_responses: list[tuple[int, str]] = []
    ack_client.on_response = lambda code, text: f1_responses.append((code, text))

    # F.2 — Mensagem Não Reconhecida
    unack_client = UnackMessageClient(node, connection_id=0)

    # F.3 — HMTP
    hmtp_client = HMTPClient(node, connection_id=0)
    hmtp_responses: list = []
    hmtp_client.on_response = lambda resps: hmtp_responses.extend(resps)

    # F.4 — HF-POP3
    pop3_client = HFPOP3Client(node, connection_id=0)
    pop3_auth_results: list = []
    pop3_messages: list[tuple[int, str]] = []
    pop3_errors: list[str] = []
    pop3_client.on_authenticated = lambda listing: pop3_auth_results.append(listing)
    pop3_client.on_message_retrieved = lambda n, body: pop3_messages.append((n, body))
    pop3_client.on_error = lambda err: pop3_errors.append(err)

    # F.7 — Orderwire / HFCHAT
    ow_client = OrderwireClient(node, connection_id=0)
    ow_received: list[tuple[int, str]] = []
    ow_client.on_message_received = lambda addr, text: ow_received.append((addr, text))

    # F.7 — FAB Generator
    fab_gen = FABGenerator(node, broadcast_addr=2, update_interval_s=5.0, connection_id=0)

    # F.12 — IP Client
    ip_client = IPClient(node, address_table={"10.0.0.2": 2}, connection_id=0)
    ip_received: list[tuple[bytes, int]] = []
    ip_client.on_ip_received = lambda data, src: ip_received.append((data, src))

    # Registra todos no dispatcher
    dispatcher.register(ack_client, service=ServiceType(transmission_mode=0))    # ARQ only
    dispatcher.register(unack_client, service=ServiceType(transmission_mode=1))  # NON_ARQ only
    dispatcher.register(hmtp_client, service=ServiceType(transmission_mode=0))   # ARQ only
    dispatcher.register(pop3_client, service=ServiceType(transmission_mode=0))   # ARQ only
    dispatcher.register(ow_client, service=ServiceType(transmission_mode=2))     # both
    dispatcher.register(fab_gen, service=ServiceType(transmission_mode=1))       # NON_ARQ only
    dispatcher.register(ip_client, service=ServiceType(transmission_mode=2))     # both

    # Hard link callbacks
    hard_link_ok = [False]
    hard_link_done = [False]

    def on_hard_link_established(addr, sap):
        hard_link_ok[0] = True
        print(f"[{_ts()}] [ANNEX-F-A] HARD LINK ESTABLISHED remote={addr} sap={sap}")

    def on_hard_link_terminated(addr, initiator_received_confirm=False):
        hard_link_done[0] = True
        print(f"[{_ts()}] [ANNEX-F-A] HARD LINK TERMINATED remote={addr} confirm={initiator_received_confirm}")
        if initiator_received_confirm or node.cas.state == CasLinkState.MADE:
            node.break_link()

    node.register_callbacks(
        unidata_indication=dispatcher._on_unidata,
        request_rejected=dispatcher._on_rejected,
        hard_link_established=on_hard_link_established,
        hard_link_terminated=on_hard_link_terminated,
    )

    print(f"[{_ts()}] [ANNEX-F-A] UDP direto — Escutando :{listen_port} -> par {peer_ip}:{target_port}")
    print(f"[{_ts()}] [ANNEX-F-A] Clientes: F.1 F.2 F.3 F.4 F.7(OW) F.7(FAB) F.12(IP)")
    time.sleep(1)

    REMOTE_ADDR = 2

    # Sincroniza o relógio interno do nó antes de chamar comandos baseados no tempo (ex: CAS)
    node.tick(int(time.monotonic() * 1000))

    # ========================================
    # 1. Estabelecer hard link (SAP 13 -> SAP 13)
    # ========================================
    print(f"\n[{_ts()}] [ANNEX-F-A] === FASE 1: Hard Link ===")
    print(f"[{_ts()}] [ANNEX-F-A] Estabelecendo hard link A -> B...")
    node.hard_link_establish(
        sap_id=13,
        link_priority=3,
        remote_addr=REMOTE_ADDR,
        remote_sap=13,
        link_type=SisHardLinkType.NO_RESERVATION,
    )

    deadline_ms = int(time.monotonic() * 1000) + 30_000
    while True:
        t_ms = int(time.monotonic() * 1000)
        if t_ms >= deadline_ms:
            print(f"[{_ts()}] [ANNEX-F-A] Timeout estabelecimento hard link.")
            return
        node.tick(t_ms)
        if node._link_session.state == SisLinkSessionState.ACTIVE:
            print(f"[{_ts()}] [ANNEX-F-A] Hard link ACTIVE.")
            break
        time.sleep(TICK_MS / 1000.0)

    # ========================================
    # 2. Testes sequenciais dos clientes
    # ========================================

    # --- F.2: Mensagem non-ARQ ---
    print(f"\n[{_ts()}] [ANNEX-F-A] === TESTE F.2: Mensagem Não Reconhecida ===")
    f2_payload = b"Mensagem broadcast non-ARQ via F.2 - teste Anexo F (UDP direto)"
    unack_client.send_message(REMOTE_ADDR, f2_payload, priority=5, ttl_seconds=60.0)
    print(f"[{_ts()}] [ANNEX-F-A] F.2 enviada: {len(f2_payload)} bytes")
    _tick_n(node, 50)

    # --- F.1: Mensagem Reconhecida ---
    print(f"\n[{_ts()}] [ANNEX-F-A] === TESTE F.1: Mensagem Reconhecida ===")
    ack_client.send_message(
        REMOTE_ADDR,
        from_user="operador_A",
        to_users=["operador_B", "supervisor_B"],
        body="Mensagem de teste F.1 - Anexo F STANAG 5066 (UDP direto)\r\nLinha 2 do corpo.",
    )
    print(f"[{_ts()}] [ANNEX-F-A] F.1 SEND enviado")
    _tick_n(node, 100)

    # --- F.3: HMTP batch ---
    print(f"\n[{_ts()}] [ANNEX-F-A] === TESTE F.3: HMTP ===")
    hmtp_client.send_batch(
        REMOTE_ADDR,
        hostname="alpha.navy.mil",
        messages=[
            MailMessage(
                sender="smith@alpha.navy.mil",
                recipients=["jones@beta.navy.mil", "brown@delta.navy.mil"],
                body="Primeira mensagem HMTP de teste.\r\nConteúdo importante.",
            ),
            MailMessage(
                sender="smith@alpha.navy.mil",
                recipients=["green@gamma.navy.mil"],
                body="Segunda mensagem HMTP.",
            ),
        ],
    )
    print(f"[{_ts()}] [ANNEX-F-A] F.3 HMTP batch enviado (2 mensagens)")
    _tick_n(node, 100)

    # --- F.4: HF-POP3 ---
    print(f"\n[{_ts()}] [ANNEX-F-A] === TESTE F.4: HF-POP3 ===")
    pop3_client.connect(REMOTE_ADDR)
    print(f"[{_ts()}] [ANNEX-F-A] F.4 CONNECT enviado")

    # Aguardar timestamp do servidor
    pop3_ts_deadline = int(time.monotonic() * 1000) + 15_000
    while int(time.monotonic() * 1000) < pop3_ts_deadline:
        node.tick(int(time.monotonic() * 1000))
        time.sleep(TICK_MS / 1000.0)
        if pop3_client._server_timestamp:
            print(f"[{_ts()}] [ANNEX-F-A] F.4 timestamp recebido: {pop3_client._server_timestamp}")
            break
    else:
        print(f"[{_ts()}] [ANNEX-F-A] F.4 AVISO: timestamp não recebido, APOP pode falhar")

    # APOP
    pop3_client.apop(REMOTE_ADDR, name="operador_A", shared_secret="segredo123")
    print(f"[{_ts()}] [ANNEX-F-A] F.4 APOP enviado")
    _tick_n(node, 100)

    # RETR
    pop3_client.retrieve(REMOTE_ADDR)
    print(f"[{_ts()}] [ANNEX-F-A] F.4 RETR enviado")
    _tick_n(node, 100)

    pop3_client.quit(REMOTE_ADDR)
    print(f"[{_ts()}] [ANNEX-F-A] F.4 QUIT enviado")
    _tick_n(node, 50)

    # --- F.7: Orderwire / HFCHAT ---
    print(f"\n[{_ts()}] [ANNEX-F-A] === TESTE F.7: Orderwire (HFCHAT) ===")
    ow_client.send_acknowledged(REMOTE_ADDR, "Orderwire ACK de A para B")
    print(f"[{_ts()}] [ANNEX-F-A] F.7 orderwire ACK enviado")
    _tick_n(node, 50)

    ow_client.send_broadcast(REMOTE_ADDR, "Orderwire broadcast de A")
    print(f"[{_ts()}] [ANNEX-F-A] F.7 orderwire broadcast enviado")
    _tick_n(node, 50)

    # --- F.7: FAB ---
    print(f"\n[{_ts()}] [ANNEX-F-A] === TESTE F.7: FAB ===")
    fai_data = b"FAI:FREQ=5.2MHz,STATUS=AVAILABLE;FREQ=8.1MHz,STATUS=BUSY"
    fab_gen.update_fai(fai_data)
    fab_gen.tick_broadcast(int(time.monotonic() * 1000))
    print(f"[{_ts()}] [ANNEX-F-A] F.7 FAB broadcast enviado: {len(fai_data)} bytes")
    _tick_n(node, 50)

    # --- F.12: IP Client ---
    print(f"\n[{_ts()}] [ANNEX-F-A] === TESTE F.12: IP Client ===")
    ip_payload = b"Hello from IP Client F.12 via STANAG 5066"
    ip_total_len = 20 + len(ip_payload)
    ip_header = bytearray(20)
    ip_header[0] = 0x45               # Version=4, IHL=5
    ip_header[1] = 0x00               # TOS=0 (best effort)
    ip_header[2] = (ip_total_len >> 8) & 0xFF
    ip_header[3] = ip_total_len & 0xFF
    ip_header[8] = 64                  # TTL
    ip_header[9] = 17                  # Protocol=UDP
    ip_header[12:16] = bytes([10, 0, 0, 1])
    ip_header[16:20] = bytes([10, 0, 0, 2])
    ip_datagram = bytes(ip_header) + ip_payload
    sent_ok = ip_client.send_ip_datagram(ip_datagram)
    print(f"[{_ts()}] [ANNEX-F-A] F.12 IP datagram enviado: {len(ip_datagram)} bytes, ok={sent_ok}")
    _tick_n(node, 50)

    # ========================================
    # 3. Drenagem final
    # ========================================
    print(f"\n[{_ts()}] [ANNEX-F-A] === FASE 3: Drenagem ===")
    for i in range(200):
        t_ms = int(time.monotonic() * 1000)
        node.tick(t_ms)
        time.sleep(TICK_MS / 1000.0)
        if (i + 1) % 100 == 0:
            print(f"[{_ts()}] [ANNEX-F-A] tick {i+1}/200 cas={node.cas.state.value}")

    # ========================================
    # 4. Encerramento
    # ========================================
    print(f"\n[{_ts()}] [ANNEX-F-A] === FASE 4: Encerramento ===")
    print(f"[{_ts()}] [ANNEX-F-A] Encerrando hard link...")
    node.hard_link_terminate(sap_id=13, remote_addr=REMOTE_ADDR)

    deadline_ms = int(time.monotonic() * 1000) + 15_000
    while True:
        t_ms = int(time.monotonic() * 1000)
        if t_ms >= deadline_ms:
            print(f"[{_ts()}] [ANNEX-F-A] Timeout aguardando IDLE.")
            break
        node.tick(t_ms)
        if node.cas.state == CasLinkState.IDLE:
            print(f"[{_ts()}] [ANNEX-F-A] Enlace IDLE.")
            # Dá mais meio segundo de ticks purgar o LINK_BREAK_CONFIRM que acabou de enfileirar
            drenagem = int(time.monotonic() * 1000) + 2000
            while int(time.monotonic() * 1000) < drenagem:
                node.tick(int(time.monotonic() * 1000))
                time.sleep(0.2)
            break
        time.sleep(TICK_MS / 1000.0)

    # ========================================
    # 5. Resumo e Verificações
    # ========================================
    print(f"\n[{_ts()}] [ANNEX-F-A] === RESUMO ===")
    print(f"  Estado final CAS: {node.cas.state.value}")
    print(f"  Hard link estabelecido: {hard_link_ok[0]}")
    print(f"  Hard link terminado: {hard_link_done[0]}")
    print(f"  F.1 respostas recebidas: {len(f1_responses)}")
    for code, text in f1_responses:
        print(f"    {code}: {text}")
    print(f"  F.3 HMTP respostas: {len(hmtp_responses)}")
    for resp in hmtp_responses:
        print(f"    {resp.code}: {resp.keyword} {resp.args}")
    print(f"  F.4 POP3 auth: {len(pop3_auth_results)}")
    print(f"  F.4 POP3 mensagens: {len(pop3_messages)}")
    print(f"  F.4 POP3 erros: {pop3_errors}")
    print(f"  F.7 orderwire recebidos: {len(ow_received)}")
    print(f"  F.12 IP recebidos: {len(ip_received)}")

    print(f"\n[{_ts()}] [ANNEX-F-A] === VERIFICAÇÕES ===")
    checks = []

    checks.append(("Hard link estabelecido", hard_link_ok[0]))
    checks.append(("F.1 respostas 250 recebidas",
                    any(code == 250 for code, _ in f1_responses)))
    checks.append(("F.3 HMTP respostas recebidas", len(hmtp_responses) > 0))
    checks.append(("F.4 POP3 sem erros fatais", len(pop3_errors) == 0 or len(pop3_auth_results) > 0))
    checks.append(("F.12 IP datagram enviado", sent_ok))

    for name, ok in checks:
        status = "OK" if ok else "FALHA"
        print(f"  [{status}] {name}")

    n_fail = sum(1 for _, ok in checks if not ok)
    if n_fail:
        print(f"\n  {n_fail} verificação(ões) falharam!")
    else:
        print(f"\n  Todas as verificações passaram.")


if __name__ == "__main__":
    main()
