"""Этап 3: учёт просмотра, режим «смотрю сериал» и чистка просмотренного.

Режим «смотрю сериал» (follow): держим скачанными текущую серию и `ahead` следующих.
Текущая — та, что начата, или следующая после последней досмотренной. Остальные
непросмотренные серии не качаются. Досмотренные серии через `keep_hours` уходят в Корзину.

Чистка (cleanup) для остальных раздач с видео только предлагает варианты: раздачи,
досмотренные больше `cleanup_watched_days` назад, и скачанные, но не открытые больше
`cleanup_unopened_days`. Удаление — только в Корзину и только по команде владельца
(`kachalka cleanup --apply` или пункт меню).
"""

from __future__ import annotations

import logging
import os
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import sorter, watch
from .store import now

log = logging.getLogger("kachalka")

FOLLOW_SECONDS = 60
CLEANUP_NOTIFY_SECONDS = 6 * 3600
VIDEO = sorter.VIDEO


def _ts(iso: str | None) -> datetime | None:
    return datetime.fromisoformat(iso) if iso else None


class Viewer:
    def __init__(self, engine) -> None:
        self.e = engine
        self.store = engine.store
        self._last_sample = 0.0
        self._last_follow = 0.0
        self._last_cleanup_note = 0.0
        self._notified_cleanup: set[str] = set()
        self._trashing: set[str] = set()

    # ---------- карта файлов библиотеки ----------

    def _video_files(self, h) -> list[tuple[int, str, int]]:
        """(индекс, полный путь, размер) видеофайлов раздачи."""
        st = h.status()
        if not st.has_metadata:
            return []
        out = []
        for i, (rel, size) in enumerate(self.e._files(h)):
            if os.path.splitext(rel)[1].lower() in VIDEO and "/.pad/" not in rel:
                out.append((i, os.path.realpath(os.path.join(st.save_path, rel)), size))
        return out

    def _record(self, ih: str, idx: int, path: str) -> dict:
        rec = self.store.watch_get(ih, idx)
        rec["path"] = path
        return rec

    def _ensure_duration(self, rec: dict) -> None:
        if not rec.get("duration") and os.path.exists(rec["path"]):
            rec["duration"] = watch.duration(rec["path"])

    def _save(self, rec: dict) -> None:
        st, _ = watch.state(rec)
        if st == "watched" and not rec.get("watched_at"):
            rec["watched_at"] = now()
        if st != "watched" and rec.get("manual") != 1:
            rec["watched_at"] = None
        self.store.watch_put(rec)

    # ---------- периодическая работа ----------

    def tick(self) -> None:
        t = time.time()
        if t - self._last_sample >= watch.WATCH_SAMPLE_SECONDS:
            elapsed = min(t - self._last_sample, 2 * watch.WATCH_SAMPLE_SECONDS) if self._last_sample else 0
            self._last_sample = t
            self.sample(elapsed)
        if t - self._last_follow >= FOLLOW_SECONDS:
            self._last_follow = t
            for ih in list(self.store.follows()):
                try:
                    self.apply_follow(ih)
                except Exception:
                    log.exception("follow %s", ih)
        if t - self._last_cleanup_note >= CLEANUP_NOTIFY_SECONDS:
            self._last_cleanup_note = t
            self._notify_cleanup()

    def sample(self, elapsed: float) -> None:
        """Сверяемся с VLC и открытыми файлами, копим время просмотра."""
        vlc = watch.vlc_positions()
        opened = watch.open_media_paths()
        for h in self.e.handles():
            ih = self.e.hash_of(h)
            for idx, path, _size in self._video_files(h):
                pos = vlc.get(path)
                is_open = path in opened
                if pos is None and not is_open:
                    continue
                rec = self._record(ih, idx, path)
                changed = False
                if pos is not None and pos != rec.get("vlc_pos"):
                    rec["vlc_pos"] = pos
                    changed = True
                if is_open:
                    rec["watched_seconds"] = (rec.get("watched_seconds") or 0) + elapsed
                    rec["last_seen"] = now()
                    changed = True
                if changed:
                    self._ensure_duration(rec)
                    self._save(rec)

    # ---------- состояние для людей и ИИ ----------

    def status(self, h) -> dict:
        ih = self.e.hash_of(h)
        recs = self.store.watch_for(ih)
        prog = h.file_progress() if h.status().has_metadata else []
        prios = h.get_file_priorities() if h.status().has_metadata else []
        files = []
        for idx, path, size in self._video_files(h):
            rec = recs.get(idx) or {}
            st, share = watch.state(rec)
            season, episode = sorter.episode_of(path)
            files.append({
                "index": idx, "name": os.path.basename(path), "season": season, "episode": episode,
                "state": st, "watched_share": round(share * 100),
                "downloaded": round(prog[idx] * 100 / size) if size and prog else 0,
                "selected": bool(prios[idx]) if prios else True,
                "exists": os.path.exists(path), "trashed": bool(rec.get("trashed_at")),
            })
        follow = self.store.follows().get(ih)
        return {"id": ih[:8], "name": h.status().name, "files": files, "follow": follow,
                "current": self._current(h, files) if files else None}

    @staticmethod
    def _episodes(files: list[dict]) -> list[dict]:
        eps = [f for f in files if f["episode"] is not None]
        return sorted(eps, key=lambda f: (f["season"] or 0, f["episode"]))

    def _current(self, h, files: list[dict], from_ep: str | None = None) -> dict | None:
        """Серия, на которой владелец сейчас: начатая или следующая после досмотренной."""
        eps = self._episodes(files)
        if not eps:
            return None
        last = None
        for k, f in enumerate(eps):
            if f["state"] in ("watched", "watching"):
                last = k
        if last is None:
            k = 0
            if from_ep:
                wanted = sorter.parse_episodes(from_ep)[:1]
                for j, f in enumerate(eps):
                    if wanted and f["episode"] == wanted[0][1] and (wanted[0][0] in (None, f["season"])):
                        k = j
                        break
            return {**eps[k], "pos": k}
        k = last if eps[last]["state"] == "watching" else last + 1
        if k >= len(eps):
            return {"done": True, "pos": k}
        return {**eps[k], "pos": k}

    # ---------- отметки вручную ----------

    def mark(self, h, episodes: str | None, files: list[int], watched: bool) -> list[str]:
        ih = self.e.hash_of(h)
        vids = self._video_files(h)
        if episodes:
            hits = set(sorter.match_episodes([(p, s) for _, p, s in vids], sorter.parse_episodes(episodes)))
            chosen = [vids[k] for k in hits]
        elif files:
            chosen = [v for v in vids if v[0] in files]
        else:
            chosen = vids  # весь фильм / вся раздача
        if not chosen:
            raise ValueError("не нашёл таких серий или файлов")
        for idx, path, _ in chosen:
            rec = self._record(ih, idx, path)
            rec["manual"] = 1 if watched else -1
            self._save(rec)
        if ih in self.store.follows():
            self.apply_follow(ih)
        return [os.path.basename(p) for _, p, _ in chosen]

    # ---------- режим «смотрю сериал» ----------

    def follow(self, h, ahead: int, keep_hours: float, cleanup: bool, from_ep: str | None) -> dict:
        ih = self.e.hash_of(h)
        if not h.status().has_metadata:
            raise ValueError("ещё нет метаданных, попробуй через минуту")
        if not self._episodes(self.status(h)["files"]):
            raise ValueError("в раздаче не нашёл серий")
        self.store.follow_set(ih, ahead, keep_hours, cleanup, from_ep)
        return self.apply_follow(ih)

    def unfollow(self, h) -> None:
        self.store.follow_del(self.e.hash_of(h))

    def apply_follow(self, ih: str) -> dict:
        f = self.store.follows().get(ih)
        hs = [h for h in self.e.handles() if self.e.hash_of(h) == ih]
        if not f or not hs:
            return {}
        h = hs[0]
        info = self.status(h)
        eps = self._episodes(info["files"])
        cur = self._current(h, info["files"], f.get("from_ep"))
        pos = cur["pos"] if cur else 0
        window = set(range(pos, pos + int(f["ahead"]) + 1))

        prios = list(h.get_file_priorities())
        new = list(prios)
        for k, ep in enumerate(eps):
            i = ep["index"]
            if k in window:
                new[i] = 7 if k == pos else 6
            elif ep["downloaded"] < 100:
                new[i] = 0                     # не качаем ни просмотренное, ни далёкое
        if new != prios:
            h.prioritize_files(new)  # пауза владельца при этом остаётся паузой
            self.e._save_resume(h, force=True)
            log.info("follow %s: window %s", info["name"], [eps[k]["name"] for k in sorted(window) if k < len(eps)])

        trash = []
        if f.get("cleanup"):
            limit = datetime.now(timezone.utc) - timedelta(hours=float(f["keep_hours"]))
            recs = self.store.watch_for(ih)
            for k, ep in enumerate(eps):
                if k >= pos or ep["state"] != "watched" or not ep["exists"] or ep["trashed"]:
                    continue
                wa = _ts((recs.get(ep["index"]) or {}).get("watched_at"))
                if wa and wa <= limit:
                    trash.append(ep["index"])
        if trash and ih not in self._trashing:
            self._trash_files(h, trash)
        nxt = [eps[k]["name"] for k in sorted(window) if k < len(eps)]
        return {"id": ih[:8], "name": info["name"], "current": cur, "ready": nxt, "trashing": len(trash)}

    def _trash_files(self, h, indices: list[int]) -> None:
        """Убирает отдельные файлы раздачи в Корзину, не трогая остальные."""
        ih = self.e.hash_of(h)
        self._trashing.add(ih)
        prios = list(h.get_file_priorities())
        for i in indices:
            prios[i] = 0
        h.prioritize_files(prios)
        h.flush_cache()
        st = h.status()
        files = self.e._files(h)
        paths = [(i, os.path.join(st.save_path, files[i][0])) for i in indices]

        def work() -> None:
            time.sleep(3)  # даём libtorrent дописать и закрыть файлы
            from .daemon import move_to_trash, notify
            done = []
            for i, path in paths:
                if move_to_trash(Path(path)):
                    rec = self._record(ih, i, path)
                    rec["trashed_at"] = now()
                    self.store.watch_put(rec)
                    done.append(os.path.basename(path))
            if done:
                # куски на стыке с удалёнными файлами больше не наши — перепроверяем раздачу
                h.force_recheck()
                self.store.event(ih, "trash_watched", ", ".join(done))
                log.info("trashed watched: %s", done)
                if self.e.cfg.get("notify", True):
                    notify("Качалка: убрала просмотренное", ", ".join(done)[:200])
            self._trashing.discard(ih)

        threading.Thread(target=work, daemon=True).start()

    # ---------- чистка ----------

    def cleanup_candidates(self, max_age: float = 0) -> list[dict]:
        """max_age > 0 — можно вернуть закэшированный список не старше max_age секунд."""
        cached = getattr(self, "_cand_cache", None)
        if max_age and cached and time.time() - cached[0] < max_age:
            return cached[1]
        res = self._cleanup_candidates()
        self._cand_cache = (time.time(), res)
        return res

    def _cleanup_candidates(self) -> list[dict]:
        watched_days = self.e.cfg.get("cleanup_watched_days")
        unopened_days = self.e.cfg.get("cleanup_unopened_days")
        t_now = datetime.now(timezone.utc)
        out = []
        for h in self.e.handles():
            ih = self.e.hash_of(h)
            st = h.status()
            vids = self._video_files(h)
            if not vids or not st.is_seeding and not st.is_finished:
                continue
            info = self.status(h)
            present = [f for f in info["files"] if f["exists"] and f["selected"]]
            if not present:
                continue
            recs = self.store.watch_for(ih)
            size = sum(s for i, _, s in vids if any(f["index"] == i for f in present))
            reason = None
            if watched_days and all(f["state"] == "watched" for f in present):
                last = max((_ts((recs.get(f["index"]) or {}).get("watched_at")) for f in present),
                           default=None, key=lambda d: d or datetime.min.replace(tzinfo=timezone.utc))
                if last and t_now - last >= timedelta(days=float(watched_days)):
                    reason = f"посмотрено {(t_now - last).days} дн. назад"
            elif unopened_days and all(f["state"] == "new" for f in present):
                done_at = _ts((self.store.torrent(ih) or {}).get("completed_at"))
                if done_at and t_now - done_at >= timedelta(days=float(unopened_days)):
                    opened = [watch.last_opened(p) for _, p, _ in vids]
                    opened = [d for d in opened if d and d > done_at]
                    if not opened:
                        reason = f"скачано {(t_now - done_at).days} дн. назад и ни разу не открыто"
            if reason:
                out.append({"id": ih[:8], "name": st.name, "size": size, "reason": reason})
        return out

    def _notify_cleanup(self) -> None:
        try:
            c = [x for x in self.cleanup_candidates() if x["id"] not in self._notified_cleanup]
        except Exception:
            log.exception("cleanup candidates")
            return
        if not c:
            return
        self._notified_cleanup |= {x["id"] for x in c}
        if self.e.cfg.get("notify", True):
            from .daemon import notify
            from .fmt import size
            notify("Качалка: можно освободить место",
                   f"{len(c)} раздач(и), {size(sum(x['size'] for x in c))}. Посмотреть: меню значка или скажи Claude.")
