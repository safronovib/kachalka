"""Вывод для SwiftBar: значок в строке меню с процентом и меню управления.

Пока ничего не качается (и ничего не докачалось за последние минуты), вывод пустой,
значка нет.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from . import fmt
from .client import NotRunning, call

DONE_VISIBLE_SECONDS = 600
ACTIVE = {"качается", "ищу метаданные", "проверка", "выделяю место"}


def _q(s: str) -> str:
    """Экранирование значения параметра SwiftBar."""
    return '"' + str(s).replace("\\", "\\\\").replace('"', '\\"') + '"'


def _clean(s: str) -> str:
    # «|» в SwiftBar отделяет параметры строки, переводы строк ломают меню
    return str(s).replace("|", "¦").replace("\n", " ")


def action(cli: str, title: str, *params: str, refresh: bool = True) -> str:
    parts = [f"{title} | bash={_q(cli)}"]
    parts += [f"param{i}={_q(p)}" for i, p in enumerate(params, 1)]
    parts.append("terminal=false")
    if refresh:
        parts.append("refresh=true")
    return " ".join(parts)


def render(cli: str) -> str:
    try:
        torrents = call("list", timeout=3)["torrents"]
        history = call("history", timeout=3, limit=20)["history"]
        iface = call("info", timeout=3).get("bind_interface", "")
    except (NotRunning, OSError, RuntimeError, ValueError):
        return ""

    active = [t for t in torrents if t["state"] in ACTIVE]
    now = datetime.now(timezone.utc)
    recent_done = [
        h for h in history
        if h.get("completed_at") and not h.get("removed_at")
        and (now - datetime.fromisoformat(h["completed_at"])).total_seconds() < DONE_VISIBLE_SECONDS
    ]
    if not active and not recent_done:
        return ""

    out: list[str] = []
    if active:
        size = sum(t["size"] for t in active) or 1
        done = sum(t["done"] for t in active)
        rate = sum(t["down_rate"] for t in active)
        pct = int(done * 100 / size)
        left = int((size - done) / rate) if rate else None
        tail = f"  {fmt.eta(left)}" if left is not None else ""
        out.append(f"⬇ {pct}%{tail}")
    else:
        out.append("⬇ ✓ скачано")
    out.append("---")

    for t in torrents:
        name = _clean(t["name"])
        # Управление — подменю у строки с названием
        out.append(f"{name[:60]} | size=13")
        target = str(Path(t["save_path"]) / t["name"])
        if t["paused"]:
            out.append("--" + action(cli, "Продолжить", "resume", t["id"]))
        else:
            out.append("--" + action(cli, "Пауза", "pause", t["id"]))
        out.append("--" + action(cli, "Показать в Finder", "reveal", target, refresh=False))
        out.append("-----")
        out.append("--" + action(cli, "Убрать из списка (файлы оставить)", "rm", t["id"]))
        out.append("--" + action(cli, "Удалить вместе с файлами (в Корзину)", "rm", t["id"], "--files"))
        out.append(f"{fmt.bar(t['progress'])}  {int(t['progress'])}% · {t['state']}")
        if t["state"] == "качается":
            out.append(f"{fmt.size(t['done'])} из {fmt.size(t['size'])} · {fmt.speed(t['down_rate'])}")
            if t["eta_seconds"] is not None:
                out.append(f"Осталось {fmt.eta(t['eta_seconds'])}, готово к {fmt.finish_at(t['eta_seconds'])}")
            out.append(f"Пиров {t['peers']}, сидов {t['seeds']}")
        elif t["state"] == "раздаётся":
            out.append(f"{fmt.size(t['size'])} · отдаю {fmt.speed(t['up_rate'])} · рейтинг {t['ratio']}".replace(".", ","))
        if t["error"]:
            out.append(f"⚠︎ {_clean(t['error'])} | color=red")
        out.append("---")

    if iface:
        out.append(f"Сеть: напрямую ({iface}), мимо VPN | size=12")
        out.append(action(cli, "Пустить через VPN", "vpn", "on"))
    else:
        out.append("Сеть: через VPN | size=12")
        out.append(action(cli, "Пустить мимо VPN", "vpn", "off"))
    out.append("---")
    out.append(action(cli, "Добавить magnet из буфера обмена", "add-clipboard"))
    out.append(action(cli, "Пауза всех", "pause", "all"))
    out.append(action(cli, "Продолжить все", "resume", "all"))
    out.append(action(cli, "Открыть папку загрузок", "reveal", "--downloads", refresh=False))
    return "\n".join(out) + "\n"
