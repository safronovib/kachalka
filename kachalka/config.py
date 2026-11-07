"""Пути и настройки. Всё состояние живёт в ~/Library/Application Support/Kachalka."""

from __future__ import annotations

import json
import os
from pathlib import Path

APP_DIR = Path(os.environ.get("KACHALKA_HOME", Path.home() / "Library/Application Support/Kachalka"))
SOCKET = APP_DIR / "kachalka.sock"
DB_PATH = APP_DIR / "history.sqlite3"
RESUME_DIR = APP_DIR / "resume"
TORRENTS_DIR = APP_DIR / "torrents"
LOG_PATH = APP_DIR / "kachalka.log"
CONFIG_PATH = APP_DIR / "config.json"
PID_PATH = APP_DIR / "kachalka.pid"

DEFAULTS: dict = {
    # Куда качать, если при добавлении папка не указана.
    "download_dir": str(Path.home() / "Movies" / "Торренты"),
    "listen_port": 6881,
    # Ограничения скорости в КБ/с, 0 = без ограничений.
    "download_limit_kbps": 0,
    "upload_limit_kbps": 0,
    # После какого рейтинга (отдано / скачано) ставить раздачу на паузу. null = раздавать всегда.
    "stop_seeding_at_ratio": 1.0,
    # Системное уведомление macOS, когда загрузка закончилась.
    "notify": True,
}


def ensure_dirs() -> None:
    for d in (APP_DIR, RESUME_DIR, TORRENTS_DIR):
        d.mkdir(parents=True, exist_ok=True)


def load_config() -> dict:
    ensure_dirs()
    cfg = dict(DEFAULTS)
    if CONFIG_PATH.exists():
        try:
            cfg.update(json.loads(CONFIG_PATH.read_text("utf-8")))
        except (OSError, ValueError):
            pass
    else:
        save_config(cfg)
    return cfg


def save_config(cfg: dict) -> None:
    ensure_dirs()
    tmp = CONFIG_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", "utf-8")
    tmp.replace(CONFIG_PATH)
