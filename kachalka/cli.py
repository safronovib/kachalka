"""Командная строка `kachalka`. Каждая команда умеет --json, чтобы ИИ читал ответ без разбора текста."""

from __future__ import annotations

import argparse
import json
import os
import plistlib
import subprocess
import sys
from pathlib import Path

from . import __version__, fmt, net
from .client import AGENT_LABEL, AGENT_PLIST, NotRunning, call, is_running, start_background
from .config import APP_DIR, LOG_PATH, load_config

ROOT = Path(__file__).resolve().parent.parent
CLI = ROOT / "bin" / "kachalka"
SWIFTBAR_DIR = Path.home() / "Library/Application Support/SwiftBarPlugins"
SWIFTBAR_PLUGIN = SWIFTBAR_DIR / "kachalka.5s.sh"
SKILL_SRC = ROOT / "skill"
SKILL_DST = Path.home() / ".claude/skills/kachalka"
PYTHON_CACHE = APP_DIR / "python"


def ensure_running() -> None:
    if not is_running() and not start_background():
        sys.exit(f"Не удалось запустить фоновую программу. Журнал: {LOG_PATH}")


def emit(args, data, text: str) -> None:
    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
    else:
        print(text)


# ---------- команды ----------

def cmd_add(args) -> None:
    ensure_running()
    results, errors = [], []
    for src in args.sources:
        try:
            follow = {"ahead": args.ahead} if getattr(args, "follow", False) else None
            results.append(call("add", source=src, save_path=args.to,
                                episodes=getattr(args, "episodes", None), follow=follow))
        except RuntimeError as e:
            errors.append({"source": src, "error": str(e)})
    lines = []
    for r in results:
        lines.append(f"+ {r['name']}  [{r['id']}] → {r['save_path']}")
        sel = r.get("select")
        if sel and sel.get("pending"):
            what = "режим «смотрю сериал»" if args.follow else f"серии {args.episodes}"
            lines.append(f"    {what}: включу, когда придут метаданные")
        elif sel and sel.get("follow"):
            lines.append(f"    смотрю сериал, держу наготове: {', '.join(sel['selected'])}")
        elif sel:
            lines.append(f"    качаю только: {', '.join(sel['selected'])}")
    lines += [f"✗ {e['source']}: {e['error']}" for e in errors]
    emit(args, {"added": results, "errors": errors}, "\n".join(lines))
    if errors and not results:
        sys.exit(1)


def cmd_add_clipboard(args) -> None:
    text = subprocess.run(["pbpaste"], capture_output=True, text=True).stdout.strip()
    if not text.startswith("magnet:"):
        subprocess.run(["osascript", "-e", 'display notification "В буфере обмена нет magnet-ссылки" with title "Качалка"'])
        sys.exit(1)
    args.sources, args.to = [text], None
    cmd_add(args)


def cmd_list(args) -> None:
    ensure_running()
    ts = call("list")["torrents"]
    if not ts:
        emit(args, {"torrents": []}, "Список пуст.")
        return
    lines = []
    for t in ts:
        line = f"[{t['id']}] {t['name']}\n    {fmt.bar(t['progress'], 16)} {t['progress']:5.1f}%  {t['state']}"
        if t["state"] == "качается":
            line += f"  {fmt.size(t['done'])}/{fmt.size(t['size'])}  ↓{fmt.speed(t['down_rate'])}  осталось {fmt.eta(t['eta_seconds'])}"
        elif t["size"]:
            line += f"  {fmt.size(t['size'])}  ↑{fmt.speed(t['up_rate'])}  рейтинг {t['ratio']}"
        if t["error"]:
            line += f"\n    ⚠︎ {t['error']}"
        lines.append(line)
    emit(args, {"torrents": ts}, "\n".join(lines))


def cmd_simple(name: str, key: str, verb: str):
    def run(args) -> None:
        ensure_running()
        extra = {"trash_files": args.files} if name == "remove" else {}
        res = call(name, ref=args.ref, **extra)
        items = res[key]
        emit(args, res, "\n".join(f"{verb}: {n}" for n in items) or "Ничего не изменилось.")
    return run


def cmd_files(args) -> None:
    ensure_running()
    res = call("files", ref=args.ref)
    lines = [f"[{res['id']}] {res['name']}"]
    if res.get("note"):
        lines.append(res["note"])
    for f in res["files"]:
        mark = "✓" if f["selected"] else "·"
        ep = ""
        if f["episode"] is not None:
            ep = f"S{f['season']:02d}E{f['episode']:02d} " if f["season"] is not None else f"серия {f['episode']} "
        lines.append(f"  {mark} {f['index']:>3}  {ep}{f['path'].split('/')[-1]}  {fmt.size(f['size'])}  {f['progress']:.0f}%")
    emit(args, res, "\n".join(lines))


def cmd_select(args) -> None:
    ensure_running()
    mode = "all" if args.all else "add" if args.add else "remove" if args.remove else "only"
    files = [int(x) for x in args.files.split(",")] if args.files else []
    if mode != "all" and not (args.episodes or files):
        sys.exit("Укажи --episodes 4,5 или --files 3,7 (или --all)")
    res = call("select", ref=args.ref, mode=mode, episodes=args.episodes, files=files)
    if res.get("pending"):
        emit(args, res, "Метаданных ещё нет — выберу, как только придут.")
    else:
        emit(args, res, f"Качаю {res['count']} файл(ов):\n  " + "\n  ".join(res["selected"]))


def cmd_sort(args) -> None:
    ensure_running()
    overrides = {"category": args.as_, "title": args.title, "year": args.year, "season": args.season}
    res = call("sort", ref=args.ref, dry_run=args.dry_run, overrides=overrides)
    lines = []
    for r in res["results"]:
        if r.get("skipped"):
            lines.append(f"· {r['name']}: {r['skipped']}")
            continue
        head = f"{r['category_ru']}: {r['title']}" + (f" ({r['year']})" if r.get("year") else "") + \
               (f", сезон {r['season']}" if r.get("season") is not None else "")
        if r.get("note"):
            lines.append(f"· {r['name']} — {head}, {r['note']}")
            continue
        verb = "План" if args.dry_run else f"Раскладываю #{r['move_id']}"
        lines.append(f"{verb}: {r['name']}\n    {head}\n    → {r['save_path']}")
        shown = [rn for rn in r["renames"] if "/.pad/" not in rn["from"]]
        for rn in shown[:5]:
            lines.append(f"      {rn['from'].split('/')[-1]} → {rn['to']}")
        if len(shown) > 5:
            lines.append(f"      … и ещё {len(shown) - 5}")
    emit(args, res, "\n".join(lines) or "Нечего раскладывать.")


def cmd_undo(args) -> None:
    ensure_running()
    res = call("undo", ref=args.ref)
    emit(args, res, f"Возвращаю #{res['move_id']} {res['name']} → {res['back_to']}")


def cmd_moves(args) -> None:
    ensure_running()
    rows = call("moves", limit=args.limit)["moves"]
    status = {"pending": "переезжает", "done": "разложено", "failed": "ошибка", "undoing": "возвращаю", "undone": "откачено"}
    lines = [f"#{m['id']:<4} {fmt.when(m['at'])}  {status.get(m['status'], m['status']):11} {m['name']}\n"
             f"       → {m['to_path']}" + (f"\n       ⚠︎ {m['error']}" if m.get("error") else "") for m in rows]
    emit(args, {"moves": rows}, "\n".join(lines) or "Раскладок ещё не было.")


STATE_ICON = {"watched": "✓", "watching": "▶", "new": "·"}
STATE_RU = {"watched": "посмотрено", "watching": "смотрю", "new": "не смотрел"}


def _ep_label(f: dict) -> str:
    if f.get("episode") is None:
        return f["name"]
    s = f"S{f['season']:02d}E{f['episode']:02d}" if f.get("season") is not None else f"серия {f['episode']}"
    return s


def cmd_watching(args) -> None:
    ensure_running()
    res = call("watching", ref=args.ref)
    lines = []
    for t in res["torrents"]:
        lines.append(f"[{t['id']}] {t['name']}")
        fl = t.get("follow")
        cur = t.get("current")
        if fl:
            lines.append(f"    режим «смотрю сериал»: держу {fl['ahead']} серии вперёд"
                         + (f", просмотренные убираю через {fl['keep_hours']:g} ч" if fl["cleanup"] else ""))
        if cur and cur.get("done"):
            lines.append("    всё досмотрено")
        elif cur and cur.get("episode") is not None:
            lines.append(f"    сейчас: {_ep_label(cur)}")
        for f in t["files"]:
            extra = []
            if f["state"] == "watching":
                extra.append(f"{f['watched_share']}%")
            if f["trashed"]:
                extra.append("в Корзине")
            elif not f["exists"]:
                extra.append("не скачано")
            elif f["downloaded"] < 100:
                extra.append(f"скачано {f['downloaded']}%" + ("" if f["selected"] else ", не качаю"))
            lines.append(f"    {STATE_ICON[f['state']]} {_ep_label(f) if f['episode'] is not None else f['name']}"
                         f"  {STATE_RU[f['state']]}" + (f" ({', '.join(extra)})" if extra else ""))
    emit(args, res, "\n".join(lines) or "Видео в раздачах нет.")


def cmd_watched(args) -> None:
    ensure_running()
    files = [int(x) for x in args.files.split(",")] if args.files else []
    res = call("watched", ref=args.ref, episodes=args.episodes, files=files, watched=not args.unwatched)
    verb = "Не смотрел" if args.unwatched else "Посмотрено"
    emit(args, res, f"{verb}: " + ", ".join(res["marked"]))


def cmd_follow(args) -> None:
    ensure_running()
    res = call("follow", ref=args.ref, ahead=args.ahead, keep_hours=args.keep_hours,
               cleanup=not args.no_cleanup, from_ep=args.from_ep)
    cur = res.get("current") or {}
    head = "всё досмотрено" if cur.get("done") else f"сейчас {_ep_label(cur)}" if cur.get("episode") is not None else ""
    emit(args, res, f"Смотрю «{res['name']}»: {head}\nДержу наготове: " + ", ".join(res.get("ready", [])))


def cmd_unfollow(args) -> None:
    ensure_running()
    res = call("unfollow", ref=args.ref)
    emit(args, res, f"Режим «смотрю сериал» выключен: {res['unfollowed']}")


def cmd_cleanup(args) -> None:
    ensure_running()
    ids = args.ids.split(",") if args.ids else None
    res = call("cleanup", apply=args.apply, ids=ids)
    if args.apply:
        items = res["trashed"]
        emit(args, res, "В Корзину: " + ", ".join(i["name"] for i in items) if items else "Убирать нечего.")
        return
    items = res["candidates"]
    if not items:
        emit(args, res, "Убирать нечего.")
        return
    lines = [f"[{i['id']}] {i['name']}  {fmt.size(i['size'])}  — {i['reason']}" for i in items]
    lines.append(f"Всего {fmt.size(sum(i['size'] for i in items))}. Убрать в Корзину: kachalka cleanup --apply [--ids …]")
    emit(args, res, "\n".join(lines))


def cmd_archive(args) -> None:
    ensure_running()
    res = call("archive", search=" ".join(args.search or []), limit=args.limit)
    lines = [f"[{r['id']}] {fmt.when(r['added_at'])}  {r['status']:12} {fmt.size(r['size']):>9}  {r['name']}"
             + ("" if r["can_redownload"] or r["in_session"] else "  (нет .torrent)") for r in res["archive"]]
    lines.append(f"Копилка .torrent: {res['folder']}")
    emit(args, res, "\n".join(lines))


def cmd_stash(args) -> None:
    ensure_running()
    res = call("stash", paths=[os.path.abspath(os.path.expanduser(x)) for x in args.files])
    emit(args, res, "\n".join(f"В копилке: {r['name']}  [{r['id']}]  {fmt.size(r['size'])}" for r in res["stashed"]))


def cmd_redownload(args) -> None:
    ensure_running()
    res = call("redownload", ref=" ".join(args.ref), episodes=args.episodes)
    text = f"Качаю заново: {res['name']}  [{res['id']}] → {res['save_path']}"
    if res.get("select") and not res["select"].get("pending"):
        text += "\n    только: " + ", ".join(res["select"]["selected"])
    emit(args, res, text)


def cmd_history(args) -> None:
    ensure_running()
    rows = call("history", limit=args.limit)["history"]
    lines = []
    for r in rows:
        status = "удалено" if r["removed_at"] else ("скачано" if r["completed_at"] else "в работе")
        lines.append(f"{fmt.when(r['added_at'])}  {status:9}  {fmt.size(r['total_size']):>9}  {r['name']}")
    emit(args, {"history": rows}, "\n".join(lines) or "История пуста.")


def cmd_limit(args) -> None:
    ensure_running()
    res = call("limit", down_kbps=args.down, up_kbps=args.up)
    show = lambda v: f"{v} КБ/с" if v else "без ограничений"
    emit(args, res, f"Скачивание: {show(res['download_limit_kbps'])}\nРаздача: {show(res['upload_limit_kbps'])}")


def cmd_set(args) -> None:
    ensure_running()
    try:
        value = json.loads(args.value)
    except ValueError:
        value = args.value
    res = call("set", key=args.key, value=value)
    emit(args, res, f"{args.key} = {json.dumps(value, ensure_ascii=False)}")


def cmd_info(args) -> None:
    if not is_running():
        emit(args, {"running": False}, "Фоновая программа не запущена.")
        return
    i = call("info")
    emit(args, {"running": True, **i},
         f"Качалка {i['version']} (libtorrent {i['libtorrent']}), pid {i['pid']}\n"
         f"Папка: {i['download_dir']}\nПорт: {i['listen_port']}, DHT-узлов: {i['dht_nodes']}\n"
         f"Раздач: {i['torrents']}, ↓{fmt.speed(i['down_rate'])} ↑{fmt.speed(i['up_rate'])}")


def amnezia_kill_switch() -> bool:
    plist = Path.home() / "Library/Preferences/org.amneziavpn.AmneziaVPN.plist"
    if not plist.exists():
        return False
    try:
        return bool(plistlib.loads(plist.read_bytes()).get("Conf.killSwitchEnabled"))
    except Exception:
        return False


def describe_route(iface: str) -> str:
    if not iface:
        return "Торренты идут так, как решит система (VPN сейчас не найден)."
    if net.is_tunnel(iface):
        return f"Торренты идут через VPN ({iface})."
    return f"Торренты идут напрямую через {iface}, мимо VPN."


def cmd_vpn(args) -> None:
    ensure_running()
    if args.mode != "status":
        call("set", key="bind_interface", value=net.AUTO if args.mode == "on" else net.DIRECT)
    i = call("info")
    iface = i.get("active_interface", "")
    via_vpn = net.is_tunnel(iface)
    warn = ""
    if not via_vpn and i.get("bind_interface") == net.DIRECT and amnezia_kill_switch():
        warn = ("\n⚠︎ В AmneziaVPN включён kill switch: он блокирует трафик мимо VPN, "
                "торренты так не пойдут.")
    emit(args, {"via_vpn": via_vpn, "mode": i.get("bind_interface"), "active_interface": iface,
                "kill_switch_blocks": bool(warn)},
         describe_route(iface) + warn)
    if warn and not args.json and not sys.stdout.isatty():
        subprocess.run(["osascript", "-e",
                        'display notification "В AmneziaVPN включён kill switch, мимо VPN торренты не пойдут" with title "Качалка"'])


def cmd_diag(args) -> None:
    ensure_running()
    print(json.dumps(call("diag"), ensure_ascii=False, indent=2))


def cmd_restart(args) -> None:
    if AGENT_PLIST.exists():
        r = subprocess.run(["launchctl", "kickstart", "-k", f"gui/{os.getuid()}/{AGENT_LABEL}"],
                           capture_output=True, text=True)
        print("Перезапущена." if r.returncode == 0 else f"Ошибка: {r.stderr.strip()}")
    else:
        cmd_stop(args)
        import time
        time.sleep(3)
        cmd_start(args)


def cmd_start(args) -> None:
    ok = start_background()
    print("Запущена." if ok else f"Не запустилась, смотри {LOG_PATH}")


def cmd_stop(args) -> None:
    try:
        call("shutdown")
        print("Останавливаю (сохраняю состояние раздач).")
    except NotRunning:
        print("И так не запущена.")


def cmd_daemon(args) -> None:
    from .daemon import main
    sys.exit(main())


def cmd_reveal(args) -> None:
    if args.downloads or not args.target:
        p = Path(os.path.expanduser(load_config()["download_dir"]))
        p.mkdir(parents=True, exist_ok=True)
        subprocess.run(["open", str(p)])
        return
    p = Path(os.path.expanduser(args.target))
    if p.is_dir():
        subprocess.run(["open", str(p)])
    elif p.exists():
        subprocess.run(["open", "-R", str(p)])
    else:
        subprocess.run(["open", str(p.parent)])


def _confirm(question: str, button: str) -> bool:
    """Системный диалог «точно?» — для удалений из меню значка, где легко промахнуться."""
    script = ('on run argv\n'
              'display dialog (item 1 of argv) buttons {"Отмена", (item 2 of argv)} '
              'default button "Отмена" cancel button "Отмена" with title "Качалка" with icon caution\n'
              'end run')
    r = subprocess.run(["osascript", "-e", script, question, button], capture_output=True, text=True)
    return r.returncode == 0 and button in r.stdout


def cmd_confirm_rm(args) -> None:
    ensure_running()
    names = [t["name"] for t in call("list")["torrents"] if t["id"].startswith(args.ref)]
    if not names:
        return
    if _confirm(f"Удалить «{names[0]}» вместе с файлами?\nФайлы уйдут в Корзину.", "В Корзину"):
        call("remove", ref=args.ref, trash_files=True)


def cmd_confirm_cleanup(args) -> None:
    ensure_running()
    cands = call("cleanup")["candidates"]
    if args.ids:
        cands = [c for c in cands if c["id"] in args.ids.split(",")]
    if not cands:
        return
    listing = "\n".join(f"• {c['name']}" for c in cands[:8]) + ("\n…" if len(cands) > 8 else "")
    if _confirm(f"Убрать в Корзину ({fmt.size(sum(c['size'] for c in cands))}):\n{listing}", "В Корзину"):
        call("cleanup", apply=True, ids=[c["id"] for c in cands])


def cmd_swiftbar_dismiss(args) -> None:
    from .swiftbar import dismiss
    dismiss()


def cmd_swiftbar(args) -> None:
    from .swiftbar import render
    sys.stdout.write(render(str(CLI)))


# ---------- установка ----------

def cmd_install(args) -> None:
    APP_DIR.mkdir(parents=True, exist_ok=True)
    PYTHON_CACHE.write_text(sys.executable + "\n")
    done = [f"Python для запуска: {sys.executable}"]

    # 1. Автозапуск при входе в систему
    AGENT_PLIST.parent.mkdir(parents=True, exist_ok=True)
    plist = {
        "Label": AGENT_LABEL,
        "ProgramArguments": [sys.executable, "-m", "kachalka.daemon"],
        "EnvironmentVariables": {"PYTHONPATH": str(ROOT)},
        "WorkingDirectory": str(APP_DIR),
        "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False},   # упала — перезапустить; остановили командой — нет
        "StandardOutPath": str(LOG_PATH),
        "StandardErrorPath": str(LOG_PATH),
        "ProcessType": "Background",
    }
    if is_running():
        call("shutdown")
        import time
        for _ in range(50):
            if not is_running():
                break
            time.sleep(0.2)
    subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}/{AGENT_LABEL}"], capture_output=True)
    AGENT_PLIST.write_bytes(plistlib.dumps(plist))
    r = subprocess.run(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(AGENT_PLIST)], capture_output=True, text=True)
    done.append("Автозапуск: " + ("включён" if r.returncode == 0 else f"ошибка {r.stderr.strip()}"))

    # 2. Значок в строке меню (SwiftBar)
    if SWIFTBAR_DIR.is_dir() and not args.no_swiftbar:
        src = ROOT / "swiftbar" / "kachalka.5s.sh"
        SWIFTBAR_PLUGIN.unlink(missing_ok=True)
        SWIFTBAR_PLUGIN.symlink_to(src)
        done.append(f"SwiftBar: {SWIFTBAR_PLUGIN.name}")

    # 3. Навык для Claude Code
    if not args.no_skill:
        SKILL_DST.parent.mkdir(parents=True, exist_ok=True)
        if SKILL_DST.is_symlink() or SKILL_DST.exists():
            SKILL_DST.unlink()
        SKILL_DST.symlink_to(SKILL_SRC)
        done.append(f"Навык Claude: {SKILL_DST}")

    # 3б. Значок не должен прятаться за Hidden Bar/Bartender: закрепляем его справа,
    #     рядом с системными значками (позиция считается от правого края экрана)
    #     SwiftBar хранит позицию под настоящим путём плагина (ссылка раскрывается), а macOS
    #     стирает её, если значок скрыть, поэтому плагин в простое показывает невидимый символ.
    if SWIFTBAR_DIR.is_dir() and not args.no_swiftbar:
        for key in (f"NSStatusItem Preferred Position {SWIFTBAR_PLUGIN.name}",
                    f"NSStatusItem Preferred Position {SWIFTBAR_PLUGIN}",
                    f"NSStatusItem Preferred Position {SWIFTBAR_PLUGIN.resolve()}"):
            subprocess.run(["defaults", "write", "com.ameba.SwiftBar", key, "-float", "600"], capture_output=True)

    # 4. Окно (этап 4): build/Kachalka.app → ~/Applications
    app_src = ROOT / "build" / "Kachalka.app"
    if not app_src.exists() and (ROOT / "app" / "build.sh").exists() and not args.no_app:
        subprocess.run([str(ROOT / "app" / "build.sh")], capture_output=True)
    if app_src.exists() and not args.no_app:
        dst = Path.home() / "Applications" / "Kachalka.app"
        dst.parent.mkdir(exist_ok=True)
        subprocess.run(["rm", "-rf", str(dst)])
        subprocess.run(["ditto", str(app_src), str(dst)])
        subprocess.run(["/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/"
                        "Support/lsregister", "-f", str(dst)], capture_output=True)
        done.append(f"Окно: {dst}")

    # 5. Команда kachalka в PATH
    local_bin = Path.home() / ".local/bin"
    local_bin.mkdir(parents=True, exist_ok=True)
    link = local_bin / "kachalka"
    if link.is_symlink() or link.exists():
        link.unlink()
    link.symlink_to(CLI)
    done.append(f"Команда: {link}")
    print("\n".join(done))


def cmd_uninstall(args) -> None:
    subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}/{AGENT_LABEL}"], capture_output=True)
    for p in (AGENT_PLIST, SWIFTBAR_PLUGIN, SKILL_DST, Path.home() / ".local/bin/kachalka"):
        if p.is_symlink() or p.exists():
            p.unlink()
    app = Path.home() / "Applications" / "Kachalka.app"
    if app.exists():
        subprocess.run(["rm", "-rf", str(app)])
    print(f"Автозапуск, значок, окно, навык и команда убраны. Раздачи и история остались в {APP_DIR}")


# ---------- разбор аргументов ----------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="kachalka", description="Торрент-качалка с управлением через ИИ.")
    p.add_argument("--version", action="version", version=__version__)
    sub = p.add_subparsers(dest="command", required=True)

    def add(name, func, help, aliases=()):
        sp = sub.add_parser(name, help=help, aliases=list(aliases))
        sp.add_argument("--json", action="store_true", help="ответ в JSON")
        sp.set_defaults(func=func)
        return sp

    sp = add("add", cmd_add, "добавить magnet, .torrent-файл или ссылку на .torrent")
    sp.add_argument("sources", nargs="+")
    sp.add_argument("--to", help="папка назначения (по умолчанию из настроек)")
    sp.add_argument("--episodes", help="качать только эти серии: 4,5 / 4-6 / S02E04")
    sp.add_argument("--follow", action="store_true", help="режим «смотрю сериал»: качать по мере просмотра")
    sp.add_argument("--ahead", type=int, help="сколько серий держать наготове (с --follow)")
    sp = add("archive", cmd_archive, "архив всех раздач (копилка .torrent), можно с поиском")
    sp.add_argument("search", nargs="*")
    sp.add_argument("--limit", type=int, default=200)
    sp = add("stash", cmd_stash, "положить .torrent в копилку, не скачивая (оригинал — в Корзину)")
    sp.add_argument("files", nargs="+")
    sp = add("redownload", cmd_redownload, "скачать заново из архива: id или часть названия")
    sp.add_argument("ref", nargs="+")
    sp.add_argument("--episodes")
    sp = add("watching", cmd_watching, "что посмотрено, что смотрю, на какой серии остановился")
    sp.add_argument("ref", nargs="?")
    sp = add("watched", cmd_watched, "отметить посмотренным (или --unwatched)")
    sp.add_argument("ref")
    sp.add_argument("--episodes")
    sp.add_argument("--files")
    sp.add_argument("--unwatched", action="store_true")
    sp = add("follow", cmd_follow, "режим «смотрю сериал»: подкачивать следующие серии, убирать просмотренные")
    sp.add_argument("ref")
    sp.add_argument("--ahead", type=int, help="сколько серий держать наготове после текущей (по умолчанию 2)")
    sp.add_argument("--keep-hours", type=float, help="через сколько часов после просмотра убирать (по умолчанию 24)")
    sp.add_argument("--no-cleanup", action="store_true", help="просмотренные не убирать")
    sp.add_argument("--from", dest="from_ep", help="с какой серии начать, если ещё не смотрел: 4 или S02E04")
    sp = add("unfollow", cmd_unfollow, "выключить режим «смотрю сериал»")
    sp.add_argument("ref")
    sp = add("cleanup", cmd_cleanup, "что можно убрать: давно посмотренное и не открытое")
    sp.add_argument("--apply", action="store_true", help="убрать в Корзину")
    sp.add_argument("--ids", help="только эти id через запятую")
    sp = add("files", cmd_files, "файлы раздачи: что выбрано, какие серии, сколько скачано")
    sp.add_argument("ref")
    sp = add("select", cmd_select, "выбрать, какие файлы или серии качать")
    sp.add_argument("ref")
    sp.add_argument("--episodes", help="4,5 / 4-6 / S02E04 / 2x4")
    sp.add_argument("--files", help="индексы из kachalka files: 3,7")
    g = sp.add_mutually_exclusive_group()
    g.add_argument("--add", action="store_true", help="добавить к уже выбранному")
    g.add_argument("--remove", action="store_true", help="перестать качать эти")
    g.add_argument("--all", action="store_true", help="качать всё")
    sp = add("sort", cmd_sort, "разложить по папкам (Фильмы, Сериалы…) и переименовать")
    sp.add_argument("ref", help="id, часть названия или all")
    sp.add_argument("--dry-run", action="store_true", help="только показать план")
    sp.add_argument("--as", dest="as_", choices=["movie", "series", "music", "book", "software", "other"])
    sp.add_argument("--title")
    sp.add_argument("--year", type=int)
    sp.add_argument("--season", type=int)
    sp = add("undo", cmd_undo, "откатить раскладку: id раздачи или номер #перемещения")
    sp.add_argument("ref")
    sp = add("moves", cmd_moves, "журнал раскладок")
    sp.add_argument("--limit", type=int, default=30)
    add("add-clipboard", cmd_add_clipboard, "добавить magnet из буфера обмена")
    add("list", cmd_list, "что качается и раздаётся", aliases=("ls",))
    for name, key, verb, aliases in (("pause", "paused", "Пауза", ()),
                                     ("resume", "resumed", "Продолжаю", ()),
                                     ("remove", "removed", "Убрано", ("rm",))):
        sp = add(name, cmd_simple(name, key, verb), f"{verb.lower()}: id, часть названия или all", aliases)
        sp.add_argument("ref")
        if name == "remove":
            sp.add_argument("--files", action="store_true", help="унести скачанные файлы в Корзину")
    sp = add("history", cmd_history, "история загрузок")
    sp.add_argument("--limit", type=int, default=50)
    sp = add("limit", cmd_limit, "ограничить скорость, КБ/с (0 = без ограничений)")
    sp.add_argument("--down", type=int)
    sp.add_argument("--up", type=int)
    sp = add("set", cmd_set, "изменить настройку (download_dir, stop_seeding_at_ratio, notify, listen_port)")
    sp.add_argument("key")
    sp.add_argument("value")
    add("info", cmd_info, "состояние фоновой программы", aliases=("status",))
    add("start", cmd_start, "запустить фоновую программу")
    add("stop", cmd_stop, "остановить фоновую программу")
    add("restart", cmd_restart, "перезапустить фоновую программу (например, после обновления кода)")
    add("diag", cmd_diag, "сетевая диагностика: DHT, трекеры, ошибки")
    sp = add("vpn", cmd_vpn, "торренты через VPN (on) или напрямую мимо него (off)")
    sp.add_argument("mode", choices=["on", "off", "status"])
    add("daemon", cmd_daemon, "запустить фоновую программу в этом окне (для отладки)")
    sp = add("reveal", cmd_reveal, "показать файл в Finder (без аргумента — папку загрузок)")
    sp.add_argument("target", nargs="?")
    sp.add_argument("--downloads", action="store_true")
    add("swiftbar", cmd_swiftbar, "вывод для плагина SwiftBar")
    add("swiftbar-dismiss", cmd_swiftbar_dismiss, "убрать «✓ скачано» из строки меню")
    sp = add("confirm-rm", cmd_confirm_rm, "удалить с файлами после подтверждения в диалоге (для меню)")
    sp.add_argument("ref")
    sp = add("confirm-cleanup", cmd_confirm_cleanup, "чистка после подтверждения в диалоге (для меню)")
    sp.add_argument("--ids")
    sp = add("install", cmd_install, "автозапуск, значок SwiftBar, навык Claude, команда в PATH")
    sp.add_argument("--no-swiftbar", action="store_true")
    sp.add_argument("--no-skill", action="store_true")
    sp.add_argument("--no-app", action="store_true", help="не ставить окно Kachalka.app")
    add("uninstall", cmd_uninstall, "убрать автозапуск, значок, навык и команду")
    return p


def main(argv=None) -> None:
    args = build_parser().parse_args(argv)
    try:
        args.func(args)
    except RuntimeError as e:
        sys.exit(f"Ошибка: {e}")
    except NotRunning:
        sys.exit("Фоновая программа не запущена: kachalka start")


if __name__ == "__main__":
    main()
