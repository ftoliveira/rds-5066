# Plano de Implementação — Integração STANAG 5066 ↔ MIL-STD-188-110D (Appendix A / LAN / TCP)

**Status:** implementado (Fases 0–4) — falta apenas o teste vivo contra o `rds-hf` real (§11.3, manual)
**Data:** 2026-06-24

## Status de implementação

| Fase | Entregável | Onde | Testes |
|------|-----------|------|--------|
| 0 | Codec puro + framing | `src/modem/appendix_a_codec.py`, `src/modem/dpdu_framing.py` | `tests/test_appendix_a_codec.py`, `tests/test_dpdu_framing.py` |
| 1–3 | Adaptador DTE (handshake, RX dispatch, keep-alive, TX worker, Receiver Master, reconexão, config) | `src/modem/tcp_110d_adapter.py` | `tests/test_tcp_110d_adapter.py` |
| 4 | Mock-modem + e2e dois nós | `tests/mock_110d_modem.py` | `tests/test_tcp_110d_end_to_end.py` |
| GUI | Chat app de demonstração (DTE 110D) + lançador de modem-mock local | `src/interface/chat_app_110d.py`, `src/interface/mock_110d_air.py` | smoke headless (replica `_init_stanag`) |
| 10 | Porta TCP fixa em 3000 | `rds-hf/config/backend.json` | — |

Cobertura: 661 testes verdes (602 pré-existentes + 59 novos), sem regressões.

Notas de fidelidade/decisões durante a implementação:
- **CRC-16 e layouts de pacote validados contra vetores golden gerados pelo próprio
  C do `rds-hf`** (`crc16.c`/`packets.c`), não contra o doc — ver `test_appendix_a_codec.py`.
- **Bug corrigido no framing herdado**: `_dpdu_wire_size` (em `hf_modem_adapter`)
  tinha um `+2` espúrio que superdimensionava cada D_PDU em 2 bytes (descartava
  frames únicos / desalinhava streams). O helper `dpdu_framing` usa o tamanho
  correto (validado contra `encode_dpdu` para todos os tipos do Annex C) e o HF
  passou a reusá-lo.
- **Pré-fill**: o modem só aceita `DATA_TRANSFER` em `ARMED_READY`/`STARTED`
  (senão NAK), então o worker aguarda `PORT_READY` antes de enviar dados; o
  pré-fill ocorre após o READY, antes do START (PLANO §6).
- O teste vivo §11.3 (subir `rds_backend` + `ale_call.py force … 110`) é manual e
  não automatizável neste ambiente; a config de porta (§10) já casa com o default
  do `Tcp110dConfig` (3000).
- **GUI de demonstração** (fora do escopo original, adicionada a pedido): um chat
  Tkinter `chat_app_110d.py` (gêmeo do `chat_app_sis.py`, trocando o transporte UDP
  pelo `Tcp110dModemAdapter`) e um lançador `mock_110d_air.py` que sobe dois modems
  simulados cruzando o ar (A↔B) para demo local sem o `rds-hf`. Ver
  `src/interface/README_chat_app_110d.md`.
**Escopo decidido:** apenas o lado **DTE** (em `rds-5066`). O modem (`rds-hf`) já
implementa o servidor Appendix A do lado **DCE** e é tratado como interface
externa estável (única alteração admitida em `rds-hf`: fixar a porta TCP em
config — ver §10).

Referências:
- `docs/stanag5066_to_110d_appendixA.md` (guia de integração)
- `docs/MIL-STD-188-110D-ANEXO-A.md` (texto normativo do Appendix A)
- STANAG 5066 Ed. 3, Annex C (DTS) / Annex D (interface com o equipamento)

---

## 1. Achado-chave: o modem já é um servidor Appendix A conforme

A varredura do `rds-hf` confirmou (lendo o código, não o doc):

| Item | Onde | Valor |
|------|------|-------|
| Preâmbulo TCP | `backend/mil110/src/lan/packets.c` | `MIL110_TCP_PREAMBLE = {0x49,0x50,0x55}` ✅ |
| CRC-16 | `backend/mil110/src/lan/crc16.c` | poly `0x9299`, init `0x0000`, LSB-first ✅ |
| Handshake | `backend/mil110/src/lan/net_reactor.c` | `CONNECT → CONNECTACK → CONNECTION_PROBE` ✅ |
| Servidor LAN | `mil110_lan_server_*` (`lan.h`) | epoll TCP (TDSI) + UDP (UDSI), tick 50 ms |
| Bridge bytes↔bits | `mil110_bridge_*` | FIFO, Tx Status, Carrier Detect, blocking factor |
| Porta | `config/backend.json` → `tcp_port: 0` | **0 = OS-assigned** (ver ambiguidade §10) |

**Conclusão:** não há trabalho de protocolo no modem. O esforço é construir, em
`rds-5066`, um **adaptador DTE** que fala Appendix A sobre TCP e se encaixa no
contrato de modem já existente do nó STANAG 5066.

## 2. Achado-chave: o ponto de extensão no 5066 já existe e é limpo

`StanagNode` consome o modem por *duck typing* (`src/modem_if.py`,
`src/stanag_node.py`). Os únicos métodos exercitados:

| Método | Uso em `stanag_node.py` |
|--------|--------------------------|
| `modem_tx_burst(frames: list[bytes]) -> int` | bursts ARQ / Expedited (linhas ~957, ~965) — **1 chamada = 1 intervalo de transmissão DTS** |
| `modem_tx_dpdu(dpdu, length=None) -> int` | warnings / respostas de management (linhas ~802, ~857, ~897, ~938) |
| `modem_rx_read_frame() -> bytes \| None` | loop do `tick()` (linha ~667), 1 D-PDU por chamada |
| `modem_get_carrier_status() -> bool` | sensoriamento de portadora |
| `modem_set_tx_enable(bool)` / `modem_rx_start()/stop()` | controle |
| `.config.data_rate_bps` | taxa reportada em respostas de management |

Os adaptadores existentes `src/modem/udp_modem_adapter.py` e
`src/modem/hf_modem_adapter.py` já seguem o padrão **thread de RX em background +
fila thread-safe de frames**, e já contêm a lógica de **re-split de stream de
D-PDUs por sync `0x90 0xEB`** (`_dpdu_split_stream` / `_dpdu_wire_size`).

**Consequência:** o novo adaptador entra como mais um `ModemInterface`. **Zero
alteração no núcleo DTS/ARQ/CAS/SIS.**

---

## 3. Arquitetura-alvo

```
 rds-5066  (processo DTE)                         rds-hf  (processo DCE/modem)
┌──────────────────────────────────────┐        ┌─────────────────────────────┐
│ StanagNode.tick()  (thread única)     │        │ rds_backend (serviço "110") │
│   modem_tx_burst / modem_rx_read_frame│        │  ┌───────────────────────┐  │
│            │                ▲          │        │  │ mil110_lan_server     │  │
│            ▼                │          │  TCP   │  │ (epoll, Appendix A)   │  │
│ ┌────────────────────────────────────┐│ :3000  │  │  CONNECT/ACK/PROBE    │  │
│ │ Tcp110dModemAdapter                ││◄──────►│  │  Tx Status / Carrier  │  │
│ │  • fila de janelas TX  ──► TX worker││        │  │  bridge bytes↔bits    │  │
│ │  • _rx_frames (deque)  ◄── RX thread││        │  └──────────┬────────────┘  │
│ │  • cond var: tx_state/FIFO/carrier  ││        │   mil110_modem (TDD) ──► HF │
│ │  • keep-alive 2s / timeout 30s      ││        └─────────────────────────────┘
│ └────────────────────────────────────┘│
└──────────────────────────────────────┘
```

O 5066 é **DTE**; o modem é **DCE**. Um único DTE por modem (o servidor rejeita o
segundo). Mesma máquina via loopback TCP ou máquinas distintas — protocolo
idêntico.

---

## 4. Componentes a construir (em `rds-5066`)

### 4.1 `src/modem/appendix_a_codec.py` — codec puro (sem I/O)
- `crc16(data: bytes) -> int` — poly `0x9299`, init `0`, LSB-first. **Validar contra
  vetor de referência idêntico ao `rds-hf/.../crc16.c`.**
- `encode_packet(ptype: int, payload: bytes) -> bytes`:
  `49 50 55 | type | size(2,BE) | hdrCRC(2,BE sobre os 6 primeiros) | payload | payloadCRC(2,BE)`.
- `PacketReader` — parser de stream com **fragmentação, resync de preâmbulo e
  descarte silencioso em falha de CRC** (espelha `packets.c`). Produz `(type, payload)`.
- Constantes de tipo: `DATA=0x00, CONNECT=0x01, CONNECTACK=0x02, ERROR=0xFF`.
- Constantes de PayloadCommand: `DATA_TRANSFER=0x00, TX_ARM=0x01, TX_START=0x02,
  REQ_TX_STATUS=0x03, TX_DATA_NAK=0x04, TX_STATUS=0x05, ABORT_RX=0x06,
  CARRIER_DETECT=0x08, TRANSMIT_SETUP=0x09, INITIAL_SETUP=0x0A, CONN_PROBE=0x0B`.
- PacketOrder: `FIRST_ONLY=1, FIRST_AND_LAST=2, CONTINUATION=3, LAST=4`.
- Encoders/decoders por payload: Data Transfer (`cmd|order|packetID(12B)|data`),
  Tx Status, Carrier Detect, Transmit Setup, Initial Setup, NAK.
- **Ação de fidelidade:** antes de codar, ler `backend/mil110/src/lan/packets.c` e
  os structs de `lan.h` e casar **larguras de campo e endianness exatamente com a
  implementação do modem** (o doc pode divergir em detalhes).

### 4.2 `src/modem/dpdu_framing.py` — helper compartilhado
- Extrair `_dpdu_split_stream()` / `_dpdu_wire_size()` de `hf_modem_adapter.py`
  para um módulo comum e reusar em HF, UDP e TCP. Evita duplicar parsing frágil de
  D-PDU (sync `0x90 0xEB`, `HDR_SIZE`/`ADR_SIZE`, CRC). Refatoração com testes que
  fixam o comportamento atual antes de mover.

### 4.3 `src/modem/tcp_110d_adapter.py` — o adaptador DTE
Implementa o contrato *duck-typed* de `ModemInterface`. Internamente:

- **Conexão + handshake** (§5).
- **Thread RX** (`recv` bloqueante → `PacketReader` → dispatch por comando) (§7).
- **Thread TX worker** (consome fila de janelas; orquestra ARM→pré-fill→START→
  DRAIN) (§6). **Decisão do usuário:** worker interno; `modem_tx_burst()` apenas
  enfileira e retorna — **o `tick()` nunca bloqueia.**
- **Keep-alive** (2 s envia DATA vazio; 30 s sem DATA → reconecta) (§9).
- **Estado compartilhado** protegido por `threading.Condition`: `tx_state`,
  `fifo_space/fill/critical_ms/critical_bytes`, `carrier_state`, `tx_data_rate`,
  `tx_blocking_factor`, `rx_data_rate`, `rx_blocking_factor`.
- **Lock de escrita no socket**: todas as escritas (worker, keep-alive, probe, ARM/
  START) serializadas para nunca intercalar pacotes parciais.

### 4.4 Config — `src/modem/tcp_110d_adapter.py::Tcp110dConfig`
`host`, `port` (default **3000**), timeouts (CONNECT 3 s, CONNECTACK 3 s, PROBE
6 s), keep-alive (2 s / 30 s), `prefill_blocking_factors=3`, `max_data_bytes=4072`,
`expect_sync_mode=True`, política de reconexão (backoff). `ModemConfig.data_rate_bps`
passa a refletir o `TxDataRate` reportado (Transmit Setup) para que respostas de
management/DRC informem a taxa correta.

---

## 5. Handshake de conexão (DTE)

```
TCP connect (host:3000)
 ├─ envia CONNECT(ver=12)                    ┐ simultâneo
 ├─ recebe CONNECT(ver=12) .... timeout 3 s  ┘  (valida ver==12, senão fecha)
 ├─ recebe CONNECT → envia CONNECTACK(ver=12)
 ├─ recebe CONNECTACK ........ timeout 3 s
 ├─ recebe CONNECTION_PROBE (modem 1º) → ecoa CONNECTION_PROBE ... timeout 6 s
 ├─ recebe Initial Setup (0x0A): guarda RTT, **valida SyncFlag=1 (síncrono)**
 ├─ recebe Transmit Setup (0x09): TxDataRate, TxBlockingFactor (dimensiona pré-fill)
 ├─ recebe Tx Status (0x05): FLUSHED
 └─ recebe Carrier Detect (0x08): estado inicial do receptor
→ conexão ativa
```

Falha/timeout em qualquer passo ⇒ fecha TCP e entra em ciclo de reconexão (§9).
**Modo síncrono é obrigatório** para o 5066 (sem start/stop bits OTA, nota do
Annex D). Se o modem reportar `SyncFlag=0`, registrar erro de configuração.

---

## 6. Caminho de TX — mapeamento D-PDU → DATA (o ponto central)

**Regra de mapeamento:** **1 chamada `modem_tx_burst(frames)` (ou `modem_tx_dpdu`)
= 1 janela de transmissão OTA do Appendix A.** Cada janela passa pelo ciclo
completo ARM → pré-fill → START → CONTINUATION… → LAST → DRAIN.

Fluxo do TX worker para cada janela enfileirada:

1. Aguarda `tx_state == FLUSHED` (cond var).
2. Envia `TRANSMIT_ARM` (DATA, cmd `0x01`).
3. Aguarda `QUEUES_ARMED_AND_PORT_READY`.
   - **Receiver Master (decidido):** pode chegar `…_NOT_READY` primeiro; o worker
     **continua o pré-fill mas NÃO envia START** até virar `…_PORT_READY` (libera
     quando a recepção em curso termina). Sem abortar por timeout curto.
4. Concatena os D-PDUs da janela em um stream de bytes (são auto-delimitados por
   sync+length) e fragmenta em pacotes `DATA_TRANSFER` (`cmd|order|packetID(12B)|data`,
   ≤ 4072 bytes de dados por pacote). Pré-enche **≥ 3 × TxBlockingFactor** bytes
   antes do START. 1º pacote: `FIRST_ONLY` (ou `FIRST_AND_LAST` se tudo couber e
   for o fim).
5. Envia `TRANSMIT_START` (cmd `0x02`); aguarda `STARTED`; se não vier em 10 ms,
   reenvia START.
6. Continua com pacotes `CONTINUATION` respeitando **backpressure**: o modem bloqueia
   o socket quando a FIFO enche (A.5.1.2.5 item 10) — usar `send` bloqueante é o
   controle de fluxo natural. Monitorar `FIFO_Critical_ms/Bytes` para antecipar
   underrun.
7. Último pacote da janela: `LAST` (pode ter 0 bytes de dados).
8. Aguarda `DRAINING_OK → FLUSHED`. Janela concluída.

`modem_tx_dpdu(single)` (warnings/management) = janela degenerada de 1 D-PDU com
order `FIRST_AND_LAST`.

NAKs (`0x04`): `TRANSMIT_UNDERRUN`, `MISSING_FIRST`, `MULTIPLE_FIRST`,
`QUEUES_NOT_ARMED` → log + (v1) abortar a janela atual e reportar; tratamento de
recuperação fino fica para fase posterior.

---

## 7. Caminho de RX — DATA → D-PDUs

A thread RX faz dispatch por PayloadCommand:

- **`DATA_TRANSFER` (0x00):** descarta `order` + `packetID(12B)`, **acumula o stream
  OTA** de uma recepção (FIRST…LAST). Entrega ao `dpdu_framing.split` que varre
  `0x90 0xEB`, lê o comprimento de cada D-PDU pelo header e enfileira **D-PDUs
  completos** em `_rx_frames`. Pré-fill/post-fill (lixo do Annex D em torno dos
  D-PDUs) é naturalmente ignorado pela varredura de sync.
- **`TX_STATUS` (0x05):** atualiza `tx_state` + campos de FIFO; `notify` na cond var.
- **`CARRIER_DETECT` (0x08):** atualiza `carrier_state` + `RxDataRate`/`RxBlockingFactor`.
- **`TRANSMIT_SETUP` (0x09):** atualiza `TxDataRate`/`TxBlockingFactor` (pré-fill).
- **`INITIAL_SETUP` (0x0A):** RTT, SyncFlag, latências.
- **`TX_DATA_NAK` (0x04):** sinaliza o worker (underrun etc.).
- Qualquer **DATA** recebido (inclusive keep-alive vazio) reseta o timeout de 30 s.

`modem_rx_read_frame()` (chamado pelo `tick()`) faz `popleft()` de `_rx_frames`.

---

## 8. Modelo de threads e sincronização

| Thread | Responsabilidade |
|--------|-------------------|
| `tick()` (do `StanagNode`, externa ao adaptador) | chama `modem_tx_burst`/`modem_rx_read_frame` — **nunca bloqueia** |
| RX thread (adaptador) | `recv` → `PacketReader` → dispatch; atualiza estado e `notify` |
| TX worker (adaptador) | drena fila de janelas; orquestra ARM/START/DRAIN |
| Keep-alive | timer 2 s (envia DATA vazio) / 30 s (timeout → reconecta) — pode viver no worker |

Primitivas: `queue.Queue` para janelas TX; `collections.deque` (com lock) para
`_rx_frames`; `threading.Condition` para `tx_state`/FIFO/carrier; `threading.Lock`
para escrita no socket. Encerramento limpo via flag `_stop` + `join`.

---

## 9. Reconexão (resiliência; v1 com taxa fixa)

`recv()==0` / `ECONNRESET` / timeout keep-alive 30 s ⇒ marca desconectado, descarta
estado da janela TX corrente, reconecta com backoff e refaz o handshake completo.
O `StanagNode` segue em `tick()`; `modem_get_carrier_status()` retorna `False`
enquanto desconectado. (Como a taxa é fixa no v1, a reconexão é só robustez — não
há reconfiguração DRC do modem.)

---

## 10. Única alteração admitida em `rds-hf`: fixar a porta TCP em 3000

**Recomendação:** fixar `tcp_port: 3000` no `config/backend.json` do `rds-hf`.

Hoje `config/backend.json` tem `tcp_port: 0`, e há ambiguidade: `lan.h` diz
"0 = OS-assigned" (porta efêmera), mas o comentário do config diz "porta default do
Anexo A". Uma porta efêmera muda a cada execução e impede o DTE de ter um alvo
estável. Fixar **3000** (porta sugerida pelo Appendix A) elimina a ambiguidade e dá
ao adaptador DTE um destino determinístico. É mudança de **configuração**, não de
código — o único toque admitido em `rds-hf`.

Edição concreta em `rds-hf/config/backend.json` (bloco `lan`):

```jsonc
// antes
"lan_host": "0.0.0.0",       // escuta do DTE externo - TCP TDSI e UDP UDSI
"tcp_port": 0,               // 0 = porta default do Anexo A
"udp_port": 0,               // 0 = porta default do Anexo A

// depois
"lan_host": "0.0.0.0",       // escuta do DTE externo - TCP TDSI e UDP UDSI
"tcp_port": 3000,            // porta TCP fixa (Appendix A) - alvo estável do DTE 5066
"udp_port": 0,               // mantém efêmera (UDSI fora do escopo do v1, só TCP)
```

O `Tcp110dConfig` do adaptador (§4.4) usa `port = 3000` por default, casando com
essa config. Alternativa sem tocar `rds-hf` (descartada por ser frágil): ler a porta
efetiva do log do backend (`mil110_lan_server_tcp_port`) e injetá-la no adaptador.

---

## 11. Estratégia de testes

**Unitários (sem rede):**
- `crc16` contra vetor de referência (igual ao do `rds-hf`).
- `encode_packet`/`PacketReader` round-trip; fragmentação; resync de preâmbulo;
  descarte em CRC inválido.
- Re-split de D-PDUs a partir de stream concatenado (com pré/post-fill no meio).
- Encoders/decoders de cada payload casando os bytes do `packets.c`.

**Integração com mock-modem (Python, sem `rds-hf`):**
- Um servidor mock que implementa o lado modem do handshake + Tx Status/Carrier +
  loopback de DATA (espelha o padrão de `tests/tools/mock_backend` do `rds-hf`).
- Topologia loopback: `StanagNode A → mock-modem (OTA em loopback) → StanagNode B`,
  exercitando hard link + UNIDATA ponta a ponta sobre TCP.

**End-to-end com o `rds-hf` real (teste de aceitação):**
1. Subir `rds_backend` com serviço `110` e `tcp_port: 3000`.
2. Estabelecer link: `./ale_call.py force <ch> 110 <ip>` (modo manual) em ambos.
3. Rodar nó 5066 com `Tcp110dModemAdapter` apontando para o modem.
4. Validar troca de hard link + UNIDATA bidirecional.

---

## 12. Faseamento e entregáveis

| Fase | Entregável | Critério de pronto |
|------|-----------|--------------------|
| **0** | `appendix_a_codec.py` + `dpdu_framing.py` (extração) | unit tests verdes; CRC/round-trip/re-split cobertos; layouts casados com `packets.c` |
| **1** | Conexão + handshake + RX dispatch + keep-alive (sem TX de dados) | conecta ao `rds-hf`, recebe Initial/Transmit Setup, mantém keep-alive, entrega D-PDUs recebidos |
| **2** | TX worker (ARM/pré-fill/START/DRAIN) + Receiver Master | uma janela TX completa percorre o ciclo e chega a OTA; trata `…_NOT_READY` |
| **3** | Reconexão + robustez + wiring de carrier/rate em `config` | derruba/reconecta sem travar o `tick()`; `data_rate_bps` reflete o modem |
| **4** | Testes mock-modem + teste vivo contra `rds-hf` | hard link + UNIDATA ponta a ponta passam |

---

## 13. Riscos e itens a verificar na implementação

1. **Fidelidade de byte:** casar Tx Status / Carrier Detect / Transmit/Initial Setup
   com `backend/mil110/src/lan/packets.c` (larguras, endianness) — confiar no
   **código**, não no doc (o explore inicial divergiu em detalhes).
2. **Porta:** resolver `tcp_port: 0` (§10) — fixar 3000.
3. **Modo síncrono:** confirmar `SyncFlag=1` no Initial Setup do `rds-hf`.
4. **Backpressure:** `send` bloqueante + lock de socket; não intercalar keep-alive
   no meio de um pacote DATA.
5. **Receiver Master:** garantir que o worker não envia START em `…_NOT_READY` e
   não aborta por timeout curto durante recepção em curso.
6. **Tx Status:** o modem manda não solicitado (em mudança e ≥ a cada 2 s em
   STARTED/DRAINING); o DTE pode complementar com `REQUEST_TX_STATUS (0x03)`.
7. **Não regressão:** a extração do `dpdu_framing` deve manter HF/UDP intactos
   (testes de fixação antes de mover).

---

## 14. Resumo executivo

- Trabalho concentrado em **um novo `ModemInterface` em `rds-5066`**; núcleo
  DTS/ARQ/CAS/SIS inalterado; modem `rds-hf` já conforme.
- **Worker TX interno** preserva o `tick()` single-thread; **RX thread** entrega
  D-PDUs re-split por sync; **Receiver Master**; **taxa fixa** no v1.
- 3 arquivos novos (`appendix_a_codec.py`, `tcp_110d_adapter.py`, helper
  `dpdu_framing.py`) + testes; 1 ajuste de config em `rds-hf` (porta).
- Caminho incremental e testável em 5 fases, com aceitação ponta a ponta contra o
  backend real via `ale_call.py force … 110`.
