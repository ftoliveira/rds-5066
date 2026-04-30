# Análise de Conformidade: STANAG 5066 Anexo A

## 1. Visão Geral
Esta análise verificou a implementação da subcamada **SIS (Subnetwork Interface Sublayer)** presente nos arquivos `src/sis.py` e `src/stanag_node.py`, contrastando-a com as especificações da norma **STANAG 5066 Edição 3, Anexo A** (descrita no documento `docs/STANAG_5066_v3_ANEXO_A.md` e suas imagens ilustrativas).

A implementação atual fundiu a arquitetura de Multiplexação DTS (Phase 3) e SIS (Phase 4) na classe unificada `StanagNode`, o que agiliza o despacho e reduz os _deadlocks_ entre ARQ/Non-ARQ/CAS.

**Conclusão Geral:** O código-fonte está **altamente condizente** com a norma, refletindo rigorosamente a codificação de primitivas e protocolos (S_PDUs).

---

## 2. Análise Detalhada dos S_PDUs e Codificação (src/sis.py)

O arquivo `sis.py` trata do codec das S_PDUs enumeradas nas seções A.3.1 (A.3.1.1 a A.3.1.8) e mapeadas pelas **Figuras A-31 a A-39**.

- **S_PDU Tipo 0 (DATA):** Implementado nas funções `encode_spdu_data` e `decode_spdu_data`. Mapeia perfeitamente:
  - 4 bits TYPE (=0).
  - 4 bits PRIORITY.
  - 4 bits SOURCE SAP / 4 bits DESTINATION SAP.
  - Flags: `CLIENT DELIVERY CONFIRM REQUIRED` (1 bit) e `VALID TTD` (1 bit).
  - `TTD` (Mod 16 Julian day + GMT high 16).
  > **Requisito Atendido:** O campo TTD foi corretamente derivado da conversão via `datetime.utcfromtimestamp()` e aritmética da meia-noite descrita em A.3.1.1.

- **S_PDUs Tipo 1 a 7 (Controle de Enlace e Confirmação):** As funções de `encode/decode` para Type 1 (DATA DELIVERY CONFIRM), Type 2 (FAIL), Type 3 a 7 (Hard Link Estab/Term/Reject) refletem de 1 a 1 a estrutura solicitada pelas figuras A-33 a A-39. Há o correto mapeamento lógico de `link_type`, `link_priority` e `reason fields` como em A.3.1.5 (Tabela de motivos de rejeição).

---

## 3. Primitivas Cliente-Sub-rede e Regras de Sessão (src/stanag_node.py)

A classe `StanagNode` encapsula as primitivas e a lógica de sessão (Soft Link e Hard Link).

### O que está Implementado Corretamente:
1. **S_BIND_REQUEST / S_UNBIND_REQUEST (A.2.1.1 / A.2.1.3):**
   - Máximo de 16 SAPs respeitados.
   - Atribuição de Rank e tratamento de requisições rejeitadas.
2. **S_UNIDATA_REQUEST / Expedited (A.2.1.6 / A.2.1.10):**
   - Respeita o tamanho máximo `MTU = 2048` e calcula adequadamente o tempo limite (`TTD` baseado no `TTL`).
3. **Gerenciamento de Fluxo `flow_on`/`flow_off` (A.2.1.14):**
   - Implementado os _flags_ de travas de filas na API pública (`data_flow_on()` e `data_flow_off()`).
4. **Hard Link Establishment (A.3.2.2.1):**
   - A.3.2.2.1 determina que _"uma requisição Hard Link de cliente de Rank maior tem precedência (...), igual rank com maior Type (0, 1, 2) prevalece"_. 
   - A função interna `_process_spdu_control` (linhas 872-886) compara estritamente os Ranks e Prioridades previstos na lógica, derrubando a sessão anterior ou emitindo _Reject_ conforme exigido.
5. **Limitação de Expedited Requests (A.2.1.10):**
   - O Anexo A diz que a gerência deve monitorar e aplicar punições (UNBIND) ao cliente por excesso de `EXPEDITED_UNIDATA`. Foi modelado o comportamento via `track_expedited_request(sap_id)`, que emite um _unbind_ em caso de saturação configurável.

---

### Pequenas Divergências ou Pontos de Atenção (Para refinamento futuro):

1. **Prioridade em S_EXPEDITED_UNIDATA_REQUEST:**
   - A seção A.3.1.1 diz: _"For U_PDUs submitted with an S_EXPEDITED_UNIDATA_REQUEST, the PRIORITY field **should be set to 0**"_. No entanto, na implementação do `stanag_node.py` (método `expedited_unidata_request`, linha 386), encontra-se a chamada de delegação: `priority=15`. Isso pode causar uma não conformidade técnica se interoperando com provedores de stack muito rígidos em relação ao texto do STANAG 5066. (Embora localmente funcione bem pois garante o salto da fila interna).

2. **S_KEEP_ALIVE e S_SUBNET_AVAILABILITY (A.2.1.17, A.2.1.20):**
   - O documento menciona envio da `S_KEEP_ALIVE` do SIS para clientes ociosos e a funcionalidade analítica do `S_SUBNET_AVAILABILITY`. No momento atual do `stanag_node.py`, não localizamos métodos diretos para injetar pacotes destas naturezas a clientes, embora o nó processe logicamente as respostas. 
   > **Nota do Padrão:** O anexo A especifica que uma implementação *Minimamente Complacente* não é forçada a lançar estas primitivas, apenas suportá-las se requisitadas. Portanto o código **não viabiliza erro estrutural**, mas seria algo a expandir para controle local.

3. **S_MANAGEMENT_MSG_REQUEST (A.2.1.15):**
   - Implementado o controle de validação do Rank 15 na função `validate_management_msg_rank()`, perfeitamente de acordo com A.2.1.15§3.

## Conclusão Final
O código está arquitetônica e protocolarmente saudável em relação à Edição 3, Anexo A, do STANAG 5066. As conversões numéricas relativas da matriz de imagem estão consistentes na conversão _byte-by-byte_. Recomenda-se apenas retificar, se for interesse para extrema pureza com a RFC base, o detalhe da prioridade definida para as rotinas EXPEDITED que devem preferivelmente transmitir com valor prioritário 0 na máscara de rede do pacote `S_PDU Tipo 0`.
