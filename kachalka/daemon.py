"""Фоновая программа: держит сессию libtorrent и принимает команды через Unix-сокет.

Протокол: клиент присылает одну строку JSON {"cmd": "...", ...}, получает одну строку JSON
{"ok": true, ...} или {"ok": false, "error": "..."}. Сокет доступен только владельцу (0600),
поэтому браузер и чужие пользователи до него не дотянутся.
"""

from __future__ import annotations

import json
import logging
import os
import signal
import socket
import socketserver
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

import libtorrent as lt

from . import __version__, net, sorter
from .config import (
    LOG_PATH,
    PID_PATH,
    RESUME_DIR,
    SOCKET,
    DB_PATH,
    load_config,
    save_config,
)
from .store import Store

log = logging.getLogger("kachalka")

STATE_NAMES = {
    "checking_files": "проверка",
    "downloading_metadata": "ищу метаданные",
    "downloading": "качается",
    "finished": "скачано",
    "seeding": "раздаётся",
    "allocating": "выделяю место",
    "checking_resume_data": "проверка",
}

PERIODIC_SECONDS = 30


# ---------- мелкие помощники ----------

def hash_of(obj) -> str:
    """Шестнадцатеричный info-hash для handle или status (v1, а для чистого v2 — усечённый v2)."""
    try:
        ih = obj.info_hashes() if callable(getattr(obj, "info_hashes", None)) else obj.info_hashes
        return str(ih.get_best())
    except AttributeError:
        return str(obj.info_hash())


def is_paused(st) -> bool:
    return bool(st.flags & lt.torrent_flags.paused)


def notify(title: str, text: str) -> None:
    script = 'on run argv\ndisplay notification (item 2 of argv) with title (item 1 of argv)\nend run'
    try:
        subprocess.Popen(["osascript", "-e", script, title, text],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        pass


def move_to_trash(path: Path) -> bool:
    if not path.exists():
        return False
    res = subprocess.run(["/usr/bin/trash", str(path)], capture_output=True, text=True)
    if res.returncode != 0:
        log.warning("trash failed for %s: %s", path, res.stderr.strip())
    return res.returncode == 0


# ---------- ядро ----------

class Engine:
    def __init__(self) -> None:
        self.cfg = load_config()
        self.store = Store(DB_PATH)
        self.lock = threading.RLock()
        self.stopping = threading.Event()
        self.pending_trash: dict[str, Path] = {}   # hash -> путь, который надо унести в корзину после удаления
        self.pending_flush: dict[str, tuple] = {}  # hash -> (handle, когда начали), ждём сброса буфера
        self.pending_moves: dict[str, dict] = {}   # hash -> идущее перемещение (раскладка или откат)
        self.manual_resume: set[str] = set()        # раздачи, которые владелец сам возобновил после лимита
        self.outstanding_resume = 0
        self.started_at = time.time()
        self.counters: dict[str, int] = {}          # последние счётчики libtorrent (session_stats)
        self._metric_index = {m.name: m.value_index for m in lt.session_stats_metrics()}
        self.net_errors: list[str] = []             # последние сетевые ошибки для diag

        self.ses = lt.session({
            **self._net_settings(),
            "alert_mask": lt.alert_category.status | lt.alert_category.error | lt.alert_category.storage,
            "enable_dht": True,
            "dht_bootstrap_nodes": "dht.libtorrent.org:25401,router.bittorrent.com:6881,"
                                   "router.utorrent.com:6881,dht.transmissionbt.com:6881",
            "enable_lsd": True,
            "enable_upnp": True,
            "enable_natpmp": True,
            "user_agent": f"Kachalka/{__version__} libtorrent/{lt.__version__}",
            "download_rate_limit": int(self.cfg["download_limit_kbps"]) * 1024,
            "upload_rate_limit": int(self.cfg["upload_limit_kbps"]) * 1024,
        })
        self._load_resume()

    def _net_settings(self) -> dict:
        """Через какой интерфейс идёт торрент-трафик. Пусто = как вся система (обычно через VPN)."""
        port = int(self.cfg["listen_port"])
        iface = net.resolve(self.cfg.get("bind_interface"))
        self.active_interface = iface
        if iface:
            return {"listen_interfaces": f"{iface}:{port}", "outgoing_interfaces": iface}
        return {"listen_interfaces": f"0.0.0.0:{port},[::]:{port}", "outgoing_interfaces": ""}

    # --- восстановление после перезапуска ---

    def _load_resume(self) -> None:
        for f in sorted(RESUME_DIR.glob("*.fastresume")):
            try:
                atp = lt.read_resume_data(f.read_bytes())
                self.ses.async_add_torrent(atp)
            except Exception as e:  # битый файл не должен ронять всю программу
                log.error("resume %s: %s", f.name, e)

    def _save_resume(self, h, force: bool = False) -> None:
        if not h.is_valid():
            return
        if force or h.need_save_resume_data():
            h.save_resume_data(lt.save_resume_flags_t.save_info_dict)
            self.outstanding_resume += 1

    # --- поиск раздач ---

    def handles(self) -> list:
        return [h for h in self.ses.get_torrents() if h.is_valid()]

    def find(self, ref: str) -> list:
        ref = (ref or "").strip().lower()
        hs = self.handles()
        if ref == "all":
            return hs
        by_hash = [h for h in hs if hash_of(h).startswith(ref)]
        if by_hash:
            return by_hash
        by_name = [h for h in hs if ref and ref in h.status().name.lower()]
        if len(by_name) == 1:
            return by_name
        if len(by_name) > 1:
            raise ValueError(f"под «{ref}» подходит несколько раздач, укажи id")
        raise ValueError(f"раздача «{ref}» не найдена")

    # --- команды ---

    def add(self, source: str, save_path: str | None = None) -> dict:
        save_path = os.path.expanduser(save_path or self.cfg["download_dir"])
        Path(save_path).mkdir(parents=True, exist_ok=True)

        if source.startswith("magnet:"):
            atp = lt.parse_magnet_uri(source)
        else:
            if source.startswith(("http://", "https://")):
                source_file = self._download_torrent_file(source)
            else:
                source_file = os.path.expanduser(source)
            if not os.path.isfile(source_file):
                raise ValueError(f"файл не найден: {source}")
            atp = lt.add_torrent_params()
            atp.ti = lt.torrent_info(source_file)
        atp.save_path = save_path

        with self.lock:
            h = self.ses.add_torrent(atp)
        ih = hash_of(h)
        st = h.status()
        size = st.total_wanted if st.has_metadata else 0
        self.store.added(ih, st.name or ih, source, save_path, size)
        self._save_resume(h, force=True)
        log.info("added %s %s", ih, st.name)
        return {"id": ih[:8], "hash": ih, "name": st.name or "(жду метаданные)", "save_path": save_path}

    # --- этап 2: файлы, выбор серий, раскладка по папкам ---

    @staticmethod
    def _files(h) -> list[tuple[str, int]]:
        fs = h.torrent_file().files()
        return [(fs.file_path(i).replace(os.sep, "/"), fs.file_size(i)) for i in range(fs.num_files())]

    def _one(self, ref: str):
        hs = self.find(ref)
        if len(hs) != 1:
            raise ValueError("укажи одну раздачу (id или часть названия)")
        return hs[0]

    def files(self, ref: str) -> dict:
        h = self._one(ref)
        st = h.status()
        if not st.has_metadata:
            return {"id": hash_of(h)[:8], "name": st.name, "files": [], "note": "ещё жду метаданные"}
        files = self._files(h)
        prog = h.file_progress()
        prio = h.get_file_priorities()
        out = []
        for i, (path, size) in enumerate(files):
            if "/.pad/" in path:
                continue
            season, episode = sorter.episode_of(path)
            out.append({
                "index": i, "path": path, "size": size,
                "progress": round(prog[i] * 100 / size, 1) if size else 100.0,
                "selected": prio[i] > 0, "season": season, "episode": episode,
            })
        return {"id": hash_of(h)[:8], "name": st.name, "save_path": st.save_path, "files": out}

    def select(self, ref: str, spec: dict) -> dict:
        h = self._one(ref)
        if not h.status().has_metadata:
            self.store.pending_select_set(hash_of(h), spec)
            return {"pending": True, "note": "выбор применится, когда придут метаданные"}
        return self._apply_select(h, spec)

    def _apply_select(self, h, spec: dict) -> dict:
        """spec: {"mode": only|add|remove|all, "episodes": "4,5"} или {"files": [индексы]}."""
        files = self._files(h)
        mode = spec.get("mode", "only")
        prios = list(h.get_file_priorities())
        if mode == "all":
            wanted = list(range(len(files)))
        elif spec.get("episodes"):
            wanted = sorter.match_episodes(files, sorter.parse_episodes(str(spec["episodes"])))
            if not wanted:
                raise ValueError(f"в раздаче не нашёл серии «{spec['episodes']}». Посмотри список: kachalka files")
        else:
            wanted = [int(i) for i in spec.get("files", []) if 0 <= int(i) < len(files)]
            if not wanted:
                raise ValueError("не указаны файлы")
        if mode in ("only", "all"):
            prios = [0] * len(files)
        for i in wanted:
            prios[i] = 0 if mode == "remove" else 4
        h.prioritize_files(prios)
        self._save_resume(h, force=True)
        chosen = [files[i][0].split("/")[-1] for i, p in enumerate(prios) if p > 0 and "/.pad/" not in files[i][0]]
        self.store.event(hash_of(h), "select", json.dumps(spec, ensure_ascii=False))
        return {"selected": chosen, "count": len(chosen)}

    def _library(self) -> tuple[str, dict]:
        root = os.path.expanduser(self.cfg.get("library_dir") or self.cfg["download_dir"])
        return root, {**sorter.DEFAULT_LIBRARY, **(self.cfg.get("library") or {})}

    def sort(self, ref: str, dry_run: bool = False, overrides: dict | None = None) -> dict:
        results = [self._sort_handle(h, dry_run, overrides) for h in self.find(ref)]
        return {"results": results}

    def _sort_handle(self, h, dry_run: bool = False, overrides: dict | None = None) -> dict:
        st = h.status()
        ih = hash_of(h)
        if not st.has_metadata:
            return {"id": ih[:8], "name": st.name, "skipped": "ещё нет метаданных"}
        if ih in self.pending_moves:
            return {"id": ih[:8], "name": st.name, "skipped": "уже перемещается"}
        root, lib = self._library()
        p = sorter.plan(st.name, self._files(h), st.save_path, root, lib, overrides)
        res = {"id": ih[:8], "name": st.name, **p.as_dict(), "from": st.save_path}
        if dry_run or p.note:
            return res
        move_id = self.store.move_start(ih, st.name, p.category, p.title, st.save_path, p.save_path,
                                        [list(r) for r in p.renames])
        res["move_id"] = move_id
        self._start_move(h, move_id, st.save_path, p.save_path, list(p.renames), undo=False)
        log.info("sort #%s %s → %s", move_id, st.name, p.save_path)
        return res

    def _start_move(self, h, move_id: int, src: str, dst: str, renames: list, undo: bool) -> None:
        """renames — тройки (индекс, имя при раскладке «было», «стало») из журнала.
        При раскладке файлы получают «стало», при откате — обратно «было»."""
        ih = hash_of(h)
        for i, old, new in renames:
            h.rename_file(i, old if undo else new)
        self.pending_moves[ih] = {"id": move_id, "from": src, "to": dst, "renames": renames, "undo": undo}
        if os.path.normpath(src) == os.path.normpath(dst):
            self._move_done(h, ih)
            return
        Path(dst).mkdir(parents=True, exist_ok=True)
        h.move_storage(dst, lt.move_flags_t.fail_if_exist)

    def _move_done(self, h, ih: str) -> None:
        m = self.pending_moves.pop(ih, None)
        if not m:
            return
        self.store.move_set(m["id"], "undone" if m["undo"] else "done")
        self.store.set_save_path(ih, m["to"])
        self._save_resume(h, force=True)
        # подчищаем опустевшие папки там, откуда уехали файлы
        old_paths = {new if m["undo"] else old for _i, old, new in m["renames"] or []}
        old_paths |= {p for p, _ in self._files(h)}  # у непереименованных файлов путь не менялся
        for rel in old_paths:
            self._prune(Path(m["from"]) / rel, base=m["from"])
        self._prune(Path(m["from"]) / "_")  # сама старая папка (например, «Фильмы/Название (Год)» после отката)

    def _move_failed(self, h, ih: str, error: str) -> None:
        m = self.pending_moves.pop(ih, None)
        if not m:
            return
        for i, old, new in m["renames"]:  # возвращаем имена, раз переезд не удался
            h.rename_file(i, new if m["undo"] else old)
        self.store.move_set(m["id"], "done" if m["undo"] else "failed", error)
        log.error("move #%s failed: %s", m["id"], error)

    def _prune(self, path: Path, base: str | None = None) -> None:
        """Удаляет опустевшие папки вверх от файла. Работает только внутри библиотеки, папки
        загрузок или (строго ниже) base — старой папки раздачи. Корни и папки категорий не трогает."""
        root, lib = self._library()
        downloads = os.path.expanduser(self.cfg["download_dir"])
        protected = {os.path.normpath(p) for p in
                     [root, downloads] + [os.path.join(root, v) for v in lib.values()] + ([base] if base else [])}
        allowed = [os.path.normpath(root) + os.sep, os.path.normpath(downloads) + os.sep]
        if base:
            allowed.append(os.path.normpath(base) + os.sep)
        d = path.parent
        while any(str(d).startswith(a) for a in allowed) and os.path.normpath(str(d)) not in protected:
            try:
                entries = [e for e in os.listdir(d) if e != ".DS_Store"]
                if entries:
                    break
                (d / ".DS_Store").unlink(missing_ok=True)
                d.rmdir()
            except OSError:
                break
            d = d.parent

    def undo(self, ref: str) -> dict:
        if ref.isdigit():
            m = self.store.move_get(int(ref))
            if not m:
                raise ValueError(f"нет перемещения #{ref}")
            h = self._one(m["info_hash"])
        else:
            h = self._one(ref)
            m = self.store.last_move(hash_of(h))
            if not m:
                raise ValueError("эту раздачу ещё не раскладывали")
        if m["status"] != "done":
            raise ValueError(f"перемещение #{m['id']} в состоянии «{m['status']}», откатывать нечего")
        ih = hash_of(h)
        if ih in self.pending_moves:
            raise ValueError("раздача сейчас перемещается, попробуй через минуту")
        self.store.move_set(m["id"], "undoing")
        self._start_move(h, m["id"], h.status().save_path, m["from_path"],
                         [tuple(r) for r in m["renames"]], undo=True)
        return {"move_id": m["id"], "name": m["name"], "back_to": m["from_path"]}

    def moves(self, limit: int = 30) -> dict:
        return {"moves": self.store.moves(limit)}

    @staticmethod
    def _download_torrent_file(url: str) -> str:
        req = urllib.request.Request(url, headers={"User-Agent": f"Kachalka/{__version__}"})
        with urllib.request.urlopen(req, timeout=30) as r:
            data = r.read(20 * 1024 * 1024)
        fd, path = tempfile.mkstemp(suffix=".torrent")
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        return path

    def describe(self, h) -> dict:
        st = h.status()
        ih = hash_of(h)
        size, done = st.total_wanted, st.total_wanted_done
        selected_files = None
        if st.has_metadata:
            prios = h.get_file_priorities()
            if 0 in prios:  # выбрана часть файлов: считаем по ним, libtorrent тут бывает неточен
                fs = h.torrent_file().files()
                prog = h.file_progress()
                idx = [i for i, p in enumerate(prios) if p > 0]
                size = sum(fs.file_size(i) for i in idx)
                done = sum(prog[i] for i in idx)
                selected_files = len(idx)
        left = max(size - done, 0)
        eta = int(left / st.download_rate) if st.download_rate > 0 and left else None
        state = str(st.state).split(".")[-1]
        paused = is_paused(st)
        downloaded = st.all_time_download or st.total_wanted_done
        return {
            "id": ih[:8],
            "hash": ih,
            "name": st.name or ih,
            "state": "пауза" if paused else STATE_NAMES.get(state, state),
            "paused": paused,
            "progress": round(done * 100 / size, 1) if size else round(st.progress * 100, 1),
            "size": size,
            "done": done,
            "selected_files": selected_files,
            "down_rate": st.download_rate,
            "up_rate": st.upload_rate,
            "eta_seconds": eta,
            "peers": st.num_peers,
            "seeds": st.num_seeds,
            "ratio": round(st.all_time_upload / downloaded, 2) if downloaded else 0.0,
            "save_path": st.save_path,
            "error": st.errc.message() if st.errc.value() else "",
        }

    def list(self) -> list[dict]:
        return [self.describe(h) for h in self.handles()]

    def pause(self, ref: str) -> list[str]:
        names = []
        for h in self.find(ref):
            h.unset_flags(lt.torrent_flags.auto_managed)
            h.pause()
            self.manual_resume.discard(hash_of(h))
            names.append(h.status().name)
        return names

    def resume(self, ref: str) -> list[str]:
        names = []
        for h in self.find(ref):
            h.set_flags(lt.torrent_flags.auto_managed)
            h.resume()
            st = h.status()
            if st.is_seeding:
                self.manual_resume.add(hash_of(h))
            names.append(st.name)
        return names

    def remove(self, ref: str, trash_files: bool = False) -> list[str]:
        names = []
        for h in self.find(ref):
            st = h.status()
            ih = hash_of(h)
            (RESUME_DIR / f"{ih}.fastresume").unlink(missing_ok=True)
            names.append(st.name)
            if trash_files and st.name:
                # Сначала останавливаем раздачу и дожидаемся, пока libtorrent допишет буфер на диск
                # (cache_flushed_alert). Иначе поздняя запись создаст файл заново после Корзины.
                self.pending_trash[ih] = Path(st.save_path) / st.name
                self.pending_flush[ih] = (h, time.time())
                h.unset_flags(lt.torrent_flags.auto_managed)
                h.pause()
                h.flush_cache()
            else:
                self.store.removed(ih, trashed=False)
                self.ses.remove_torrent(h)
        return names

    def _finish_removal(self, ih: str) -> None:
        entry = self.pending_flush.pop(ih, None)
        if entry and entry[0].is_valid():
            self.ses.remove_torrent(entry[0])

    def set_limits(self, down_kbps: int | None, up_kbps: int | None) -> dict:
        if down_kbps is not None:
            self.cfg["download_limit_kbps"] = int(down_kbps)
        if up_kbps is not None:
            self.cfg["upload_limit_kbps"] = int(up_kbps)
        save_config(self.cfg)
        self.ses.apply_settings({
            "download_rate_limit": int(self.cfg["download_limit_kbps"]) * 1024,
            "upload_rate_limit": int(self.cfg["upload_limit_kbps"]) * 1024,
        })
        return {"download_limit_kbps": self.cfg["download_limit_kbps"], "upload_limit_kbps": self.cfg["upload_limit_kbps"]}

    def diag(self) -> dict:
        """Сетевая диагностика: DHT, трекеры, пиры, последние ошибки."""
        torrents = []
        for h in self.handles():
            st = h.status()
            trackers = []
            for t in h.trackers():
                ends = t.get("endpoints") or []
                msgs = [e.get("message", "") for e in ends if e.get("message")]
                fails = max([e.get("fails", 0) for e in ends] or [0])
                trackers.append({"url": t["url"], "fails": fails, "message": "; ".join(msgs)[:200]})
            torrents.append({
                "id": hash_of(h)[:8], "name": st.name, "has_metadata": st.has_metadata,
                "peers": st.num_peers, "seeds": st.num_seeds, "connect_candidates": st.connect_candidates,
                "trackers": trackers,
            })
        return {
            "counters": self.counters,
            "listen_port": self.ses.listen_port(),
            "is_listening": self.ses.is_listening(),
            "bootstrap": self.ses.get_settings().get("dht_bootstrap_nodes"),
            "net_errors": self.net_errors,
            "torrents": torrents,
        }

    def info(self) -> dict:
        s = self.ses.status()
        dht = self.counters.get("dht.dht_nodes", s.dht_nodes)
        return {
            "version": __version__,
            "libtorrent": lt.__version__,
            "pid": os.getpid(),
            "uptime_seconds": int(time.time() - self.started_at),
            "download_dir": self.cfg["download_dir"],
            "listen_port": self.ses.listen_port(),
            "bind_interface": self.cfg.get("bind_interface") or "",
            "active_interface": self.active_interface,
            "dht_nodes": dht,
            "down_rate": s.payload_download_rate,
            "up_rate": s.payload_upload_rate,
            "torrents": len(self.handles()),
            "config": self.cfg,
        }

    # --- обработка событий libtorrent ---

    def handle_alert(self, a) -> None:
        if isinstance(a, lt.save_resume_data_alert):
            self.outstanding_resume = max(self.outstanding_resume - 1, 0)
            ih = hash_of(a.handle)
            data = lt.write_resume_data_buf(a.params)
            tmp = RESUME_DIR / f"{ih}.tmp"
            tmp.write_bytes(data)
            tmp.replace(RESUME_DIR / f"{ih}.fastresume")
        elif isinstance(a, lt.save_resume_data_failed_alert):
            self.outstanding_resume = max(self.outstanding_resume - 1, 0)
        elif isinstance(a, lt.metadata_received_alert):
            h = a.handle
            st = h.status()
            self.store.update_meta(hash_of(h), st.name, st.total_wanted)
            spec = self.store.pending_select_pop(hash_of(h))
            if spec:
                try:
                    self._apply_select(h, spec)
                except ValueError as e:
                    log.error("select after metadata: %s", e)
                    notify("Качалка: не нашёл серии", f"{st.name}: {e}")
            self._save_resume(h, force=True)
        elif isinstance(a, lt.torrent_finished_alert):
            h = a.handle
            st = h.status()
            ih = hash_of(h)
            self._save_resume(h, force=True)
            if self.store.completed(ih):
                log.info("finished %s %s", ih, st.name)
                where = ""
                if self.cfg.get("auto_sort", True):
                    try:
                        r = self._sort_handle(h)
                        if r.get("move_id"):
                            where = f" → {r['category_ru']}: {r['title']}"
                    except Exception:
                        log.exception("auto sort")
                if self.cfg.get("notify", True):
                    notify("Качалка: скачано", st.name + where)
        elif isinstance(a, lt.storage_moved_alert):
            self._move_done(a.handle, hash_of(a.handle))
        elif isinstance(a, lt.storage_moved_failed_alert):
            self._move_failed(a.handle, hash_of(a.handle), a.message())
        elif isinstance(a, lt.cache_flushed_alert):
            ih = hash_of(a.handle)
            if ih in self.pending_flush:
                self._finish_removal(ih)
        elif isinstance(a, lt.torrent_removed_alert):
            ih = str(a.info_hashes.get_best()) if hasattr(a, "info_hashes") else str(a.info_hash)
            path = self.pending_trash.pop(ih, None)
            if path is not None:
                # даём libtorrent закрыть файлы, потом уносим в корзину
                threading.Timer(1.5, self._trash_later, args=(ih, path)).start()
        elif isinstance(a, (lt.torrent_error_alert, lt.file_error_alert)):
            log.error("%s", a.message())
            try:
                self.store.event(hash_of(a.handle), "error", a.message())
            except Exception:
                pass
        elif isinstance(a, lt.session_stats_alert):
            values = a.values  # в Python-обёртке это уже словарь «имя → значение»
            if not isinstance(values, dict):
                values = {name: values[i] for name, i in self._metric_index.items()}
            self.counters = {k: v for k, v in values.items()
                             if k.startswith(("dht.", "net.", "peer.", "ses.num_")) and v}
        elif isinstance(a, (lt.tracker_error_alert, lt.listen_failed_alert,
                            getattr(lt, "udp_error_alert", lt.listen_failed_alert))):
            self.net_errors = (self.net_errors + [f"{time.strftime('%H:%M:%S')} {a.message()}"])[-20:]
        elif isinstance(a, lt.add_torrent_alert) and a.error.value():
            log.error("restore failed: %s", a.message())

    def _trash_later(self, ih: str, path: Path) -> None:
        ok = move_to_trash(path)
        # Контроль: если запоздалая запись всё же создала файл заново, это мусор — тоже в Корзину
        time.sleep(3)
        if ok and path.exists():
            log.warning("file reappeared after trash, trashing again: %s", path)
            move_to_trash(path)
        self.store.removed(ih, trashed=ok)
        log.info("trashed=%s %s", ok, path)

    def periodic(self) -> None:
        # VPN переподключился (сменился utunN) или включился/выключился — перепривязываемся
        if net.resolve(self.cfg.get("bind_interface")) != self.active_interface:
            old = self.active_interface
            self.ses.apply_settings(self._net_settings())
            log.info("interface %s -> %s", old or "system", self.active_interface or "system")
        limit = self.cfg.get("stop_seeding_at_ratio")
        for h in self.handles():
            self._save_resume(h)
            if not limit:
                continue
            st = h.status()
            ih = hash_of(h)
            if st.is_seeding and not is_paused(st) and ih not in self.manual_resume:
                downloaded = st.all_time_download or st.total_wanted
                if downloaded and st.all_time_upload / downloaded >= float(limit):
                    h.unset_flags(lt.torrent_flags.auto_managed)
                    h.pause()
                    self.store.event(ih, "seed_limit", f"ratio {st.all_time_upload / downloaded:.2f}")
                    log.info("seed limit reached %s", st.name)

    def run(self) -> None:
        last = 0.0
        while not self.stopping.is_set():
            self.ses.wait_for_alert(500)
            for a in self.ses.pop_alerts():
                try:
                    self.handle_alert(a)
                except Exception:
                    log.exception("alert %s", type(a).__name__)
            # Если сброс буфера не подтвердился за 10 с, удаляем раздачу всё равно
            for ih, (_, since) in list(self.pending_flush.items()):
                if time.time() - since > 10:
                    self._finish_removal(ih)
            if time.time() - getattr(self, "_last_stats", 0) > 5:
                self._last_stats = time.time()
                self.ses.post_session_stats()
            if time.time() - last > PERIODIC_SECONDS:
                last = time.time()
                try:
                    self.periodic()
                except Exception:
                    log.exception("periodic")
        self._shutdown()

    def _shutdown(self) -> None:
        log.info("shutting down")
        self.ses.pause()
        for h in self.handles():
            self._save_resume(h, force=True)
        deadline = time.time() + 10
        while self.outstanding_resume > 0 and time.time() < deadline:
            self.ses.wait_for_alert(500)
            for a in self.ses.pop_alerts():
                self.handle_alert(a)

    # --- диспетчер команд ---

    def dispatch(self, req: dict) -> dict:
        cmd = req.get("cmd")
        if cmd == "ping":
            return {"pong": True}
        if cmd == "diag":
            return self.diag()
        if cmd == "info":
            return self.info()
        if cmd == "add":
            res = self.add(req["source"], req.get("save_path"))
            if req.get("episodes") or req.get("files"):
                res["select"] = self.select(res["hash"], {"mode": "only", "episodes": req.get("episodes"),
                                                          "files": req.get("files") or []})
            return res
        if cmd == "files":
            return self.files(req["ref"])
        if cmd == "select":
            return self.select(req["ref"], {"mode": req.get("mode", "only"), "episodes": req.get("episodes"),
                                            "files": req.get("files") or []})
        if cmd == "sort":
            return self.sort(req["ref"], bool(req.get("dry_run")), req.get("overrides"))
        if cmd == "undo":
            return self.undo(str(req["ref"]))
        if cmd == "moves":
            return self.moves(int(req.get("limit", 30)))
        if cmd == "list":
            return {"torrents": self.list()}
        if cmd == "pause":
            return {"paused": self.pause(req["ref"])}
        if cmd == "resume":
            return {"resumed": self.resume(req["ref"])}
        if cmd == "remove":
            return {"removed": self.remove(req["ref"], bool(req.get("trash_files")))}
        if cmd == "history":
            return {"history": self.store.history(int(req.get("limit", 50)))}
        if cmd == "limit":
            return self.set_limits(req.get("down_kbps"), req.get("up_kbps"))
        if cmd == "set":
            key, value = req["key"], req["value"]
            if key not in self.cfg:
                raise ValueError(f"нет такой настройки: {key}")
            self.cfg[key] = value
            save_config(self.cfg)
            if key in ("bind_interface", "listen_port"):
                self.ses.apply_settings(self._net_settings())  # сокеты переоткрываются сразу
            return {"config": self.cfg}
        if cmd == "shutdown":
            self.stopping.set()
            return {"stopping": True}
        raise ValueError(f"неизвестная команда: {cmd}")


# ---------- сокет ----------

class _Handler(socketserver.StreamRequestHandler):
    engine: Engine

    def handle(self) -> None:
        line = self.rfile.readline(1024 * 1024)
        try:
            req = json.loads(line or b"{}")
            resp = {"ok": True, **self.engine.dispatch(req)}
        except (KeyError, ValueError, RuntimeError) as e:
            resp = {"ok": False, "error": str(e)}
        except Exception as e:
            log.exception("command failed")
            resp = {"ok": False, "error": f"{type(e).__name__}: {e}"}
        try:
            self.wfile.write(json.dumps(resp, ensure_ascii=False).encode() + b"\n")
        except BrokenPipeError:
            pass  # клиент (например, SwiftBar по таймауту) уже ушёл


class _Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True


def _socket_alive() -> bool:
    if not SOCKET.exists():
        return False
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        s.connect(str(SOCKET))
        return True
    except OSError:
        return False
    finally:
        s.close()


def main() -> int:
    load_config()
    logging.basicConfig(
        filename=str(LOG_PATH), level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    if _socket_alive():
        print("Качалка уже запущена.", file=sys.stderr)
        return 1
    SOCKET.unlink(missing_ok=True)

    engine = Engine()
    _Handler.engine = engine
    old_umask = os.umask(0o077)
    server = _Server(str(SOCKET), _Handler)
    os.umask(old_umask)
    os.chmod(SOCKET, 0o600)
    PID_PATH.write_text(str(os.getpid()))

    def stop(*_):
        engine.stopping.set()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    threading.Thread(target=server.serve_forever, name="socket", daemon=True).start()
    log.info("started pid=%s port=%s", os.getpid(), engine.cfg["listen_port"])
    try:
        engine.run()
    finally:
        server.shutdown()
        server.server_close()
        SOCKET.unlink(missing_ok=True)
        PID_PATH.unlink(missing_ok=True)
        log.info("stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
