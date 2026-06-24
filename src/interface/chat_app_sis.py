import argparse
import asyncio
import os
import queue
import sys
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, scrolledtext, ttk
from typing import Optional

# Garante que a raiz do repositório esteja no sys.path para importar `src.*`
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from src.cas import CasConfig
from src.modem.udp_modem_adapter import UDPModemAdapter
from src.raw_sis_socket import RawSisSocketServer
from src.stanag_node import StanagNode
from src.arq import repetition_count_for_rate
from src.stypes import (
    CasLinkState,
    DeliveryMode,
    SisRejectReason,
    SisUnidataIndication,
)

# ---------------------------------------------------------------------------
# Data rate / interleaver — alimentam a contagem de repetição do ARQ.
# Neste repositório o canal é UDP simulado (UDPModemAdapter), sem modem DSP.
# ---------------------------------------------------------------------------
SUPPORTED_BITRATES = [150, 300, 600, 1200, 2400]
SUPPORTED_INTERLEAVERS = ["short", "long"]


class ChatApp:
    def __init__(self, root: tk.Tk, node_name: str):
        self.root = root
        self.node_name = node_name
        self.is_node_a = node_name.upper() == "A"

        self.local_id = 1 if self.is_node_a else 2
        self.remote_id = 2 if self.is_node_a else 1
        self.listen_port = 8000 if self.is_node_a else 8001
        self.target_port = 8001 if self.is_node_a else 8000
        # SAP 0 é reservado ao Subnet Management (requer allow_management_rank);
        # usamos SAPs de dados de usuário comuns para o chat.
        self.bound_saps = (3, 5)

        self.root.title(f"STANAG 5066 HF Chat — Node {self.node_name}")
        self.root.geometry("820x680")
        self.root.minsize(780, 620)

        self.node: Optional[StanagNode] = None
        self._thread_stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

        # Asyncio event loop para o Raw SIS Socket Server (System API)
        self._sis_loop: Optional[asyncio.AbstractEventLoop] = None
        self._sis_loop_thread: Optional[threading.Thread] = None
        self._sis_server: Optional[RawSisSocketServer] = None

        # Eventos de callbacks SIS vindo da thread de tick.
        self._event_queue: "queue.Queue[tuple[str, object]]" = queue.Queue()
        self._rx_count = 0
        self._tx_count = 0
        self._prev_cas_ui_state: CasLinkState | None = None

        # Rastreamento de envio de arquivo em burst
        self._file_tx_active = False
        self._file_tx_name = ""
        self._file_tx_total_chunks = 0
        self._file_tx_sent_chunks = 0
        self._file_tx_total_bytes = 0
        self._file_tx_start_time = 0.0
        # Retransmissões acumuladas (janela ARQ pode ser limpa ao receber ACK)
        self._file_tx_retx_accumulated = 0
        self._file_tx_retx_last_snapshot = 0
        # Recepção de arquivo
        self._file_rx_buffers: dict[str, bytearray] = {}  # filename -> dados

        self._build_ui()
        self._update_ui_loop()

    # ---------------------------------------------------------------
    # UI
    # ---------------------------------------------------------------

    def _build_ui(self) -> None:
        style = ttk.Style()
        style.configure("Status.TLabel", font=("Consolas", 9))
        style.configure("Header.TLabelframe.Label", font=("Segoe UI", 9, "bold"))

        main_frame = ttk.Frame(self.root, padding="8")
        main_frame.pack(fill=tk.BOTH, expand=True)

        # ── Configuração do Nó ──────────────────────────────────
        cfg_frame = ttk.LabelFrame(
            main_frame, text="Configuração do Nó", padding="6",
            style="Header.TLabelframe",
        )
        cfg_frame.pack(fill=tk.X, pady=(0, 6))

        ttk.Label(cfg_frame, text="Bitrate (bps)").grid(
            row=0, column=0, padx=(0, 4), sticky="w",
        )
        self.cmb_bitrate = ttk.Combobox(
            cfg_frame, width=8,
            values=[str(b) for b in SUPPORTED_BITRATES],
            state="readonly",
        )
        self.cmb_bitrate.grid(row=0, column=1, padx=(0, 12))
        self.cmb_bitrate.set("2400")

        ttk.Label(cfg_frame, text="Interleaver").grid(
            row=0, column=2, padx=(0, 4), sticky="w",
        )
        self.cmb_interleaver = ttk.Combobox(
            cfg_frame, width=8,
            values=SUPPORTED_INTERLEAVERS,
            state="readonly",
        )
        self.cmb_interleaver.grid(row=0, column=3, padx=(0, 12))
        self.cmb_interleaver.set("short")

        self.btn_init = ttk.Button(
            cfg_frame, text="⚡ Inicializar Nó", command=self._cmd_init_node,
        )
        self.btn_init.grid(row=0, column=4, padx=(8, 0))

        self.lbl_init_status = ttk.Label(
            cfg_frame, text="⬤ Offline", foreground="gray",
            style="Status.TLabel",
        )
        self.lbl_init_status.grid(row=0, column=5, padx=(12, 0))

        self.lbl_sys_api = ttk.Label(
            cfg_frame, text="API: —", foreground="gray",
            style="Status.TLabel",
        )
        self.lbl_sys_api.grid(row=0, column=6, padx=(12, 0))

        # ── Status SIS / CAS / DTS ─────────────────────────────
        status_frame = ttk.LabelFrame(
            main_frame, text="Status do Enlace", padding="5",
            style="Header.TLabelframe",
        )
        status_frame.pack(fill=tk.X, pady=(0, 6))

        left_status = ttk.Frame(status_frame)
        left_status.pack(side=tk.LEFT, fill=tk.X, expand=True)

        self.lbl_cas = ttk.Label(left_status, text="CAS: —", style="Status.TLabel")
        self.lbl_cas.grid(row=0, column=0, padx=(0, 14), sticky="w")
        self.lbl_sis = ttk.Label(left_status, text="SIS: —", style="Status.TLabel")
        self.lbl_sis.grid(row=0, column=1, padx=(0, 14), sticky="w")
        self.lbl_dts = ttk.Label(left_status, text="DTS: —", style="Status.TLabel")
        self.lbl_dts.grid(row=0, column=2, padx=(0, 14), sticky="w")
        self.lbl_arq = ttk.Label(left_status, text="ARQ: —", style="Status.TLabel")
        self.lbl_arq.grid(row=0, column=3, padx=(0, 14), sticky="w")

        self.lbl_pending = ttk.Label(left_status, text="TX: 0", style="Status.TLabel")
        self.lbl_pending.grid(row=1, column=0, padx=(0, 14), sticky="w")
        self.lbl_rx = ttk.Label(left_status, text="RX: 0", style="Status.TLabel")
        self.lbl_rx.grid(row=1, column=1, padx=(0, 14), sticky="w")
        self.lbl_arq_win = ttk.Label(left_status, text="ARQ Win: —", style="Status.TLabel")
        self.lbl_arq_win.grid(row=1, column=2, padx=(0, 14), sticky="w")
        self.lbl_reset = ttk.Label(left_status, text="", style="Status.TLabel")
        self.lbl_reset.grid(row=1, column=3, padx=(0, 14), sticky="w")

        right_status = ttk.Frame(status_frame)
        right_status.pack(side=tk.RIGHT)

        self.btn_connect = ttk.Button(
            right_status, text="🔗 Conectar", command=self._cmd_connect,
            state="disabled",
        )
        self.btn_connect.pack(side=tk.RIGHT, padx=3)
        self.btn_disconnect = ttk.Button(
            right_status, text="✂ Desconectar", command=self._cmd_disconnect,
            state="disabled",
        )
        self.btn_disconnect.pack(side=tk.RIGHT, padx=3)

        # ── Parâmetros S_UNIDATA_REQUEST ────────────────────────
        controls_frame = ttk.LabelFrame(
            main_frame, text="Parâmetros de Envio", padding="5",
            style="Header.TLabelframe",
        )
        controls_frame.pack(fill=tk.X, pady=(0, 6))

        ttk.Label(controls_frame, text="Src SAP").grid(row=0, column=0, padx=(0, 4), sticky="w")
        self.cmb_src_sap = ttk.Combobox(
            controls_frame, width=5,
            values=[str(v) for v in self.bound_saps],
            state="readonly",
        )
        self.cmb_src_sap.grid(row=0, column=1, padx=(0, 10))
        self.cmb_src_sap.set(str(self.bound_saps[0]))

        ttk.Label(controls_frame, text="Dest SAP").grid(row=0, column=2, padx=(0, 4), sticky="w")
        self.cmb_dest_sap = ttk.Combobox(
            controls_frame, width=5,
            values=[str(v) for v in self.bound_saps],
            state="readonly",
        )
        self.cmb_dest_sap.grid(row=0, column=3, padx=(0, 10))
        self.cmb_dest_sap.set(str(self.bound_saps[0]))

        ttk.Label(controls_frame, text="Prioridade").grid(row=0, column=4, padx=(0, 4), sticky="w")
        self.spn_priority = tk.Spinbox(controls_frame, from_=0, to=15, width=5)
        self.spn_priority.grid(row=0, column=5, padx=(0, 10))
        self.spn_priority.delete(0, tk.END)
        self.spn_priority.insert(0, "10")

        ttk.Label(controls_frame, text="TTL(s)").grid(row=0, column=6, padx=(0, 4), sticky="w")
        self.ent_ttl = ttk.Entry(controls_frame, width=8)
        self.ent_ttl.grid(row=0, column=7, padx=(0, 10))
        self.ent_ttl.insert(0, "120")

        ttk.Label(controls_frame, text="Modo").grid(row=0, column=8, padx=(0, 4), sticky="w")
        self.cmb_mode = ttk.Combobox(
            controls_frame, width=12,
            values=["ARQ", "NON_ARQ", "EXP_ARQ", "EXP_NON_ARQ"],
            state="readonly",
        )
        self.cmb_mode.grid(row=0, column=9, padx=(0, 4))
        self.cmb_mode.set("ARQ")

        # ── Histórico ──────────────────────────────────────────
        history_frame = ttk.LabelFrame(
            main_frame, text="Histórico", padding="5",
            style="Header.TLabelframe",
        )
        history_frame.pack(fill=tk.BOTH, expand=True, pady=4)

        self.txt_history = scrolledtext.ScrolledText(
            history_frame, wrap=tk.WORD, state="disabled", height=14,
            font=("Consolas", 9),
        )
        self.txt_history.pack(fill=tk.BOTH, expand=True)

        # Tags de cor para o histórico
        self.txt_history.tag_configure("system", foreground="#2196F3")
        self.txt_history.tag_configure("sent", foreground="#4CAF50")
        self.txt_history.tag_configure("received", foreground="#FF9800")
        self.txt_history.tag_configure("error", foreground="#F44336")

        # ── Progresso de arquivo ──────────────────────────────
        self.file_progress_frame = ttk.LabelFrame(
            main_frame, text="Transferência de Arquivo", padding="5",
            style="Header.TLabelframe",
        )
        # Inicialmente oculto — exibido durante transferência
        self.lbl_file_info = ttk.Label(
            self.file_progress_frame, text="", style="Status.TLabel",
        )
        self.lbl_file_info.pack(anchor="w")
        self.lbl_file_progress = ttk.Label(
            self.file_progress_frame, text="", style="Status.TLabel",
        )
        self.lbl_file_progress.pack(anchor="w")
        self.file_progressbar = ttk.Progressbar(
            self.file_progress_frame, mode="determinate", length=400,
        )
        self.file_progressbar.pack(fill=tk.X, pady=(2, 0))

        # ── Input ──────────────────────────────────────────────
        self.input_frame = input_frame = ttk.Frame(main_frame)
        input_frame.pack(fill=tk.X, pady=(4, 0))
        self.entry_msg = ttk.Entry(input_frame, font=("Segoe UI", 10))
        self.entry_msg.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 5))
        self.entry_msg.bind("<Return>", lambda _e: self._cmd_send())
        self.btn_send = ttk.Button(input_frame, text="📨 Enviar", command=self._cmd_send)
        self.btn_send.pack(side=tk.RIGHT)
        self.btn_send_file = ttk.Button(
            input_frame, text="📎 Enviar Arquivo", command=self._cmd_send_file,
        )
        self.btn_send_file.pack(side=tk.RIGHT, padx=(0, 5))

    # ---------------------------------------------------------------
    # Log helpers
    # ---------------------------------------------------------------

    def _log_msg(self, sender: str, text: str, tag: str = "") -> None:
        self.txt_history.configure(state="normal")
        ts = time.strftime("%H:%M:%S")
        self.txt_history.insert(tk.END, f"[{ts}] {sender}: {text}\n", tag)
        self.txt_history.see(tk.END)
        self.txt_history.configure(state="disabled")

    # ---------------------------------------------------------------
    # Node init (deferred — triggered by button)
    # ---------------------------------------------------------------

    def _cmd_init_node(self) -> None:
        if self.node is not None:
            self._log_msg("SYSTEM", "Nó já inicializado.", "system")
            return

        bitrate = int(self.cmb_bitrate.get())
        interleaver = self.cmb_interleaver.get()

        self._log_msg(
            "SYSTEM",
            f"Inicializando nó... bitrate={bitrate} bps, interleaver={interleaver}",
            "system",
        )

        # Disable config controls during init
        self.btn_init.config(state="disabled")
        self.cmb_bitrate.config(state="disabled")
        self.cmb_interleaver.config(state="disabled")

        try:
            self._init_stanag(bitrate, interleaver)
            self.lbl_init_status.config(text="⬤ Online", foreground="#4CAF50")
            self.btn_connect.config(state="normal")
            self.btn_disconnect.config(state="normal")
            self._log_msg(
                "SYSTEM",
                f"Nó inicializado (Fase 4/SIS). UDP local:{self.listen_port}, "
                f"peer:{self.target_port}, SAPs:{self.bound_saps}, "
                f"bitrate={bitrate}, interleaver={interleaver}",
                "system",
            )
            self._start_sis_api()
        except Exception as exc:
            self._log_msg("SYSTEM", f"Falha ao inicializar: {exc}", "error")
            self.btn_init.config(state="normal")
            self.cmb_bitrate.config(state="readonly")
            self.cmb_interleaver.config(state="readonly")

    def _init_stanag(self, bitrate: int, interleaver: str) -> None:
        peer_ip = "127.0.0.1"

        # Canal UDP simulado (sem modem HF/DSP). Transporta D_PDUs crus.
        adapter = UDPModemAdapter(
            listen_port=self.listen_port,
            target_address=(peer_ip, self.target_port),
            data_rate_bps=bitrate,
        )

        cas_config = CasConfig(
            call_timeout_seconds=15.0,
            break_timeout_seconds=10.0,
            max_retries=3,
        )
        self.node = StanagNode(
            self.local_id,
            adapter,
            cas_config=cas_config,
            max_user_data_bytes=128,
            use_arq_data=True,
            soft_link_idle_timeout_ms=60_000,
            arq_reset_retransmit_ms=3000,
            arq_retx_timeout_ms=3000,
            arq_max_retries=5,
        )

        # P2: configurar data_rate no ARQ para repetição RESET correta
        self.node.arq.data_rate_bps = bitrate
        self.node.arq.long_interleave = (interleaver == "long")

        for sap in self.bound_saps:
            self.node.bind(sap)

        self.node.register_callbacks(
            unidata_indication=self._on_unidata_indication,
            request_rejected=self._on_request_rejected,
            hard_link_established=self._on_hard_link_established,
            hard_link_terminated=self._on_hard_link_terminated,
        )

        self._thread = threading.Thread(target=self._node_loop, daemon=True)
        self._thread.start()

    # ---------------------------------------------------------------
    # Raw SIS Socket Server (System API — F.16)
    # ---------------------------------------------------------------

    def _start_sis_api(self) -> None:
        """Inicia o Raw SIS Socket Server num asyncio loop em thread separada."""
        if self._sis_loop is not None:
            return

        sis_port = 5066 if self.is_node_a else 5067
        self._sis_server = RawSisSocketServer(self.node, host="127.0.0.1", port=sis_port)

        self._sis_loop = asyncio.new_event_loop()
        self._sis_loop_thread = threading.Thread(
            target=self._run_sis_loop, daemon=True, name="SisApiLoop"
        )
        self._sis_loop_thread.start()
        self._log_msg("SYSTEM", f"System API (Raw SIS Socket) iniciando em 127.0.0.1:{sis_port}...", "system")

    def _run_sis_loop(self) -> None:
        """Entry point da thread do asyncio loop do SIS API."""
        asyncio.set_event_loop(self._sis_loop)
        self._sis_loop.run_until_complete(self._sis_api_main())

    async def _sis_api_main(self) -> None:
        """Coroutine principal do SIS API: inicia servidor e aguarda encerramento."""
        try:
            await self._sis_server.start()
            addr = self._sis_server._server.sockets[0].getsockname()
            self._event_queue.put(("log", f"System API escutando em {addr[0]}:{addr[1]}"))
            self.root.after(0, lambda: self.lbl_sys_api.config(
                text=f"API: :{addr[1]}", foreground="#4CAF50"
            ))
            # Mantém o servidor rodando até o loop ser parado
            await asyncio.get_event_loop().run_in_executor(None, self._thread_stop.wait)
        finally:
            await self._sis_server.stop()

    def _stop_sis_api(self) -> None:
        """Para o Raw SIS Socket Server."""
        if self._sis_loop is not None and not self._sis_loop.is_closed():
            self._sis_loop.call_soon_threadsafe(self._sis_loop.stop)
        self._sis_loop = None
        self._sis_server = None

    # ---------------------------------------------------------------
    # Node loop (background thread)
    # ---------------------------------------------------------------

    def _node_loop(self) -> None:
        while not self._thread_stop.is_set():
            t_ms = int(time.monotonic() * 1000)
            try:
                if self.node is not None:
                    self.node.tick(t_ms)
            except Exception as exc:
                self._event_queue.put(("error", f"Node tick error: {exc}"))
            time.sleep(0.2)

    # ---------------------------------------------------------------
    # SIS callbacks (chamados da thread de tick)
    # ---------------------------------------------------------------

    def _on_unidata_indication(self, indication: SisUnidataIndication) -> None:
        ts = time.strftime("%H:%M:%S")
        print(
            f"[{ts}] [ChatApp] unidata_indication: src={indication.src_addr} "
            f"sap={indication.src_sap}->{indication.dest_sap} len={len(indication.updu)}"
        )
        self._event_queue.put(("rx", indication))

    def _on_request_rejected(self, sap_id: int, reason: SisRejectReason) -> None:
        self._event_queue.put(("reject", (sap_id, reason)))

    def _on_hard_link_established(self, remote_addr: int, remote_sap: int) -> None:
        self._event_queue.put(("hard_up", (remote_addr, remote_sap)))

    def _on_hard_link_terminated(
        self, remote_addr: int, initiator_received_confirm: bool = False
    ) -> None:
        if initiator_received_confirm and self.node is not None:
            self.node.break_link()
            self._event_queue.put(
                ("log", f"Iniciador recebeu TERMINATE_CONFIRM (remote={remote_addr}) → break_link()")
            )
        elif not initiator_received_confirm and self.node is not None:
            if self.node.cas.state == CasLinkState.MADE:
                self.node.break_link()
                self._event_queue.put(
                    ("log", f"Remote recebeu TERMINATE (src={remote_addr}) → break_link()")
                )
        self._event_queue.put(("hard_down", remote_addr))

    # ---------------------------------------------------------------
    # UI update loop (main thread, 200ms)
    # ---------------------------------------------------------------

    def _update_ui_loop(self) -> None:
        if self.node is not None:
            self._update_status_labels()
            self._update_file_progress()

        # Drenar event queue
        while True:
            try:
                event_type, payload = self._event_queue.get_nowait()
            except queue.Empty:
                break
            self._handle_event(event_type, payload)

        self.root.after(200, self._update_ui_loop)

    def _update_status_labels(self) -> None:
        node = self.node
        if node is None:
            return

        # CAS state + transition logging
        cas_state = node.cas.state
        cas_name = cas_state.name
        cas_color = {
            CasLinkState.IDLE: "gray",
            CasLinkState.CALLING: "#FF9800",
            CasLinkState.MADE: "#4CAF50",
            CasLinkState.BREAKING: "#F44336",
        }.get(cas_state, "gray")
        self.lbl_cas.config(text=f"CAS: {cas_name}", foreground=cas_color)

        # Log CAS transitions para visibilidade do processo de hard link
        if self._prev_cas_ui_state != cas_state:
            prev = self._prev_cas_ui_state
            self._prev_cas_ui_state = cas_state
            if prev is not None:
                if cas_state == CasLinkState.CALLING:
                    self._log_msg("SYSTEM", "CAS: LINK_REQUEST enviado (camada 3)...", "system")
                elif cas_state == CasLinkState.MADE and prev == CasLinkState.CALLING:
                    self._log_msg("SYSTEM", "CAS: enlace MADE. Negociando hard link SIS (S_PDU tipo 3)...", "system")
                elif cas_state == CasLinkState.MADE and prev != CasLinkState.CALLING:
                    self._log_msg("SYSTEM", "CAS: enlace MADE (aceitou chamada remota).", "system")
                elif cas_state == CasLinkState.BREAKING:
                    self._log_msg("SYSTEM", "CAS: BREAKING...", "system")
                elif cas_state == CasLinkState.IDLE and prev in (CasLinkState.MADE, CasLinkState.BREAKING):
                    self._log_msg("SYSTEM", "CAS: enlace encerrado (IDLE).", "system")

        # SIS state
        sis_state = node._link_session.state.value
        sis_type = node._link_session.link_type.value
        self.lbl_sis.config(text=f"SIS: {sis_state}/{sis_type}")

        # DTS state
        dts_state = node._dts.state
        if dts_state.is_idle:
            dts_color = "gray"
        elif dts_state.is_data:
            dts_color = "#4CAF50"
        elif dts_state.is_management:
            dts_color = "#FF9800"
        elif dts_state.is_expedited:
            dts_color = "#2196F3"
        else:
            dts_color = "gray"
        self.lbl_dts.config(text=f"DTS: {dts_state.value}", foreground=dts_color)

        # ARQ state
        arq = node.arq
        arq_state = arq._tx_state.name
        self.lbl_arq.config(text=f"ARQ: {arq_state}")

        # ARQ window info (P3/P4: tx_uwe/tx_lwe dinâmicos)
        tx_q = len(arq._tx_queue)
        tx_win = len(arq._tx_window)
        self.lbl_arq_win.config(
            text=f"ARQ Win: {tx_win} | Q: {tx_q} | LWE:{arq._tx_lwe} UWE:{arq._tx_uwe}"
        )

        # RESET state (P2: repetição RESET com contagem por data rate)
        if arq.reset_pending:
            reps = repetition_count_for_rate(arq.data_rate_bps, arq.long_interleave)
            remaining = arq._reset_reps_remaining
            self.lbl_reset.config(
                text=f"RESET pend. (reps={reps}, rest={remaining})",
                foreground="#F44336",
            )
        elif hasattr(node, 'expedited_arq') and node.expedited_arq.has_pending_tx():
            self.lbl_reset.config(text="EXP-ARQ ativo", foreground="#2196F3")
        else:
            self.lbl_reset.config(text="", foreground="gray")

        # TX/RX counters
        self.lbl_pending.config(text=f"TX fila SIS: {len(node._tx_queue)}")
        self.lbl_rx.config(text=f"RX: {self._rx_count}")

        # Button state
        if cas_state == CasLinkState.IDLE:
            self.btn_connect.config(state="normal")
            self.btn_disconnect.config(state="disabled")
        elif cas_state == CasLinkState.MADE:
            self.btn_connect.config(state="disabled")
            self.btn_disconnect.config(state="normal")
        else:
            self.btn_connect.config(state="disabled")
            self.btn_disconnect.config(state="disabled")

    def _handle_event(self, event_type: str, payload: object) -> None:
        if event_type == "rx":
            indication = payload
            self._rx_count += 1
            raw = indication.updu
            print(
                f"[ChatApp UI] rx event: len={len(raw)} "
                f"head={raw[:20]!r}"
            )

            # Detectar chunks de arquivo
            if self._handle_file_rx(raw, indication):
                return

            # SAP 5 = HFCHAT (F.7): payload ASCII terminado em CRLF
            if indication.dest_sap == 5:
                text = raw.decode("ascii", errors="replace").rstrip("\r\n")
            else:
                text = raw.decode("utf-8", errors="replace")
            self._log_msg(
                f"Node {indication.src_addr} (SAP {indication.src_sap}→{indication.dest_sap}, prio={indication.priority})",
                text,
                "received",
            )
        elif event_type == "reject":
            sap_id, reason = payload
            self._log_msg("SYSTEM", f"SIS rejeitou envio (sap={sap_id}, reason={reason.name})", "error")
        elif event_type == "hard_up":
            remote_addr, remote_sap = payload
            self._log_msg("SYSTEM", f"SIS: Hard link CONFIRMADO com Node {remote_addr} (SAP={remote_sap}) ✓", "system")
        elif event_type == "hard_down":
            self._log_msg("SYSTEM", f"Hard link terminado com Node {payload}", "system")
        elif event_type == "log":
            self._log_msg("SYSTEM", payload, "system")
        elif event_type == "error":
            self._log_msg("ERRO", str(payload), "error")

    # ---------------------------------------------------------------
    # Recepção de arquivo
    # ---------------------------------------------------------------

    def _handle_file_rx(self, raw: bytes, indication) -> bool:
        """Trata chunks de arquivo recebidos. Retorna True se era chunk de arquivo."""
        # Protocolos de chunk: FILE: FALL: FCON: FEND: (prefixo ASCII antes do \x00)
        PREFIXES = (b"FILE:", b"FALL:", b"FCON:", b"FEND:")
        matched = False
        for pfx in PREFIXES:
            if raw.startswith(pfx):
                matched = True
                break
        if not matched:
            return False

        # Extrair nome do arquivo e dados binários após o \x00
        null_pos = raw.find(b"\x00")
        if null_pos < 0:
            return False
        try:
            header = raw[:null_pos].decode("utf-8")
        except UnicodeDecodeError:
            return False
        data = raw[null_pos + 1:]
        parts = header.split(":", 1)
        if len(parts) != 2:
            return False
        tag, filename = parts

        src_label = f"Node {indication.src_addr}"

        if tag == "FALL":
            # Arquivo completo em um chunk
            self._log_msg(
                src_label,
                f"Arquivo recebido: '{filename}' ({len(data)} bytes, 1 chunk)",
                "received",
            )
            self._save_received_file(filename, data)
            return True

        if tag == "FILE":
            # Primeiro chunk de arquivo multi-chunk
            self._file_rx_buffers[filename] = bytearray(data)
            self._log_msg(
                src_label,
                f"Recebendo arquivo '{filename}' — chunk 1 ({len(data)} bytes)...",
                "received",
            )
            return True

        if tag == "FCON":
            buf = self._file_rx_buffers.get(filename)
            if buf is not None:
                buf.extend(data)
                chunk_num = 1 + len(buf) // max(1, len(data)) if len(data) > 0 else "?"
                self._log_msg(
                    src_label,
                    f"Arquivo '{filename}' — chunk (acumulado: {len(buf)} bytes)",
                    "received",
                )
            else:
                self._file_rx_buffers[filename] = bytearray(data)
                self._log_msg(
                    src_label,
                    f"Arquivo '{filename}' — chunk continuação ({len(data)} bytes, sem header inicial)",
                    "received",
                )
            return True

        if tag == "FEND":
            buf = self._file_rx_buffers.pop(filename, bytearray())
            buf.extend(data)
            self._log_msg(
                src_label,
                f"Arquivo recebido: '{filename}' ({len(buf)} bytes completos)",
                "received",
            )
            self._save_received_file(filename, bytes(buf))
            return True

        return False

    def _save_received_file(self, filename: str, data: bytes) -> None:
        """Salva arquivo recebido na pasta 'recebidos/' ao lado do script."""
        save_dir = Path(__file__).resolve().parent / "recebidos"
        save_dir.mkdir(exist_ok=True)
        dest = save_dir / filename
        # Evitar sobrescrever: adicionar sufixo se já existir
        if dest.exists():
            stem, ext = dest.stem, dest.suffix
            counter = 1
            while dest.exists():
                dest = save_dir / f"{stem}_{counter}{ext}"
                counter += 1
        dest.write_bytes(data)
        self._log_msg("SYSTEM", f"Arquivo salvo em: {dest}", "system")

    # ---------------------------------------------------------------
    # Commands
    # ---------------------------------------------------------------

    def _cmd_connect(self) -> None:
        if self.node is None:
            self._log_msg("SYSTEM", "Nó não inicializado. Use 'Inicializar Nó' primeiro.", "error")
            return
        if self.node.cas.state != CasLinkState.IDLE:
            self._log_msg("SYSTEM", f"Não é possível conectar: CAS={self.node.cas.state.name}", "error")
            return

        src_sap = int(self.cmb_src_sap.get())
        dest_sap = int(self.cmb_dest_sap.get())
        self._log_msg("SYSTEM", f"Solicitando hard link → Node {self.remote_id} (SAP {src_sap}→{dest_sap})...", "system")
        self.node.hard_link_establish(
            sap_id=src_sap,
            link_priority=15,
            remote_addr=self.remote_id,
            remote_sap=dest_sap,
        )

    def _cmd_disconnect(self) -> None:
        if self.node is None:
            return
        src_sap = int(self.cmb_src_sap.get())
        self._log_msg("SYSTEM", "Solicitando terminação de hard link...", "system")
        self.node.hard_link_terminate(sap_id=src_sap, remote_addr=self.remote_id)

    def _build_delivery_mode(self) -> DeliveryMode:
        mode_name = self.cmb_mode.get().strip().upper()
        if mode_name == "NON_ARQ":
            return DeliveryMode(arq_mode=False, expedited=False)
        if mode_name == "EXP_NON_ARQ":
            return DeliveryMode(arq_mode=False, expedited=True)
        if mode_name == "EXP_ARQ":
            return DeliveryMode(arq_mode=True, expedited=True)
        return DeliveryMode(arq_mode=True, expedited=False)

    def _cmd_send(self) -> None:
        text = self.entry_msg.get().strip()
        if not text:
            return
        if self.node is None:
            self._log_msg("SYSTEM", "Nó não inicializado.", "error")
            return

        try:
            src_sap = int(self.cmb_src_sap.get())
            dest_sap = int(self.cmb_dest_sap.get())
            priority = int(self.spn_priority.get())
            ttl_seconds = float(self.ent_ttl.get())
        except ValueError:
            self._log_msg("SYSTEM", "Parâmetros inválidos: SAP/prioridade/TTL.", "error")
            return

        priority = max(0, min(15, priority))
        if ttl_seconds < 0:
            ttl_seconds = 0

        # SAP 5 = HFCHAT (F.7): payload ASCII puro terminado em CRLF
        if dest_sap == 5:
            payload = text.encode("ascii", errors="replace") + b"\r\n"
        else:
            payload = text.encode("utf-8")
        mode = self._build_delivery_mode()

        if mode.arq_mode and mode.expedited:
            self.node.expedited_unidata_request(
                sap_id=src_sap,
                dest_addr=self.remote_id,
                dest_sap=dest_sap,
                ttl_seconds=ttl_seconds,
                updu=payload,
            )
        else:
            self.node.unidata_request(
                sap_id=src_sap,
                dest_addr=self.remote_id,
                dest_sap=dest_sap,
                priority=priority,
                ttl_seconds=ttl_seconds,
                mode=mode,
                updu=payload,
            )
        self._tx_count += 1
        mode_str = self.cmb_mode.get()
        self._log_msg(
            "Você",
            f"{text}  [SAP {src_sap}→{dest_sap} | prio={priority} | {mode_str}]",
            "sent",
        )
        self.entry_msg.delete(0, tk.END)

    # ---------------------------------------------------------------
    # Envio de arquivo em burst
    # ---------------------------------------------------------------

    def _cmd_send_file(self) -> None:
        if self.node is None:
            self._log_msg("SYSTEM", "Nó não inicializado.", "error")
            return

        filepath = filedialog.askopenfilename(
            title="Selecionar arquivo para envio",
            filetypes=[("Todos", "*.*"), ("Texto", "*.txt"), ("Binário", "*.bin")],
        )
        if not filepath:
            return

        try:
            with open(filepath, "rb") as f:
                file_data = f.read()
        except Exception as exc:
            self._log_msg("SYSTEM", f"Erro ao ler arquivo: {exc}", "error")
            return

        filename = os.path.basename(filepath)
        file_size = len(file_data)

        if file_size == 0:
            self._log_msg("SYSTEM", "Arquivo vazio, nada a enviar.", "error")
            return

        # Parâmetros de envio
        try:
            src_sap = int(self.cmb_src_sap.get())
            dest_sap = int(self.cmb_dest_sap.get())
            priority = max(0, min(15, int(self.spn_priority.get())))
            ttl_seconds = max(0.0, float(self.ent_ttl.get()))
        except ValueError:
            self._log_msg("SYSTEM", "Parâmetros inválidos.", "error")
            return

        mode = self._build_delivery_mode()

        # Determinar tamanho do chunk (MTU SIS menos overhead do header de arquivo)
        # Header: "FILE:<filename>\x00" prefixado no primeiro chunk,
        # demais chunks: "FCON:<filename>\x00" para continuação
        header_prefix = f"FILE:{filename}\x00".encode("utf-8")
        cont_prefix = f"FCON:{filename}\x00".encode("utf-8")

        # MTU = máximo de bytes de dados de usuário por S_PRIMITIVE (SIS)
        mtu = getattr(self.node, "_max_user_data_bytes", 128)
        chunk_data_size = mtu - len(cont_prefix)
        if chunk_data_size <= 0:
            chunk_data_size = 128

        # Quebrar arquivo em chunks
        chunks: list[bytes] = []
        offset = 0
        while offset < file_size:
            if offset == 0:
                pfx = header_prefix
                data_space = mtu - len(pfx)
            else:
                pfx = cont_prefix
                data_space = chunk_data_size
            chunk = file_data[offset : offset + data_space]
            # Último chunk: marcar com "FEND" em vez de "FCON"
            if offset + len(chunk) >= file_size and offset > 0:
                pfx = f"FEND:{filename}\x00".encode("utf-8")
            elif offset == 0 and len(chunk) >= file_size:
                # Arquivo cabe em 1 chunk: usar header especial
                pfx = f"FALL:{filename}\x00".encode("utf-8")
            chunks.append(pfx + chunk)
            offset += len(chunk)

        total_chunks = len(chunks)

        self._file_tx_active = True
        self._file_tx_name = filename
        self._file_tx_total_chunks = total_chunks
        self._file_tx_sent_chunks = 0
        self._file_tx_total_bytes = file_size
        self._file_tx_start_time = time.monotonic()
        self._file_tx_retx_accumulated = 0
        self._file_tx_retx_last_snapshot = 0

        # Exibir frame de progresso
        self.file_progress_frame.pack(fill=tk.X, pady=(0, 4),
                                       before=self.input_frame)
        self.file_progressbar["maximum"] = total_chunks
        self.file_progressbar["value"] = 0
        self.lbl_file_info.config(
            text=f"Arquivo: {filename} | {file_size} bytes | {total_chunks} chunks | Modo: {self.cmb_mode.get()}"
        )
        self.lbl_file_progress.config(text="Enfileirando chunks...")

        self._log_msg(
            "SYSTEM",
            f"Enviando arquivo '{filename}' ({file_size} bytes) em {total_chunks} chunk(s), "
            f"modo={self.cmb_mode.get()}, MTU={mtu}",
            "system",
        )

        # Enfileirar todos os chunks via SIS (burst)
        for i, chunk_payload in enumerate(chunks):
            if mode.arq_mode and mode.expedited:
                self.node.expedited_unidata_request(
                    sap_id=src_sap,
                    dest_addr=self.remote_id,
                    dest_sap=dest_sap,
                    ttl_seconds=ttl_seconds,
                    updu=chunk_payload,
                )
            else:
                self.node.unidata_request(
                    sap_id=src_sap,
                    dest_addr=self.remote_id,
                    dest_sap=dest_sap,
                    priority=priority,
                    ttl_seconds=ttl_seconds,
                    mode=mode,
                    updu=chunk_payload,
                )
            self._file_tx_sent_chunks = i + 1

        self._log_msg(
            "SYSTEM",
            f"Todos os {total_chunks} chunks enfileirados no SIS. Aguardando transmissão ARQ...",
            "system",
        )

    def _update_file_progress(self) -> None:
        """Atualiza indicadores de progresso de envio de arquivo."""
        if not self._file_tx_active or self.node is None:
            return

        arq = self.node.arq
        sis_queue = len(self.node._tx_queue)

        # Contadores ARQ — excluir frames já ACKED da contagem pendente
        tx_window_pending = sum(
            1 for slot in arq._tx_window.values()
            if slot.status != 1  # AckStatus.ACKED = 1
        )
        tx_queue_count = len(arq._tx_queue)
        total_pending = sis_queue + tx_queue_count + tx_window_pending
        tx_window_count = len(arq._tx_window)

        # Log apenas quando estado muda (evita spam a cada 200ms)
        progress_key = (sis_queue, tx_queue_count, tx_window_count, tx_window_pending, total_pending)
        if not hasattr(self, '_file_tx_last_progress_key') or self._file_tx_last_progress_key != progress_key:
            self._file_tx_last_progress_key = progress_key
            print(
                f"[FileProgress] sis_q={sis_queue} arq_q={tx_queue_count} "
                f"arq_win={tx_window_count} arq_win_pend={tx_window_pending} "
                f"tx_lwe={arq._tx_lwe} next_seq={arq._next_seq} tx_count={arq._tx_count} "
                f"tx_state={arq._tx_state.name} total_pend={total_pending} "
                f"sent={self._file_tx_sent_chunks}/{self._file_tx_total_chunks}"
            )

        # Retransmissões: acumular antes que a janela seja limpa pelo ACK
        current_retx = sum(slot.retx_count for slot in arq._tx_window.values())
        if current_retx < self._file_tx_retx_last_snapshot:
            # Janela foi limpa (ACK recebido) — acumular o que havia
            self._file_tx_retx_accumulated += self._file_tx_retx_last_snapshot
        self._file_tx_retx_last_snapshot = current_retx
        retx_total = self._file_tx_retx_accumulated + current_retx

        # Frames aguardando ACK vs NACK
        frames_wait_ack = sum(
            1 for slot in arq._tx_window.values()
            if slot.status == 3  # SENT_WAIT_ACK
        )
        frames_nacked = sum(
            1 for slot in arq._tx_window.values()
            if slot.status == 2  # NACKED
        )

        # Progresso: chunks "resolvidos" = total - pendentes
        resolved = max(0, self._file_tx_total_chunks - total_pending)
        self.file_progressbar["value"] = resolved

        elapsed = time.monotonic() - self._file_tx_start_time
        elapsed_str = f"{elapsed:.1f}s"

        frames_acked = sum(
            1 for slot in arq._tx_window.values()
            if slot.status == 1  # ACKED
        )
        self.lbl_file_progress.config(
            text=(
                f"Progresso: {resolved}/{self._file_tx_total_chunks} chunks | "
                f"SIS fila: {sis_queue} | ARQ fila: {tx_queue_count} | "
                f"ARQ janela: {tx_window_count} (pend: {frames_wait_ack}, NACK: {frames_nacked}, ACK'd: {frames_acked}) | "
                f"Retx: {retx_total} | Tempo: {elapsed_str}"
            )
        )

        # Verificar se envio terminou
        if total_pending == 0 and self._file_tx_sent_chunks >= self._file_tx_total_chunks:
            self._file_tx_active = False
            throughput = self._file_tx_total_bytes / elapsed if elapsed > 0 else 0
            self.lbl_file_progress.config(
                text=(
                    f"Concluído: {self._file_tx_total_chunks} chunks em {elapsed_str} | "
                    f"{self._file_tx_total_bytes} bytes | "
                    f"Throughput: {throughput:.0f} B/s ({throughput*8:.0f} bps) | "
                    f"Retransmissões: {retx_total}"
                )
            )
            self.file_progressbar["value"] = self._file_tx_total_chunks
            self._log_msg(
                "SYSTEM",
                f"Transferência concluída: '{self._file_tx_name}' | "
                f"{self._file_tx_total_bytes} bytes em {self._file_tx_total_chunks} chunk(s) | "
                f"Tempo: {elapsed_str} | Throughput: {throughput:.0f} B/s ({throughput*8:.0f} bps) | "
                f"Retransmissões ARQ: {retx_total}",
                "system",
            )

    # ---------------------------------------------------------------
    # Cleanup
    # ---------------------------------------------------------------

    def on_closing(self) -> None:
        self._thread_stop.set()
        self._stop_sis_api()
        if self.node is not None and self.node.modem is not None:
            self.node.modem.modem_rx_stop()
        self.root.destroy()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="STANAG 5066 HF Chat App (Fase 4 SIS + Client API)",
    )
    parser.add_argument(
        "--node", choices=["A", "B"], required=True,
        help="Identidade do nó (A ou B)",
    )
    args = parser.parse_args()

    root = tk.Tk()
    app = ChatApp(root, args.node)
    root.protocol("WM_DELETE_WINDOW", app.on_closing)
    root.mainloop()


if __name__ == "__main__":
    main()
