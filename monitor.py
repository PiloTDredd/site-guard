#!/usr/bin/env python3
"""site-guard monitor: проверяет сайты, SSL, диск и свежесть бэкапа, шлёт уведомления в Telegram.

Запускается по таймеру раз в несколько минут. Между запусками хранит состояние в STATE_DIR,
чтобы присылать сообщение только при изменении («упал» / «снова работает»), а не каждый раз.

Только стандартная библиотека Python 3.8+, ничего ставить не нужно.
"""

import argparse
import json
import os
import shlex
import shutil
import socket
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_CONFIG = "/etc/site-guard/site-guard.conf"
SSL_REPEAT_SECONDS = 24 * 3600     # напоминать про сертификат не чаще раза в сутки
DISK_REPEAT_SECONDS = 12 * 3600    # и про диск — не чаще раза в 12 часов
BACKUP_REPEAT_SECONDS = 12 * 3600
USER_AGENT = "site-guard/1.0 (+uptime check)"


# --- конфиг и состояние -----------------------------------------------------

def load_config(path):
    """Читает файл вида KEY="value" (тот же, что подключает backup.sh)."""
    config = {}
    with open(path, encoding="utf-8-sig") as f:  # -sig: файл мог быть сохранён в Windows с BOM
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            parts = shlex.split(value, comments=True)
            config[key.strip()] = " ".join(parts)
    return config


def get_int(config, key, default):
    try:
        return int(config.get(key, default))
    except ValueError:
        return default


def load_state(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(path, state):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def read_timestamp(path):
    try:
        with open(path, encoding="utf-8") as f:
            return int(f.read().strip())
    except (FileNotFoundError, ValueError):
        return None


# --- уведомления ------------------------------------------------------------

class Notifier:
    def __init__(self, config, dry_run):
        self.token = config.get("TG_BOT_TOKEN", "")
        self.chat_id = config.get("TG_CHAT_ID", "")
        self.server = config.get("SERVER_NAME", socket.gethostname())
        self.dry_run = dry_run

    def send(self, text):
        message = f"[{self.server}] {text}"
        if self.dry_run:
            print(f"[dry-run] {message}")
            return True
        if not self.token or not self.chat_id:
            print("TG_BOT_TOKEN / TG_CHAT_ID не заданы, сообщение не отправлено:", message, file=sys.stderr)
            return False
        url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        data = urllib.parse.urlencode({
            "chat_id": self.chat_id,
            "text": message,
            "disable_web_page_preview": "true",
        }).encode()
        try:
            with urllib.request.urlopen(url, data=data, timeout=15) as resp:
                return resp.status == 200
        except (urllib.error.URLError, OSError) as e:
            print(f"Не удалось отправить в Telegram: {e}", file=sys.stderr)
            return False


# --- проверки ---------------------------------------------------------------

def check_http(url, timeout):
    """Возвращает (ok, описание): ok=True, если сайт ответил кодом < 400."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    started = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            elapsed = time.monotonic() - started
            return True, f"HTTP {resp.status}, {elapsed:.1f} с"
    except urllib.error.HTTPError as e:
        return False, f"HTTP {e.code}"
    except urllib.error.URLError as e:
        reason = e.reason
        if isinstance(reason, ssl.SSLCertVerificationError):
            return False, f"ошибка SSL-сертификата: {reason.verify_message}"
        if isinstance(reason, socket.timeout):
            return False, f"нет ответа за {timeout} с"
        if isinstance(reason, socket.gaierror):
            return False, "домен не найден (DNS)"
        return False, f"нет соединения: {reason}"
    except (socket.timeout, TimeoutError):
        return False, f"нет ответа за {timeout} с"
    except OSError as e:
        return False, f"ошибка соединения: {e}"


def ssl_days_left(url, timeout):
    """Сколько дней до окончания сертификата. None — не https или не удалось проверить."""
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname:
        return None
    host, port = parsed.hostname, parsed.port or 443
    context = ssl.create_default_context()
    try:
        with socket.create_connection((host, port), timeout=timeout) as sock:
            with context.wrap_socket(sock, server_hostname=host) as tls:
                cert = tls.getpeercert()
    except (OSError, ssl.SSLError):
        return None  # недоступность или битый сертификат уже покажет check_http
    expires = ssl.cert_time_to_seconds(cert["notAfter"])
    return int((expires - time.time()) // 86400)


def format_duration(seconds):
    minutes = int(seconds // 60)
    if minutes < 60:
        return f"{minutes} мин"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours} ч {minutes} мин"
    days, hours = divmod(hours, 24)
    return f"{days} д {hours} ч"


def monitor_sites(config, state, notify, now):
    timeout = get_int(config, "HTTP_TIMEOUT", 15)
    fails_needed = max(1, get_int(config, "FAILS_BEFORE_ALERT", 2))
    warn_days = get_int(config, "SSL_WARN_DAYS", 14)
    sites_state = state.setdefault("sites", {})
    ssl_state = state.setdefault("ssl_warned", {})

    for url in config.get("SITES", "").split():
        s = sites_state.setdefault(url, {"fails": 0, "down": False})
        ok, detail = check_http(url, timeout)

        if ok:
            if s.get("down"):
                downtime = format_duration(now - s.get("down_since", now))
                notify.send(f"✅ Сайт снова работает: {url}\n{detail}. Был недоступен ~{downtime}.")
            sites_state[url] = {"fails": 0, "down": False}
        else:
            s["fails"] = s.get("fails", 0) + 1
            s.setdefault("first_fail", now)
            if not s.get("down") and s["fails"] >= fails_needed:
                s["down"] = True
                s["down_since"] = s["first_fail"]
                notify.send(f"🔴 Сайт недоступен: {url}\nПричина: {detail}")

        days = ssl_days_left(url, timeout) if ok else None
        if days is not None and days <= warn_days:
            last = ssl_state.get(url, 0)
            if now - last >= SSL_REPEAT_SECONDS:
                notify.send(f"⚠️ SSL-сертификат {url} истекает через {days} дн. Продлите, иначе браузеры покажут «Небезопасно».")
                ssl_state[url] = now
        elif days is not None:
            ssl_state.pop(url, None)


def monitor_disks(config, state, notify, now):
    limit = get_int(config, "DISK_WARN_PERCENT", 90)
    disk_state = state.setdefault("disk", {})
    for path in config.get("DISK_PATHS", "/").split():
        try:
            usage = shutil.disk_usage(path)
        except OSError as e:
            print(f"Не удалось проверить диск {path}: {e}", file=sys.stderr)
            continue
        percent = usage.used * 100 // usage.total
        free_gb = usage.free / 1024 ** 3
        d = disk_state.setdefault(path, {"alert": False, "last": 0})
        if percent >= limit:
            if not d["alert"] or now - d["last"] >= DISK_REPEAT_SECONDS:
                notify.send(f"💾 Диск {path} заполнен на {percent}% (свободно {free_gb:.1f} ГБ).")
                d.update(alert=True, last=now)
        elif d["alert"]:
            notify.send(f"✅ Диск {path} в норме: заполнен на {percent}%.")
            d.update(alert=False, last=now)


def monitor_backup(config, state, notify, now, state_dir):
    max_age_hours = get_int(config, "BACKUP_MAX_AGE_HOURS", 26)
    if max_age_hours <= 0:
        return
    last_ok = read_timestamp(os.path.join(state_dir, "last_backup_ok"))
    installed = read_timestamp(os.path.join(state_dir, "installed_at"))
    reference = last_ok or installed
    if reference is None:
        return  # бэкап ещё ни разу не запускался и дата установки неизвестна
    age = now - reference
    if age < max_age_hours * 3600:
        state.pop("backup_alert_ts", None)
        return
    if now - state.get("backup_alert_ts", 0) >= BACKUP_REPEAT_SECONDS:
        if last_ok:
            text = f"🗄 Последний успешный бэкап был {format_duration(age)} назад. Проверьте: journalctl -u site-guard-backup"
        else:
            text = "🗄 С момента установки не было ни одного успешного бэкапа. Проверьте: journalctl -u site-guard-backup"
        notify.send(text)
        state["backup_alert_ts"] = now


# --- запуск -----------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Мониторинг сайтов, SSL, диска и бэкапов с уведомлениями в Telegram")
    parser.add_argument("--config", default=DEFAULT_CONFIG, help=f"путь к конфигу (по умолчанию {DEFAULT_CONFIG})")
    parser.add_argument("--dry-run", action="store_true", help="печатать сообщения вместо отправки в Telegram")
    parser.add_argument("--test", action="store_true", help="отправить тестовое сообщение и выйти")
    args = parser.parse_args()

    config = load_config(args.config)
    notify = Notifier(config, args.dry_run)

    if args.test:
        ok = notify.send("👋 site-guard на связи. Уведомления настроены правильно.")
        print("Отправлено" if ok else "Не отправлено, см. ошибку выше")
        return 0 if ok else 1

    state_dir = config.get("STATE_DIR", "/var/lib/site-guard")
    os.makedirs(state_dir, exist_ok=True)
    state_path = os.path.join(state_dir, "state.json")
    state = load_state(state_path)
    now = int(time.time())

    monitor_sites(config, state, notify, now)
    monitor_disks(config, state, notify, now)
    monitor_backup(config, state, notify, now, state_dir)

    save_state(state_path, state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
