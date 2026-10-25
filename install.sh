#!/usr/bin/env bash
# Установка site-guard.
#   sudo bash install.sh             — скопировать файлы и создать конфиг
#   sudo bash install.sh --enable    — проверить Telegram, сделать первый бэкап, включить таймеры
#   sudo bash install.sh --uninstall — выключить и удалить (конфиг и бэкапы остаются)

set -euo pipefail

APP_DIR=/opt/site-guard
CONF_DIR=/etc/site-guard
CONF="${CONF_DIR}/site-guard.conf"
UNITS=(site-guard-monitor.service site-guard-monitor.timer site-guard-backup.service site-guard-backup.timer)
SRC="$(cd "$(dirname "$0")" && pwd)"

die() { echo "Ошибка: $*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "запустите от root: sudo bash $0 $*"

state_dir() {
    # STATE_DIR берём из конфига, если он там задан
    local dir
    dir="$(bash -c 'source "$1" >/dev/null 2>&1; echo "${STATE_DIR:-/var/lib/site-guard}"' _ "$CONF")"
    echo "$dir"
}

install_files() {
    for cmd in python3 curl tar gzip sha256sum flock systemctl; do
        command -v "$cmd" >/dev/null || die "не найдена команда $cmd"
    done
    python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)' \
        || die "нужен Python 3.8 или новее"

    install -d -m 755 "$APP_DIR"
    install -m 755 "$SRC/monitor.py" "$SRC/backup.sh" "$APP_DIR/"
    install -d -m 700 "$CONF_DIR"
    if [[ -f "$CONF" ]]; then
        echo "Конфиг уже есть, не трогаю: $CONF"
    else
        install -m 600 "$SRC/site-guard.conf.example" "$CONF"
        echo "Создан конфиг: $CONF"
    fi
    for unit in "${UNITS[@]}"; do
        install -m 644 "$SRC/systemd/$unit" /etc/systemd/system/
    done
    systemctl daemon-reload

    cat <<EOF

Файлы установлены. Дальше:
  1. Заполните настройки:      nano $CONF
  2. Проверьте без отправки:   sudo python3 $APP_DIR/monitor.py --dry-run
  3. Включите:                 sudo bash $0 --enable
EOF
}

enable() {
    [[ -f "$APP_DIR/monitor.py" ]] || die "сначала выполните: sudo bash $0"
    echo "1/3 Тестовое сообщение в Telegram..."
    python3 "$APP_DIR/monitor.py" --config "$CONF" --test \
        || die "не удалось отправить сообщение — проверьте TG_BOT_TOKEN и TG_CHAT_ID"

    local dir
    dir="$(state_dir)"
    install -d -m 700 "$dir"
    [[ -f "$dir/installed_at" ]] || date +%s > "$dir/installed_at"

    echo "2/3 Первый бэкап (может занять несколько минут)..."
    systemctl start site-guard-backup.service \
        || die "бэкап не получился, смотрите: journalctl -u site-guard-backup -n 50"

    echo "3/3 Включаю таймеры..."
    systemctl enable --now site-guard-monitor.timer site-guard-backup.timer
    systemctl list-timers 'site-guard-*' --no-pager
    echo
    echo "Готово. Мониторинг каждые 5 минут, бэкап каждую ночь около 03:30."
}

uninstall() {
    systemctl disable --now site-guard-monitor.timer site-guard-backup.timer 2>/dev/null || true
    for unit in "${UNITS[@]}"; do
        rm -f "/etc/systemd/system/$unit"
    done
    systemctl daemon-reload
    rm -rf "$APP_DIR"
    echo "site-guard удалён. Конфиг ($CONF_DIR) и бэкапы оставлены — удалите вручную, если не нужны."
}

case "${1:-}" in
    "")          install_files ;;
    --enable)    enable ;;
    --uninstall) uninstall ;;
    *)           die "неизвестный параметр $1 (есть: --enable, --uninstall)" ;;
esac
