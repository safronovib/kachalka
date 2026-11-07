#!/bin/bash
# <swiftbar.title>Качалка</swiftbar.title>
# <swiftbar.hideAbout>true</swiftbar.hideAbout>
# <swiftbar.hideRunInTerminal>true</swiftbar.hideRunInTerminal>
# <swiftbar.hideLastUpdated>true</swiftbar.hideLastUpdated>
# <swiftbar.hideDisablePlugin>true</swiftbar.hideDisablePlugin>
# <swiftbar.hideSwiftBar>true</swiftbar.hideSwiftBar>
#
# Загрузки в строке меню: процент и время до конца, по нажатию подробности и управление.
# Ничего не качается: нет вывода и нет значка.
SELF="$(readlink -f "$0")"
exec "$(dirname "$SELF")/../bin/kachalka" swiftbar 2>/dev/null
