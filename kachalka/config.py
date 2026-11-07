"""Пути и настройки. Всё состояние живёт в ~/Library/Application Support/Kachalka."""

from __future__ import annotations

import json
import os
from pathlib import Path

APP_DIR = Path(os.environ.get("KACHALKA_HOME", Path.home() / "Library/Application Support/Kachalka"))
SOCKET = APP_DIR / "kachalka.sock"
if len(os.fsencode(SOCKET)) > 100:  # у macOS предел пути Unix-сокета 104 байта
    import hashlib
    import tempfile
    _tag = hashlib.sha1(os.fsencode(APP_DIR)).hexdigest()[:10]
    SOCKET = Path(tempfile.gettempdir()) / f"kachalka-{os.getuid()}-{_tag}.sock"
DB_PATH = APP_DIR / "history.sqlite3"
RESUME_DIR = APP_DIR / "resume"
TORRENTS_DIR = APP_DIR / "torrents"
LOG_PATH = APP_DIR / "kachalka.log"
CONFIG_PATH = APP_DIR / "config.json"
PID_PATH = APP_DIR / "kachalka.pid"

DEFAULTS: dict = {
    # Куда качать, если при добавлении папка не указана.
    "download_dir": str(Path.home() / "Movies"),  # в Finder это папка «Фильмы»
    "listen_port": 6881,
    # Через какой интерфейс идут торренты:
    #   "auto"   — через VPN-туннель, если он поднят (имя utunN находится само), иначе как система;
    #   "direct" — напрямую через Wi-Fi/Ethernet, мимо VPN;
    #   "en0", "utun6"… — конкретный интерфейс; "" — как решит система.
    "bind_interface": "auto",
    # Ограничения скорости в КБ/с, 0 = без ограничений.
    "download_limit_kbps": 0,
    "upload_limit_kbps": 0,
    # После какого рейтинга (отдано / скачано) ставить раздачу на паузу. null = раздавать всегда.
    "stop_seeding_at_ratio": 1.0,
    # Системное уведомление macOS, когда загрузка закончилась.
    "notify": True,
    # Этап 2: раскладывать скачанное по папкам сразу после окончания загрузки.
    "auto_sort": True,
    # "flat": «Фильмы/Трудно быть богом/серии» (один клик); "nested": «Сериалы/Название/Сезон N/…»
    "layout": "flat",
    # Корень библиотеки; пусто = download_dir. Внутри — папки из "library".
    "library_dir": "",
    # Этап 3: предлагать убрать раздачи с видео, досмотренные больше N дней назад
    # и скачанные, но не открытые больше N дней. 0 = не предлагать. Удаление — только по команде.
    "cleanup_watched_days": 3,
    "cleanup_unopened_days": 30,
    # Режим «смотрю сериал» по умолчанию: сколько серий держать наготове после текущей
    # и через сколько часов после просмотра убирать серию в Корзину.
    "follow_ahead": 2,
    "follow_keep_hours": 24,
    # Названия папок по типам; можно переопределить любую.
    "library": {
        "movie": "Фильмы", "series": "Сериалы", "music": "Музыка",
        "book": "Книги", "software": "Программы", "other": "Другое",
    },
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
