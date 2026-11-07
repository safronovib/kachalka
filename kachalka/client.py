"""Клиент: отправляет команду фоновой программе и при необходимости запускает её."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

from .config import APP_DIR, LOG_PATH, SOCKET, ensure_dirs

AGENT_LABEL = "com.kachalka.daemon"
AGENT_PLIST = Path.home() / "Library/LaunchAgents" / f"{AGENT_LABEL}.plist"


class NotRunning(Exception):
    pass


def call(cmd: str, timeout: float = 30, **args) -> dict:
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect(str(SOCKET))
    except (FileNotFoundError, ConnectionRefusedError) as e:
        s.close()
        raise NotRunning() from e
    try:
        s.sendall(json.dumps({"cmd": cmd, **args}, ensure_ascii=False).encode() + b"\n")
        buf = b""
        while not buf.endswith(b"\n"):
            chunk = s.recv(65536)
            if not chunk:
                break
            buf += chunk
    finally:
        s.close()
    resp = json.loads(buf or b"{}")
    if not resp.get("ok"):
        raise RuntimeError(resp.get("error", "нет ответа"))
    resp.pop("ok", None)
    return resp


def is_running() -> bool:
    try:
        call("ping", timeout=2)
        return True
    except (NotRunning, OSError, RuntimeError, ValueError):
        return False


def start_background(wait: float = 10) -> bool:
    """Запускает фоновую программу отдельным процессом. True, если она ответила."""
    if is_running():
        return True
    ensure_dirs()
    if AGENT_PLIST.exists():
        # Установлен автозапуск: поднимаем через launchd, чтобы программа не стала
        # дочерним процессом того, кто вызвал команду (терминала, песочницы, SwiftBar).
        subprocess.run(["launchctl", "kickstart", f"gui/{os.getuid()}/{AGENT_LABEL}"],
                       capture_output=True)
    else:
        root = Path(__file__).resolve().parent.parent
        env = dict(os.environ, PYTHONPATH=str(root))
        with open(LOG_PATH, "ab") as logf:
            subprocess.Popen(
                [sys.executable, "-m", "kachalka.daemon"],
                cwd=str(APP_DIR), env=env, stdin=subprocess.DEVNULL,
                stdout=logf, stderr=logf, start_new_session=True,
            )
    deadline = time.time() + wait
    while time.time() < deadline:
        if is_running():
            return True
        time.sleep(0.2)
    return False
