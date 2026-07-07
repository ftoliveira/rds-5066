# Protocolo de Controle Remoto — Estação ALE 2G (porta 54001/UDP)

Referência para implementar uma **tela de controle remota** (cliente nativo/Python
na LAN) que fala com o backend `rds_backend` **apenas na camada de controle e
telemetria** — comandos do operador e snapshots de estado. Áudio (voz AM na 54000,
voz/dados 110 nas 3000/3001) **está fora deste protocolo**.

Fonte de verdade em código: [`shared/proto/ale_link_proto.h`](../shared/proto/ale_link_proto.h)
(structs + helpers). Servidor: [`backend/ale_tenant.c`](../backend/ale_tenant.c).
Cliente de referência (LVGL): [`frontend/ui/core/state.cpp`](../frontend/ui/core/state.cpp).
Este documento descreve o **protocolo como está** (não propõe nada novo).

> **Escopo confirmado:** cliente nativo/Python na **LAN**, transporte UDP existente,
> **sem autenticação nem criptografia** (rede confiável). Para acesso via internet,
> navegador ou com autenticação, este protocolo NÃO é adequado — precisaria de um
> gateway (WebSocket/TCP+JSON+TLS), que está fora do escopo aqui.

---

## 1. Visão geral

| Item | Valor |
|---|---|
| Transporte | UDP (datagramas, sem conexão) |
| Porta | **54001** (`ALEL_PORT_CTRL`); backend faz `bind` em `0.0.0.0:54001` |
| Magic | `0x414C4543` = `"ALEC"` (little-endian no header) |
| Versão | **4** (`ALEL_VERSION`) — precisa bater **exato**, senão o datagrama é descartado em silêncio |
| Endianness | **Little-endian nativo x86** (FE e BE assumidos de mesma arquitetura) |
| Empacotamento | `#pragma pack(1)` — structs **sem padding**, exceto campos `_pad` explícitos |
| Tamanho máx. datagrama | 1024 bytes (`ALEL_MAX_DGRAM`); maior payload real é `alel_channels_t` = 452 B |

Config no `config/backend.json` (bloco `ale`):
- `udp_port` — porta de controle (`0` = default 54001). **Não mude** sem reconfigurar o
  cliente: o painel embarcado e o `ale_call.py` assumem 54001 fixo.
- `max_clients` — nº de telas de controle simultâneas (default 8; teto de compilação 8).
- `client_timeout_ms` — expira uma tela que fique N ms sem `HELLO` (default 5000).

### Topologia (learn-address, multi-cliente)

```
  CLIENTE(S) (socket efêmero cada)         BACKEND (bind 0.0.0.0:54001)
  ────────────────────────────────         ────────────────────────────
  sendto(be:54001, HELLO)  ───────────────▶ adiciona/renova (IP:porta) na
                                            tabela de clientes (até max_clients)
                           ◀─────────────── emit_scene() ao cliente novo
  recv() estado/telemetria ◀━━━━━━━━━━━━━━━ FAN-OUT: STATE ~5 Hz + tabelas + eventos
  sendto(be:54001, CMD_*)  ───────────────▶ executa; responde STATE imediato
  ...HELLO a cada 1 s (keepalive)────────▶ renova; sem HELLO por client_timeout_ms → expira
```

- O **backend não conhece o endereço de um cliente até receber algo dele.** Qualquer
  datagrama válido (inclusive `HELLO`) faz o backend aprender `IP:porta` de origem,
  adicioná-lo à tabela de clientes e passar a mandar telemetria para lá.
- **Múltiplos clientes simultâneos** (até `max_clients`, default 8): o backend mantém uma
  tabela de peers e faz **fan-out** da telemetria a todos. Cada cliente que fica
  `client_timeout_ms` (default 5000) sem `HELLO` é removido. Todos os clientes são
  **iguais** — qualquer um pode enviar comandos, sem arbitragem. Ver [limitações](#8-limitações-e-gotchas).
- O cliente deve **manter a mesma porta de origem** durante toda a sessão (um único
  socket resolve isso). É recomendável dar `connect()` no socket para receber só do
  backend, mas basta um `sendto`/`recvfrom` com o mesmo socket.

---

## 2. Cabeçalho comum (16 bytes)

Todo datagrama — comando ou evento — começa com este header:

```c
typedef struct {          // offset
    uint32_t magic;       //  0  ALEL_MAGIC = 0x414C4543
    uint16_t version;     //  4  ALEL_VERSION = 4
    uint16_t type;        //  6  enum alel_type (ver §4/§5)
    uint32_t seq;         //  8  sequência incremental (detecta perda/reorder)
    uint32_t len;         // 12  bytes de payload APÓS o header
} alel_hdr_t;             // = 16 bytes
```

Python: `struct` format **`<IHHII`** (16 bytes).

Validação do lado receptor (`alel_hdr_ok`): `magic` correto, `version == 4`, e
`16 + len <= bytes_recebidos`. Falhou qualquer uma → datagrama **ignorado sem erro**.

O `seq` é meramente incremental por remetente; serve para o cliente detectar perda
ou reordenação. O backend não exige contiguidade e não retransmite.

---

## 3. Ciclo de vida e cadência

### Cliente → Backend
- Envie **`ALEL_CMD_HELLO`** (sem payload) ao iniciar e depois a **cada ~1 s** como
  keepalive. É o que anuncia/renova o cliente como peer.
- Comandos (`ALEL_CMD_*`) são **fire-and-forget**: o efeito volta no próximo `STATE`
  ou `LOG`. Não há ACK nem código de retorno no protocolo.

### Backend → Cliente (para cada cliente aprendido, por fan-out)
| Evento | Quando é enviado |
|---|---|
| `ALEL_EVT_STATE` | a cada **200 ms (~5 Hz)**, **imediatamente ao trocar de canal** e **após cada comando** |
| `ALEL_EVT_SCAN`, `ALEL_EVT_LQA` | a cada **~1 s** |
| `ALEL_EVT_CHANNELS`, `ALEL_EVT_SOUND_HIST` | no `emit_scene()` (novo peer) e sob mudança (ex.: `CHEDIT`, sounding) |
| `ALEL_EVT_LOG` | a cada linha nova do event log (evento pontual) |
| `ALEL_EVT_AMD_RX` | a cada AMD recebido no ar (evento pontual) |

Ao ser reconhecido como **novo peer**, o backend dispara um `emit_scene()` que manda
**CHANNELS + LQA + SCAN + SOUND_HIST + STATE** de uma vez — o cliente já sobe com o
estado completo, sem precisar pedir nada.

Todo `STATE` é **idempotente**: é um snapshot completo; se um datagrama se perder, o
próximo corrige. Trate a UI como função pura do último `STATE` recebido.

---

## 4. Comandos (Frontend → Backend)

`type` no header + payload da struct correspondente. Campos `char[]` são strings
**NUL-terminadas** dentro de buffer fixo (zero-padded). Envie o tamanho exato da
struct em `len`.

### Tabela de tipos

| `type` | Nome | Struct | Tam. payload | Python `struct` |
|---:|---|---|---:|---|
| 1 | `ALEL_CMD_CALL` | `alel_call_t` | 18 | `<16sh` |
| 2 | `ALEL_CMD_GROUP` | `alel_group_t` | 81 | `<B80s` |
| 3 | `ALEL_CMD_NET` | `alel_net_t` | 16 | `<16s` |
| 4 | `ALEL_CMD_AMD` | `alel_amd_t` | 112 | `<16sB3x92s` |
| 5 | `ALEL_CMD_TERM` | `alel_term_t` | 16 | `<16s` |
| 6 | `ALEL_CMD_SOUND` | `alel_sound_t` | 1 (ou 0) | `<B` |
| 7 | `ALEL_CMD_SET_MODE` | `alel_mode_t` | 1 | `<B` (só logado; não aplica modo) |
| 8 | `ALEL_CMD_CONFIG` | `alel_config_t` | 12 | `<BBBBHhHH` |
| 9 | `ALEL_CMD_HELLO` | — | 0 | (sem payload) |
| 10 | `ALEL_CMD_FORCE_LINK` | `alel_force_link_t` | 11 | `<hB8s` |
| 11 | `ALEL_CMD_CHEDIT` | `alel_chedit_t` | 22 | `<h8s12s` |

### Detalhe dos comandos

**`ALEL_CMD_CALL`** — chamada individual.
```c
char    addr[16];   // destino ALE (ex.: "BR2")
int16_t channel;    // -1 = auto (melhor canal por LQA); >=0 = canal explícito
```

**`ALEL_CMD_GROUP`** — chamada de grupo.
```c
uint8_t n;               // 1..5 membros
char    addr[5][16];     // membros (o resto ignorado)
```

**`ALEL_CMD_NET`** — chamada de rede pré-registrada.
```c
char netid[16];          // id da net registrada no backend (config `ale.nets`)
```

**`ALEL_CMD_AMD`** — mensagem AMD (texto curto).
```c
char    dest[16];        // destino; "" = manda no link corrente
uint8_t connect_first;   // a UI sempre manda 0; o backend NÃO lê este campo
uint8_t _pad[3];
char    text[92];        // texto do AMD (charset ALE)
```

**`ALEL_CMD_TERM`** — encerra link. Também **sai do modo FORCED** de volta ao NORMAL.
```c
char addr[16];           // "" = encerra o link corrente
```

**`ALEL_CMD_SOUND`** — dispara sounding. Payload **opcional**: 0 bytes = `SCANNING`
(retrocompat). Com 1 byte:
```c
uint8_t mode;            // enum alel_sound_mode: 0=SINGLE 1=SCANNING 2=HANDSHAKE
```

**`ALEL_CMD_CONFIG`** — ajusta configuração em runtime. **Cada campo tem sentinela
"inalterado"** para editar um sem zerar os outros:
```c
uint8_t  scan_rate;        // canais/s (2/5/10); 0 = inalterado
uint8_t  sideband;         // enum alel_sideband; 0xFF = inalterado (voz AM)
uint8_t  sounding_enabled; // 0/1; 0xFF = inalterado
uint8_t  occupancy_detect; // 0/1 (LBT A.5.4.7); 0xFF = inalterado
uint16_t twa_s;            // wait-for-activity (s); 0xFFFF = inalterado; 0 = desliga term auto
int16_t  tx_power_dbm;     // dBm (degrau RF_POWER_*: 30/40/43/47/60); <=0 = inalterado
uint16_t tcc_max_s;        // Tcc max (s); 0 = inalterado
uint16_t tm_max_s;         // Tm max (s); 0 = inalterado
```
Sentinelas: `ALEL_CFG_KEEP_U8 = 0xFF`, `ALEL_CFG_KEEP_U16 = 0xFFFF`. **Sempre preencha
os campos que NÃO quer mudar com a sentinela** (um CONFIG zerado desliga Twa e sounding).

**`ALEL_CMD_FORCE_LINK`** — modo manual / link forçado.
```c
int16_t channel;         // canal estacionado (>=0); usado em FORCED
uint8_t forced;          // 1 = FORCED (fixa canal, suprime scan/burst ALE, ar sempre aberto)
                         // 0 = NORMAL (scan/sounding voltam; 'service' = plano pós-link)
char    service[8];      // tenant: "am"/"fm"/"110"
```

**`ALEL_CMD_CHEDIT`** — edita e **persiste** freq/nome de um canal (tela Scanning).
```c
int16_t idx;             // índice 0-based do canal
char    freq[8];         // "24.928" (MHz decimal); "" = inalterado
char    name[12];        // "LONG-C"; "" = inalterado
```
Efeito volta no próximo `ALEL_EVT_CHANNELS`.

---

## 5. Eventos / telemetria (Backend → Frontend)

| `type` | Nome | Struct | Tam. payload | Python `struct` |
|---:|---|---|---:|---|
| 64 | `ALEL_EVT_STATE` | `alel_state_t` | 127 | ver abaixo |
| 65 | `ALEL_EVT_LOG` | `alel_log_t` | 80 | `<B3x12s64s` |
| 66 | `ALEL_EVT_LQA` | `alel_lqa_t` | 400 | `<BBxx` + 12×`<16sB16s` |
| 67 | `ALEL_EVT_SCAN` | `alel_scan_t` | 164 | `<Bxxx` + 16×`<8sbB` |
| 68 | `ALEL_EVT_AMD_RX` | `alel_amd_rx_t` | 124 | `<16s12sB3x92s` |
| 69 | `ALEL_EVT_SOUND_HIST` | `alel_sound_hist_t` | 260 | `<Bxxx` + 8×`<12shh16s` |
| 70 | `ALEL_EVT_CHANNELS` | `alel_channels_t` | 452 | `<Bxxx` + 16×`<h8s12s4sBx` |

### `alel_state_t` (127 B) — o snapshot principal

Format Python: **`<BBBBhhhhhHH16s16sBBBBQQQBfff8s8sBhB8sHHBBh`**

```c
uint8_t  fsm_state;      // enum alel_fsm (0..4)
uint8_t  radio_mode;     // enum alel_mode — a UI NÃO usa este campo
uint8_t  scanning;       // 0/1
uint8_t  scan_rate;      // canais/s
int16_t  cur_channel;    // índice corrente (-1 = nenhum)
int16_t  sinad;          // dB 0..30 (<0 = sem medida)
int16_t  ber;            // código BER 0..30 (<0 = sem medida)
int16_t  rssi;           // dBm se rssi_cal_db!=0; senão escala relativa dBFS
int16_t  noise;          // proxy do gate
uint16_t twa_remain;     // s (regressivo do Twa)
uint16_t twa_max;        // s
char     self_addr[16];  // endereço desta estação
char     link_peer[16];  // "" se não-LINKED
uint8_t  voice_open;     // 1 = caminho de voz aberto (derivado de LINKED)
uint8_t  ptt;            // 1 = TX de voz em curso
uint8_t  rx_voice;       // 1 = squelch do demod aberto (recebendo voz)
uint8_t  sideband;       // enum alel_sideband
uint64_t frames_rx;      // IQ frames consumidos
uint64_t sounds_rx;      // soundings recebidos
uint64_t words_valid;    // palavras ALE válidas
uint8_t  tx_active;      // 1 = emitindo (telemetria TX abaixo válida só com isto=1)
float    tx_power;       // ADC "Tx Power"
float    tx_refl;        // ADC "REFL.PWR ANT"
float    tx_vswr;        // VSWR (adimensional)
char     tx_power_unit[8];
char     tx_refl_unit[8];
uint8_t  sounding;       // 1 = transmitindo um sounding agora
int16_t  sounding_channel; // canal no ar durante o sounding (-1 = nenhum)
uint8_t  forced;         // 1 = modo manual (FORCED) ativo
char     active_service[8]; // tenant ativo no FORCED ("am"/"fm"/"110")
uint16_t tcc_max;        // Tcc max efetivo (s)
uint16_t tm_max;         // Tm max efetivo (s)
uint8_t  occupancy_detect; // 0/1 (LBT)
uint8_t  _pad_cfg;
int16_t  tx_power_dbm;   // potência de TX configurada (dBm, degrau RF_POWER_*)
```

### `alel_log_t` (80 B) — uma linha do event log
```c
uint8_t kind;            // enum alel_logkind (0..5)
uint8_t _pad[3];
char    t[12];           // "HH:MM:SS"
char    text[64];
```

### `alel_lqa_t` (400 B) — matriz LQA peer × canal
```c
uint8_t n_peers;         // 0..12
uint8_t n_channels;      // 0..16
uint8_t _pad[2];
struct {                 // peer[12]  (cada = 33 B: 16 + 1 + 16)
    char    addr[16];
    uint8_t online;      // 0/1 — o BE hoje sempre envia 1
    uint8_t lqa[16];     // 0..30; 31 = desconhecido
} peer[12];
```

### `alel_scan_t` (164 B) — ocupação/qualidade por canal
```c
uint8_t n;               // 0..16
uint8_t _pad[3];
struct {                 // row[16]  (cada = 10 B: 8 + 1 + 1)
    char   label[8];     // frequência formatada, ex.: "14.109"
    int8_t lqa;          // 0..30; 31 = sem medida
    uint8_t occ;         // enum alel_occ (0=off 1=on 2=occ 3=busy; BUSY nunca emitido)
} row[16];
```

### `alel_amd_rx_t` (124 B) — AMD recebido
```c
char    from[16];
char    t[12];           // "HH:MM:SS"
uint8_t read;            // 0/1
uint8_t _pad[3];
char    text[92];
```

### `alel_sound_hist_t` (260 B) — histórico de sounding
```c
uint8_t n;               // 0..8
uint8_t _pad[3];
struct {                 // row[8]  (cada = 32 B: 12 + 2 + 2 + 16)
    char    t[12];
    int16_t ch;
    int16_t q;           // qualidade 0..30
    char    ack[16];     // quem reconheceu ("—" em UTF-8 = nenhum)
} row[8];
```

### `alel_channels_t` (452 B) — tabela de canais
```c
uint8_t n;               // 0..16
uint8_t _pad[3];
struct {                 // ch[16]  (cada = 28 B: 2 + 8 + 12 + 4 + 1 + 1)
    int16_t idx;
    char    freq[8];     // "24.928" (MHz decimal)
    char    name[12];
    char    band[4];     // "LF"/"MF"/"HF"
    uint8_t enabled;     // 0/1 — o BE hoje sempre envia 1
    uint8_t _pad;
} ch[16];
```

---

## 6. Enums

```c
enum alel_fsm  { AVAILABLE=0, LINKING=1, LINKED=2, GROUP_LINKED=3, NET_LINKED=4 };
enum alel_mode { ALE_PURO=0, AM_VOZ=1 };
enum alel_occ  { OFF=0, ON=1, OCC=2, BUSY=3 };   // BUSY existe mas nunca é emitido
enum alel_logkind   { RX=0, TX=1, SYS=2, SND=3, LQA=4, ERR=5 };
enum alel_sideband  { USB=0, LSB=1, DSB=2 };
enum alel_sound_mode{ SINGLE=0, SCANNING=1, HANDSHAKE=2 };
```

> **Atenção à ordem do `sideband`:** a ordenação de `alel_sideband` difere do
> `am_mode_t` interno e da tela Calling — converta na fronteira se for espelhar
> aquelas telas.

`LINKED` (para efeito de UI) = `fsm ∈ {LINKED, GROUP_LINKED, NET_LINKED}`.

---

## 7. Cliente mínimo em Python (referência)

Cliente de controle completo em ~40 linhas. Espelha o `state.cpp`: um socket, HELLO
a cada 1 s, laço de recepção decodificando por `type`.

```python
import socket, struct, threading, time

BACKEND = ("192.168.0.50", 54001)   # IP do rádio/backend na LAN
MAGIC, VERSION = 0x414C4543, 4
HDR = struct.Struct("<IHHII")       # magic, version, type, seq, len

# --- tipos ---
CMD_CALL, CMD_SOUND, CMD_TERM, CMD_HELLO = 1, 6, 5, 9
EVT_STATE, EVT_LOG, EVT_CHANNELS = 64, 65, 70

STATE = struct.Struct("<BBBBhhhhhHH16s16sBBBBQQQBfff8s8sBhB8sHHBBh")  # 127 B

sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
sock.connect(BACKEND)               # recv() só do backend; fixa a porta de origem
_seq = 0

def send(mtype, payload=b""):
    global _seq
    hdr = HDR.pack(MAGIC, VERSION, mtype, _seq, len(payload)); _seq += 1
    sock.send(hdr + payload)

def cstr(b): return b.split(b"\x00", 1)[0].decode("utf-8", "replace")

def keepalive():
    while True:
        send(CMD_HELLO)             # anuncia/renova o peer
        time.sleep(1.0)

def rx_loop():
    while True:
        dg = sock.recv(1024)
        if len(dg) < 16: continue
        magic, ver, mtype, seq, ln = HDR.unpack_from(dg, 0)
        if magic != MAGIC or ver != VERSION or 16 + ln > len(dg): continue
        pl = dg[16:16+ln]
        if mtype == EVT_STATE and len(pl) >= STATE.size:
            f = STATE.unpack_from(pl, 0)
            fsm, cur_ch, self_addr, link_peer = f[0], f[4], cstr(f[11]), cstr(f[12])
            print(f"FSM={fsm} ch={cur_ch} self={self_addr} peer={link_peer or '-'}")
        elif mtype == EVT_LOG:
            kind, t, text = pl[0], cstr(pl[4:16]), cstr(pl[16:80])
            print(f"LOG[{kind}] {t} {text}")
        # ... decodifique EVT_CHANNELS/SCAN/LQA/etc. conforme a §5

threading.Thread(target=keepalive, daemon=True).start()
threading.Thread(target=rx_loop,   daemon=True).start()

# --- exemplo: chamar "BR2" em canal automático ---
time.sleep(0.2)                                    # deixa o HELLO chegar
send(CMD_CALL, struct.pack("<16sh", b"BR2", -1))
input("enter para encerrar o link...\n")
send(CMD_TERM, struct.pack("<16s", b""))
```

Pontos-chave para qualquer cliente:
- **Um único socket** para toda a sessão (fixa a porta de origem → o backend não perde
  o peer). Dê `connect()` ou apenas reuse o mesmo socket em todo `sendto`/`recvfrom`.
- **HELLO a cada ~1 s.** Sem tráfego, o backend continua mandando para o último peer,
  mas o keepalive é o contrato esperado e garante reaprendizado após reinício do BE.
- Todo `char[]` é **NUL-terminado** dentro do buffer fixo — corte no primeiro `\x00`.
- Trate `STATE` como **verdade absoluta idempotente**; não acumule estado derivado.

---

## 8. Limitações e gotchas

1. **Múltiplos clientes, todos iguais.** O backend atende até `max_clients` (default 8)
   telas simultâneas via fan-out; o painel embarcado e telas remotas coexistem sem
   "brigar". **Não há arbitragem**: qualquer cliente pode comandar o rádio — dois
   operadores podem enviar comandos conflitantes (`CALL`, `FORCE_LINK`, `TERM`…). Um
   cliente sem `HELLO` por `client_timeout_ms` (default 5000) é descartado; ao voltar, é
   readmitido e recebe a cena completa. Acima de `max_clients`, o peer mais antigo
   (provável morto) é evictado para dar lugar ao novo.
2. **Sem autenticação nem criptografia.** Qualquer host na LAN pode comandar o rádio
   (`CALL`, `FORCE_LINK`, `CONFIG`…) — inclusive acionar o **transmissor de RF**. Use
   só em rede confiável/isolada.
3. **`version` precisa ser exatamente 4.** Datagrama com versão diferente é descartado
   **em silêncio** (sem log, sem erro). Ao atualizar o backend, atualize o cliente junto.
4. **Mesma arquitetura (x86 little-endian).** Os campos são nativos e `pack(1)`. Um
   cliente em outra plataforma deve reproduzir little-endian + sem padding (o `<` do
   `struct` Python já faz isso). `float` é IEEE-754 32-bit LE.
5. **Sem ACK / sem confiabilidade.** Comandos são fire-and-forget; confirme o efeito
   observando o próximo `STATE`/`LOG`. Não há retransmissão nem numeração exigida.
6. **`CMD_SET_MODE` é inerte** (só logado). Para trocar de modo use `FORCE_LINK`
   (FORCED) e `TERM` (volta a NORMAL).
7. **Campos marcados "o BE hoje sempre envia 1"** (`peer.online`, `ch.enabled`) e
   **`ALEL_OCC_BUSY`** (nunca emitido) são placeholders — não construa lógica que
   dependa deles variarem.
8. **Config no `backend.json` (bloco `ale`):** `udp_port` (porta; 54001 fixo no painel e
   no `ale_call.py` — não mude sem reconfigurar o cliente), `max_clients` (telas
   simultâneas, default 8) e `client_timeout_ms` (expiração por silêncio em ms, default
   5000).

---

## 9. Checklist de implementação do cliente

- [ ] Socket UDP único; `connect()` em `BACKEND:54001` (ou reuse fixo).
- [ ] Enviar `HELLO` no start e a cada 1 s.
- [ ] Laço de recepção: validar `magic`/`version==4`/tamanho; despachar por `type`.
- [ ] Decodificar ao menos `STATE` (127 B) para a tela principal; depois `CHANNELS`,
      `SCAN`, `LQA`, `LOG`, `AMD_RX`, `SOUND_HIST`.
- [ ] Cortar strings no primeiro `\x00`.
- [ ] Comandos com `struct` LE exato e `len` = tamanho da struct.
- [ ] Em `CONFIG`, preencher campos não editados com as sentinelas `0xFF`/`0xFFFF`.
- [ ] UI = função pura do último `STATE` (idempotência).
```
