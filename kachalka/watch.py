"""Этап 3: что владелец посмотрел.

Источники:
  • VLC — ~/Library/Preferences/org.videolan.vlc.plist, словарь recentlyPlayedMedia
    «file://… → секунда, на которой остановился». VLC хранит позицию и для досмотренных файлов.
  • Любой плеер — раз в WATCH_SAMPLE_SECONDS смотрим через lsof, какие файлы библиотеки
    открыты плеером, и копим время просмотра.
  • Spotlight — kMDItemLastUsedDate: когда файл последний раз открывали (для правила
    «не открывал N дней»).
  • Вручную — команда `kachalka watched`.
"""

from __future__ import annotations

import os
import plistlib
import shutil
import subprocess
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

VLC_PLIST = Path.home() / "Library/Preferences/org.videolan.vlc.plist"
PLAYERS = ["VLC", "IINA", "QuickTime", "Elmedia", "mpv", "Infuse", "Movist", "MPlayerX", "Plex"]
WATCH_SAMPLE_SECONDS = 15
WATCHED_SHARE = 0.9          # досмотрено, если позиция ≥ 90 % длительности…
CREDITS_SECONDS = 300        # …или до конца меньше 5 минут (титры)
SAMPLED_SHARE = 0.85         # для других плееров: накоплено ≥ 85 % длительности

_EXTRA_PATH = ["/usr/local/bin", "/opt/homebrew/bin", "/usr/bin", "/bin", "/usr/sbin"]


def _which(name: str) -> str | None:
    return shutil.which(name, path=os.pathsep.join([os.environ.get("PATH", "")] + _EXTRA_PATH))


# ---------- VLC ----------

_vlc_cache: tuple[float, dict[str, float]] = (0.0, {})


def vlc_positions() -> dict[str, float]:
    """Путь файла → секунда, на которой остановились в VLC."""
    global _vlc_cache
    try:
        mtime = VLC_PLIST.stat().st_mtime
    except OSError:
        return {}
    if mtime == _vlc_cache[0]:
        return _vlc_cache[1]
    try:
        data = plistlib.loads(VLC_PLIST.read_bytes())
    except Exception:
        return _vlc_cache[1]
    out = {}
    for url, pos in (data.get("recentlyPlayedMedia") or {}).items():
        if isinstance(url, str) and url.startswith("file://") and isinstance(pos, (int, float)):
            out[os.path.realpath(urllib.parse.unquote(url[len("file://"):]))] = float(pos)
    _vlc_cache = (mtime, out)
    return out


# ---------- открытые сейчас файлы ----------

def open_media_paths() -> set[str]:
    """Файлы, которые прямо сейчас открыты плеерами."""
    lsof = _which("lsof")
    if not lsof:
        return set()
    args = [lsof, "-nP", "-Fn"]
    for p in PLAYERS:
        args += ["-c", p]
    try:
        out = subprocess.run(args, capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.TimeoutExpired):
        return set()
    return {os.path.realpath(line[1:]) for line in out.splitlines() if line.startswith("n/")}


# ---------- длительность и дата открытия ----------

def duration(path: str) -> float | None:
    """Длительность видео в секундах: Spotlight, а если нет — ffprobe."""
    v = _mdls(path, "kMDItemDurationSeconds")
    if v:
        try:
            return float(v)
        except ValueError:
            pass
    ffprobe = _which("ffprobe")
    if not ffprobe:
        return None
    try:
        out = subprocess.run([ffprobe, "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", path],
                             capture_output=True, text=True, timeout=30).stdout.strip()
        return float(out) if out else None
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None


def last_opened(path: str) -> datetime | None:
    """Когда файл последний раз открывали через Finder/LaunchServices (Spotlight)."""
    v = _mdls(path, "kMDItemLastUsedDate")
    if not v:
        return None
    try:
        return datetime.strptime(v, "%Y-%m-%d %H:%M:%S %z").astimezone(timezone.utc)
    except ValueError:
        return None


def _mdls(path: str, attr: str) -> str | None:
    try:
        out = subprocess.run(["/usr/bin/mdls", "-raw", "-name", attr, path],
                             capture_output=True, text=True, timeout=10).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return None
    return None if not out or out == "(null)" else out


# ---------- вывод «посмотрено / смотрю / не смотрел» ----------

def state(rec: dict) -> tuple[str, float]:
    """rec — строка таблицы watch. → (состояние, доля просмотра 0..1).

    Состояния: watched (посмотрено), watching (начато), new (не смотрел).
    """
    if rec.get("manual") == 1:
        return "watched", 1.0
    if rec.get("manual") == -1:
        return "new", 0.0
    dur = rec.get("duration") or 0
    pos = rec.get("vlc_pos") or 0
    sampled = rec.get("watched_seconds") or 0
    share = 0.0
    if dur > 0:
        share = max(pos / dur, sampled / dur)
        if pos and (pos >= dur * WATCHED_SHARE or dur - pos <= CREDITS_SECONDS):
            return "watched", 1.0
        if sampled >= dur * SAMPLED_SHARE:
            return "watched", 1.0
    if pos > 30 or sampled > 60:
        return "watching", min(share, 0.99)
    return "new", 0.0
