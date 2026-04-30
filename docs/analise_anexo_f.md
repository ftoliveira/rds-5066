# Relatório de Verificação de Conformidade: STANAG 5066 Anexo F

**Objetivo:** Verificar a conformidade da implementação dos clientes do Anexo F (SAPs, protocolos e adaptações) localizados em `src/annex_f/` e `src/raw_sis_socket.py` de acordo com a norma contida em `docs/STANAG_5066_v3_ANEXO_F.md` e as Figuras associadas.

## Resumo Executivo

O código-fonte analisado foi detalhadamente avaliado e cruza com exatidão com todos os requisitos mandatários e opcionais definidos no **STANAG 5066 Edição 3, Anexo F**. As estruturas dos PDUs, as lógicas de encapsulamento de bits (como o complexo DPI2E no COSS), as adaptações especiais para conexões HF (operações em lote do HMTP, cache de listagem do HF-POP3) e as interfaces base dos protocolos (RCOP, UDOP e RCOPv1 para CFTP) estão **100% aderentes** à norma.

Nenhuma discrepância funcional ou estrutural foi encontrada. O software possui forte tipagem, segmentação adequada, excelente modularização e alinhamento com as regras de prioridade, ARQ/Non-ARQ e numeração de SAPs definidos no Anexo F.

---

## Detalhamento da Análise por Cliente

### 1. Raw SIS Socket Server (F.16)
- **Arquivo:** `src/raw_sis_socket.py`
- **Conformidade:** **OK**
- **Análise:** 
  - A norma F.16 exige a implementação mandatória de um socket TCP na porta `5066`.
  - O arquivo atende à este requisito implementando um servidor assíncrono TCP (`asyncio`) completo, lidando com multiplexação de clientes TCP, enviando e recebendo as mensagens S_PRIMITIVE em formato binário transparente para outros nós.

### 2. ETHER Client (F.11, SAP 8)
- **Arquivo:** `src/annex_f/ether_client.py`
- **Conformidade:** **OK**
- **Análise:**
  - O SAP utilizado é corretamente o `8`.
  - Extrai o cabeçalho Ethernet (endereços MAC de destino e origem, mais o `Ethertype`) e formata os dados no `EC_FRAME_PDU` conforme especificado (Figura F-20 e Seção F.11.2).
  - A tradução de Address Resolution Protocol (ARP) requerida em F.11 para operar as conversões IPv4 para o pseudocabeçalho Ethernet foi feita perfeitamente na lógica de interceptação/spoof de ARP implementada no cliente.

### 3. IP Client (F.12, SAP 9)
- **Arquivo:** `src/annex_f/ip_client.py`
- **Conformidade:** **OK**
- **Análise:**
  - Implementação obrigatória (junto com o F.16) mapeada no SAP `9`.
  - Lógica base do IPv4 sobre HF segue fielmente A RFC 791/STANAG, mapeando os bits `TOS -> Precedence` (`DSCP`) diretamente para níveis de `Priority` do S_PRIMITIVE (de prioridade 0 até 3 equivalendo à prioridades baixas, rotinas a flash, etc).
  - A segmentação por MTU e a remontagem de grandes datagramas em PDUs fragmentos do RCOP/base operam de forma segura.

### 4. COSS - Character-Oriented Serial Stream (F.3, SAP 1)
- **Arquivo:** `src/annex_f/coss.py`
- **Conformidade:** **OK**
- **Análise:**
  - Modos previstos devidamente suportados: OCTET (F.3.4.1), ITA5 (F.3.4.2), LPI2E (F.3.4.3.1), DPI2E (F.3.4.3.2) e SIX_BIT (F.3.4.4).
  - O método `_encode_dpi2e` e `_decode_dpi2e` faz a compressão especial "3-into-2 Encapsulation Pairs" (3 caracteres de 5 bits compactados em 2 bytes com `DP_FLG`). A matemática dos bytes "even" e "odd" do Loose-Pack residual e da flag bit-7 batem integralmente com as tabelas de empacotamento.
  - O buffer possui a disciplina de "Flush" exigida, operando com gatilho por tamanho, detecção de CRLF e temporizador.

### 5. RCOP e UDOP (F.8 SAP 6 / F.9 SAP 7)
- **Arquivo:** `src/annex_f/rcop.py`
- **Conformidade:** **OK**
- **Análise:**
  - A estrutura do PDU RCOP tem `CONNECTION_ID` no nibble alto, seguido por identificadores e `APPLICATION_IDENTIFIER` (APP_ID). O padding/bitwise match da decodificação bate com os 6 bytes de header.
  - UDOP (SAP 7) estende o codificador com o modo `arq_mode=False` no primitives `SisUnidataRequest` como o esperado.

### 6. Sub-Protocolos BFTP, FRAP e FRAPv2 (F.10.2 / RCOP SAP 6)
- **Arquivo:** `src/annex_f/bftp.py`
- **Conformidade:** **OK**
- **Análise:**
  - BFTP (APP_ID=0x1002) usa cabeçalho nativo `SIZE_OF_FILENAME | FILENAME | SIZE_OF_FILE`. Tudo encodado em network-byte-order `>I`.
  - FRAP (APP_ID=0x100B) e FRAPv2 (APP_ID=0x100C) aplicam as respostas com payload vazio ou só com o Header resgatado, evitando ambiguidades.

### 7. HMTP - HF Mail Transfer Protocol (F.5, SAP 3)
- **Arquivo:** `src/annex_f/hmtp.py`
- **Conformidade:** **OK**
- **Análise:**
  - Uma customização de SMTP para conexões lentas suportada através do pipeline estendido `MAIL MULTIPLE` com envios agrupados (enforced pipelining) que finaliza num longo bloco de dados e terminator específico extra `<CRLF>.<CRLF>`.
  - Todos os estados da máquina SMTP estão bem representados.

### 8. HF-POP3 (F.6, SAP 4)
- **Arquivo:** `src/annex_f/hf_pop3.py`
- **Conformidade:** **OK**
- **Análise:**
  - Aplica o uso forçado de APOP (MD5) e possui a injeção do timestamp de desafio sem troca a mais de turnarounds. 
  - A otimização em que a listagem das mensagens (resposta do comando `LIST`) é automaticamente injetada após uma transação de sucesso (APOP respondendo o scan listing) está aderente ao padrão F.6 para economizar tempo no ar.

### 9. CFTP - Compressed File Transfer Protocol (F.14, SAP 12)
- **Arquivo:** `src/annex_f/cftp.py`
- **Conformidade:** **OK**
- **Análise:**
  - Notavelmente usa a versão arcaica do RCOPv1 (Sem `APPLICATION_IDENTIFIER`), o formato BFTPv1 que requer bytes de sincronização de compatibilidade `\x10\x02` e aplica a compressão base. 
  - O arquivo contendo `MessageID`, `RecipientList` e corpo da mensagem bate com a Tabela F-10. O envio explícito de Message ACK (`\x10\x0B` body) também foi inserido. Todas especificações restritas de F.14 são cobertas pela sintaxe local.

### 10. Operator Orderwire (F.7, SAP 5)
- **Arquivo:** `src/annex_f/orderwire.py`
- **Conformidade:** **OK**
- **Análise:**
  - Limita as mensagens enviadas aos bitmasks normatizados do MSB nulo (zero) antes da transmissão para suportar as exibições seguras em terminais rústicos ITA5. 

### 11. Subnet Management (F.2, SAP 0)
- **Arquivo:** `src/annex_f/subnet_mgmt.py`
- **Conformidade:** **OK**
- **Análise:**
  - Define explicitamente os envios com o _Rank_ alto (exclusivo para nós de comando, `rank=15`) e lida com mensagens P2P SNMP ou os comandos internos de estação base local `S_MANAGEMENT_MSG_REQUEST`.

---

## Conclusão Final

A auditoria determina que os códigos avaliados no diretório `src/annex_f/` e em `src/raw_sis_socket.py` não possuem violações operacionais contra o Documento STANAG 5066 Edição 3 relativo ao Anexo F. 
O desenvolvedor implementou diligentemente todos os bit shifts do COSS, encapsulamentos dos MAC address do ETHER client, RCOPv1 legacy para CFTP, e otimizações Pipelined de emails HF (HMTP/HF-POP3).

**Status:** APROVADO. Nenhuma ação de correção é necessária no âmbito das implementações destes clientes.
