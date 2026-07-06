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
| SECTIONS | **Subnet Dashboard** | KPIs, ligações/peers, qualidade, SAPs, filas TX/RX, S-Primitives | demo |
| SECTIONS | **Traffic Monitor** | contadores, alocação de SAP (Anexo F Tabela F-1), log de eventos | demo |
| SIS CLIENTS | **HFCHAT Orderwire** (SAP 5) | operadores, *thread*, *feed* de primitivas | demo |
| SIS CLIENTS | **HF Mail** (HMTP 3 / HFPOP 4) | caixas, leitura/composição, *pipelining* HMTP | demo |
| SIS CLIENTS | **IP Client** (SAP 9) | binding, QoS, rotas IP→STANAG, log de datagramas | demo |
| SIS CLIENTS | **File Transfer** (RCOP 6 / UDOP 7) | compositor, fila, log de primitivas | demo |
| SIS CLIENTS | **Raw SIS Socket** (F.16) | parâmetros, clientes ligados, *wire log* | demo |
| SETUP | **Modem Link** (110C no design / 110D real) | ligação, taxa, interleaver | **✅ live** |
| SETUP | **Configuration** | servidor SIS, requisitos de serviço por cliente | demo |

## 4. Arquitetura

```
subnet_console/
├── __main__.py        # argparse (--live/--bitrate/--interleaver, …) + bootstrap
├── app.py             # QApplication, fontes, run() — cria NodeController se --live
├── theme.py           # tokens de design: cores, accent, tint(), fontes
├── model.py           # ConsoleModel — estado + dados demo + sinais + seam live
├── window.py          # SubnetConsoleWindow (frameless): chrome + QStackedWidget
├── backend/
│   └── node_controller.py  # NodeController — nó STANAG 5066 real atrás de sinais Qt
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
dts, arq_state, arq_window, arq_queue, arq_lwe, arq_uwe, reset_pending, tx_queue`.

## 6. Estado e roteiro da Fase 2 (atividades)

Fatiar por ecrã, sempre verificando *headless* contra `tests/mock_110d_modem`
(um nó, ou dois nós A/B com ar cruzado via `mock_110d_air.py` para RX real).

- [x] **Fatia 1 — Modem Link ao vivo** *(feito, commit `fd668c0`)*
      NodeController + flag `--live`; painel Modem, pill do toolbar e dot da sidebar
      refletem `is_connected`/taxa reais; botão Connect/Disconnect arranca/desliga o nó.
- [ ] **Fatia 2 — HFCHAT (SAP 5)** *(recomendada a seguir)*
      Connect/Disconnect = `controller.hard_link_establish(5, 5)` / `hard_link_terminate(5)`;
      enviar = `controller.send_unidata(5, 5, texto.encode('ascii')+b'\r\n', mode=DeliveryMode(arq_mode=True))`;
      receber = ligar `unidata_received` (SAP 5, decodificar ASCII, tirar CRLF) → *append* à
      thread; alimentar o *feed* de S-primitives a partir dos sinais. É o que torna o
      `run_110d_real.sh` num teste de chat de ponta a ponta.
- [ ] **Fatia 3 — Dashboard + Traffic Monitor + status bar**
      Usar o snapshot de `status()` (CAS/SIS/DTS/ARQ, `tx_queue`); manter um log de
      eventos S-primitive em memória alimentado pelos sinais e mostrá-lo no monitor.
- [ ] **Fatia 4 — File Transfer (RCOP 6 / UDOP 7)**
      Fatiar ficheiro em chunks com o protocolo `FILE:/FCON:/FEND:/FALL:` (ver
      `chat_app_110d._send_file`), RCOP→ARQ / UDOP→non-ARQ, progresso na fila e log.
- [ ] **Fatia 5 — Raw SIS Socket (F.16)**
      Correr `RawSisSocketServer(node, "127.0.0.1", 5066/5067)` num loop asyncio em
      thread própria (ver `chat_app_110d._start_sis_api`); listar clientes/wire log reais.
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
- O relógio do *status bar* é UTC ao vivo (QTimer 1 s).
