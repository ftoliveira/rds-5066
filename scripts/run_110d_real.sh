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
# NOTA (Fase 1): o Subnet Console é, nesta fase, a casca de UI. O túnel e o
# preflight abaixo continuam válidos (verificam a rota até o modem), e o console é
# lançado com o painel "Modem Link" já pré-preenchido com host/porta do túnel —
# mas a ligação TCP ao vivo ao modem (data path DTE↔DCE) é da Fase 2. Para um teste
# de tráfego real contra o modem hoje, use src/interface/chat_app_110d.py.
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

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP="$REPO_ROOT/src/interface/subnet_console/__main__.py"

usage() {
    cat <<EOF
Uso: $(basename "$0") <serial_number> [opções]

Sobe um túnel SSH pela red e lança o Subnet Console (PyQt6) na sua máquina,
com o painel "Modem Link" apontando para o modem 110D real na black via túnel.

Positional:
  serial_number         SN do equipamento (1-254). Red IP = ${RED_PREFIX}.<SN>

Opções:
  -n, --node A|B        Identidade do nó STANAG (default: ${NODE})
  -a, --accent COR      Cor do tema: blue|green|purple|orange|gray (default: app)
  -u, --user USER       Usuário SSH na red (default: \$USER = ${SSH_USER})
  -b, --black-ip IP     IP do modem visto pela red (default: ${BLACK_IP})
  -p, --modem-port N    Porta TCP do modem / Appendix A (default: ${MODEM_PORT})
  -l, --local-port N    Porta local do túnel (default: = porta do modem)
      --red-prefix P    Prefixo /24 da red (default: ${RED_PREFIX})
      --no-app          Só sobe o túnel (não lança a GUI); Enter encerra
      --skip-preflight  Não testa a acessibilidade do modem pela red
  -h, --help            Esta ajuda

Exemplos:
  # red em 192.168.108.12, nó A, modem 192.168.0.2:3000 via 127.0.0.1:3000
  $(basename "$0") 12

  # usuário SSH 'rds' na red, nó B
  $(basename "$0") 12 -u rds -n B

  # só o túnel, para usar com uma app já aberta
  $(basename "$0") 12 --no-app
EOF
}

# ---------------------------------------------------------------------------
# Parse de argumentos
# ---------------------------------------------------------------------------
SN=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        -n|--node)        NODE="${2:-}"; shift 2 ;;
        -a|--accent)      ACCENT="${2:-}"; shift 2 ;;
        -u|--user)        SSH_USER="${2:-}"; shift 2 ;;
        -b|--black-ip)    BLACK_IP="${2:-}"; shift 2 ;;
        -p|--modem-port)  MODEM_PORT="${2:-}"; shift 2 ;;
        -l|--local-port)  LOCAL_PORT="${2:-}"; shift 2 ;;
        --red-prefix)     RED_PREFIX="${2:-}"; shift 2 ;;
        --no-app)         NO_APP=1; shift ;;
        --skip-preflight) SKIP_PREFLIGHT=1; shift ;;
        -h|--help)        usage; exit 0 ;;
        -*)               echo "ERRO: opção desconhecida: $1" >&2; usage >&2; exit 2 ;;
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

RED_IP="${RED_PREFIX}.${SN}"
SSH_TARGET="${SSH_USER}@${RED_IP}"
CTRL="$(mktemp -u "${TMPDIR:-/tmp}/ssh-110d-${SN}-XXXXXX")"

# ---------------------------------------------------------------------------
# Teardown do túnel ao sair (fecha o control-master)
# ---------------------------------------------------------------------------
cleanup() {
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
 App:           Subnet Console (PyQt6) — Fase 1 (UI; painel Modem pré-preenchido)
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
# Lança a app (ou mantém só o túnel)
# ---------------------------------------------------------------------------
# Argumentos da app (--accent só é passado se fornecido).
APP_ARGS=(--node "$NODE" --modem-host 127.0.0.1 --modem-port "$LOCAL_PORT")
[[ -n "$ACCENT" ]] && APP_ARGS+=(--accent "$ACCENT")

if [[ "$NO_APP" -eq 1 ]]; then
    echo
    echo "Túnel no ar. No painel 'Modem Link' use  Modem host=127.0.0.1  Porta=${LOCAL_PORT}."
    echo "Ex.: $PYTHON \"$APP\" ${APP_ARGS[*]}"
    echo
    read -r -p "Pressione Enter para encerrar o túnel..." _
    exit 0
fi

echo "==> Lançando Subnet Console (nó ${NODE})..."
echo "    (feche a janela da app para encerrar o túnel)"
echo
set +e
"$PYTHON" "$APP" "${APP_ARGS[@]}"
APP_RC=$?
set -e

echo
echo "==> App encerrada (rc=${APP_RC}). Fechando túnel."
exit "$APP_RC"
