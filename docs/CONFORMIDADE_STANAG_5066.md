# Relatório de Conformidade — STANAG 5066 Edição 3

**Data:** 2026-04-30
**Repositório:** `rds-5066`
**Norma de referência:** STANAG 5066 Edição 3 — Anexos A, B, C, F (`docs/STANAG_5066_v3_ANEXO_*.md`)
**Cobertura de testes:** 450 testes pytest, todos passando

---

## 1. Sumário Executivo

A implementação está **funcionalmente operante** e atende ao núcleo do protocolo (CRC-16/32 com vetores oficiais validados, sincronização Maury-Styles 0xEB90, enums DPDUType/CPDUType bit-corretos, máquina ARQ sliding-window, Raw SIS Socket TCP/5066, todos os clientes Anexo F principais).

Auditoria independente cruzada (4 agentes especialistas + revisão direta) identificou **55 itens de não-conformidade** com a Edição 3, classificados por severidade:

| Severidade | Anexo A (SIS) | Anexo B (CAS) | Anexo C (DTS) | Anexo F (Clientes) | **Total** |
|---|:-:|:-:|:-:|:-:|:-:|
| **CRÍTICA** | 4 | 0 | 0 | 0 | **4** |
| **ALTA**    | 4 | 1 | 4 | 4 | **13** |
| **MÉDIA**   | 6 | 3 | 5 | 6 | **20** |
| **BAIXA**   | 5 | 5 | 5 | 3 | **18** |

**Conclusão geral:** o repositório está em **~85 % de conformidade** com a Edição 3. Os 4 itens críticos concentram-se em **gestão de Hard Link** (Anexo A) e devem ser tratados antes de qualquer interoperabilidade entre nós conformantes. Os 13 itens ALTA podem comprometer interoperabilidade real (estado no Tipo 4 C_PDU ID, Expedited ACK rx_lwe, ack id de FRAP, send_ppp).

---

## 2. Vetores Oficiais Validados (passam byte-a-byte)

| Item | Norma | Resultado |
|---|---|---|
| Maury-Styles 0xEB90 → wire `0x90 0xEB` | C.2.1 §(4)(5) | ✅ `src/stypes.py:17` |
| CRC-16 reflected poly `0x9299` | C.3.2.8 §(2) | ✅ `src/crc.py:18` |
| CRC-32 reflected poly `0xF3A4E550` | C.3.2.11 | ✅ `src/crc.py:79` |
| CRC-32 vetor `F0 00 00 47 05 64 02` → `0xF4178F95` | Code Example C-2 | ✅ Verificado em runtime |
| Warning DPDU `90 EB F0 00 00 47 05 64 02 5F 1E` | C.3.2.8 §(827) | ✅ Encoder reproduz exato |
| HDR_SIZE exclui address, inclui CRC-16 | C.3.2.5 §(675) | ✅ `dpdu_frame.py:358` |
| Header CRC abrange common+address+type-specific | C.3.2.8 §(729) | ✅ `dpdu_frame.py:370` |
| DPDUType 0,1,2,3,4,5,6,7,8,15 | Anexo C, Tabela implícita | ✅ `stypes.py:22-32` |
| CPDUType 0..5 + Tabelas B-4/B-5 reasons | Anexo B | ✅ `stypes.py:35-60` |
| Raw SIS Socket TCP porta 5066 | F.16 | ✅ `raw_sis_socket.py` |
| SAP map (0,1,3,4,5,6,7,8,9,12) Tabela F-1 | F.0 | ✅ Todos os clientes corretos |
| RCOP/UDOP header 6 bytes `>BBHH` + APP_ID | F.8.1 | ✅ `rcop.py:72-88` |
| BFTP APP_ID 0x1002, FRAP 0x100B, FRAPv2 0x100C | F.10, Tabela F-5 | ✅ `rcop.py:38-40` |
| 450 testes pytest passando | — | ✅ |

---

## 3. Não-Conformidades Detalhadas

### 3.1 ANEXO A (SIS) — Subnetwork Interface Sublayer

#### [CRÍTICA-A1] Hard Link control via canal errado (Non-ARQ ao invés de Expedited ARQ)
- **Cláusula:** A.3.2.2.2 §11
- **Local:** `src/stanag_node.py:964-966` (`_send_control_expedited`)
- **Sintoma:** S_PDUs tipo 3-7 (REQUEST/CONFIRM/REJECT/TERMINATE/TERM_CONFIRM) são enviados via `DPDUType.EXPEDITED_NON_ARQ` (D_PDU Tipo 8 — broadcast não confiável). A norma exige Expedited **ARQ** (D_PDU Tipo 4) com confirmação stop-and-wait.
- **Impacto:** controle de Hard Link sujeito a perda silenciosa; protocolo de estabelecimento pode falhar em condições de ruído.
- **Correção:** rotear via `self.expedited_arq.submit_cpdu(self._wrap_in_cpdu(payload))` após CAS estar `MADE`; manter Non-ARQ apenas como pré-CAS.

#### [CRÍTICA-A2] Limitação de Expedited Requests é código morto
- **Cláusula:** A.2.1.10 §3-4
- **Local:** `src/stanag_node.py:1221-1230` (definido), `:373-390` (não chamado)
- **Sintoma:** `track_expedited_request()` está implementado mas nunca é invocado em `expedited_unidata_request()`. Cliente pode submeter expedited ilimitadamente sem sofrer disconnect.
- **Correção:** chamar `track_expedited_request(sap_id)` no início de `expedited_unidata_request`; ao retornar `False`, emitir `S_UNBIND_INDICATION reason=4` ("too many expedited-data requests").

#### [CRÍTICA-A3] Hard Link de menor precedência não é terminado antes de aceitar novo
- **Cláusula:** A.3.2.2.2 §8
- **Local:** `src/stanag_node.py:907-916`
- **Sintoma:** ao receber REQUEST que vence em precedência, a sessão é sobrescrita in-place; o owner anterior nunca recebe `S_HARD_LINK_TERMINATED reason=2` ("HIGHER_PRIORITY_LINK_REQUESTED"); nenhum TERMINATE é enviado ao peer remoto.
- **Correção:** antes do reassign, executar protocolo TERMINATE (S_PDU tipo 6) ao peer corrente e disparar callback `hard_link_terminated(reason=2)` ao owner local.

#### [CRÍTICA-A4] Precedência Hard Link incompleta + REJECT silencioso
- **Cláusula:** A.3.2.2.1 §(1)(2)(3)(4)(6)
- **Local:** `src/stanag_node.py:881-919`
- **Sintoma:** `requester_rank=0` hard-coded ignora regra (1); regras (3) e (4) parcialmente implementadas; quando perde a precedência, faz `return` silencioso em vez de enviar REJECTED com reason adequado (1=Busy / 2=Higher-Priority-Existing / 5=Type0-Exists). S_PDU tipo 3 não carrega Rank, exigindo extensão ou default configurável por SAP.
- **Correção:** sempre emitir `S_HARD_LINK_REJECTED` com reason apropriada; introduzir tabela rank-por-SAP (configurada localmente) para resolver a regra (1).

#### [ALTA-A1] Rank não validado e Rank 15 sem autorização
- **Cláusula:** A.2.1.1 §(5)(6)
- **Local:** `src/stanag_node.py:256-281`, `src/raw_sis_socket.py:179-225`
- **Sintoma:** `bind(rank=99)` é aceito; rank=15 (gerência) não exige autorização explícita.
- **Correção:** validar `0 ≤ rank ≤ 15`; expor flag de autorização para rank=15 (ex.: configuração `allow_management_rank_clients`); rejeitar com `S_BIND_REJECTED reason=NOT_AUTHORIZED`.

#### [ALTA-A2] Link Priority não restrito a 0-3
- **Cláusula:** A.2.1.11 §3
- **Local:** `src/stanag_node.py:413`
- **Sintoma:** `min(15, max(0, link_priority))` permite até 15. S_PDU tipo 3 reserva apenas 2 bits (0-3).
- **Correção:** `min(3, max(0, link_priority))`.

#### [ALTA-A3] Terminate Hard Link aceita qualquer SAP local quando owner=-1
- **Cláusula:** A.2.1.12 §2
- **Local:** `src/stanag_node.py:419-432`
- **Sintoma:** quando `hard_link_owner == -1` (Type 0/1 lado chamado), qualquer SAP local pode invocar terminate.
- **Correção:** rastrear o originador local mesmo em Type 0/1 e validar.

#### [ALTA-A4] DATA DELIVERY CONFIRM/FAIL não copia campos S_PCI do DATA original
- **Cláusula:** A.3.1.2 §(7), A.3.1.3 análoga
- **Local:** `src/sis.py:206-225`
- **Sintoma:** spec exige "remaining fields shall be equal in value to the corresponding fields of the DATA S_PDU"; código emite apenas `[type<<4, src|dst, ...]` sem PRIORITY/VTTD/TTD.
- **Correção:** propagar PRIORITY, VALID_TTD, Julian, GMT do S_PDU original.

#### [MÉDIA-A1] DELIVERY MODE codec pode estar faltando `min_retransmissions`
- **Cláusula:** A.2.2.28.2 (Fig A-29)
- **Local:** `src/s_primitive_codec.py:91-107`
- **Correção:** confirmar contra Fig A-29 e ampliar codec se necessário.

#### [MÉDIA-A2] Bit DELIVERY_CONFIRM mistura node OR client
- **Cláusula:** A.3.1.1 §13-14
- **Local:** `src/sis.py:63-64`
- **Correção:** codificar somente `client_delivery_confirm_required`.

#### [MÉDIA-A3] Decode S_PRIMITIVE não valida versão
- **Cláusula:** A.2.2 (versão = 0x00)
- **Local:** `src/s_primitive_codec.py:45`
- **Correção:** `if version != VERSION: raise ValueError(...)`.

#### [MÉDIA-A4] TERMINATE não notifica todos clientes do Hard Link
- **Cláusula:** A.3.2.2.3 §3
- **Local:** `src/stanag_node.py:941-958`
- **Correção:** iterar SAPs ativos no link e disparar `S_HARD_LINK_TERMINATED` para cada.

#### [MÉDIA-A5] `hard_link_terminate` sempre envia reason=1
- **Local:** `src/stanag_node.py:432`
- **Correção:** aceitar parâmetro `reason: int`.

#### [MÉDIA-A6] Indicação Type 2 pendente única
- **Local:** `src/sis.py:331`, `src/stanag_node.py:897-906`
- **Sintoma:** segunda Type 2 que chegue antes de ACCEPT/REJECT sobrescreve a primeira.
- **Correção:** fila ou rejeitar imediatamente segunda indicação.

#### [BAIXA-A1] SAP 0 sem proteção de gerência
- **Cláusula:** Tabela F-1
- **Correção:** exigir rank=15 ou flag de autorização para `bind(sap_id=0)`.

#### [BAIXA-A2] TTL=0 mapeado para 7 dias em vez de "infinito"
- **Cláusula:** A.2.1.5 §8
- **Local:** `src/stanag_node.py:347`

#### [BAIXA-A3] `decode_spdu` genérico não trata tipos 3-7
- **Local:** `src/sis.py:259-281`

#### [BAIXA-A4] `link_priority=5` hard-coded no callback do socket
- **Local:** `src/raw_sis_socket.py:381-391`

#### [BAIXA-A5] `S_SUBNET_AVAILABILITY` nunca emitida
- **Cláusula:** A.2.1.18-20

---

### 3.2 ANEXO B (CAS) — Channel Access Sublayer

#### [ALTA-B1] `CASEngine.send_data` só suporta Non-ARQ
- **Cláusula:** B.3.1 §(5)(6)(7)
- **Local:** `src/cas.py:286-298`
- **Sintoma:** `CASEngine.send_data()` sempre enfileira via `non_arq.queue_cpdu`. Em `StanagNode` o roteamento ARQ/Non-ARQ é feito por outro caminho, mas qualquer cliente que use `CASEngine.send_data` diretamente violaria a shall.
- **Correção:** aceitar `use_arq: bool` e despachar para `arq.submit_cpdu` quando ARQ; ou documentar como API privada.

#### [MÉDIA-B1] `LINK_BREAK` recebido emite evento mesmo sem contexto de link
- **Local:** `src/cas.py:362-368`
- **Correção:** filtrar emissão de evento `IDLE` quando `ctx is None`.

#### [MÉDIA-B2] `received_cpdus` em `StanagNode` tem semântica mista
- **Local:** `src/stanag_node.py:595-596` (Non-ARQ) vs `:609` (ARQ)
- **Correção:** separar em `received_data_cpdus` e `received_control_cpdus`.

#### [MÉDIA-B3] Decodificador permissivo em campos reservados
- **Cláusula:** B.3 §(8), B.3.1.2 §(4)
- **Local:** `src/cas.py:79, :81, :85`
- **Correção:** logar warning quando bits NOT_USED ≠ 0 em modo strict.

#### [BAIXA-B1] Reasons 4-15 não são unspecified explicitamente
- **Local:** `src/stypes.py:44-60`
- **Comentário:** comportamento conforme; sem ação.

#### [BAIXA-B2] `_handle_link_request` usa `REASON_UNKNOWN` em rejeição por excesso
- **Local:** `src/cas.py:493-496`
- **Comentário:** valor 0 é sempre válido pela norma; sem ação.

#### [BAIXA-B3] Idle timeout do Called não emite `LINK_BREAK`
- **Cláusula:** B.3.2.1 §16
- **Local:** `src/cas.py:410-417`
- **Correção (recomendada):** emitir `LINK_BREAK reason=NO_MORE_DATA` antes de remover.

#### [BAIXA-B4] `make_link` não verifica precondição B.3.2 (4) para Nonexclusive
- **Local:** `src/cas.py:246-268`

#### [BAIXA-B5] `decode_cpdu` aceita DATA C_PDU com payload vazio
- **Local:** `src/cas.py:76-77`

---

### 3.3 ANEXO C (DTS) — Data Transfer Sublayer

#### [ALTA-C1] D_PDU Tipo 4: C_PDU ID NUMBER deveria ser 4 bits, código transmite 8
- **Cláusula:** C.3.7 §7 (modulo 16)
- **Local:** `src/dpdu_frame.py:144,265`, `src/stypes.py:158`
- **Sintoma:** Encoder grava `cpdu_id` como byte completo. Como `expedited_arq.py:33` restringe a 0..15, na prática o nibble alto é zero, mas peers que validem máscara estrita não interoperam.
- **Correção:** mascarar `cpdu_id & 0x0F` no encoder e validar em `DataHeader.__post_init__` quando `dpdu_type == EXPEDITED_DATA_ONLY`.

#### [ALTA-C2] DROP_PDU não é ACK positivo independentemente do CRC
- **Cláusula:** C.3.4 §7
- **Local:** `src/arq.py:666-671`
- **Sintoma:** qualquer `data_crc_ok is False` marca slot como `ERROR` (NACK). A norma diz que frames com DROP_PDU set devem ser ACKed mesmo com payload corrompido.
- **Correção:** `if dpdu.data and dpdu.data.drop_pdu: status = RECEIVED` antes do teste de CRC.

#### [ALTA-C3] Expedited ACK envia `rx_lwe = seq` em vez de `seq + 1`
- **Cláusula:** C.6.2 §12 + C.3.4 §3
- **Local:** `src/expedited_arq.py:273`
- **Sintoma:** RX LWE deve ser "oldest D_PDU number that has not been received". Peer ARQ conformante considerará `seq` ainda outstanding, falhando o stop-and-wait.
- **Correção:** `build_expedited_ack_only(..., rx_lwe=(seq + 1) & 0xFF)`.

#### [ALTA-C4] EOW Type 7 (HDR Change Request) sem implementação de payload
- **Cláusula:** C.5.5, Tabelas C-9-1/C-9-4
- **Local:** `src/eow.py:37` (apenas declara enum), `src/drc.py` (não cobre Type 7)
- **Correção:** implementar parser/builder Type 7 incluindo Extended Management Message field (waveform, channels, data rate, interleaver).

#### [MÉDIA-C1] Posição do TYPE field dentro do EOW de 12 bits ambígua
- **Cláusula:** C.5 §4 (linha 1750)
- **Local:** `src/eow.py:173,231`, `src/dpdu_frame.py:364`
- **Sintoma:** texto da norma não esclarece se TYPE está nos 4 LSB ou 4 MSB do EOW. Código coloca em 4 LSB. Confirmar com Figura C-37 ou vetor de outro implementador.

#### [MÉDIA-C2] Non-ARQ Error-Free vs Deliver-w/-Errors não distinguido
- **Cláusula:** C.3.13 §10-11
- **Local:** `src/non_arq.py:533-558`
- **Correção:** adicionar `delivery_mode: NonArqDelivery` em `NonArqEngine.__init__` e suprimir entregas parciais quando `error_free`.

#### [MÉDIA-C3] Reasons 0 e 2 da Tabela C-3 ausentes
- **Cláusula:** C.3.12 Tabela C-3
- **Local:** `src/dts_state.py:84-86`
- **Correção:** adicionar `WARNING_REASON_UNRECOGNIZED_TYPE = 0` e `WARNING_REASON_INVALID_DPDU = 2`.

#### [MÉDIA-C4] Flags TX_UWE/TX_LWE no DATA usam critério não-normativo
- **Cláusula:** C.3.3 §11-12
- **Local:** `src/arq.py:454-466`
- **Sintoma:** norma exige flag set sempre que `seq == TX_UWE/LWE`, não apenas "janela cheia".
- **Correção:** `tx_uwe = (seq == self._tx_uwe)` e `tx_lwe = (seq == self._tx_lwe)` por D_PDU.

#### [MÉDIA-C5] EXPEDITED_CONNECTED aceita Tipos 0/1/2 (regular DATA)
- **Cláusula:** Tabela C-20
- **Local:** `src/dts_state.py:141-148`
- **Correção:** remover `DATA_ONLY/ACK_ONLY/DATA_ACK` do conjunto válido para EXPEDITED_CONNECTED.

#### [BAIXA-C1] Não há proteção explícita contra WARNING-em-resposta-a-WARNING
- **Cláusula:** C.3.12 §10

#### [BAIXA-C2] `DataHeader.cpdu_id` validado em 0..255 globalmente
- **Local:** `src/stypes.py:158`

#### [BAIXA-C3] Address size=0 rejeitado no decoder
- **Local:** `src/dpdu_frame.py:416`
- **Comentário:** consistente com norma; sem ação.

#### [BAIXA-C4] Comentário desencontrado em `EOW_TYPE_NON_ARQ = 3`
- **Local:** `src/non_arq.py:8`
- **Correção:** limpar comentário.

#### [BAIXA-C5] Alias `VERSION = 3` "old mapping (was incorrect)"
- **Local:** `src/eow.py:42`
- **Correção:** remover.

---

### 3.4 ANEXO F (Clientes) — Subnetwork Clients

#### [ALTA-F1] FRAP/FRAPv2 enviam `updu_id+1` em vez de `updu_id` solicitado
- **Cláusula:** F.10.2.3 (linha 1460)
- **Local:** `src/annex_f/bftp.py:158-172` (FRAP), `:215-228` (FRAPv2)
- **Sintoma:** código faz `self._rcop_updu_id = updu_id` antes de `send()`, mas `send()` chama `_alloc_rcop_id()` que **incrementa** o contador antes de usar. Resultado: ACK FRAP envia sempre `(updu_id+1) & 0xFF`. **Bug crítico de interoperabilidade.**
- **Correção:** chamar `_build_segments()` diretamente ou expor override que aceite `updu_id` explícito.

#### [ALTA-F2] ETHER `send_ppp` ignora `priority`/`ttl_seconds` e tem `**kw` ambíguo
- **Cláusula:** F.11.5.5 (linhas 1703-1717)
- **Local:** `src/annex_f/ether_client.py:184-195`
- **Sintoma:** assinatura sem defaults para `priority`/`ttl_seconds` lança `TypeError` em uso típico.
- **Correção:** definir defaults `priority=5, ttl_seconds=120.0`.

#### [ALTA-F3] IP Client: MTU=2048 não validado contra mínimo IPv4 (28 bytes)
- **Cláusula:** F.12 (linha 1739)
- **Local:** `src/annex_f/ip_client.py:57,95-104`
- **Sintoma:** se `self.mtu < 28`, `_fragment_ipv4` calcula `max_payload=0` e entra em loop infinito.
- **Correção:** validar `mtu >= 28` no setter.

#### [ALTA-F4] HF-POP3: greeting via NOOP não é POP3-conformante
- **Cláusula:** F.6 (linhas 1024-1046, RFC 1939)
- **Local:** `src/annex_f/hf_pop3.py:61-76` (cliente), `:278-282` (servidor)
- **Sintoma:** RFC 1939 exige greeting espontâneo do servidor após conexão; código exige cliente enviar `NOOP\r\n` para receber greeting. Quebra interoperabilidade com qualquer cliente POP3 padrão.
- **Correção:** servidor envia greeting espontaneamente após `S_HARD_LINK_ESTABLISHED` ou primeiro `S_UNIDATA_INDICATION`.

#### [MÉDIA-F1] Raw SIS Socket não envia `S_UNBIND_INDICATION` ao desconectar
- **Cláusula:** A.2.1
- **Local:** `src/raw_sis_socket.py:448-460`
- **Correção:** invocar `encode_unbind_indication` antes de fechar e chamar `node.unbind(sap_id)`.

#### [MÉDIA-F2] Raw SIS Socket: `_install_sap_callback` substitui callback global
- **Local:** `src/raw_sis_socket.py:347-369`
- **Sintoma:** múltiplos clientes encadeados podem ter race conditions.
- **Correção:** usar `AnnexFDispatcher` central.

#### [MÉDIA-F3] Raw SIS Socket: `link_priority=5` chumbado no callback de hard-link
- **Cláusula:** A.2.1.10
- **Local:** `src/raw_sis_socket.py:381-391`

#### [MÉDIA-F4] RCOP/UDOP: heurística "último segmento por tamanho < MTU" falha em múltiplos exatos do MTU
- **Cláusula:** F.8.3 (linha 1198)
- **Local:** `src/annex_f/rcop.py:113-123,255-256`
- **Sintoma:** mensagens com tamanho exato `k * RCOP_MAX_APP_DATA` nunca são detectadas como completas → memory leak.
- **Correção:** adicionar timeout de remontagem e flag de "última fração".

#### [MÉDIA-F5] CFTP: `_decode_cftp_message` descarta bytes silenciosamente
- **Local:** `src/annex_f/cftp.py:143-156`
- **Correção:** logar warning quando `len(lines[3]) > message_size`.

#### [MÉDIA-F6] HMTP cliente aceita `recipients=[]` sem validação
- **Local:** `src/annex_f/hmtp.py:61-92`

#### [BAIXA-F1] RCOP: `RcopPDU.connection_id` não impõe `RESERVED=0` ao decodificar
- **Local:** `src/annex_f/rcop.py:79-88`

#### [BAIXA-F2] `text_protocol.byte_stuff` pode mudar tamanho em UTF-8
- **Local:** `src/annex_f/text_protocol.py:116-125`

#### [BAIXA-F3] FAB (`fab.py`) é extensão extra-norma em diretório `annex_f/`
- **Comentário:** docstring já admite. Aceitável como extensão proprietária; mover para `extras/` evitaria confusão.

---

## 4. Arquivos Identificados como Deprecated

| Arquivo / Bloco | Estado | Ação recomendada |
|---|---|---|
| `src/phase3_node.py` | wrapper alias deprecated, 17 linhas | **Remover** |
| `src/phase4_node.py` | wrapper alias deprecated, 17 linhas | **Remover** |
| `src/sis.py:347-378` (classe `SIS`) | alias deprecated; codecs S_PDU permanecem | **Remover só a classe** |
| `src/__init__.py:28-32,55,59` (exports `Phase3Node`/`Phase4Node`) | re-exports do deprecated | **Remover** |
| `src/non_arq.py:8` `EOW_TYPE_NON_ARQ = 3` | comentário desencontrado | **Limpar comentário** |
| `src/eow.py:42` `VERSION = 3` "old mapping (was incorrect)" | lixo legado | **Remover** |

**Testes:** Nenhum teste deprecated. 450 testes pytest passando. Os scripts `tests/annex_f_udp_node_A.py` e `tests/annex_f_udp_node_B.py` são utilitários de integração manual (UDPModemAdapter), não testes pytest — manter.

---

## 5. Plano de Desenvolvimento Priorizado

### Sprint 1 — CRÍTICAS (Anexo A, Hard Link)
*Pré-requisito para interoperabilidade entre nós conformantes.*

1. **A1** — Rotear S_PDUs de controle Hard Link via Expedited ARQ (`stanag_node.py:964-966`)
2. **A2** — Invocar `track_expedited_request` em `expedited_unidata_request` e emitir `S_UNBIND_INDICATION reason=4`
3. **A3** — Implementar TERMINATE do Hard Link prévio antes de aceitar novo (notificar peer + owner)
4. **A4** — Sempre emitir `S_HARD_LINK_REJECTED` com reason adequada quando perde precedência; introduzir tabela rank-por-SAP

### Sprint 2 — ALTAS interop (Anexos C e F)
*Bugs que quebram interoperabilidade real com outros stacks 5066.*

5. **C3** — `expedited_arq.py:273` corrigir `rx_lwe=(seq+1)&0xFF` (1 linha)
6. **C2** — `arq.py:666-671` adicionar `if drop_pdu: status=RECEIVED`
7. **C1** — `dpdu_frame.py:144` mascarar `cpdu_id & 0x0F` para Tipo 4
8. **F1** — `bftp.py` corrigir FRAP/FRAPv2 para usar `updu_id` recebido (substituir `send()` por `_build_segments()`)
9. **F2** — `ether_client.py:184` adicionar defaults `priority=5, ttl_seconds=120.0`
10. **F4** — `hf_pop3.py` enviar greeting espontâneo
11. **F3** — `ip_client.py` validar `mtu >= 28`

### Sprint 3 — ALTAS Anexo A + B + C
12. **A1** (rank) — validar `0 ≤ rank ≤ 15`, autorização para rank=15
13. **A2** (link_priority) — `min(3, max(0, ...))`
14. **A3** (terminate by owner) — rastrear originador local em Type 0/1
15. **A4** (delivery confirm) — copiar PRIORITY/VTTD/TTD do DATA original
16. **B1** — `CASEngine.send_data` aceitar `use_arq`
17. **C4** — Implementar EOW Type 7 (HDR Change Request)

### Sprint 4 — MÉDIAS (robustez + interop secundária)
18. **A1-A6, B1-B3, C1-C5, F1-F6** (20 itens) — ver detalhes acima

### Sprint 5 — BAIXAS + Limpeza
19. **18 itens BAIXA** + remoções de código deprecated:
    - Remover `src/phase3_node.py` e `src/phase4_node.py`
    - Remover classe `SIS` de `src/sis.py`
    - Limpar exports em `src/__init__.py`
    - Limpar `eow.py:42` (`VERSION=3`) e comentário em `non_arq.py:8`
    - Mover `fab.py` para `extras/` (renomeação opcional)

### Sprint 6 — Testes complementares
20. Testes para cada correção crítica/alta. Vetores externos para validação cruzada (especialmente posição do TYPE no EOW, MÉDIA-C1).

---

## 6. Referências

- Auditorias independentes geradas em 2026-04-30 por agentes especialistas (Anexos A, B, C, F)
- `docs/STANAG_5066_v3_ANEXO_A.md` — 2973 linhas
- `docs/STANAG_5066_v3_ANEXO_B.md` — 732 linhas
- `docs/STANAG_5066_v3_ANEXO_C.md` — 5627 linhas (mais denso)
- `docs/STANAG_5066_v3_ANEXO_F.md` — 3101 linhas
- Vetores oficiais de teste: Code Examples C-1, C-2 e Warning DPDU sample
