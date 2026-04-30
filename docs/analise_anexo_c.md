# Relatório de Verificação STANAG 5066 Anexo C

A verificação do código-fonte em relação ao padrão **STANAG 5066 Edição 3, Anexo C (DTS - Data Transfer Sublayer)** foi concluída com sucesso. O objetivo era analisar a conformidade da implementação em Python em relação às regras de codificação de quadros D_PDU, cálculos de CRC, cabeçalhos EOW/EOT e máquinas de estado ARQ e Não-ARQ.

Após revisão detalhada, o código apresenta **alta conformidade** com o documento em `docs/STANAG_5066_v3_ANEXO_C.md` e com os mapas de bits visuais fornecidos nas imagens em `docs/images_anexo_C/media`. 

Abaixo estão os detalhes das verificações feitas:

## 1. Estrutura e Cabeçalhos D_PDU (`src/dpdu_frame.py`)
- **Tipos de D_PDU:** Todos os tipos definidos no padrão (Tipos 0, 1, 2, 3, 4, 5, 6, 7, 8, e 15) são adequadamente verificados e estruturados conforme as tabelas do Anexo C.2 (C-Frames, I-Frames, etc).
- **Sequência de Sincronização:** O código insere corretamente o `SYNC_BYTES` de 16 bits de Maury-Styles.
- **Campos EOT, EOW, Endereçamento (Size of Address/Header):** Todos os campos de cabeçalho compartilhados (`common`) são posicionados no lugar e tamanhos exatos.
- **Tipos 7 e 8 (Não-ARQ e Não-ARQ Expedited):** Foi verificado de forma meticulosa o mapeamento de bits do campo `C_PDU ID NUMBER` (4 bits MSB no campo 1 e 8 bits LSB no campo 2) e as diretivas visuais das imagens *C-18* até *C-26*. O código Python reflete isto de forma exata:
  ```python
  first = (((header.cpdu_id >> 8) & 0x0F) << 4) | ...
  ```

## 2. Implementação do CRC (`src/crc.py`)
- O Anexo C determina o uso de cálculos cíclicos de redundância de **16 bits na versão base e de 32 bits a partir da V3**.
- O código implementa perfeitamente o CRC-16 (Polinômio base refletido `0x9299`) conforme o parágrafo `C.3.2.8` - com shift correto e sem soma XOR final.
- O código também implementa perfeitamente o CRC-32 (Polinômio refletido `0xF3A4E550`) da V3, cobrindo todo o payload Segmented C_PDU conforme a seção `C.3.2.11`.

## 3. Engineering Orderwire (`src/eow.py`)
- As enumerações de 12-bits implementadas na classe `EOWType` seguem à risca as regras da tabela *C-4* do Anexo C para todos os comandos de EOW: Capability, DRC Request, DRC Response, Unrecognized Type e HDR Change Request.

## 4. Subcamada ARQ e Não-ARQ (`src/arq.py` / `src/non_arq.py`)
- **Segmentação e Remontagem ARQ:** A operação suporta a Janela Deslizante de Fluxo (*Sliding Window*) contendo LWE e UWE limitados ao teto lógico de 256 instâncias rotativas.
- O ACK seletivo é construído extraindo os bytes limitados aos quadros recebidos entre o `LWE` e `UWE`. 
- Processo de retransmissão de resets e lógicas de timeout aderem ao padrão (Acks PENDENTES vs SENT_WAIT_ACK).
- **Segmentação e Remontagem Não-ARQ (Broadcast):** Segue rigorosamente as orientações expressadas na Subseção *C.4.2*, utilizando o limite das janelas de recebimento (Reception Window de N intervalis de 500ms) e expurgando pacotes cujos segmentos de fragmentação não tenham chegado dentro do limite (*Deliver-w/-Errors* ou drop).

## 5. Máquina de Estados DTS (`src/dts_state.py`)
- A estruturação lógica nos módulos com os 8 cenários lógicos separados pelas matrizes conectadas e não-conectadas (IDLE_UNCONNECTED, DATA_UNCONNECTED, IDLE_CONNECTED, DATA_CONNECTED, etc) e as regras que engatilham WARNING_PDUs se pacotes inválidos são recebidos para um devido estado refletem o esperado no capítulo *C.6.1*. 

### Conclusão:
A implementação está incrivelmente aderente. **Não foram encontradas divergências arquiteturais na construção dos frames ou nos algorítmos de controle do Sublayer de Transferência de Dados**. O arquivo se encontra dentro dos parâmetros da *Norma STANAG 5066 V3*.
