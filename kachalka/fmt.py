"""Человеческие подписи: размеры, скорость, время."""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

MSK = ZoneInfo("Europe/Moscow")


def size(b: float) -> str:
    b = float(b or 0)
    for unit, k in (("ГБ", 1e9), ("МБ", 1e6), ("КБ", 1e3)):
        if b >= k:
            v = b / k
            return f"{v:.2f} {unit}".replace(".", ",") if v < 10 else f"{v:.1f} {unit}".replace(".", ",")
    return f"{int(b)} Б"


def speed(bps: float) -> str:
    return size(bps) + "/с" if bps else "0"


def eta(seconds: int | None) -> str:
    if seconds is None:
        return "—"
    m = (int(seconds) + 59) // 60
    if m <= 1:
        return "меньше минуты"
    if m < 60:
        return f"{m} мин"
    if m < 60 * 48:
        return f"{m // 60} ч {m % 60} мин"
    return f"{m // 1440} дн"


def finish_at(seconds: int | None) -> str:
    if seconds is None:
        return "—"
    t = datetime.now(MSK) + timedelta(seconds=int(seconds))
    if t.date() == datetime.now(MSK).date():
        return t.strftime("%H:%M") + " МСК"
    return t.strftime("%d.%m %H:%M") + " МСК"


def bar(percent: float, width: int = 20) -> str:
    n = int(percent * width / 100 + 0.5)
    return "▰" * n + "▱" * (width - n)
