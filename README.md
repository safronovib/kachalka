# Качалка

Торрент-клиент для macOS, которым удобно управлять через ИИ-ассистента (Claude Code). Пишешь «скачай вот это», кидаешь magnet или `.torrent`, и загрузка пошла. Прогресс виден в строке меню.

*A small libtorrent-based torrent client for macOS, built to be driven by an AI assistant: a background daemon, a JSON-friendly CLI, a SwiftBar menu-bar item and a Claude Code skill.*

## Что умеет сейчас (этап 1)

- Качает magnet-ссылки, `.torrent`-файлы и ссылки на `.torrent` (движок libtorrent: DHT, LSD, UPnP/NAT-PMP, докачка).
- Работает в фоне и сам запускается при входе в систему. После перезапуска продолжает с того же места.
- Значок в строке меню через [SwiftBar](https://github.com/swiftbar/SwiftBar): `⬇ 57%  12 мин`. По нажатию видно каждую загрузку, скорость и время окончания, есть пауза, «показать в Finder», удаление в Корзину и добавление magnet из буфера обмена. Когда ничего не качается, значка нет.
- Уведомление macOS, когда загрузка закончилась.
- Ставит раздачу на паузу после заданного рейтинга (по умолчанию 1.0).
- Пишет историю в SQLite: что добавлено, когда скачано, куда легло, что удалено.
- Навык для Claude Code: ассистент сам вызывает нужные команды.

## Дальше

- **Этап 2. Порядок.** Распознавание фильмов и сериалов по названию, раскладка по папкам, переименование, журнал с откатом.
- **Этап 3. Чистка.** Правила вида «посмотрел и прошло 3 дня» или «не открывал месяц»: удаление в Корзину со списком на подтверждение.
- **Этап 4.** Окно со списком загрузок.

## Установка

Готовый пакет libtorrent для macOS есть на PyPI под Python 3.11–3.13. Проще всего взять Python 3.13 через [uv](https://docs.astral.sh/uv/):

```bash
git clone https://github.com/safronovib/kachalka.git
cd kachalka
V="$HOME/Library/Application Support/Kachalka/venv"
uv venv --python 3.13 "$V" && uv pip install --python "$V/bin/python" libtorrent
PYTHONPATH="$PWD" "$V/bin/python" -m kachalka.cli install
```

`brew install libtorrent-rasterbar` тоже подойдёт, но на Intel-Маке Homebrew собирает его из исходников больше часа.

`install` запоминает Python, в котором есть libtorrent, и включает автозапуск (LaunchAgent). Если стоит SwiftBar, он кладёт ссылку на плагин в папку плагинов. Ещё он ставит навык Claude в `~/.claude/skills/kachalka` и команду `~/.local/bin/kachalka`. Ненужное отключается флагами `--no-swiftbar` и `--no-skill`. Всё это убирает `kachalka uninstall`.

## Команды

```text
kachalka add <magnet|файл.torrent|url> [--to папка]
kachalka list                      # что качается
kachalka pause|resume <id|название|all>
kachalka rm <id> [--files]         # --files: скачанное в Корзину
kachalka history
kachalka limit --down 5000 --up 500   # КБ/с, 0 = без ограничений
kachalka set download_dir "~/Movies/Торренты"
kachalka vpn on|off|status         # торренты через VPN или напрямую мимо него
kachalka diag                      # почему не качается: DHT, трекеры, ошибки
kachalka info | start | stop | restart
```

### VPN

Многие VPN-серверы режут торрент-трафик. Тогда magnet-ссылки висят на «ищу метаданные», а `kachalka diag` показывает растущий `dht_messages_out_dropped`. `kachalka vpn off` (или пункт меню «Пустить мимо VPN») привязывает Качалку к Wi-Fi или Ethernet, и торренты идут напрямую, а весь остальной трафик по-прежнему через VPN. Если в VPN включён kill switch, он заблокирует и это. Качалка предупредит, когда видит kill switch у AmneziaVPN.

У каждой команды есть `--json` для машинного чтения.

## Где что лежит

| Что | Где |
|---|---|
| Настройки | `~/Library/Application Support/Kachalka/config.json` |
| История | `…/Kachalka/history.sqlite3` |
| Состояние раздач | `…/Kachalka/resume/*.fastresume` |
| Журнал | `…/Kachalka/kachalka.log` |
| Загрузки по умолчанию | `~/Movies/Торренты` |

Команды идут через Unix-сокет с правами `0600`. Сеть, браузер и другие пользователи Мака до управления не дотянутся.

## Устройство

```text
Claude Code ──(навык)──┐
SwiftBar ──────────────┼──> kachalka (CLI) ──Unix-сокет──> фоновая программа ──> libtorrent
Терминал ──────────────┘                                    │
                                                           SQLite-история, fastresume
```

## Лицензия

MIT. Качайте только то, что вам разрешено качать.
