"""
Nó B — Testes Anexo F sobre SIS com UDPModemAdapter (sem modem MIL-STD-110C).
Escuta na porta 9001 e envia para o par na 9000.

Papel: callee — registra servidores F.1, F.3, F.4 e receptores F.2, F.7, FAB, F.12(IP).
Aceita chamada automaticamente (CAS auto-accept).
Comunica com annex_f_udp_node_A.py via UDP direto (D_PDUs crus).
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
    ServiceType,
)
from src.annex_f import (
    AnnexFDispatcher,
    AckMessageServer,
    UnackMessageClient,
    HMTPServer,
    MailMessage,
    HFPOP3Server,
    StoredMessage,
    OrderwireClient,
    FABReceiver,
    IPClient,
)


def _ts() -> str:
    return time.strftime("%H:%M:%S")


# Intervalo de tick (ms) — rápido pois UDP direto não tem latência de waveform
TICK_MS = 10


def main():
    peer_ip = "127.0.0.1"
    listen_port = 9001
    target_port = 9000

    adapter = UDPModemAdapter(
        listen_port=listen_port,
        target_address=(peer_ip, target_port),
    )

    # --- StanagNode (SIS) ---
    cas_config = CasConfig(call_timeout_seconds=5.0, break_timeout_seconds=5.0, max_retries=5)
    node = StanagNode(
        2, adapter,
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

    # --- Registrar servidores/receptores Anexo F ---
    dispatcher = AnnexFDispatcher(node)

    # F.1 — Servidor de Mensagem Reconhecida
    f1_messages: list[tuple[str, list[str], str]] = []

    def on_f1_message(from_user, to_users, body):
        f1_messages.append((from_user, to_users, body))
        print(f"[{_ts()}] [ANNEX-F-B] F.1 RX: from={from_user} to={to_users} body={body[:60]!r}")

    ack_server = AckMessageServer(node, connection_id=0)
    ack_server.set_known_users({"operador_B", "supervisor_B"})
    ack_server.on_message_received = on_f1_message

    # F.2 — Receptor de Mensagem Não Reconhecida
    f2_messages: list[tuple[int, bytes]] = []

    def on_f2_message(src_addr, data):
        f2_messages.append((src_addr, data))
        print(f"[{_ts()}] [ANNEX-F-B] F.2 RX: src={src_addr} len={len(data)} data={data[:60]!r}")

    unack_client = UnackMessageClient(node, connection_id=0)
    unack_client.on_message_received = on_f2_message

    # F.3 — Servidor HMTP
    f3_messages: list[MailMessage] = []

    def on_hmtp_mail(msg: MailMessage):
        f3_messages.append(msg)
        print(f"[{_ts()}] [ANNEX-F-B] F.3 RX: from={msg.sender} to={msg.recipients} body={msg.body[:60]!r}")

    hmtp_server = HMTPServer(node, connection_id=0)
    hmtp_server.set_known_domains({"beta.navy.mil", "delta.navy.mil"})
    hmtp_server.on_mail_received = on_hmtp_mail

    # F.4 — Servidor HF-POP3
    maildrop = {
        "operador_A": [
            StoredMessage(body="Mensagem 1 no maildrop para operador_A.\r\nConteúdo de teste."),
            StoredMessage(body="Mensagem 2 - informações operacionais."),
        ]
    }
    shared_secrets = {"operador_A": "segredo123"}
    pop3_server = HFPOP3Server(
        node, connection_id=0,
        maildrop=maildrop,
        shared_secrets=shared_secrets,
    )

    # F.7 — Orderwire / HFCHAT
    f6_messages: list[tuple[int, str]] = []

    def on_ow_message(src_addr, text):
        f6_messages.append((src_addr, text))
        print(f"[{_ts()}] [ANNEX-F-B] F.7 RX: src={src_addr} text={text[:60]!r}")

    ow_client = OrderwireClient(node, connection_id=0)
    ow_client.on_message_received = on_ow_message

    # F.7/F.8 — FAB Receptor
    fab_received: list[tuple[int, bytes]] = []

    def on_fab(src_addr, data):
        fab_received.append((src_addr, data))
        print(f"[{_ts()}] [ANNEX-F-B] F.8 FAB RX: src={src_addr} len={len(data)} data={data[:60]!r}")

    fab_recv = FABReceiver(node, connection_id=0)
    fab_recv.on_fai_received = on_fab

    # F.12 — IP Client (receptor)
    ip_received: list[tuple[bytes, int]] = []

    def on_ip(data, src_addr):
        ip_received.append((data, src_addr))
        print(f"[{_ts()}] [ANNEX-F-B] F.12 IP RX: src={src_addr} len={len(data)} data={data[:40]!r}")

    ip_client = IPClient(node, address_table={"10.0.0.1": 1}, connection_id=0)
    ip_client.on_ip_received = on_ip

    # Registra todos no dispatcher
    dispatcher.register(ack_server, service=ServiceType(transmission_mode=0))    # ARQ only
    dispatcher.register(unack_client, service=ServiceType(transmission_mode=1))  # NON_ARQ only
    dispatcher.register(hmtp_server, service=ServiceType(transmission_mode=0))   # ARQ only
    dispatcher.register(pop3_server, service=ServiceType(transmission_mode=0))   # ARQ only
    dispatcher.register(ow_client, service=ServiceType(transmission_mode=2))     # both
    dispatcher.register(fab_recv, service=ServiceType(transmission_mode=1))      # NON_ARQ only
    dispatcher.register(ip_client, service=ServiceType(transmission_mode=2))     # both

    # Hard link callbacks
    def on_hard_link_established(remote_addr, remote_sap):
        print(f"[{_ts()}] [ANNEX-F-B] HARD LINK ESTABLISHED remote={remote_addr} sap={remote_sap}")

    def on_hard_link_terminated(remote_addr, initiator_received_confirm=False):
        print(f"[{_ts()}] [ANNEX-F-B] HARD LINK TERMINATED remote={remote_addr} confirm={initiator_received_confirm}")
        if initiator_received_confirm or node.cas.state == CasLinkState.MADE:
            node.break_link()

    node.register_callbacks(
        unidata_indication=dispatcher._on_unidata,
        request_rejected=dispatcher._on_rejected,
        hard_link_established=on_hard_link_established,
        hard_link_terminated=on_hard_link_terminated,
    )

    print(f"[{_ts()}] [ANNEX-F-B] UDP direto — Escutando :{listen_port} -> par {peer_ip}:{target_port}")
    print(f"[{_ts()}] [ANNEX-F-B] Servidores: F.1 F.2 F.3 F.4 F.7(OW) F.8(FAB) F.12(IP)")
    print(f"[{_ts()}] [ANNEX-F-B] Aguardando chamada de A...")
    time.sleep(0.5)

    # Main loop
    deadline_ms = int(time.monotonic() * 1000) + 90_000
    was_made = False
    tick_count = 0

    while int(time.monotonic() * 1000) < deadline_ms:
        t_ms = int(time.monotonic() * 1000)
        node.tick(t_ms)
        tick_count += 1

        if node.cas.state == CasLinkState.MADE:
            if not was_made:
                was_made = True
                print(f"[{_ts()}] [ANNEX-F-B] Enlace MADE (aceitou chamada de A).")

        # Status periódico
        if tick_count % 200 == 0 and was_made:
            total_rx = (len(f1_messages) + len(f2_messages) + len(f3_messages) +
                        len(f6_messages) + len(fab_received) + len(ip_received))
            print(f"[{_ts()}] [ANNEX-F-B] tick {tick_count} cas={node.cas.state.value} "
                  f"rx_total={total_rx} "
                  f"f1={len(f1_messages)} f2={len(f2_messages)} f3={len(f3_messages)} "
                  f"f6={len(f6_messages)} fab={len(fab_received)} ip={len(ip_received)}")

        if was_made and node.cas.state == CasLinkState.IDLE:
            print(f"[{_ts()}] [ANNEX-F-B] Enlace IDLE.")
            time.sleep(0.5)
            break

        time.sleep(TICK_MS / 1000.0)

    # Tick final para drenar
    node.tick(int(time.monotonic() * 1000))

    # ========================================
    # Resumo e Verificações
    # ========================================
    print(f"\n[{_ts()}] [ANNEX-F-B] === RESUMO ===")
    print(f"  Estado final CAS: {node.cas.state.value}")

    print(f"\n  F.1 mensagens reconhecidas recebidas: {len(f1_messages)}")
    for from_u, to_u, body in f1_messages:
        print(f"    from={from_u} to={to_u} body={body[:80]!r}")

    print(f"\n  F.2 mensagens non-ARQ recebidas: {len(f2_messages)}")
    for src, data in f2_messages:
        print(f"    src={src} len={len(data)} data={data[:80]!r}")

    print(f"\n  F.3 HMTP mensagens recebidas: {len(f3_messages)}")
    for msg in f3_messages:
        print(f"    from={msg.sender} to={msg.recipients} body={msg.body[:80]!r}")

    print(f"\n  F.7 orderwire recebidos: {len(f6_messages)}")
    for src, text in f6_messages:
        print(f"    src={src} text={text[:80]!r}")

    print(f"\n  F.8 FAB recebidos: {len(fab_received)}")
    for src, data in fab_received:
        print(f"    src={src} len={len(data)} data={data[:80]!r}")

    print(f"\n  F.12 IP recebidos: {len(ip_received)}")
    for data, src in ip_received:
        print(f"    src={src} len={len(data)} data={data[:40]!r}")

    print(f"\n[{_ts()}] [ANNEX-F-B] === VERIFICAÇÕES ===")
    checks = []

    checks.append(("F.2 mensagem non-ARQ recebida", len(f2_messages) > 0))
    checks.append(("F.2 conteúdo íntegro",
                    any(b"non-ARQ via F.2" in d for _, d in f2_messages) if f2_messages else False))

    checks.append(("F.1 mensagem SEND recebida", len(f1_messages) > 0))
    checks.append(("F.1 corpo entregue",
                    any("F.1" in body for _, _, body in f1_messages) if f1_messages else False))

    checks.append(("F.3 HMTP mensagem recebida", len(f3_messages) > 0))
    checks.append(("F.3 remetente correto",
                    any(m.sender == "smith@alpha.navy.mil" for m in f3_messages) if f3_messages else False))

    checks.append(("F.7 orderwire recebido", len(f6_messages) > 0))

    checks.append(("F.8 FAB recebido", len(fab_received) > 0))
    checks.append(("F.8 FAB conteúdo íntegro",
                    any(b"FAI:FREQ=" in d for _, d in fab_received) if fab_received else False))

    checks.append(("F.12 IP datagram recebido", len(ip_received) > 0))

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
