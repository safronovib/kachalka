"""Выбор сетевого интерфейса для торрент-трафика.

Зачем: VPN вроде AmneziaVPN заворачивают трафик двумя «половинками» маршрута (0/1 и 128/1),
а маршрут по умолчанию оставляют на Wi-Fi. libtorrent смотрит на маршрут по умолчанию
и шлёт пакеты с адреса Wi-Fi, а kill switch их режет. Поэтому в режиме VPN мы явно
привязываемся к туннелю, а в прямом режиме — к физическому интерфейсу.
"""

from __future__ import annotations

import subprocess

TUNNEL_PREFIXES = ("utun", "ipsec", "ppp", "tun", "tap", "wg")
PROBE_IP = "1.1.1.1"

# Значения настройки bind_interface
AUTO = "auto"      # через VPN, если он есть, иначе как система
DIRECT = "direct"  # напрямую через Wi-Fi или Ethernet, мимо VPN


def _route_interface(target: str) -> str:
    out = subprocess.run(["route", "-n", "get", target], capture_output=True, text=True).stdout
    for line in out.splitlines():
        if line.strip().startswith("interface:"):
            return line.split(":", 1)[1].strip()
    return ""


def is_tunnel(iface: str) -> bool:
    return iface.startswith(TUNNEL_PREFIXES)


def vpn_interface() -> str:
    """Туннель, через который сейчас идёт интернет, или пусто, если VPN выключен."""
    iface = _route_interface(PROBE_IP)
    return iface if is_tunnel(iface) else ""


def physical_interface() -> str:
    """Основной физический интерфейс (Wi-Fi или Ethernet), даже когда VPN включён."""
    iface = _route_interface("default")
    if iface and not is_tunnel(iface):
        return iface
    ports = subprocess.run(["networksetup", "-listallhardwareports"], capture_output=True, text=True).stdout
    for line in ports.splitlines():
        if line.startswith("Device:"):
            dev = line.split(":", 1)[1].strip()
            if subprocess.run(["ipconfig", "getifaddr", dev], capture_output=True, text=True).stdout.strip():
                return dev
    return "en0"


def resolve(setting: str) -> str:
    """Настройка → конкретный интерфейс. Пустая строка = пусть решает система."""
    setting = (setting or "").strip()
    if setting == AUTO:
        return vpn_interface()
    if setting == DIRECT:
        return physical_interface()
    return setting
