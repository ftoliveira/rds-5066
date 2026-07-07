#!/usr/bin/env bash
#
# run_110d_real.sh — Lança a interface Qt "Subnet Console" (PyQt6) contra um modem
# MIL-STD-188-110D real, rodando a GUI NA SUA MÁQUINA e alcançando o modem (na
# "black") através de um túnel SSH pela "red" (Opção 1 do plano de teste).
#
# Topologia:
#
#   [sua máquina]  --SSH-->  red (192.168.108.<SN>)  -->  black (modem :3000)
#        |                                                     ^
#        | app conecta em 127.0.0.1:<porta-local>  ───────────┘
#        └──── túnel SSH -L encaminha até o modem na black ─────
#
# O nó STANAG 5066 é um DTE: abre UMA conexão TCP (cliente) para o modem (DCE,
# servidor na porta 3000 do Appendix A). Um único encaminhamento SSH -L basta —
# o modem nunca inicia conexão de volta.
#
# NOTA (Fase 2): o console é lançado em modo --live, arrancando um StanagNode real
# e ABRINDO a ligação TCP ao modem (data path DTE↔DCE) via túnel — o painel
# "Modem Link" mostra LINKED e a taxa reportada quando o handshake conclui.
#
# RADIO CONTROL (ALE 2G, UDP :54001): o ecrã "Radio Control" fala o protocolo de
# controlo remoto do rádio (docs/PROTOCOLO-CONTROLE-REMOTO.md) — freq/potência/
# scanning/links — por UDP, no MESMO host do modem (a black). Como o SSH -L só
# encaminha TCP, este UDP é tunelado com `socat` nas duas pontas (ponte UDP↔TCP):
#
#   app  --UDP:54001-->  socat(local)  --TCP--> [ -L túnel ] --TCP--> socat(red)  --UDP:54001-->  radio backend (black)
#
# Requer `socat` na sua máquina E na red. Alternativas:
#   --ale-host <ip>   backend do rádio diretamente alcançável (sem ponte/túnel)
#   --no-ale          não abrir o Radio Control (fica OFFLINE)
#
# Uso:
#   scripts/run_110d_real.sh <serial_number> [opções]
#
# O <serial_number> (SN) determina o IP da red: 192.168.108.<SN>.
# Ex.: SN 12 -> red em 192.168.108.12.
#
set -euo pipefail

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------
RED_PREFIX="192.168.108"     # rede da red (last octet = SN)
BLACK_IP="192.168.0.2"       # IP do modem visto pela red
MODEM_PORT="3000"            # porta TCP fixa do Appendix A (DCE)
LOCAL_PORT=""                # porta local do túnel (default = MODEM_PORT)
NODE="A"                     # identidade STANAG do nó (A|B)
ACCENT=""                    # cor de destaque do console (blue|green|purple|orange|gray)
SSH_USER="${SSH_USER:-$USER}"
PYTHON="${PYTHON:-python3}"
NO_APP=0
SKIP_PREFLIGHT=0

# ---- Radio Control (ALE 2G, UDP) ----
ALE_PORT="54001"             # porta UDP do backend de controlo do rádio (na black)
ALE_BLACK_IP=""              # IP do backend do rádio visto pela red (default = BLACK_IP)
LOCAL_ALE_PORT=""            # porta UDP local onde a app fala com o socat (default = ALE_PORT)
ALE_BRIDGE_PORT=""           # porta TCP da ponte socat sobre o túnel (default = LOCAL_ALE_PORT+1)
ALE_HOST_OVERRIDE=""         # se definido: liga direto a este host (sem ponte/túnel)
NO_ALE=0                     # 1 = não abrir o Radio Control

# ---- runtime state (cleanup) ----
LOCAL_SOCAT_PID=""
ALE_BRIDGE_ACTIVE=0

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP="$REPO_ROOT/src/interface/subnet_console/__main__.py"

usage() {
    cat <<EOF
Uso: $(basename "$0") <serial_number> [opções]

Sobe um túnel SSH pela red e lança o Subnet Console (PyQt6) na sua máquina, com o
painel "Modem Link" apontando para o modem 110D real na black via túnel e o ecrã
"Radio Control" (ALE 2G, UDP :54001) tunelado via socat.

Positional:
  serial_number         SN do equipamento (1-254). Red IP = ${RED_PREFIX}.<SN>

Opções (modem / túnel):
  -n, --node A|B        Identidade do nó STANAG (default: ${NODE})
  -a, --accent COR      Cor do tema: blue|green|purple|orange|gray (default: app)
  -u, --user USER       Usuário SSH na red (default: \$USER = ${SSH_USER})
  -b, --black-ip IP     IP do modem visto pela red (default: ${BLACK_IP})
  -p, --modem-port N    Porta TCP do modem / Appendix A (default: ${MODEM_PORT})
  -l, --local-port N    Porta local do túnel (default: = porta do modem)
      --red-prefix P    Prefixo /24 da red (default: ${RED_PREFIX})
      --no-app          Só sobe o(s) túnel(is) (não lança a GUI); Enter encerra
      --skip-preflight  Não testa a acessibilidade do modem pela red

Opções (Radio Control / ALE 2G, UDP):
      --ale-port N      Porta UDP do backend do rádio (default: ${ALE_PORT})
      --ale-black-ip IP IP do backend do rádio visto pela red (default: = --black-ip)
      --local-ale-port N  Porta UDP local da ponte (default: = --ale-port)
      --ale-bridge-port N Porta TCP da ponte socat (default: = local-ale-port + 1)
      --ale-host IP     Liga direto a este host (backend alcançável; sem ponte/túnel)
      --no-ale          Não abrir o Radio Control (fica OFFLINE)

  -h, --help            Esta ajuda

Exemplos:
  # red em 192.168.108.12, nó A; modem e rádio em 192.168.0.2 (via túnel/socat)
  $(basename "$0") 12

  # usuário SSH 'rds' na red, nó B
  $(basename "$0") 12 -u rds -n B

  # backend do rádio diretamente alcançável (sem socat), noutro IP
  $(basename "$0") 12 --ale-host 192.168.0.50

  # sem Radio Control (só STANAG 5066)
  $(basename "$0") 12 --no-ale

  # só o(s) túnel(is), para usar com uma app já aberta
  $(basename "$0") 12 --no-app
EOF
}

# ---------------------------------------------------------------------------
# Parse de argumentos
# ---------------------------------------------------------------------------
SN=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        -n|--node)         NODE="${2:-}"; shift 2 ;;
        -a|--accent)       ACCENT="${2:-}"; shift 2 ;;
        -u|--user)         SSH_USER="${2:-}"; shift 2 ;;
        -b|--black-ip)     BLACK_IP="${2:-}"; shift 2 ;;
        -p|--modem-port)   MODEM_PORT="${2:-}"; shift 2 ;;
        -l|--local-port)   LOCAL_PORT="${2:-}"; shift 2 ;;
        --red-prefix)      RED_PREFIX="${2:-}"; shift 2 ;;
        --no-app)          NO_APP=1; shift ;;
        --skip-preflight)  SKIP_PREFLIGHT=1; shift ;;
        --ale-port)        ALE_PORT="${2:-}"; shift 2 ;;
        --ale-black-ip)    ALE_BLACK_IP="${2:-}"; shift 2 ;;
        --local-ale-port)  LOCAL_ALE_PORT="${2:-}"; shift 2 ;;
        --ale-bridge-port) ALE_BRIDGE_PORT="${2:-}"; shift 2 ;;
        --ale-host)        ALE_HOST_OVERRIDE="${2:-}"; shift 2 ;;
        --no-ale)          NO_ALE=1; shift ;;
        -h|--help)         usage; exit 0 ;;
        -*)                echo "ERRO: opção desconhecida: $1" >&2; usage >&2; exit 2 ;;
        *)
            if [[ -z "$SN" ]]; then SN="$1"; shift
            else echo "ERRO: argumento posicional extra: $1" >&2; exit 2; fi
            ;;
    esac
done

# ---------------------------------------------------------------------------
# Validação
# ---------------------------------------------------------------------------
if [[ -z "$SN" ]]; then
    echo "ERRO: informe o serial number (SN)." >&2
    usage >&2
    exit 2
fi
if ! [[ "$SN" =~ ^[0-9]+$ ]] || (( SN < 1 || SN > 254 )); then
    echo "ERRO: SN inválido ('$SN'). Deve ser inteiro 1-254 (é o último octeto de ${RED_PREFIX}.<SN>)." >&2
    exit 2
fi
if [[ "$NODE" != "A" && "$NODE" != "B" ]]; then
    echo "ERRO: --node deve ser A ou B (recebido: '$NODE')." >&2
    exit 2
fi
if [[ -n "$ACCENT" && ! "$ACCENT" =~ ^(blue|green|purple|orange|gray)$ ]]; then
    echo "ERRO: --accent inválido ('$ACCENT'). Use: blue|green|purple|orange|gray." >&2
    exit 2
fi
if [[ -z "$SSH_USER" ]]; then
    echo "ERRO: usuário SSH vazio. Use -u/--user ou defina \$USER." >&2
    exit 2
fi
if [[ ! -f "$APP" ]]; then
    echo "ERRO: app não encontrada em: $APP" >&2
    exit 1
fi
[[ -z "$LOCAL_PORT" ]] && LOCAL_PORT="$MODEM_PORT"

# ---- derivar defaults do ALE ----
[[ -z "$ALE_BLACK_IP" ]] && ALE_BLACK_IP="$BLACK_IP"
[[ -z "$LOCAL_ALE_PORT" ]] && LOCAL_ALE_PORT="$ALE_PORT"
[[ -z "$ALE_BRIDGE_PORT" ]] && ALE_BRIDGE_PORT=$(( LOCAL_ALE_PORT + 1 ))
for _p in "$ALE_PORT" "$LOCAL_ALE_PORT" "$ALE_BRIDGE_PORT"; do
    if ! [[ "$_p" =~ ^[0-9]+$ ]] || (( _p < 1 || _p > 65535 )); then
        echo "ERRO: porta ALE inválida ('$_p'). Deve ser inteiro 1-65535." >&2
        exit 2
    fi
done

RED_IP="${RED_PREFIX}.${SN}"
SSH_TARGET="${SSH_USER}@${RED_IP}"
CTRL="$(mktemp -u "${TMPDIR:-/tmp}/ssh-110d-${SN}-XXXXXX")"

# Plano do ALE (para o resumo); a viabilidade real da ponte só se sabe com o master no ar.
if (( NO_ALE )); then
    ALE_SUMMARY="desativado (--no-ale)"
elif [[ -n "$ALE_HOST_OVERRIDE" ]]; then
    ALE_SUMMARY="${ALE_HOST_OVERRIDE}:${ALE_PORT}/udp (direto, sem túnel)"
else
    ALE_SUMMARY="ponte socat UDP -> ${ALE_BLACK_IP}:${ALE_PORT}/udp (via red)"
fi

# ---------------------------------------------------------------------------
# Teardown ao sair: mata os socats da ponte ALE e fecha o control-master.
# ---------------------------------------------------------------------------
cleanup() {
    if [[ -n "$LOCAL_SOCAT_PID" ]]; then
        kill "$LOCAL_SOCAT_PID" 2>/dev/null || true
    fi
    if [[ "$ALE_BRIDGE_ACTIVE" -eq 1 && -S "$CTRL" ]]; then
        ssh -S "$CTRL" "$SSH_TARGET" \
            "pkill -f 'socat.*TCP4-LISTEN:${ALE_BRIDGE_PORT}'" 2>/dev/null || true
    fi
    if [[ -S "$CTRL" ]]; then
        ssh -S "$CTRL" -O exit "$SSH_TARGET" 2>/dev/null || true
    fi
}
trap cleanup EXIT INT TERM

# ---------------------------------------------------------------------------
# Resumo
# ---------------------------------------------------------------------------
cat <<EOF
────────────────────────────────────────────────────────────
 Serial (SN):   ${SN}
 Red (SSH):     ${SSH_TARGET}
 Modem (black): ${BLACK_IP}:${MODEM_PORT}   (via red)
 Túnel local:   127.0.0.1:${LOCAL_PORT}  ->  ${BLACK_IP}:${MODEM_PORT}
 Nó STANAG:     ${NODE}
 Radio Control: ${ALE_SUMMARY}
 App:           Subnet Console (PyQt6) — modo --live (liga ao modem real)
────────────────────────────────────────────────────────────
EOF

# ---------------------------------------------------------------------------
# Sobe o túnel SSH (control-master).
#   -f : autentica em foreground (permite digitar senha/passphrase) e vai pro
#        background só depois; retorna != 0 se o forward falhar.
# ---------------------------------------------------------------------------
echo "==> Estabelecendo túnel SSH pela red (${RED_IP})..."
if ! ssh -f -N -M -S "$CTRL" \
        -o ExitOnForwardFailure=yes \
        -o ConnectTimeout=10 \
        -o ServerAliveInterval=15 -o ServerAliveCountMax=3 \
        -L "127.0.0.1:${LOCAL_PORT}:${BLACK_IP}:${MODEM_PORT}" \
        "$SSH_TARGET"; then
    echo "ERRO: falha ao subir o túnel SSH para ${SSH_TARGET}." >&2
    echo "      Verifique acesso SSH à red e se a porta local ${LOCAL_PORT} está livre." >&2
    exit 1
fi
echo "    túnel ativo: 127.0.0.1:${LOCAL_PORT} -> ${BLACK_IP}:${MODEM_PORT}"

# ---------------------------------------------------------------------------
# Preflight: o modem está acessível A PARTIR DA RED? (reusa a conexão SSH)
# Distingue "modem/rede fora" de "bug da app". Não-fatal.
# ---------------------------------------------------------------------------
if [[ "$SKIP_PREFLIGHT" -eq 0 ]]; then
    echo "==> Verificando ${BLACK_IP}:${MODEM_PORT} a partir da red..."
    if ssh -S "$CTRL" "$SSH_TARGET" \
            "timeout 3 bash -c '</dev/tcp/${BLACK_IP}/${MODEM_PORT}'" 2>/dev/null; then
        echo "    modem acessível pela red ✓"
    else
        echo "    AVISO: a red NÃO conseguiu conectar em ${BLACK_IP}:${MODEM_PORT}." >&2
        echo "           O modem pode estar fora, na porta errada, ou bloqueado por firewall." >&2
        echo "           A app vai ficar em 'Conectando…' até o modem responder." >&2
    fi
fi

# ---------------------------------------------------------------------------
# Radio Control (ALE 2G, UDP): monta a ponte UDP-sobre-SSH com socat, ou liga
# direto (--ale-host), ou desativa (--no-ale / socat ausente).
# ---------------------------------------------------------------------------
ALE_APP_ARGS=()
if (( NO_ALE )); then
    echo "==> Radio Control (ALE) desativado por --no-ale."
    ALE_APP_ARGS=(--no-ale)
elif [[ -n "$ALE_HOST_OVERRIDE" ]]; then
    echo "==> Radio Control (ALE) direto: ${ALE_HOST_OVERRIDE}:${ALE_PORT}/udp (sem túnel)."
    ALE_APP_ARGS=(--ale-host "$ALE_HOST_OVERRIDE" --ale-port "$ALE_PORT")
elif ! command -v socat >/dev/null 2>&1; then
    echo "    AVISO: 'socat' não encontrado localmente — não é possível tunelar o UDP :${ALE_PORT} do ALE." >&2
    echo "           Radio Control ficará OFFLINE. Instale socat, use --ale-host <ip-direto>, ou --no-ale." >&2
    ALE_APP_ARGS=(--no-ale)
elif ! ssh -S "$CTRL" "$SSH_TARGET" 'command -v socat >/dev/null 2>&1'; then
    echo "    AVISO: 'socat' não encontrado na red (${RED_IP}) — não é possível tunelar o UDP do ALE." >&2
    echo "           Radio Control ficará OFFLINE. Instale socat na red, use --ale-host, ou --no-ale." >&2
    ALE_APP_ARGS=(--no-ale)
else
    echo "==> Montando ponte UDP-sobre-SSH do ALE (socat):"
    echo "    127.0.0.1:${LOCAL_ALE_PORT}/udp  ->  [tcp ${ALE_BRIDGE_PORT} via red]  ->  ${ALE_BLACK_IP}:${ALE_PORT}/udp"
    # 1) forward do porto TCP da ponte sobre o master já ativo
    if ! ssh -O forward -S "$CTRL" \
            -L "127.0.0.1:${ALE_BRIDGE_PORT}:127.0.0.1:${ALE_BRIDGE_PORT}" "$SSH_TARGET" 2>/dev/null; then
        echo "    AVISO: não foi possível abrir o forward TCP :${ALE_BRIDGE_PORT} da ponte ALE." >&2
        echo "           Radio Control ficará OFFLINE (tente --ale-bridge-port com outra porta livre)." >&2
        ALE_APP_ARGS=(--no-ale)
    else
        # 2) lado red: TCP-LISTEN -> UDP para o backend do rádio na black
        ssh -S "$CTRL" "$SSH_TARGET" \
            "nohup socat -T30 TCP4-LISTEN:${ALE_BRIDGE_PORT},reuseaddr,fork UDP4:${ALE_BLACK_IP}:${ALE_PORT} >/dev/null 2>&1 &" \
            2>/dev/null || true
        ALE_BRIDGE_ACTIVE=1
        # 3) lado local: UDP-LISTEN (alvo da app) -> TCP para dentro do túnel
        socat -T30 "UDP4-LISTEN:${LOCAL_ALE_PORT},reuseaddr,fork" \
              "TCP4:127.0.0.1:${ALE_BRIDGE_PORT}" >/dev/null 2>&1 &
        LOCAL_SOCAT_PID=$!
        echo "    ponte ALE ativa (socat local pid ${LOCAL_SOCAT_PID}; red em ${ALE_BRIDGE_PORT}/tcp)"
        ALE_APP_ARGS=(--ale-host 127.0.0.1 --ale-port "$LOCAL_ALE_PORT")
    fi
fi

# ---------------------------------------------------------------------------
# Lança a app (ou mantém só o(s) túnel(is))
# ---------------------------------------------------------------------------
# --live arranca um StanagNode real ligado ao modem via túnel; as flags do ALE
# apontam o Radio Control para a ponte socat (ou o host direto / --no-ale).
APP_ARGS=(--live --node "$NODE" --modem-host 127.0.0.1 --modem-port "$LOCAL_PORT" "${ALE_APP_ARGS[@]}")
[[ -n "$ACCENT" ]] && APP_ARGS+=(--accent "$ACCENT")

if [[ "$NO_APP" -eq 1 ]]; then
    echo
    echo "Túnel(is) no ar. No painel 'Modem Link' use  Modem host=127.0.0.1  Porta=${LOCAL_PORT}."
    if [[ "$ALE_BRIDGE_ACTIVE" -eq 1 ]]; then
        echo "Radio Control: aponte para  --ale-host 127.0.0.1 --ale-port ${LOCAL_ALE_PORT}  (ponte socat ativa)."
    fi
    echo "Ex.: $PYTHON \"$APP\" ${APP_ARGS[*]}"
    echo
    read -r -p "Pressione Enter para encerrar o(s) túnel(is)..." _
    exit 0
fi

echo "==> Lançando Subnet Console (nó ${NODE})..."
echo "    (feche a janela da app para encerrar o(s) túnel(is))"
echo
set +e
"$PYTHON" "$APP" "${APP_ARGS[@]}"
APP_RC=$?
set -e

echo
echo "==> App encerrada (rc=${APP_RC}). Fechando túnel(is)."
exit "$APP_RC"
