#!/usr/bin/env bash
# site-guard backup: дамп базы + архив файлов сайта, контрольные суммы, ротация,
# копия на удалённый сервер. При любой ошибке — сообщение в Telegram.
#
# Запуск: backup.sh [путь к конфигу]   (по умолчанию /etc/site-guard/site-guard.conf)

set -Eeuo pipefail

CONFIG="${1:-/etc/site-guard/site-guard.conf}"
# shellcheck source=/dev/null
source "$CONFIG"

BACKUP_DIR="${BACKUP_DIR:-/var/backups/site-guard}"
BACKUP_KEEP_DAYS="${BACKUP_KEEP_DAYS:-7}"
STATE_DIR="${STATE_DIR:-/var/lib/site-guard}"
DB_TYPE="${DB_TYPE:-none}"
DB_NAMES="${DB_NAMES:-}"
SITE_DIRS="${SITE_DIRS:-}"
REMOTE_TARGET="${REMOTE_TARGET:-}"

STEP="подготовка"
TARGET=""

notify() {
    local text="[${SERVER_NAME:-$(hostname)}] $1"
    if [[ -z "${TG_BOT_TOKEN:-}" || -z "${TG_CHAT_ID:-}" ]]; then
        echo "$text"
        return 0
    fi
    curl -fsS -m 15 -o /dev/null "https://api.telegram.org/bot${TG_BOT_TOKEN}/sendMessage" \
        --data-urlencode "chat_id=${TG_CHAT_ID}" \
        --data-urlencode "text=${text}" \
        --data-urlencode "disable_web_page_preview=true" \
        || echo "Не удалось отправить в Telegram: $text" >&2
}

on_error() {
    local code=$?
    trap - ERR
    # недоделанную копию удаляем, чтобы не принять её за рабочую
    if [[ -n "$TARGET" && -d "$TARGET" ]]; then
        rm -rf "$TARGET"
    fi
    notify "❌ Бэкап НЕ выполнен. Шаг: ${STEP} (код ${code}). Подробности: journalctl -u site-guard-backup"
    exit "$code"
}
trap on_error ERR

cd /
umask 077
mkdir -p "$BACKUP_DIR" "$STATE_DIR"

# не запускать два бэкапа одновременно
exec 9>"${STATE_DIR}/backup.lock"
if ! flock -n 9; then
    echo "Бэкап уже выполняется, выхожу."
    exit 0
fi

STEP="проверка настроек"
if [[ "$DB_TYPE" == "none" && -z "$SITE_DIRS" ]]; then
    echo "Нечего копировать: DB_TYPE=none и SITE_DIRS пуст." >&2
    false
fi

TARGET="${BACKUP_DIR}/$(date +%Y-%m-%d_%H-%M)"
mkdir -p "$TARGET"

# --- база данных ---
case "$DB_TYPE" in
    mysql)
        for db in $DB_NAMES; do
            STEP="дамп MySQL ${db}"
            mysqldump --single-transaction --quick --routines --triggers --events "$db" \
                | gzip > "${TARGET}/db_${db}.sql.gz"
        done
        ;;
    postgres)
        for db in $DB_NAMES; do
            STEP="дамп PostgreSQL ${db}"
            runuser -u postgres -- pg_dump "$db" | gzip > "${TARGET}/db_${db}.sql.gz"
        done
        ;;
    none)
        ;;
    *)
        STEP="проверка настроек"
        echo "Неизвестный DB_TYPE: ${DB_TYPE} (нужно mysql, postgres или none)" >&2
        false
        ;;
esac

# --- файлы сайтов ---
for dir in $SITE_DIRS; do
    STEP="архив ${dir}"
    name="${dir#/}"
    name="${name//\//_}"
    # код 1 у tar — «файл изменился во время чтения», для живого сайта это нормально
    rc=0
    tar --warning=no-file-changed -czf "${TARGET}/files_${name}.tar.gz" \
        -C "$(dirname "$dir")" "$(basename "$dir")" || rc=$?
    if (( rc > 1 )); then
        false
    fi
done

STEP="контрольные суммы"
cd "$TARGET"
sha256sum -- * > SHA256SUMS
cd /

# --- копия на другой сервер ---
if [[ -n "$REMOTE_TARGET" ]]; then
    STEP="копирование на ${REMOTE_TARGET}"
    rsync -a -e "ssh -o BatchMode=yes -o ConnectTimeout=20" "$TARGET" "${REMOTE_TARGET}/"
fi

# копия готова — дальше её уже не удаляем, даже если ротация упадёт
DONE="$TARGET"
TARGET=""
date +%s > "${STATE_DIR}/last_backup_ok"

STEP="удаление копий старше ${BACKUP_KEEP_DAYS} дн."
find "$BACKUP_DIR" -mindepth 1 -maxdepth 1 -type d -name '20??-??-??_??-??' \
    -mtime "+$((BACKUP_KEEP_DAYS - 1))" -exec rm -rf {} +

SIZE="$(du -sh "$DONE" | cut -f1)"
echo "Бэкап готов: ${DONE} (${SIZE})"
if [[ "${NOTIFY_BACKUP_OK:-no}" == "yes" ]]; then
    notify "✅ Бэкап готов: ${DONE} (${SIZE})"
fi
