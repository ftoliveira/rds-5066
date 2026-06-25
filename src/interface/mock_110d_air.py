"""Lançador de modem-mock local para os chats 110D/TCP (`chat_app_110d.py`).

Sobe dois modems MIL-STD-188-110D simulados (Appendix A / TCP) em portas fixas e
cruza o "ar" entre eles: o que o nó A transmite (keyed-up) chega como RX ao nó B
e vice-versa — modelando dois modems `rds-hf` enlaçados por OTA. Permite demonstrar
dois `chat_app_110d` na mesma máquina **sem** o backend `rds-hf` real.

Reusa o mock fiel `tests/mock_110d_modem.py` (espelha `net_reactor.c`).

Uso:
    python src/interface/mock_110d_air.py            # portas 3000 (A) e 3001 (B)
    python src/interface/mock_110d_air.py --port-a 3000 --port-b 3001 --data-rate 2400

Depois, em dois terminais:
    python src/interface/chat_app_110d.py --node A   # conecta no modem A (3000)
    python src/interface/chat_app_110d.py --node B   # conecta no modem B (3001)
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

# Raiz do repositório no sys.path para importar `src.*` e `tests.*`.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from tests.mock_110d_modem import MockModem110d


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Modem-mock MIL-STD-188-110D local (ar cruzado A↔B) para os chats 110D/TCP.",
    )
    parser.add_argument("--host", default="127.0.0.1", help="Host de bind (default 127.0.0.1)")
    parser.add_argument("--port-a", type=int, default=3000, help="Porta TCP do modem do nó A (default 3000)")
    parser.add_argument("--port-b", type=int, default=3001, help="Porta TCP do modem do nó B (default 3001)")
    parser.add_argument("--data-rate", type=int, default=2400, help="Taxa OTA reportada (Transmit Setup), bps")
    parser.add_argument("--blocking-factor", type=int, default=600, help="Blocking factor reportado")
    args = parser.parse_args()

    modem_a = MockModem110d(
        host=args.host, port=args.port_a,
        data_rate=args.data_rate, blocking_factor=args.blocking_factor,
    )
    modem_b = MockModem110d(
        host=args.host, port=args.port_b,
        data_rate=args.data_rate, blocking_factor=args.blocking_factor,
    )
    # Cruza o ar: TX keyed-up de um vira RX do outro.
    modem_a.on_air_tx = modem_b.deliver_air_rx
    modem_b.on_air_tx = modem_a.deliver_air_rx

    modem_a.start()
    modem_b.start()
    print(f"[mock-110d] Modem A escutando TCP {args.host}:{modem_a.port}  → use: chat_app_110d.py --node A")
    print(f"[mock-110d] Modem B escutando TCP {args.host}:{modem_b.port}  → use: chat_app_110d.py --node B")
    print(f"[mock-110d] Ar cruzado A↔B ativo (taxa={args.data_rate} bps). Ctrl+C para encerrar.")
    try:
        while True:
            time.sleep(1.0)
    except KeyboardInterrupt:
        print("\n[mock-110d] Encerrando...")
    finally:
        modem_a.stop()
        modem_b.stop()


if __name__ == "__main__":
    main()
