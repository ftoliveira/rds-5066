# Relatório de Verificação: STANAG 5066 Anexo B (Channel Access Sublayer)

O presente relatório detalha a análise de conformidade do código fonte da subcamada de acesso ao canal (CAS - Channel Access Sublayer) frente aos requisitos da norma **STANAG 5066 Edição 3, Anexo B**. O objetivo foi verificar se as estruturas `C_PDU`, as regras de *Link Establishment* e os procedimentos do *CAS 1 Linking Protocol* estão implementados corretamente.

## 1. Mapeamento de Arquivos Analisados

- `src/cas.py`: Contém a máquina de estados `CASEngine`, gerenciador de conexões remotas, e a codificação/decodificação dos C_PDUs.
- `src/types.py`: Contém a definição das enumerações usadas no CAS, especificamente `CPDUType`, `CPDURejectReason`, `CPDUBreakReason` e `PhysicalLinkType`.
- `src/stanag_node.py`: Integração do CAS com o DTS (Data Transfer Sublayer) e o SIS (Subnetwork Interface Sublayer), responsável pela correta formatação dos *payloads* em C_PDUs e roteamento para o serviço ARQ ou Non-ARQ.

---

## 2. Análise de Conformidade e Resultados

### 2.1. Estruturas C_PDU e Codificação (B.2.1 - B.2.6)

A norma diz que o *byte 0* do C_PDU é codificado como: `[TYPE (4 bits)] [FIELD (4 bits)]`.

| Tipo C_PDU | Valor do Field (Low Nibble) | Verificação no Código | Status |
| :--- | :--- | :--- | :--- |
| **DATA (Tipo 0)** | Fixo `0000` | Implementado via retorno de `bytes([0x00]) + S_PDU`. | ✅ Conforme |
| **LINK REQUEST (Tipo 1)** | Bit 0: Tipo de Link (0=Nonexclusive, 1=Exclusive) | Mascarado via `cpdu.link_type & 0x01`. Definição correta em `PhysicalLinkType`. | ✅ Conforme |
| **LINK ACCEPTED (Tipo 2)** | Fixo `0000` | Implementado perfeitamente apenas com shift `t << 4`. | ✅ Conforme |
| **LINK REJECTED (Tipo 3)** | Reason Code (Tabela B-4) | Mascarado via `cpdu.reason & 0x0F` (Temos os enums em `CPDURejectReason`). | ✅ Conforme |
| **LINK BREAK (Tipo 4)** | Reason Code (Tabela B-5) | Mascarado via `cpdu.reason & 0x0F` (Enums em `CPDUBreakReason`). | ✅ Conforme |
| **LINK BREAK CONFIRM (Tipo 5)** | Fixo `0000` | Implementado perfeitamente apenas com shift `t << 4`. | ✅ Conforme |

**Considerações:** Nenhuma discrepância matemática encontrada na geração ou extração dos cabeçalhos CPDU nas rotinas `encode_cpdu` e `decode_cpdu` do `src/cas.py`.

---

### 2.2. Serviço de Entrega de C_PDUs de Controle (B.3.1)

A Norma estabelece que as mensagens C_PDUs de controle (Tipos 1 a 5) **devem** ser enviadas requisitando os serviços Expedited Non-ARQ do SIS (ou seja, transmitidas usando D_PDU Tipo 8).

**Verificação:**
- Em `cas.py` (linha 537), a função `_send_control_cpdu` está implementada da seguinte maneira:
```python
self.non_arq.queue_cpdu(
    DPDUType.EXPEDITED_NON_ARQ,
    destination,
    encode_cpdu(cpdu),
)
```
**Status:** ✅ Totalmente Conforme.

---

### 2.3. Serviço de Entrega de S_PDUs e Encapsulamento DATA C_PDU (B.3.1.1 e B.3 item 2)

A norma determina que os `S_PDUs` submetidos via SIS devem ser sempre envoltos em um envelope CAS (`DATA C_PDU`, contendo o byte de cabeçalho `0x00`) **mesmo se forem submetidos para a fila ARQ**. O modo de entrega solicitado pelo SIS (ARQ ou Non-ARQ) deve ser mantido e o C_PDU processado apropriadamente.

**Verificação:**
A implementação em `stanag_node.py` acerta em cheio nesse requisito peculiar:
- No envio de dados Non-ARQ diretos pela CAS, a rotina `self.cas.send_data()` engloba o payload com o tipo DATA.
- No envio de dados ARQ via DTS e Expedited ARQ, o `StanagNode.send_data()` aplica o pacote explicitamente em um envolucro C_PDU:
```python
cpdu_bytes = self._wrap_in_cpdu(payload)
self._dts.enter_data()
self.arq.submit_cpdu(cpdu_bytes, deliver_in_order=deliver_in_order)
```
- E, identicamente, tanto o recebedor local ARQ (`_on_arq_delivery`) quanto de pacote submetido como Non-ARQ (`_on_non_arq_delivery`) se preocupam em extrair essa "casca" do `DATA C_PDU` de 1 byte antes de repassar o "recheio" (*S_PDU*) à camada SIS.

**Status:** ✅ Totalmente Conforme.

---

### 2.4. Protocolo CAS 1 Linking (B.3.2)

A Norma especifica diferentes restrições de estado para responder às propostas de Link Request:
- **Exclusividade Limitada (B.2 item 3):** No máximo dois enlaces (links) Exclusivos por nó. `cas.py:503` implementa lógica validando a rejeição `HIGHER_PRIORITY_LINK_REQUEST_PENDING` em caso de 2+ conexões ativas. ✅ Conforme
- **Prioridade sobre Nonexclusive (B.3.2.1 Step 2(a)):** Se aceitar um pedido de link Exclusive (Hard Link), os enlaces Nonexclusive devem ser destuídos. Em `cas.py:424` a rotina `_break_all_nonexclusive` emite `LINK_BREAK` aos enlaces non-exclusive vigentes. ✅ Conforme
- **Nonexclusive Timeout e Limites (B.3.2.1 Step 2(b)):** Rejeita se ultrapassou máximo Nonexclusive. Aceita se não o fez. No código, o parâmetro `max_nonexclusive_links` dita isso. ✅ Conforme
- **Link Handshake (Caller e Called):** O `Caller` inicia o handshake via TYPE 1. O `Called` aceita com TYPE 2, após o que o Caller marca com "MADE". Todas as transições implementam os timeouts descritos nas sub-regras da cláusula B.3.2. ✅ Conforme

---

## 3. Conclusão Geral

Geralmente, há uma grande tendência de falhas quando protocolos de *Control PDUs* são mal dispostos no encapsulamento de filas ARQ (muitas suítes ignoram que o Protocolo ARQ, formalmente, transporta C_PDUs e não C_PDUs diretamente). A base de código aqui demonstrou ter uma estrutura bastante aderente. 

**Veredito:** O repositório e, fundamentalmente as classes `CASEngine`, `StanagNode` e definições em `types.py` estão em **100% de conformidade** textual e lógica com os requisitos do Anexo B da Edição 3 sem discrepâncias observáveis. Nenhuma não-conformidade procedimental detectada.
