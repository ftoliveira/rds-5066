# Interface STANAG 5066 ↔ MIL-STD-188-110D Appendix A

**Referências normativas:**
- STANAG 5066 Ed. 3, Annex D (interface obrigatória com equipamento de comunicações)
- STANAG 5066 Ed. 3, Annex E (controle remoto do modem, informativo)
- STANAG 5066 Ed. 3, Annex C §C.6.4 (Data Rate Control)
- MIL-STD-188-110D, Appendix A (LAN Interface for HF Data Modems)

---

## 1. Visão Geral Arquitetural

O STANAG 5066 define a interface entre o Data Transfer Sublayer (DTS) e o equipamento de comunicações no **Annex D**. Essa interface é obrigatoriamente síncrona serial quando o equipamento é físico (EIA-422/232). Quando o modem 188-110D possui interface Ethernet, o **Appendix A** do MIL-STD-188-110D substitui funcionalmente essa interface serial, transportando os mesmos D-PDUs sobre TCP/UDP.

```
┌─────────────────────────────────────────┐
│         Aplicação (IP, COSS, HMTP)      │  ← Clientes SIS (Annex F)
├─────────────────────────────────────────┤
│      Subnetwork Interface (SIS)         │  ← Annex A  [porta TCP 5066]
├─────────────────────────────────────────┤
│        Channel Access Sublayer          │  ← Annex B
├─────────────────────────────────────────┤
│       Data Transfer Sublayer (DTS)      │  ← Annex C  [DTE — Appendix A]
├─────────────────────────────────────────┤
│   MIL-STD-188-110D Modem (Appendix A)  │  ← DCE  [porta TCP 3000]
├─────────────────────────────────────────┤
│              Rádio HF SSB               │
└─────────────────────────────────────────┘
```

O STANAG 5066 atua como **DTE**. O modem 188-110D atua como **DCE**. Apenas um DTE pode controlar o modem por vez; uma segunda conexão é rejeitada pelo modem.

Ambos os processos podem rodar na mesma máquina (via loopback TCP) ou em máquinas distintas ligadas por Ethernet — o protocolo é idêntico nos dois casos.

---

## 2. Interface Física vs. Interface Ethernet

### 2.1 Interface serial (Annex D — referência normativa)

Quando o modem é um equipamento físico sem Ethernet, a interface é serial síncrona:

| Parâmetro          | Requisito (STANAG 5066 Annex D)                                |
|--------------------|----------------------------------------------------------------|
| Modo               | Síncrono serial (obrigatório; assíncrono só se o modem remover bits de framing) |
| Drivers            | EIA-232D/423 (unbalanced) ou EIA-422 (balanced)               |
| Relógio TX         | Configurável: do DTE ou do DCE                                 |
| Relógio RX         | Sempre do DCE (modem)                                          |
| Controle de fluxo  | Hardware full handshake (RTS/CTS)                              |
| Compatibilidade    | MIL-STD-188-114 para interop com equipamentos COMSEC legacy    |

Regras de framing de dados (Annex D):
- **Pre-fill**: número arbitrário de caracteres antes do primeiro D-PDU válido é permitido (para aquecimento do modem/preamble).
- **Post-fill**: número arbitrário de caracteres após o último D-PDU é permitido; o campo EOT de cada D-PDU deve ser calculado incluindo os post-fill.
- **Proibido**: inserir caracteres estranhos entre D-PDUs numa mesma janela de transmissão.

### 2.2 Interface Ethernet via Appendix A (MIL-STD-188-110D)

O Appendix A define um protocolo TCP/UDP sobre Ethernet que substitui funcionalmente a interface serial do Annex D. É a abordagem recomendada para implementações software puras ou quando 5066 e modem rodam em máquinas separadas.

Características gerais:
- Apenas um DTE por vez controla o modem.
- Ambos os lados (DTE e modem) podem enviar pacotes assincronamente — threads independentes de leitura são obrigatórias em cada lado.
- Todos os campos multi-byte em **network byte order** (big-endian).
- Porta TCP padrão sugerida: **3000** (configurável no modem).

---

## 3. Formato de Pacotes (TCP — Appendix A §A.5.1)

### 3.1 Formato do pacote (header + payload + CRC)

Cada pacote possui um header de 8 bytes, seguido de payload variável e CRC-16 de payload:

```
 Byte 0    Byte 1    Byte 2    Byte 3    Bytes 4-5       Bytes 6-7
┌─────────┬─────────┬─────────┬─────────┬───────────────┬──────────────┐
│  0x49   │  0x50   │  0x55   │  Type   │ PayloadSize   │  HeaderCRC   │
│ Preamble│ Preamble│ Preamble│ (1 byte)│   (16 bits)   │  (16 bits)   │
└─────────┴─────────┴─────────┴─────────┴───────────────┴──────────────┘
             seguido de:
┌──────────────────────────────────────┬──────────────┐
│  Payload (0 .. 4086 bytes)           │ PayloadCRC   │
│  (presente apenas se PayloadSize > 0)│  (16 bits)   │
└──────────────────────────────────────┴──────────────┘
```

- Comprimento total máximo do pacote: **4096 bytes**.
- Payload máximo: **4086 bytes** (4096 − 8 header − 2 CRC payload).
- O HeaderCRC cobre apenas os 6 primeiros bytes do header.
- Pacotes com falha em qualquer CRC são **descartados silenciosamente** (sem NACK gerado).

### 3.2 Tipos de pacote (campo Type)

| Valor  | Nome         | Direção      | Descrição                                      |
|--------|--------------|--------------|------------------------------------------------|
| `0x00` | DATA         | ambos        | Transporte de dados e comandos de controle     |
| `0x01` | CONNECT      | DTE → modem  | Conexão inicial TCP                            |
| `0x02` | CONNECTACK   | modem → DTE  | Confirmação da conexão inicial                 |
| `0xFF` | ERROR        | ambos        | Erro de formato ou protocolo; fecha conexão    |

Valores não especificados **não devem ser enviados**; se recebidos, resultam em resposta ERROR e fechamento da conexão TCP.

### 3.3 Cálculo do CRC-16

Polinômio: `x^16 + x^15 + x^12 + x^11 + x^8 + x^6 + x^3 + x^0`.

O registrador CRC é inicializado em `0x0000`. Os bits são inseridos a partir do LSB do primeiro byte.

```c
unsigned short CalculateCRC16(unsigned char *pData, unsigned short nBytes)
{
    unsigned short nCrc = 0x0000;
    for (unsigned short i = 0; i < nBytes; i++) {
        for (unsigned char j = 0x01; j; j <<= 1) {
            unsigned char bit = (((nCrc & 0x0001) ? 1 : 0) ^
                                 ((pData[i] & j)  ? 1 : 0));
            nCrc >>= 1;
            if (bit) nCrc ^= 0x9299;  /* representação do polinômio */
        }
    }
    return nCrc;
}
```

O CRC é transmitido **MSB primeiro** (big-endian), mas o algoritmo acima já produz os bits na ordem correta.

---

## 4. Protocolo de Conexão TCP Inicial

### 4.1 Sequência de estabelecimento

```
DTE (STANAG 5066)              Modem (188-110D)
        │                            │
        │──── TCP connect ──────────►│  porta configurável (sugerida: 3000)
        │                            │
        │──── CONNECT (ver=12) ─────►│  simultâneo; timeout: 3 s
        │◄─── CONNECT (ver=12) ──────│
        │                            │
        │──── CONNECTACK (ver=12) ──►│  simultâneo; timeout: 3 s
        │◄─── CONNECTACK (ver=12) ───│
        │                            │
        │◄─── CONNECTION_PROBE ──────│  modem envia primeiro
        │──── CONNECTION_PROBE ─────►│  DTE responde; timeout: 6 s
        │                            │
        │◄─── Initial Setup (0x0A) ──│  parâmetros do socket
        │◄─── Transmit Setup (0x09) ─│  taxa TX e blocking factor
        │◄─── Tx Status (FLUSHED) ───│  transmissor ocioso
        │◄─── Carrier Detect ────────│  estado inicial do receptor
        │                            │
        │       [conexão ativa]       │
```

Payload de CONNECT e CONNECTACK: **1 byte** com o número de versão (`0x0C` = 12). Se a versão diferir de 12, a conexão TCP é encerrada imediatamente.

### 4.2 Keep-alive

Após a conexão estar ativa, se nenhum pacote (dado, status, controle) for enviado por **2 segundos**, qualquer lado deve enviar um pacote DATA com payload vazio (0 bytes) como keep-alive.

Se o timer de keep-alive atingir **30 segundos** sem receber nenhum pacote DATA, a entidade cujo timer expirou encerra a conexão TCP.

---

## 5. Comandos do Payload (DATA packets)

O primeiro byte do payload de qualquer pacote DATA é o **PayloadCommand**. Os comandos válidos são:

| Cmd byte | Nome              | Enviado por | Descrição                                                |
|----------|-------------------|-------------|----------------------------------------------------------|
| `0x00`   | Data Transfer     | ambos       | D-PDUs do 5066 ou dados recebidos OTA                   |
| `0x01`   | Transmit Arm      | DTE         | Arma as filas de transmissão do modem                    |
| `0x02`   | Transmit Start    | DTE         | Inicia a transmissão após pré-enchimento                 |
| `0x03`   | Request Tx Status | DTE         | Solicita status imediato do transmissor                  |
| `0x04`   | Tx Data NAK       | modem       | Rejeição de pacote de dados; inclui causa e Packet ID    |
| `0x05`   | Tx Status         | modem       | Estado do transmissor + ocupação da FIFO                 |
| `0x06`   | Abort Reception   | DTE         | Aborta recepção em curso; força retorno ao sync-acquire  |
| `0x08`   | Carrier Detect    | modem       | Mudança no estado do receptor (NO_CARRIER / CARRIER_DETECTED) |
| `0x09`   | Transmit Setup    | modem       | Taxa TX e blocking factor correntes                      |
| `0x0A`   | Initial Setup     | modem       | Parâmetros do socket (RTT, latência, modo sínc/assínc)   |
| `0x0B`   | Connection Probe  | ambos       | Usado no handshake inicial para medir RTT                |

### 5.1 Data Transfer (0x00) — transporte dos D-PDUs do 5066

Este é o comando que carrega os D-PDUs do STANAG 5066. Formato:

```
┌────────┬─────────────┬──────────────────────┬──────────────────────┐
│  0x00  │ PacketOrder │    PacketID (12 B)    │  Data (0..4072 B)    │
│ (1 B)  │   (1 B)     │                      │  ← D-PDUs do 5066    │
└────────┴─────────────┴──────────────────────┴──────────────────────┘
```

Valores de PacketOrder:

| Valor | Nome           | Descrição                                            |
|-------|----------------|------------------------------------------------------|
| `1`   | FIRST_ONLY     | Primeiro pacote de uma transmissão multi-pacote      |
| `2`   | FIRST_AND_LAST | Único pacote de uma transmissão completa             |
| `3`   | CONTINUATION   | Pacote intermediário de uma transmissão multi-pacote |
| `4`   | LAST           | Último pacote de uma transmissão multi-pacote        |

O PacketID (12 bytes) é um identificador único por pacote, usado apenas em NACKs.

### 5.2 Tx Status (0x05) — estados do transmissor

```
┌────────┬────────────────┬──────────────────┬──────────────────┬──────────────────────┬──────────────────────┐
│  0x05  │ TxState (1 B)  │ FIFO Space (4 B) │ FIFO Fill (4 B)  │ Critical ms (4 B)    │ Critical bytes (4 B) │
└────────┴────────────────┴──────────────────┴──────────────────┴──────────────────────┴──────────────────────┘
```

Estados do transmissor (TxState):

| Valor | Nome                            | Descrição                                                  |
|-------|---------------------------------|------------------------------------------------------------|
| `1`   | FLUSHED                         | Transmissor ocioso (estado inicial após conexão)           |
| `2`   | QUEUES_ARMED_AND_PORT_NOT_READY | Filas armadas; ainda não pode iniciar transmissão          |
| `3`   | QUEUES_ARMED_AND_PORT_READY     | Filas armadas e prontas; DTE pode enviar TRANSMIT_START    |
| `4`   | STARTED                         | Transmissão em curso                                       |
| `5`   | DRAINING_OK                     | Drenagem normal após receber pacote LAST                   |
| `6`   | DRAINING_FORCED                 | Drenagem forçada por underrun da FIFO                      |

### 5.3 Carrier Detect (0x08)

```
┌────────┬──────────────────┬────────────────────────┬──────────────────────────┐
│  0x08  │ CarrierState (1B)│  RxDataRate (32 bits)  │  RxBlockingFactor (32 b) │
└────────┴──────────────────┴────────────────────────┴──────────────────────────┘
```

| CarrierState | Nome              | Descrição                                              |
|--------------|-------------------|--------------------------------------------------------|
| `0`          | NO_CARRIER        | Receptor ocioso                                        |
| `1`          | CARRIER_DETECTED  | Preamble detectado ou dados sendo recebidos do canal   |

### 5.4 Tx Data NAK (0x04)

```
┌────────┬──────────┬────────────────────────┐
│  0x04  │ Causa(1B)│  NACKed PacketID (12B) │
└────────┴──────────┴────────────────────────┘
```

| Causa | Nome                     | Descrição                                                    |
|-------|--------------------------|--------------------------------------------------------------|
| `0`   | TRANSMIT_QUEUES_NOT_ARMED | Filas não estavam armadas                                   |
| `1`   | TRANSMIT_UNDERRUN        | FIFO do transmissor esvaziou (DRAINING_FORCED)               |
| `2`   | MISSING_FIRST_PACKET     | Nenhum pacote FIRST_ONLY recebido para esta transmissão      |
| `3`   | MULTIPLE_FIRST_PACKET    | Mais de um pacote FIRST_ONLY recebido para a mesma transmissão |

### 5.5 Initial Setup (0x0A)

Enviado pelo modem ao DTE logo após o handshake inicial:

```
┌────────┬──────────────┬────────────────────┬────────────────────┬───────────┬──────────────┬──────────────┬──────────────┬──────────────┐
│  0x0A  │  RTT (32 b)  │  Min Latency (32b) │  Max Latency (32b) │ SyncFlag  │  AsyncDtBit  │ AsyncStopBit │  AsyncParity │ AsyncDtMode  │
│        │  ms          │  ms                │  ms                │  (1 B)    │  (1 B)       │  (1 B)       │  (1 B)       │  (1 B)       │
└────────┴──────────────┴────────────────────┴────────────────────┴───────────┴──────────────┴──────────────┴──────────────┴──────────────┘
```

SyncFlag: `0` = modo assíncrono; `1` = modo síncrono (campos Async ignorados).

O STANAG 5066 deve operar em **modo síncrono** para garantir que os D-PDUs não carreguem bits de framing assíncrono (start/stop bits) no canal OTA, conforme a nota do Annex D.

### 5.6 Transmit Setup (0x09)

Enviado pelo modem ao DTE após handshake e após qualquer mudança de configuração:

```
┌────────┬──────────────────────┬────────────────────────────┐
│  0x09  │  TxDataRate (32 bits)│  TxBlockingFactor (32 bits)│
│        │  bits/s              │  bits                      │
└────────┴──────────────────────┴────────────────────────────┘
```

O TxBlockingFactor indica o tamanho do bloco de interleaving em bits — o DTE deve enviar dados em múltiplos desse valor para evitar underrun.

---

## 6. Fluxo de Transmissão (DTE → Modem → OTA)

### 6.1 Máquina de estados do transmissor

```
         DISCONNECTED
              │ TCP connect
              ▼
           FLUSHED ◄──────────────────────────── transmissão completa
              │ DTE envia TRANSMIT_ARM (0x01)
              ▼
   QUEUES_ARMED_AND_PORT_NOT_READY
              │ (half-duplex receiver master: aguarda fim da recepção)
              │ ou imediato em full-duplex/transmitter master
              ▼
   QUEUES_ARMED_AND_PORT_READY
              │ DTE envia ≥3 blocking factors de dados (pré-enchimento)
              │ DTE envia TRANSMIT_START (0x02)
              ▼
           STARTED ──────────► DTE envia CONTINUATION packets
              │ pacote LAST recebido sem underrun
              ▼
         DRAINING_OK
              │ modem termina de esvaziar a FIFO
              ▼
           FLUSHED

         (underrun em qualquer momento → DRAINING_FORCED → FLUSHED)
```

### 6.2 Procedimento passo a passo

1. Aguardar Tx Status = **FLUSHED** (estado após conexão ou transmissão anterior).
2. Enviar `TRANSMIT_ARM` (DATA packet, PayloadCommand `0x01`, payload = apenas o byte do comando).
3. Aguardar Tx Status = `QUEUES_ARMED_AND_PORT_READY` (ou `NOT_READY` em half-duplex receiver master — aguardar até virar READY).
4. Enviar pelo menos **3 blocking factors** de dados como pré-enchimento do interleaver do modem. Cada pacote usa o formato Data Transfer (0x00). O primeiro pacote deve ter PacketOrder = `FIRST_ONLY` ou `FIRST_AND_LAST`.
5. Após receber Tx Status = `QUEUES_ARMED_AND_PORT_READY`, enviar `TRANSMIT_START` (PayloadCommand `0x02`).
6. Aguardar Tx Status = `STARTED`; se não vier em 10 ms, retransmitir TRANSMIT_START.
7. Continuar enviando pacotes DATA (PacketOrder = `CONTINUATION`) conforme a FIFO do modem abre espaço (backpressure via TCP blocking).
8. Monitorar os campos `FIFO_Critical_Milliseconds` e `FIFO_Critical_Bytes` do Tx Status para alimentar o modem antes do underrun.
9. No último D-PDU da janela, enviar pacote com PacketOrder = `LAST` (pode ter zero bytes de dados).
10. Aguardar Tx Status = `DRAINING_OK` → `FLUSHED`.

**Mapeamento de D-PDUs do 5066 para pacotes DATA:**

```
STANAG 5066 Data Transfer Sublayer
  └── D-PDU stream (incluindo pre-fill e post-fill per Annex D)
         │
         ▼
  Appendix A DATA packet:
    Header (8B) + [0x00 | PacketOrder | PacketID(12B) | D-PDU bytes] + PayloadCRC(2B)
         │
         ▼ TCP socket
  Modem 188-110D: modula com waveform serial tone (PSK, 2400 sym/s)
    preamble (200 ms) → data phase → EOM → flush
```

O pre-fill do Annex D da 5066 corresponde diretamente ao pré-enchimento de ≥3 blocking factors exigido pelo Appendix A antes do TRANSMIT_START.

---

## 7. Fluxo de Recepção (OTA → Modem → DTE)

### 7.1 Máquina de estados do receptor

```
DISCONNECTED
     │ TCP connect
     ▼
NO_CARRIER (sync-acquire)
     │ preamble detectado
     ▼
CARRIER_DETECTED  ──► modem envia Carrier Detect (CarrierState=1, RxRate, BlockingFactor)
     │ interleaver preenchido
     ▼
RECEIVING         ──► modem envia pacotes DATA (primeiro: FIRST_ONLY ou FIRST_AND_LAST)
     │ EOM detectado ou recepção abortada
     ▼
NO_CARRIER        ──► modem envia Carrier Detect (CarrierState=0)
                       ──► modem envia último pacote DATA (LAST, pode ter 0 bytes)
```

### 7.2 Procedimento de recepção no DTE (5066)

1. Monitorar pacotes Carrier Detect recebidos do modem.
2. Ao receber `CarrierState = CARRIER_DETECTED`: registrar `RxDataRate` e `RxBlockingFactor` — esses valores informam o 5066 sobre a taxa recebida (relevante para DRC advisory).
3. Receber pacotes Data Transfer (0x00): extrair os bytes do payload (após PacketOrder e PacketID) e entregar ao Data Transfer Sublayer como fluxo de D-PDUs.
4. Respeitar a ordem: FIRST_ONLY → CONTINUATION → LAST.
5. Ao receber `CarrierState = NO_CARRIER` após uma recepção: a transmissão OTA terminou.
6. Para abortar uma recepção em curso: enviar `Abort Reception` (PayloadCommand `0x06`).

---

## 8. Controle de Taxa — DRC (Data Rate Control)

O STANAG 5066 §C.6.4 define o protocolo DRC para adaptação de taxa. A interface com o modem ocorre em dois planos:

### 8.1 Plano de controle (peer-to-peer via OTA)

Os D-PDUs TYPE 6 (MANAGEMENT) carregam EOW messages:

| EOW Type | Nome                      | Uso                                                        |
|----------|---------------------------|------------------------------------------------------------|
| 1        | Data Rate Change Request  | Nó receptor informa/solicita nova taxa e interleaving      |
| 2        | Data Rate Change Response | Nó transmissor aceita, recusa ou confirma a mudança        |
| 4        | Capability Advertisement  | Anuncia capacidades: DRC, waveforms, full duplex, etc.     |

O Type 4 EOW inclui o bit 7 (DRC capable) e o bit 5 (MIL-STD-188-110 75–2400 bps disponível).

Taxa inicial de todas as conexões DRC: **300 bps, interleaving SHORT** (§C.6.4.1).

### 8.2 Plano de configuração do modem local (via Appendix A)

Após uma decisão DRC, o DTE configura o modem local via comandos. O Appendix A não define comandos explícitos de taxa — a configuração de taxa e interleaving é feita via mecanismo fora do escopo do Appendix A (p.ex., interface de gerenciamento do modem ou reconexão TCP com nova configuração).

Quando a configuração muda, o modem pode fechar a conexão TCP; o DTE deve detectar o fechamento e reconectar.

O Annex E do 5066 (informativo) documenta comandos ASCII de controle remoto equivalentes:

| Comando ASCII                   | Função                          |
|---------------------------------|---------------------------------|
| `<addr> MODEM RATE <x>`         | Seleciona taxa: 75..2400 bps    |
| `<addr> MODEM INTERLEAVE <x>`   | Seleciona interleaving: ZERO/SHORT/LONG |
| `<addr> MODEM WAVEFORM <110A>`  | Seleciona waveform MIL-STD-188-110A serial tone |
| `<addr> MODEM SNR?`             | Solicita SNR atual do receptor  |

---

## 9. Modos Half-Duplex

O Appendix A suporta dois modos half-duplex além do full-duplex:

### 9.1 Transmitter Master

O transmissor tem prioridade. Se uma recepção estiver em curso e o DTE enviar TRANSMIT_ARM, o modem inicia a transmissão e **aborta a recepção** — envia um pacote LAST para o DTE e retorna a NO_CARRIER.

### 9.2 Receiver Master

O receptor tem prioridade. Se uma recepção estiver em curso e o DTE enviar TRANSMIT_ARM, o modem transita para `QUEUES_ARMED_AND_PORT_NOT_READY` e **mantém a recepção**. O DTE pode pré-encher as filas mas não pode enviar TRANSMIT_START enquanto o receptor estiver ativo. Quando a recepção termina, o modem envia Tx Status = `QUEUES_ARMED_AND_PORT_READY` e o DTE pode prosseguir.

O STANAG 5066 opera naturalmente em half-duplex sobre HF; o modo **Receiver Master** é o mais adequado para garantir que D-PDUs de ACK/NACK inbound não sejam perdidos.

---

## 10. Interface UDP (Appendix A §A.5.2)

Para redes com alta latência ou perda de pacotes não nula, o Appendix A define também um protocolo UDP. O header UDP é de **4 bytes**:

```
┌──────────────┬──────────────┬──────────────────────────────┐
│ Version (4b) │ PacketType(4b)│  Session ID (24 bits)        │
└──────────────┴──────────────┴──────────────────────────────┘
```

Comprimento máximo do pacote UDP: **1226 bytes** (4 header + 1220 payload + 2 CRC).

Diferenças em relação ao TCP:
- Não há CONNECT/CONNECTACK — a conexão de controle é estabelecida via TCP (RCI, fora do escopo do Appendix A) antes de qualquer tráfego UDP.
- Keep-alive: PING REQUEST a cada **5 s**; timeout de **30 s** sem PING REPLY aborta transmissão.
- O DTE é responsável por reordenar pacotes e descartar os que chegam atrasados.
- O modem decodifica interleaver sets à medida que chegam, substituindo pacotes faltantes por erasures no decoder FEC/RS.

Para implementações loopback (mesma máquina), TCP é preferível por garantir ordem e entrega sem overhead de reordenação.

---

## 11. Considerações de Implementação

### 11.1 Threading

Ambos os lados (DTE e modem) **devem** usar threads separadas para leitura do socket. Sem isso, um lado que aguarda resposta pode bloquear indefinidamente.

Estrutura mínima no processo 5066 (DTE):

```
thread_rx:  loop { recv(sock) → dispatch by PayloadCommand }
thread_tx:  loop { wait D-PDU from DTS → arm → fill → start → send DATA packets }
```

### 11.2 Controle de fluxo e underrun

O campo `FIFO_Critical_Bytes` do Tx Status indica quantos bytes devem ser entregues antes de `FIFO_Critical_Milliseconds` expirar. O DTE deve monitorar esse campo e enviar dados com antecedência suficiente, especialmente em redes com latência não trivial.

Para operação em modo síncrono com taxas baixas (p.ex. 300 bps com interleaving LONG), o blocking factor é grande e o tempo de enchimento do interleaver pode ser de vários segundos — o DTE deve pre-encher as filas antes do TRANSMIT_START.

### 11.3 Reconexão após mudança de configuração

Sempre que o modem alterar sua configuração (p.ex. após DRC), ele pode fechar a conexão TCP. O DTE deve detectar o fechamento via `recv() == 0` ou `ECONNRESET`, aguardar brevemente, e reconectar executando o handshake completo (CONNECT → CONNECTACK → CONNECTION_PROBE → Initial/Transmit Setup).

### 11.4 Mapeamento D-PDU → Appendix A DATA packet

Para cada janela de transmissão do 5066 (conjunto de D-PDUs entre duas janelas de silêncio):

```c
/* Pseudocódigo do DTE (processo 5066) */

// 1. Aguardar FLUSHED
wait_tx_status(FLUSHED);

// 2. Armar
send_payload_cmd(TRANSMIT_ARM);
wait_tx_status(QUEUES_ARMED_AND_PORT_READY);

// 3. Pré-encher (≥ 3 × blocking_factor bytes)
size_t prefill = 3 * tx_blocking_factor_bytes;
send_data_transfer(FIRST_ONLY, prefill_bytes, prefill);

// 4. Iniciar
send_payload_cmd(TRANSMIT_START);
wait_tx_status(STARTED);

// 5. Enviar D-PDUs
while (has_more_dpdu()) {
    dpdu = next_dpdu();
    bool is_last = !has_more_dpdu();
    uint8_t order = is_last ? ORDER_LAST : ORDER_CONTINUATION;
    send_data_transfer(order, dpdu->data, dpdu->len);
}

// 6. Aguardar FLUSHED
wait_tx_status(DRAINING_OK);
wait_tx_status(FLUSHED);
```

---

## 12. Resumo dos Timers

| Timer                       | Valor    | Condição de disparo                                       |
|-----------------------------|----------|-----------------------------------------------------------|
| CONNECT timeout             | 3 s      | Sem CONNECT após TCP estabelecido                         |
| CONNECTACK timeout          | 3 s      | Sem CONNECTACK após enviar CONNECT                        |
| CONNECTION_PROBE timeout    | 6 s      | Modem sem resposta ao CONNECTION_PROBE                    |
| Keep-alive interval         | 2 s      | Sem pacote DATA enviado por qualquer lado                 |
| Keep-alive timeout          | 30 s     | Sem pacote DATA recebido por qualquer lado                |
| TRANSMIT_START retry        | 10 ms    | Tx Status não = STARTED após TRANSMIT_START               |
| Tx Status update interval   | ≤ 2 s    | Modem em STARTED/DRAINING deve atualizar DTE periodicamente |
| UDP keep-alive interval     | 5 s      | PING REQUEST periódico (modo UDP)                         |
| UDP keep-alive timeout      | 30 s     | Sem PING REPLY (modo UDP)                                 |
