# S5066 Subnet Console (PyQt6)

`subnet_console/` — uma interface **Qt (PyQt6)** que reúne, numa única janela, todos
os clientes SIS de um nó **STANAG 5066** ("Annex F Client Manager"): dashboard da
sub-rede, monitor de tráfego, HFCHAT, HF Mail, IP Client, transferência de ficheiros
(RCOP/UDOP), Raw SIS Socket, ligação ao modem MIL-STD-188-110C e configuração.

É a transposição fiel do design **"S5066 Subnet Console"** (projeto Claude Design
*Qt interface para STANAG-5066*) para PyQt6. Complementa — não substitui — os apps
Tkinter existentes (`chat_app*.py`).

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
│  Monitor     │                                                             │
│ SIS CLIENTS  │                                                             │
│  HFCHAT …    │                                                             │
│ SETUP        │                                                             │
│  Modem/Config│                                                             │
├──────────────┴─────────────────────────────────────────────────────────────┤
│ ● SIS 127.0.0.1:5066 LISTENING │ 5 CLIENTS BOUND │ TX 14 · RX 2 …   14:22 UTC│  ← status
└──────────────────────────────────────────────────────────────────────────┘
```

---

## 1. Requisitos

- Python 3.11+ e **PyQt6** (`pip install PyQt6`) — já presente neste ambiente.
- Display gráfico (X11/Wayland; no WSL, WSLg).
- *(Opcional)* tipos de letra **IBM Plex Sans/Mono** para fidelidade total ao design.
  Sem eles usa-se um *fallback* limpo (DejaVu/Segoe/Ubuntu). Para os embutir, largue
  os `*.ttf` em `subnet_console/assets/fonts/` — são carregados automaticamente.

## 2. Como executar

```bash
# como módulo (recomendado)
python -m src.interface.subnet_console

# ou diretamente
python src/interface/subnet_console/__main__.py
```

Opções de linha de comando:

| Flag | Valores | Efeito |
|------|---------|--------|
| `--node` | `A` \| `B` | perfil/identidade do nó local (callsign, endereço, porta do modem) |
| `--accent` | `blue` `green` `purple` `orange` `gray` | cor de destaque do tema |
| `--modem-host` | IP | pré-preenche o campo *Modem IP Address* |
| `--modem-port` | porta | pré-preenche o campo *TCP Port* |

```bash
python -m src.interface.subnet_console --node A --accent blue --modem-host 10.0.0.10 --modem-port 3000
```

## 3. Ecrãs

| Secção | Ecrã | Conteúdo |
|--------|------|----------|
| SECTIONS | **Subnet Dashboard** | KPIs, ligações/peers, qualidade do canal, SAPs ligados, filas TX/RX, S-Primitives |
| SECTIONS | **Traffic Monitor** | contadores, alocação de SAP (Anexo F Tabela F-1), log de eventos S-Primitive |
| SIS CLIENTS | **HFCHAT Orderwire** (SAP 5) | operadores, *thread* de mensagens, *feed* de primitivas |
| SIS CLIENTS | **HF Mail** (HMTP 3 / HFPOP 4) | caixas de correio, leitura/composição, *pipelining* HMTP |
| SIS CLIENTS | **IP Client** (SAP 9) | binding, mapeamento QoS, rotas IP→STANAG, log de datagramas |
| SIS CLIENTS | **File Transfer** (RCOP 6 / UDOP 7) | compositor, fila de transferências, log de primitivas |
| SIS CLIENTS | **Raw SIS Socket** (F.16) | parâmetros do servidor, clientes ligados, *wire log* |
| SETUP | **Modem Link** (110C) | ligação (IP/porta), taxa de dados, *interleaver* |
| SETUP | **Configuration** | servidor SIS, requisitos de serviço por cliente |

## 4. Arquitetura

```
subnet_console/
├── __main__.py        # argparse + bootstrap
├── app.py             # QApplication, fontes, run()
├── theme.py           # tokens de design: cores, accent, tint(), fontes
├── model.py           # ConsoleModel — estado + dados de demonstração + sinais
├── window.py          # SubnetConsoleWindow (frameless): chrome + QStackedWidget
└── widgets/
    ├── common.py      # primitivas reutilizáveis (Card, KpiTile, Table, pill, …)
    ├── titlebar/menubar/toolbar/sidebar/statusbar.py   # chrome
    └── screens/       # base.py + os 9 ecrãs
```

O ponto central é o **`ConsoleModel`**: expõe *view accessors* (`links()`,
`sap_table()`, `mail_view()`, …) que devolvem dicionários com as cores já resolvidas
contra o tema atual. Os ecrãs são pura disposição visual e reconstroem-se quando o
modelo emite `changed(<tópico>)` (mudança estrutural) ou `accent_changed`. Edições de
texto usam *setters* silenciosos para não perder o foco do campo.

### Fase 1 (este pacote) vs Fase 2

Esta é a **Fase 1**: uma casca de UI totalmente navegável e interativa, alimentada
pelos **mesmos dados de demonstração** do *mockup*. Tudo o que é dinâmico passa pelo
`ConsoleModel`, de modo que a **Fase 2** — ligar ao backend real
(`StanagNode` + `Tcp110dModemAdapter` + `RawSisSocketServer` + clientes `annex_f/*`)
— consiste em substituir os *accessors* de dados do modelo por feeds ao vivo, sem
tocar nos *widgets*. Pontos de ligação naturais já refletidos na UI: campos do
*Modem Link* → `Tcp110dConfig`; HFCHAT/File Transfer → `S_UNIDATA_REQUEST` via
`StanagNode`; Raw SIS Socket → `RawSisSocketServer`.

## 5. Notas de implementação

- Janela **sem moldura** (frameless): a barra de título tem os "semáforos"
  funcionais (fechar / minimizar / maximizar) e permite arrastar; o *status bar* tem
  um *size grip* para redimensionar.
- **QLabel é subclasse de QFrame** — por isso qualquer estilo `QFrame{…}` com borda
  aplicado a um cartão "vazava" para os rótulos filhos. O *helper* `common.scoped()`
  isola cada regra ao seu *objectName*; use-o sempre que estilizar um `QFrame` com
  borda/fundo.
- O relógio do *status bar* é UTC ao vivo (QTimer 1 s).
