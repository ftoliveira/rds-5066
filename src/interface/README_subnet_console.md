# S5066 Subnet Console (PyQt6)

`subnet_console/` — uma interface **Qt (PyQt6)** que reúne, numa única janela, todos
os clientes SIS de um nó **STANAG 5066** ("Annex F Client Manager"): dashboard da
sub-rede, monitor de tráfego, HFCHAT, HF Mail, IP Client, transferência de ficheiros
(RCOP/UDOP), Raw SIS Socket, ligação ao modem MIL-STD-188-110D e configuração.

É a transposição fiel do design **"S5066 Subnet Console"** (projeto Claude Design
*Qt interface para STANAG-5066*) para PyQt6. Complementa — não substitui — os apps
Tkinter existentes (`chat_app*.py`); o `chat_app_110d.py` continua a ser a referência
fiel do *data path* completo e o guia para ligar as próximas telas ao vivo.

```
┌──────────────────────────────────────────────────────────────────────────┐
│ ● ● ●   STANAG 5066 Subnet Console — Annex F Client Manager · HQ Node  v3.0│  ← title bar
├──────────────────────────────────────────────────────────────────────────┤
│ File  Subnet  Clients  Tools  View  Help                                   │  ← menu
├──────────────────────────────────────────────────────────────────────────┤
│ ● MODEM LINKED │ BIND ALL │ Hard Link │ Broadcast      FREQ MODE RATE SNR  │  ← toolbar
├──────────────┬─────────────────────────────────────────────────────────────┤
│ LOCAL NODE   │                                                             │
│ FALCON-01    │   (conteúdo do ecrã seleccionado)                           │
│ SECTIONS     │                                                             │
│  Dashboard   │                                                             │
│ SIS CLIENTS  │                                                             │
│  HFCHAT …    │                                                             │
│ SETUP        │                                                             │
│  Modem/Config│                                                             │
├──────────────┴─────────────────────────────────────────────────────────────┤
│ ● SIS 127.0.0.1:5066 LISTENING │ 5 CLIENTS BOUND │ TX 14 · RX 2 …   14:22 UTC│  ← status
└──────────────────────────────────────────────────────────────────────────┘
```

> **Estado:** Fase 1 (UI completa, dados de demonstração) **concluída**. Fase 2
> (ligação ao backend real) **em progresso** — ver §6. Branch: `feat/qt-subnet-console`.

---

## 1. Requisitos

- Python 3.11+ e **PyQt6** (`pip install PyQt6`) — já presente neste ambiente.
- Display gráfico (X11/Wayland; no WSL, WSLg).
- Para o modo `--live`: acesso a um modem 110D (Appendix A/TCP) real **ou** ao mock
  local (`tests/mock_110d_modem.py`, lançado por `src/interface/mock_110d_air.py`).
- *(Opcional)* tipos de letra **IBM Plex Sans/Mono** para fidelidade total ao design.
  Sem eles usa-se um *fallback* limpo (DejaVu/Segoe/Ubuntu). Para os embutir, largue
  os `*.ttf` em `subnet_console/assets/fonts/` — são carregados automaticamente.

## 2. Como executar

```bash
# Modo DEMO (default, Fase 1 — dados de demonstração, sem backend)
python -m src.interface.subnet_console
python src/interface/subnet_console/__main__.py        # equivalente (via ficheiro)

# Modo LIVE (Fase 2 — arranca um StanagNode real ligado ao modem)
python src/interface/mock_110d_air.py                  # 2 modems mock (portas 3000/3001)
python -m src.interface.subnet_console --live --node A  # nó A → modem :3000
python -m src.interface.subnet_console --live --node B  # nó B → modem :3001

# Contra modem real (via túnel SSH): o script já passa --live
scripts/run_110d_real.sh <SN>
```

Opções de linha de comando:

| Flag | Valores | Efeito |
|------|---------|--------|
| `--node` | `A` \| `B` | identidade do nó (A: local 1/remote 2/modem 3000; B: 2/1/3001) |
| `--accent` | `blue` `green` `purple` `orange` `gray` | cor de destaque do tema |
| `--modem-host` | IP | alvo do modem (default `127.0.0.1`) |
| `--modem-port` | porta | porta TCP do modem (default 3000/3001 por nó) |
| `--live` | — | liga a um nó STANAG 5066 real em vez de usar dados de demo |
| `--bitrate` | bps | taxa inicial em modo `--live` (default 2400) |
| `--interleaver` | `short` \| `long` | interleaver inicial em `--live` (default `long`) |

## 3. Ecrãs

| Secção | Ecrã | Conteúdo | Ao vivo? |
|--------|------|----------|:--------:|
| SECTIONS | **Subnet Dashboard** | KPIs, peer/link, métricas de ligação, SAPs, filas, S-Primitives | **✅ live** |
| SECTIONS | **Traffic Monitor** | contadores, alocação de SAP (Anexo F Tabela F-1), event log | **✅ live** |
| SIS CLIENTS | **HFCHAT Orderwire** (SAP 5) | operadores (demo), *thread* + *feed* ao vivo, hard link | **✅ live** |
| SIS CLIENTS | **HF Mail** (HMTP 3 / HFPOP 4) | caixas, leitura/composição, *pipelining* HMTP | demo |
| SIS CLIENTS | **IP Client** (SAP 9) | binding, QoS, rotas IP→STANAG, log de datagramas | demo |
| SIS CLIENTS | **File Transfer** (RCOP 6 / UDOP 7) | chunking FILE/FCON/FEND/FALL, envio, RX+reassembly, progresso | **✅ live** |
| SIS CLIENTS | **Raw SIS Socket** (F.16) | parâmetros, clientes ligados, *wire log* | **✅ live** |
| SETUP | **Modem Link** (110C no design / 110D real) | ligação, taxa, interleaver | **✅ live** |
| SETUP | **Configuration** | servidor SIS; requisitos HFCHAT (ARQ/prio/in-order/confirm) ligados ao envio | **✅ HFCHAT live** |

> **Modo LIVE arranca DESCONECTADO:** o painel Modem mostra OFFLINE e liga-se com
> **Connect Modem** (o botão arranca o `NodeController`). No separador **Configuration →
> HFCHAT · SAP 5**, os controlos Transmission Mode (ARQ/non-ARQ), Delivery Confirmation,
> Deliver In Order e Traffic Priority editam um *rascunho*; **Apply && Rebind** confirma-o e
> os envios HFCHAT seguintes usam esses argumentos no `S_UNIDATA_REQUEST`; **Revert** descarta.
>
> **Raw SIS Socket (F.16):** o servidor de socket TCP arranca/pára junto do nó (com o
> **Connect/Disconnect Modem**) em `127.0.0.1:5066` (nó A) / `:5067` (nó B). Clientes SIS
> externos podem então ligar-se e fazer bind de SAPs por TCP; o ecrã **Raw SIS Socket** mostra
> os clientes ligados e o *wire log* ao vivo. Até ligar o modem o ecrã mostra **SERVER OFFLINE**.

## 4. Arquitetura

```
subnet_console/
├── __main__.py        # argparse (--live/--bitrate/--interleaver, …) + bootstrap
├── app.py             # QApplication, fontes, run() — cria NodeController se --live
├── theme.py           # tokens de design: cores, accent, tint(), fontes
├── model.py           # ConsoleModel — estado + dados demo + sinais + seam live
├── window.py          # SubnetConsoleWindow (frameless): chrome + QStackedWidget
├── backend/
│   ├── node_controller.py  # NodeController — nó STANAG 5066 real atrás de sinais Qt
│   └── sis_server.py       # InstrumentedSisServer — RawSisSocketServer + wire log/roster
└── widgets/
    ├── common.py      # primitivas reutilizáveis (Card, KpiTile, Table, pill, …)
    ├── titlebar/menubar/toolbar/sidebar/statusbar.py   # chrome
    └── screens/       # base.py + os 9 ecrãs
```

O ponto central é o **`ConsoleModel`**: expõe *view accessors* (`links()`,
`sap_table()`, `mail_view()`, `modem_view()`, …) que devolvem dicionários com as
cores já resolvidas contra o tema. Os ecrãs são pura disposição visual e
reconstroem-se quando o modelo emite `changed(<tópico>)` ou `accent_changed`. Edições
de texto usam *setters* silenciosos para não perder o foco do campo.

**Seam de dados ao vivo:** `ConsoleModel(controller=…)` ativa `self.live`. Um
`NodeController.status_changed` alimenta `model.apply_live_status(snap)`, que repinta
apenas quando um campo visível muda (evita rebuild sob o cursor). Os accessors
ligados ramificam em `self.live` (ex.: `modem_view()` mostra LINKED/CONNECTING/OFFLINE
reais; `toggle_modem()` arranca/desliga o nó). **Padrão para ligar mais telas: ver §6.**

## 5. NodeController — API (Fase 2)

`backend/node_controller.py` arranca e opera um `StanagNode` real sobre
`Tcp110dModemAdapter` (110D Appendix A/TCP), tal como `chat_app_110d`: thread de tick
a 200 ms, callbacks SIS a emitir sinais Qt, e um `QTimer` (500 ms) que faz *poll* do
estado na thread da GUI. Já expõe **tudo** o que as próximas fatias precisam:

**Sinais** (entregues na thread da GUI):
| Sinal | Payload | Uso |
|-------|---------|-----|
| `status_changed(dict)` | snapshot (ver abaixo) | poll periódico → `model.apply_live_status` |
| `unidata_received(dict)` | `{sap, src_addr, src_sap, priority, text, updu}` | RX de HFCHAT/IP/ficheiros |
| `link_established(int,int)` | `remote_addr, remote_sap` | hard link estabelecido |
| `link_terminated(int,bool)` | `remote_addr, confirm` | hard link terminado (faz CAS break) |
| `request_rejected(int,str)` | `sap_id, motivo` | envio rejeitado |
| `node_error(str)` | mensagem | erro no tick |

**Comandos:** `start()/stop()`, `set_rate(bps)`, `set_interleaver(v)`,
`hard_link_establish(sap, dest_sap, priority=15)`, `hard_link_terminate(sap)`,
`send_unidata(sap, dest_sap, payload, priority=4, ttl_seconds=0.0, mode=DeliveryMode)`.

**`status()` → dict:** `running, connected, rate, blocking, cas, sis_state, sis_type,
dts, arq_state, arq_window, arq_unacked, arq_queue, arq_lwe, arq_uwe, reset_pending,
tx_queue`, e (Fatia 5) `sis_server_running, sis_server_host, sis_server_port,
sis_max_clients, sis_clients, sis_wire, sis_prim_count`. (`arq_unacked` = frames em voo
ainda não confirmados — o progresso de ficheiros usa-o para a fila drenar a zero. SAPs
ligados por omissão: `(3, 5, 6, 7)`. O Raw SIS Socket Server arranca junto do nó em
`start()` e fecha em `stop()`.)

## 6. Estado e roteiro da Fase 2 (atividades)

Fatiar por ecrã, sempre verificando *headless* contra `tests/mock_110d_modem`
(um nó, ou dois nós A/B com ar cruzado via `mock_110d_air.py` para RX real).

- [x] **Fatia 1 — Modem Link ao vivo** *(feito, commit `fd668c0`)*
      NodeController + flag `--live`; painel Modem, pill do toolbar e dot da sidebar
      refletem `is_connected`/taxa reais; botão Connect/Disconnect arranca/desliga o nó.
- [x] **Fatia 2 — HFCHAT (SAP 5)** *(feito)*
      `app.run()` liga `unidata_received`/`link_established`/`link_terminated`/`request_rejected`
      aos slots `model.on_rx`/`on_link_up`/`on_link_down`/`on_rejected`. Botão **Establish/Terminate
      Link** no cabeçalho da *thread* = `controller.hard_link_establish(5, 5)` /
      `hard_link_terminate(5)`; enviar = `controller.send_unidata(5, 5, texto+CRLF,
      mode=DeliveryMode(arq_mode=True))`; RX SAP 5 decodifica ASCII, tira CRLF e faz *append* à
      *thread* ao vivo; o *feed* de S-primitives (`chat_prims()`) passa a ser real. Os accessors
      `chat_messages()`/`chat_prims()`/`chat_header()` ramificam em `self.live`. Verificado ponta a
      ponta em `tests/test_subnet_console_chat.py` (dois nós via `MockAir`, offscreen). É o que torna
      o `run_110d_real.sh` num teste de chat de ponta a ponta.
- [x] **Fatia 3 — Dashboard + Traffic Monitor + status bar** *(feito)*
      Log único de eventos S-primitive em memória (`model.live_events`, cap 200) alimentado
      pelos mesmos sinais da Fatia 2 — é a fonte única do *feed* do chat (`chat_prims`), do
      *event log* do monitor (`event_log`) e das "recent primitives" do dashboard (`dash_prims`).
      O snapshot de `status()` (CAS/SIS/DTS/ARQ/`tx_queue`/`arq_window`/`blocking`) passa a
      alimentar `dashboard_kpis`/`counters`/`quality`/`queues`/`links`/`sap_table` e a barra de
      estado (`statusbar_view`); contadores `live_tx`/`live_rx`/`live_rejected` agregam o tráfego.
      `apply_live_status` repinta dashboard/monitor/statusbar quando um campo visível muda; os
      ecrãs ganharam `topics={"dashboard"}`/`{"monitor"}`. Coberto por `test_subnet_console_chat.py`.
- [x] **Fatia 4 — File Transfer (RCOP 6 / UDOP 7)** *(feito)*
      `NodeController` liga também os SAPs 6/7 (`bound_saps=(3,5,6,7)`) e expõe
      `max_user_data_bytes`. `send_ft` (live) fatia cada ficheiro com o protocolo
      `FILE:/FCON:/FEND:/FALL:<nome>\x00<dados>` (blocos MTU, ver `_chunk_file`), enfileira via
      `send_unidata` em SAP 6 (RCOP → `DeliveryMode(arq_mode=True)`) ou 7 (UDOP → non-ARQ). O
      progresso vem do esvaziamento da fila (`tx_queue`+`arq_queue`+`arq_unacked` no snapshot,
      repartido FIFO pelos jobs em `_update_ft_progress`). `on_rx` deteta os prefixos e
      reassembla (`_handle_ft_rx` → `ft_received`), mostrando o ficheiro recebido na fila e o
      log RCOP/UDOP (`ft_events`). Verificado ponta a ponta (UDOP e RCOP c/ hard link) em
      `tests/test_subnet_console_filexfer.py`.
- [x] **Fatia 5 — Raw SIS Socket (F.16)** *(feito)*
      `backend/sis_server.py` traz `InstrumentedSisServer` (subclasse de
      `RawSisSocketServer`) que grava o *wire log* (ambas as direções, via os *hooks*
      `_dispatch_primitive`/`_send_raw`) e o *roster* de clientes TCP (socket remoto, SAP
      ligado, rank, hora de ligação). O `NodeController.start()` arranca-o num loop asyncio
      em thread própria (`_start_sis_server`, espelha `chat_app_110d._start_sis_api`) em
      `127.0.0.1:5066` (nó A) / `5067` (nó B) — `sis_port=0` dá porta efémera nos testes; o
      `stop()` fecha-o limpo (evento + `server.stop()`). O snapshot de `status()` ganha
      `sis_server_running/host/port`, `sis_clients`, `sis_wire`, `sis_prim_count`;
      `apply_live_status` repinta `sissocket` quando o servidor liga/desliga, um cliente
      liga/faz bind/sai, ou uma primitiva nova cruza o fio. Os accessors
      `sk_status/sk_kpis/sk_server/sk_clients/sk_wire` ramificam em `self.live`. *Thread-safety*
      sem *locks*: `deque.append` + `list(...)` e cópias atómicas de dict (ver docstring do
      módulo). Verificado ponta a ponta (cliente TCP real → bind SAP 9 → aparece na consola +
      wire log) em `tests/test_subnet_console_sissocket.py`.
- [ ] **Fatia 6 — IP Client (SAP 9) + HF Mail (HMTP 3/HFPOP 4)**
      Ligar os clientes `annex_f/*` (`ip_client`, `hmtp`, `hf_pop3`); maior esforço,
      menor prioridade.

### Padrão para ligar um ecrã ao vivo

1. **`app.run()`** já cria o `controller` e liga `status_changed → model.apply_live_status`.
   Ligar aqui os sinais extra da fatia (ex.: `controller.unidata_received.connect(model.on_rx)`).
2. **`model.py`**: guardar o estado live novo (ex.: lista de mensagens recebidas), fazer
   o accessor ramificar em `self.live` (ex.: `chat_messages()` devolve o real), e emitir
   `changed('<tópico>')` para repintar. Comandos do ecrã chamam `self.controller.*`.
3. **Ecrã**: nada a mudar na navegação — já reconstrói em `changed('<tópico>')`.
4. **Verificar** *headless* com o mock (padrão do teste da Fatia 1: iniciar `MockModem110d`,
   criar `QApplication`, `NodeController`, `ConsoleModel(controller=…)`, `controller.start()`,
   bombear `app.processEvents()` num loop e asserir o resultado).

Referência canónica de todo o *data path* (bind, hard link, envio, RX, chunks de
ficheiro, servidor SIS): **`src/interface/chat_app_110d.py`**.

## 7. Notas de implementação

- Janela **sem moldura** (frameless): a barra de título tem os "semáforos" funcionais
  (fechar / minimizar / maximizar) e permite arrastar; o *status bar* tem *size grip*.
- **QLabel é subclasse de QFrame** — qualquer estilo `QFrame{…}` com borda aplicado a um
  cartão "vazava" para os rótulos filhos. O *helper* `common.scoped()` isola cada regra ao
  seu *objectName*; use-o sempre que estilizar um `QFrame` com borda/fundo.
- **Threading (modo live):** o nó é passado por uma thread daemon (`node.tick()` a 200 ms);
  os callbacks SIS correm nessa thread e **só** emitem sinais Qt (entregues à GUI por
  *queued connection*). Leituras de estado acontecem na GUI via `QTimer` (500 ms). Nunca
  ler/alterar widgets a partir da thread do nó.
- **Raw SIS Socket Server (F.16):** corre num **segundo** thread daemon — um `asyncio` *event
  loop* próprio (`NodeController._start_sis_server`/`_sis_main`, espelha
  `chat_app_110d._start_sis_api`), separado do thread de tick. A paragem é limpa via um
  `threading.Event` (`_sis_thread_stop`) que desbloqueia o `run_in_executor` e deixa o
  `finally` correr `await server.stop()`. Porta 0 = efémera (testes); a real lê-se de
  `server._server.sockets[0].getsockname()`. O `InstrumentedSisServer` observa o servidor
  **sem locks**: o *wire log* é um `deque` (append no thread asyncio, `list(...)` na GUI, ambos
  atómicos no CPython) e o *roster* de clientes usa `list(self._connections.items())` (uma só
  chamada C, sem soltar o GIL); o mapa de horas de ligação só é tocado na GUI. Como o servidor
  liga assincronamente, o ecrã só passa a **LISTENING** no poll seguinte (~500 ms após Connect).
- O relógio do *status bar* é UTC ao vivo (QTimer 1 s).
- **Rebuild sob o cursor:** um `ClickableFrame` cujo clique dispara `changed(<tópico>)`
  reconstrói o próprio ecrã — e o `QScrollArea.setWidget()` apagaria o widget clicado a
  meio do seu `mouseReleaseEvent` (crash `wrapped C/C++ object … has been deleted`). Por
  isso `Screen.rebuild` destaca o conteúdo antigo com `takeWidget()` + `deleteLater()`
  (deleção diferida) e o `ClickableFrame` corre o handler base **antes** de emitir, deixando
  `clicked.emit()` por último. Ao ligar novos controlos clicáveis que repintam o ecrã, conte
  com este padrão (nunca toque em `self` depois de emitir).
