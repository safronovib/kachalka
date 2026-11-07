"""Этап 2: куда положить скачанное и как назвать.

Чистая логика без libtorrent: на вход название раздачи и список файлов, на выход план —
новая папка раздачи (save_path) и переименования файлов внутри неё. Применяет план
фоновая программа через move_storage/rename_file, поэтому раздача не прерывается.

Раскладка (папки настраиваются в config.json → "library"):
    Фильмы/Название (Год)/Название (Год).mkv
    Сериалы/Название/Сезон 2/…
    Музыка/…, Книги/…, Программы/…, Другое/…
"""

from __future__ import annotations

import posixpath
import re
from dataclasses import dataclass, field

VIDEO = {".mkv", ".mp4", ".avi", ".mov", ".m4v", ".ts", ".m2ts", ".wmv", ".webm", ".mpg", ".mpeg", ".vob", ".flv"}
AUDIO = {".mp3", ".flac", ".m4a", ".m4b", ".ogg", ".opus", ".wav", ".aac", ".ape", ".alac", ".wv", ".dsf", ".aiff"}
BOOK = {".pdf", ".epub", ".fb2", ".djvu", ".mobi", ".azw3", ".cbr", ".cbz", ".doc", ".docx", ".rtf"}
SOFT = {".dmg", ".pkg", ".iso", ".img", ".exe", ".msi", ".app", ".apk", ".deb", ".rpm"}

DEFAULT_LIBRARY = {
    "movie": "Фильмы",
    "series": "Сериалы",
    "music": "Музыка",
    "book": "Книги",
    "software": "Программы",
    "other": "Другое",
}
CATEGORY_NAMES = {"movie": "фильм", "series": "сериал", "music": "музыка", "book": "книга",
                  "software": "программа", "other": "другое"}


@dataclass
class Plan:
    category: str
    title: str
    year: int | None = None
    season: int | None = None
    episode: int | None = None
    save_path: str = ""
    renames: list[tuple[int, str, str]] = field(default_factory=list)  # (индекс, было, стало)
    note: str = ""

    def as_dict(self) -> dict:
        return {
            "category": self.category, "category_ru": CATEGORY_NAMES.get(self.category, self.category),
            "title": self.title, "year": self.year, "season": self.season, "episode": self.episode,
            "save_path": self.save_path,
            "renames": [{"index": i, "from": a, "to": b} for i, a, b in self.renames],
            "note": self.note,
        }


def _clean(name: str) -> str:
    """Имя, безопасное для файловой системы macOS."""
    name = re.sub(r'[/\\:*?"<>|\x00-\x1f]', " ", name)
    name = re.sub(r"\s+", " ", name).strip(" .")
    return name or "Без названия"


def _ext(path: str) -> str:
    return posixpath.splitext(path)[1].lower()


def _dominant(files: list[tuple[str, int]]) -> str:
    weight = {"video": 0, "music": 0, "book": 0, "software": 0, "other": 0}
    for path, size in files:
        if "/.pad/" in path or path.startswith(".pad/"):
            continue  # служебные файлы-заполнители BitTorrent v2
        e = _ext(path)
        kind = ("video" if e in VIDEO else "music" if e in AUDIO else "book" if e in BOOK
                else "software" if e in SOFT else "other")
        weight[kind] += max(size, 1)
    return max(weight, key=weight.get)


def _guess(text: str) -> dict:
    try:
        from guessit import guessit
        return dict(guessit(text))
    except Exception:
        return {}


def classify(name: str, files: list[tuple[str, int]], overrides: dict | None = None) -> Plan:
    """Определяет тип и название. overrides: category, title, year, season — подсказки от владельца или ИИ."""
    o = {k: v for k, v in (overrides or {}).items() if v not in (None, "")}
    kind = _dominant(files)
    g = _guess(name)

    file_seasons: set[int] = set()
    if o.get("category"):
        category = o["category"]
    elif kind == "video":
        episodes = 0
        for path, size in files:
            if _ext(path) in VIDEO and size > 50_000_000:
                fg = _guess(posixpath.basename(path))
                if fg.get("episode") is not None:
                    episodes += 1
                    if isinstance(fg.get("season"), int):
                        file_seasons.add(fg["season"])
        is_series = g.get("type") == "episode" or g.get("season") is not None or episodes >= 2
        category = "series" if is_series else "movie"
    else:
        category = kind
    if g.get("season") is None and len(file_seasons) == 1:
        g["season"] = next(iter(file_seasons))  # сезон не в названии раздачи, но у всех серий один

    title = o.get("title") or g.get("title") or posixpath.splitext(name)[0]
    year = o.get("year") or (g.get("year") if isinstance(g.get("year"), int) else None)
    season = o.get("season") if o.get("season") is not None else g.get("season")
    if isinstance(season, list):
        season = None  # несколько сезонов в одной раздаче — оставляем одну папку на сериал
    episode = g.get("episode") if isinstance(g.get("episode"), int) else None

    if category in ("music", "book", "software", "other") and not o.get("title"):
        title = name  # для не-видео название раздачи обычно уже человеческое
    return Plan(category=category, title=_clean(str(title)), year=year,
                season=int(season) if season is not None else None, episode=episode)


_EP_PATTERNS = [
    re.compile(r"^s?(\d{1,2})\s*[ex]\s*(\d{1,3})$", re.I),  # S02E04, 2x04, s2e4
]


def parse_episodes(spec: str) -> list[tuple[int | None, int]]:
    """«4,5», «4-6», «S02E04», «2x4-2x6» → [(сезон или None, серия), …]."""
    out: list[tuple[int | None, int]] = []
    for token in re.split(r"[,\s;]+", spec.strip()):
        if not token:
            continue
        lo, _, hi = token.partition("-")

        def one(t: str) -> tuple[int | None, int]:
            t = t.strip()
            if t.isdigit():
                return None, int(t)
            for pat in _EP_PATTERNS:
                m = pat.match(t)
                if m:
                    return int(m.group(1)), int(m.group(2))
            raise ValueError(f"не понял серию «{t}»: пиши 4, 4-6, S02E04 или 2x4")

        s1, e1 = one(lo)
        if not hi:
            out.append((s1, e1))
            continue
        s2, e2 = one(hi)
        if s2 is not None and s1 is not None and s2 != s1:
            raise ValueError("диапазон через несколько сезонов не поддерживается, перечисли по сезонам")
        for e in range(e1, e2 + 1):
            out.append((s1 if s1 is not None else s2, e))
    return out


def episode_of(path: str) -> tuple[int | None, int | None]:
    """(сезон, серия) по имени файла; (None, None), если это не серия."""
    if _ext(path) not in VIDEO:
        return None, None
    g = _guess(posixpath.basename(path))
    ep = g.get("episode")
    if isinstance(ep, list):
        ep = ep[0]
    season = g.get("season") if isinstance(g.get("season"), int) else None
    if not isinstance(ep, int):
        # «Серия 4», «04 серия», просто «04.mkv»
        m = re.search(r"(?:серия|series|ep)[ ._-]*(\d{1,3})|(\d{1,3})[ ._-]*серия|^(\d{1,3})[ ._-]", posixpath.basename(path), re.I)
        if not m:
            return season, None
        ep = int(next(x for x in m.groups() if x))
    return season, ep


def match_episodes(files: list[tuple[str, int]], wanted: list[tuple[int | None, int]]) -> list[int]:
    """Индексы файлов-серий, подходящих под запрос."""
    hits = []
    for i, (path, _) in enumerate(files):
        s, e = episode_of(path)
        if e is None:
            continue
        for ws, we in wanted:
            if we == e and (ws is None or s is None or ws == s):
                hits.append(i)
                break
    return hits


def plan(name: str, files: list[tuple[str, int]], current_save_path: str, library_root: str,
         library: dict | None = None, overrides: dict | None = None) -> Plan:
    """Полный план: куда переложить раздачу и какие файлы переименовать.

    files — пути внутри раздачи (у многофайловой раздачи первый компонент — корневая папка).
    """
    lib = {**DEFAULT_LIBRARY, **(library or {})}
    p = classify(name, files, overrides)
    base = posixpath.join(library_root, lib.get(p.category, lib["other"]))
    single = len(files) == 1 and "/" not in files[0][0]
    root = None if single else files[0][0].split("/", 1)[0]

    def rename_root(new_root: str) -> None:
        if root is None or new_root == root:
            return
        for i, (path, _) in enumerate(files):
            if path.split("/", 1)[0] == root:
                p.renames.append((i, path, new_root + path[len(root):]))

    if p.category == "movie":
        label = f"{p.title} ({p.year})" if p.year else p.title
        if single:
            p.save_path = posixpath.join(base, label)
            new = label + _ext(files[0][0])
            if new != files[0][0]:
                p.renames.append((0, files[0][0], new))
        else:
            p.save_path = base
            rename_root(label)
    elif p.category == "series":
        show = posixpath.join(base, p.title)
        season_dir = f"Сезон {p.season}" if p.season is not None else None
        if single:
            p.save_path = posixpath.join(show, season_dir) if season_dir else show
            if p.season is not None and p.episode is not None:
                new = f"{p.title} S{p.season:02d}E{p.episode:02d}{_ext(files[0][0])}"
                if new != files[0][0]:
                    p.renames.append((0, files[0][0], new))
        elif season_dir:
            p.save_path = show
            rename_root(season_dir)
        else:
            p.save_path = base
            rename_root(p.title)
    else:
        p.save_path = base

    if posixpath.normpath(p.save_path) == posixpath.normpath(current_save_path) and not p.renames:
        p.note = "уже на месте"
    return p
