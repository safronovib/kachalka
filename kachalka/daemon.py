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

from . import __version__
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
        self.manual_resume: set[str] = set()        # раздачи, которые владелец сам возобновил после лимита
        self.outstanding_resume = 0
        self.started_at = time.time()

        port = int(self.cfg["listen_port"])
        self.ses = lt.session({
            "listen_interfaces": f"0.0.0.0:{port},[::]:{port}",
            "alert_mask": lt.alert_category.status | lt.alert_category.error | lt.alert_category.storage,
            "enable_dht": True,
            "enable_lsd": True,
            "enable_upnp": True,
            "enable_natpmp": True,
            "user_agent": f"Kachalka/{__version__} libtorrent/{lt.__version__}",
            "download_rate_limit": int(self.cfg["download_limit_kbps"]) * 1024,
            "upload_rate_limit": int(self.cfg["upload_limit_kbps"]) * 1024,
        })
        self._load_resume()

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
        left = max(st.total_wanted - st.total_wanted_done, 0)
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
            "progress": round(st.progress * 100, 1),
            "size": st.total_wanted,
            "done": st.total_wanted_done,
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
            if trash_files and st.name:
                self.pending_trash[ih] = Path(st.save_path) / st.name
            elif not trash_files:
                self.store.removed(ih, trashed=False)
            self.ses.remove_torrent(h)
            (RESUME_DIR / f"{ih}.fastresume").unlink(missing_ok=True)
            names.append(st.name)
        return names

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

    def info(self) -> dict:
        s = self.ses.status()
        return {
            "version": __version__,
            "libtorrent": lt.__version__,
            "pid": os.getpid(),
            "uptime_seconds": int(time.time() - self.started_at),
            "download_dir": self.cfg["download_dir"],
            "listen_port": self.ses.listen_port(),
            "dht_nodes": s.dht_nodes,
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
            self._save_resume(h, force=True)
        elif isinstance(a, lt.torrent_finished_alert):
            h = a.handle
            st = h.status()
            ih = hash_of(h)
            self._save_resume(h, force=True)
            if self.store.completed(ih):
                log.info("finished %s %s", ih, st.name)
                if self.cfg.get("notify", True):
                    notify("Качалка: скачано", st.name)
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
        elif isinstance(a, lt.add_torrent_alert) and a.error.value():
            log.error("restore failed: %s", a.message())

    def _trash_later(self, ih: str, path: Path) -> None:
        ok = move_to_trash(path)
        self.store.removed(ih, trashed=ok)

    def periodic(self) -> None:
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
        if cmd == "info":
            return self.info()
        if cmd == "add":
            return self.add(req["source"], req.get("save_path"))
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
            return {"config": self.cfg, "note": "listen_port применится после перезапуска"}
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
        self.wfile.write(json.dumps(resp, ensure_ascii=False).encode() + b"\n")


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
