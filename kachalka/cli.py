"""Командная строка `kachalka`. Каждая команда умеет --json, чтобы ИИ читал ответ без разбора текста."""

from __future__ import annotations

import argparse
import json
import os
import plistlib
import subprocess
import sys
from pathlib import Path

from . import __version__, fmt
from .client import NotRunning, call, is_running, start_background
from .config import APP_DIR, LOG_PATH, load_config

ROOT = Path(__file__).resolve().parent.parent
CLI = ROOT / "bin" / "kachalka"
AGENT_LABEL = "com.kachalka.daemon"
AGENT_PLIST = Path.home() / "Library/LaunchAgents" / f"{AGENT_LABEL}.plist"
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
            results.append(call("add", source=src, save_path=args.to))
        except RuntimeError as e:
            errors.append({"source": src, "error": str(e)})
    lines = [f"+ {r['name']}  [{r['id']}] → {r['save_path']}" for r in results]
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


def cmd_history(args) -> None:
    ensure_running()
    rows = call("history", limit=args.limit)["history"]
    lines = []
    for r in rows:
        status = "удалено" if r["removed_at"] else ("скачано" if r["completed_at"] else "в работе")
        lines.append(f"{r['added_at'][:16].replace('T', ' ')}  {status:9}  {fmt.size(r['total_size']):>9}  {r['name']}")
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

    # 4. Команда kachalka в PATH
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
    print(f"Автозапуск, значок, навык и команда убраны. Раздачи и история остались в {APP_DIR}")


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
    add("daemon", cmd_daemon, "запустить фоновую программу в этом окне (для отладки)")
    sp = add("reveal", cmd_reveal, "показать файл в Finder (без аргумента — папку загрузок)")
    sp.add_argument("target", nargs="?")
    sp.add_argument("--downloads", action="store_true")
    add("swiftbar", cmd_swiftbar, "вывод для плагина SwiftBar")
    sp = add("install", cmd_install, "автозапуск, значок SwiftBar, навык Claude, команда в PATH")
    sp.add_argument("--no-swiftbar", action="store_true")
    sp.add_argument("--no-skill", action="store_true")
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
