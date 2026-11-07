# Качалка

Торрент-клиент для macOS, которым удобно управлять через ИИ-ассистента (Claude Code). Пишешь «скачай вот это», кидаешь magnet или `.torrent`, и загрузка пошла. Прогресс виден в строке меню.

*A small libtorrent-based torrent client for macOS, built to be driven by an AI assistant: a background daemon, a JSON-friendly CLI, a SwiftBar menu-bar item and a Claude Code skill.*

## Этап 1: загрузки

- Качает magnet-ссылки, `.torrent`-файлы и ссылки на `.torrent` (движок libtorrent: DHT, LSD, UPnP/NAT-PMP, докачка).
- Работает в фоне и сам запускается при входе в систему. После перезапуска продолжает с того же места.
- Значок в строке меню через [SwiftBar](https://github.com/swiftbar/SwiftBar): `⬇ 57%  12 мин`. По нажатию видно каждую загрузку, скорость и время окончания, есть пауза, «показать в Finder», удаление в Корзину и добавление magnet из буфера обмена. Когда ничего не качается, значка нет.
- Уведомление macOS, когда загрузка закончилась.
- Ставит раздачу на паузу после заданного рейтинга (по умолчанию 1.0).
- Пишет историю в SQLite: что добавлено, когда скачано, куда легло, что удалено.
- Навык для Claude Code: ассистент сам вызывает нужные команды.

## Этап 2: порядок

- **Раскладка по папкам.** Когда загрузка закончилась, Качалка распознаёт её по названию и файлам ([guessit](https://github.com/guessit-io/guessit) плюс типы файлов) и кладёт в библиотеку:
  ```text
  Фильмы/Total Recall (1990)/Total Recall (1990).mkv
  Сериалы/Breaking Bad/Сезон 2/…
  Музыка/…  Книги/…  Программы/…  Другое/…
  ```
  Переезд идёт через libtorrent (`move_storage`, `rename_file`), поэтому раздача не прерывается. Пустые папки после переезда подчищаются.
- **Журнал и откат.** Каждая раскладка записана в журнал (`kachalka moves`), `kachalka undo` возвращает файлы на место со старыми именами.
- **Поправки.** `kachalka sort <id> --title "Вспомнить всё" --year 1990` перекладывает под нужным названием, `--dry-run` только показывает план.
- **Выбор серий.** `kachalka add <magnet> --episodes 4,5` качает только эти серии. У magnet выбор применяется, как только придут метаданные. Потом `kachalka select <id> --episodes 6 --add` докачивает ещё, `--remove` снимает, `--all` возвращает всё. Понимает `4,5`, `4-6`, `S02E04`, `2x4`, «Серия 7», «04 серия».

Отключить автораскладку можно командой `kachalka set auto_sort false`. Папки переименовываются через настройку `library` в `config.json`, корень библиотеки задаёт `library_dir` (по умолчанию папка загрузок).

## Этап 3: просмотр

- **Что посмотрено.** Качалка сама собирает сведения из нескольких источников:
  - VLC хранит в `org.videolan.vlc.plist` позицию остановки для каждого файла. Досмотрено, если позиция дальше 90% или до конца меньше 5 минут (титры). Длительность берётся из Spotlight или `ffprobe`.
  - Для любого плеера (VLC, IINA, QuickTime, Elmedia, mpv, Infuse) раз в 15 секунд проверяется, какой файл библиотеки открыт, и копится время просмотра.
  - Spotlight отдаёт дату последнего открытия в Finder.
  - Отметка вручную: `kachalka watched <id> --episodes 4`.

  `kachalka watching` показывает, где ты остановился.
- **Режим «смотрю сериал».** `kachalka follow <id>` или сразу `kachalka add <magnet> --follow`. Качается текущая серия и ещё две за ней (`--ahead`), остальные ждут. Досмотрел серию, и окно сдвигается: подкачивается следующая. Просмотренная серия через 24 часа (`--keep-hours`) уходит в Корзину, раздача при этом перепроверяется и остаётся целой.
- **Чистка.** `kachalka cleanup` предлагает убрать раздачи с видео, досмотренные больше 3 дней назад (`cleanup_watched_days`), и скачанные, но не открытые 30 дней (`cleanup_unopened_days`). Убирает только в Корзину и только по команде: `kachalka cleanup --apply` или пункт «Можно освободить…» в меню значка. Когда появляются новые кандидаты, приходит уведомление.

## Дальше

- **Этап 4.** Окно со списком загрузок.

## Установка

Готовый пакет libtorrent для macOS есть на PyPI под Python 3.11–3.13. Проще всего взять Python 3.13 через [uv](https://docs.astral.sh/uv/):

```bash
git clone https://github.com/safronovib/kachalka.git
cd kachalka
V="$HOME/Library/Application Support/Kachalka/venv"
uv venv --python 3.13 "$V" && uv pip install --python "$V/bin/python" -r requirements.txt
PYTHONPATH="$PWD" "$V/bin/python" -m kachalka.cli install
```

`brew install libtorrent-rasterbar` тоже подойдёт, но на Intel-Маке Homebrew собирает его из исходников больше часа.

`install` запоминает Python, в котором есть libtorrent, и включает автозапуск (LaunchAgent). Если стоит SwiftBar, он кладёт ссылку на плагин в папку плагинов. Ещё он ставит навык Claude в `~/.claude/skills/kachalka` и команду `~/.local/bin/kachalka`. Ненужное отключается флагами `--no-swiftbar` и `--no-skill`. Всё это убирает `kachalka uninstall`.

## Команды

```text
kachalka add <magnet|файл.torrent|url> [--to папка] [--episodes 4,5]
kachalka files <id>                # файлы и серии раздачи
kachalka select <id> --episodes 6 [--add|--remove|--all]
kachalka sort <id|all> [--dry-run] [--title … --year … --as series]
kachalka undo <id|номер>           # откат раскладки
kachalka moves                     # журнал раскладок
kachalka watching [id]             # что посмотрено, на какой серии остановился
kachalka watched <id> [--episodes 4] [--unwatched]
kachalka follow <id> [--ahead 2] [--keep-hours 24] [--no-cleanup] [--from 4]
kachalka unfollow <id>
kachalka cleanup [--apply] [--ids a,b]
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

VPN-клиенты вроде AmneziaVPN и WireGuard заворачивают трафик двумя половинками маршрута (`0/1` и `128/1`) и оставляют маршрут по умолчанию на Wi-Fi. libtorrent смотрит именно на маршрут по умолчанию и шлёт пакеты с адреса Wi-Fi. С включённым kill switch такие пакеты режутся: magnet висит на «ищу метаданные», DHT пустой, трекеры отвечают «No route to host».

Поэтому по умолчанию (`vpn on`, настройка `bind_interface = auto`) Качалка сама находит туннель (`utunN`) и привязывается к нему. Если VPN переподключился и номер туннеля сменился, Качалка в течение 30 секунд перепривязывается сама. Если VPN выключен, она работает как обычно.

`kachalka vpn off` (или пункт меню «Пустить мимо VPN») пускает торренты напрямую через Wi-Fi или Ethernet, а весь остальной трафик идёт как прежде. При включённом kill switch так работать не будет, и Качалка об этом предупредит, если видит AmneziaVPN.

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
