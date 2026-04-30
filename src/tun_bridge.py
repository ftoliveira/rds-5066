"""
TUN Bridge — Ponte entre interface TUN (Linux) e IPClient (STANAG 5066).

Cria uma interface de rede virtual (tun0) que encaminha datagramas IP
para a sub-rede HF via IPClient e vice-versa.

Uso típico:
    # Terminal 1 — inicia o nó STANAG + túnel
    python -m stanag.tun_bridge --local-addr 0x01 --local-ip 10.66.0.1/24 --peer 10.66.0.2=0x02 --peer 10.66.0.3=0x03

    # Terminal 2 — usa normalmente
    ping 10.66.0.2        # vai pelo HF!
    ssh user@10.66.0.2    # SSH sobre HF (lento mas funciona)

Requer: Linux, root (ou CAP_NET_ADMIN), Python 3.10+.
"""

from __future__ import annotations

import asyncio
import ctypes
import fcntl
import logging
import os
import struct
import subprocess
import sys
from pathlib import Path

# Adjust PYTHONPATH
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'stanag'))

from src.annex_f.ip_client import IPClient

logger = logging.getLogger(__name__)

# ioctl constants for TUN/TAP (Linux)
TUNSETIFF = 0x400454CA
IFF_TUN = 0x0001      # Layer 3 (IP packets)
IFF_NO_PI = 0x1000    # No packet info header


class TunDevice:
    """Interface TUN Linux — lê e escreve datagramas IP raw."""

    def __init__(self, name: str = 'tun_hf'):
        self.name = name
        self.fd: int = -1

    def open(self) -> str:
        """Abre /dev/net/tun e retorna o nome real da interface."""
        self.fd = os.open('/dev/net/tun', os.O_RDWR)

        # struct ifreq: 16 bytes name + 2 bytes flags + padding
        ifr = struct.pack('16sH', self.name.encode('utf-8'), IFF_TUN | IFF_NO_PI)
        result = fcntl.ioctl(self.fd, TUNSETIFF, ifr)

        # Kernel pode ter atribuído nome diferente (tun0, tun1, ...)
        self.name = result[:16].split(b'\x00', 1)[0].decode('utf-8')
        logger.info("TUN device opened: %s (fd=%d)", self.name, self.fd)
        return self.name

    def configure(self, ip_cidr: str, mtu: int = 1400):
        """Configura IP, MTU e ativa a interface via ip command."""
        subprocess.run(['ip', 'addr', 'add', ip_cidr, 'dev', self.name], check=True)
        subprocess.run(['ip', 'link', 'set', 'dev', self.name, 'mtu', str(mtu)], check=True)
        subprocess.run(['ip', 'link', 'set', 'dev', self.name, 'up'], check=True)
        logger.info("TUN %s configured: %s mtu=%d", self.name, ip_cidr, mtu)

    def read_packet(self) -> bytes:
        """Lê um datagrama IP do TUN (bloqueante)."""
        return os.read(self.fd, 65535)

    def write_packet(self, datagram: bytes):
        """Injeta um datagrama IP no TUN (entrega ao kernel/network stack)."""
        os.write(self.fd, datagram)

    def fileno(self) -> int:
        return self.fd

    def close(self):
        if self.fd >= 0:
            os.close(self.fd)
            self.fd = -1


class TunBridge:
    """Ponte bidirecional TUN <-> IPClient.

    Direção TX (local -> HF):
        Aplicação -> kernel -> tun0 -> TunBridge.read -> IPClient.send_ip_datagram

    Direção RX (HF -> local):
        HF -> IPClient._on_data_received -> TunBridge.write -> tun0 -> kernel -> Aplicação
    """

    def __init__(self, ip_client: IPClient, tun_name: str = 'tun_hf', local_ip_cidr: str = '10.66.0.1/24', mtu: int = 1400):
        self.ip_client = ip_client
        self.tun = TunDevice(tun_name)
        self.local_ip_cidr = local_ip_cidr
        self.mtu = mtu
        self._running = False
        self._stats = {'tx_packets': 0, 'rx_packets': 0, 'tx_bytes': 0, 'rx_bytes': 0, 'tx_drops': 0}

    def start(self):
        """Abre TUN, configura, e conecta callbacks."""
        self.tun.open()
        self.tun.configure(self.local_ip_cidr, self.mtu)

        # Ajusta MTU do IPClient para casar com o TUN
        self.ip_client.mtu = self.mtu

        # Callback RX: quando IPClient recebe datagrama do HF, injeta no TUN
        self.ip_client.on_ip_received = self._on_hf_received

        self._running = True
        logger.info("TUN bridge started: %s <-> IPClient SAP %d", self.tun.name, self.ip_client.SAP_ID)

    def stop(self):
        self._running = False
        self.tun.close()
        logger.info("TUN bridge stopped. Stats: %s", self._stats)

    def run_tx_loop(self):
        """Loop bloqueante: lê pacotes do TUN e encaminha ao HF.

        Chamar em uma thread dedicada ou integrar com asyncio via add_reader().
        """
        while self._running:
            try:
                datagram = self.tun.read_packet()
                if not datagram:
                    continue

                ok = self.ip_client.send_ip_datagram(datagram)
                if ok:
                    self._stats['tx_packets'] += 1
                    self._stats['tx_bytes'] += len(datagram)
                else:
                    self._stats['tx_drops'] += 1

            except OSError as e:
                if self._running:
                    logger.error("TUN read error: %s", e)
                break

    async def run_tx_loop_async(self, loop: asyncio.AbstractEventLoop | None = None):
        """Versão asyncio do TX loop usando add_reader."""
        if loop is None:
            loop = asyncio.get_running_loop()

        future: asyncio.Future[None] = loop.create_future()

        def on_tun_readable():
            try:
                datagram = self.tun.read_packet()
                if datagram:
                    ok = self.ip_client.send_ip_datagram(datagram)
                    if ok:
                        self._stats['tx_packets'] += 1
                        self._stats['tx_bytes'] += len(datagram)
                    else:
                        self._stats['tx_drops'] += 1
            except OSError as e:
                if self._running:
                    logger.error("TUN read error: %s", e)
                    loop.remove_reader(self.tun.fileno())
                    if not future.done():
                        future.set_result(None)

        loop.add_reader(self.tun.fileno(), on_tun_readable)
        logger.info("TUN async TX loop started (fd=%d)", self.tun.fileno())

        try:
            await future
        except asyncio.CancelledError:
            pass
        finally:
            loop.remove_reader(self.tun.fileno())

    def _on_hf_received(self, datagram: bytes, src_stanag_addr: int):
        """Callback: datagrama IP chegou do HF -> injeta no TUN."""
        try:
            self.tun.write_packet(datagram)
            self._stats['rx_packets'] += 1
            self._stats['rx_bytes'] += len(datagram)
        except OSError as e:
            logger.error("TUN write error: %s", e)

    @property
    def stats(self) -> dict:
        return dict(self._stats)
