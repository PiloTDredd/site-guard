# site-guard

Ежедневные бэкапы сайта и мониторинг с уведомлениями в Telegram для Linux-сервера (Ubuntu/Debian).
Без внешних сервисов и платных подписок: Python 3 из стандартной поставки, bash и systemd.

## Что делает

**Бэкап — каждую ночь (≈03:30):**
- дамп базы MySQL/MariaDB или PostgreSQL;
- архив папок сайта;
- контрольные суммы `SHA256SUMS`, чтобы проверить, что архив не повреждён;
- хранит копии N дней, старые удаляет сам;
- по желанию — копия на второй сервер через rsync/ssh;
- если бэкап не получился — сообщение в Telegram с шагом, на котором упало.

**Мониторинг — каждые 5 минут:**
- сайт открывается и отвечает кодом < 400;
- SSL-сертификат не истекает в ближайшие дни;
- на диске есть место;
- последний успешный бэкап не старше суток.

Сообщения приходят только при изменении состояния — без спама:

```
[my-vps] 🔴 Сайт недоступен: https://example.com
Причина: HTTP 502

[my-vps] ✅ Сайт снова работает: https://example.com
HTTP 200, 0.4 с. Был недоступен ~15 мин.

[my-vps] ⚠️ SSL-сертификат https://example.com истекает через 6 дн.
[my-vps] 💾 Диск / заполнен на 91% (свободно 3.2 ГБ).
[my-vps] ❌ Бэкап НЕ выполнен. Шаг: дамп MySQL shop (код 2).
```

## Установка

Требования: Ubuntu 20.04+ / Debian 11+, root, `python3`, `curl` (обычно уже есть).
Для копии на другой сервер — `rsync` и вход по SSH-ключу.

```bash
git clone https://github.com/PiloTDredd/site-guard.git
cd site-guard
sudo bash install.sh
```

### 1. Telegram-бот

1. Напишите [@BotFather](https://t.me/BotFather) команду `/newbot`, получите токен.
2. Напишите своему боту любое сообщение.
3. Откройте `https://api.telegram.org/bot<ТОКЕН>/getUpdates` — в ответе найдите `"chat":{"id": ...}`. Это `TG_CHAT_ID`.
   Для группы: добавьте бота в группу, напишите в ней сообщение, ID будет отрицательным числом.

### 2. Настройки

```bash
sudo nano /etc/site-guard/site-guard.conf
```

Минимум: `TG_BOT_TOKEN`, `TG_CHAT_ID`, `SITES`, `SITE_DIRS`, `DB_TYPE`, `DB_NAMES`.
Каждый параметр описан в самом файле.

Доступ к базе:
- **MySQL/MariaDB** — на Ubuntu root обычно входит через сокет без пароля. Если нет — создайте `/root/.my.cnf`:
  ```ini
  [client]
  user=backup
  password=СЕКРЕТ
  ```
  и выполните `chmod 600 /root/.my.cnf`.
- **PostgreSQL** — дамп делается от системного пользователя `postgres`, пароль не нужен.

### 3. Проверка и запуск

```bash
sudo python3 /opt/site-guard/monitor.py --dry-run   # покажет, что бы отправил, ничего не шлёт
sudo bash install.sh --enable                        # тест Telegram + первый бэкап + включение таймеров
```

Проверить, что таймеры работают:

```bash
systemctl list-timers 'site-guard-*'
journalctl -u site-guard-backup -n 30
journalctl -u site-guard-monitor -n 30
ls -lh /var/backups/site-guard/
```

## Восстановление из бэкапа

Копии лежат в `/var/backups/site-guard/ГГГГ-ММ-ДД_ЧЧ-ММ/`.

```bash
cd /var/backups/site-guard/2026-10-25_03-30
sha256sum -c SHA256SUMS                      # архивы целы?

# база MySQL
gunzip < db_shop.sql.gz | mysql shop

# база PostgreSQL
gunzip < db_shop.sql.gz | sudo -u postgres psql shop

# файлы: сначала распакуйте во временную папку и сравните
mkdir /tmp/restore && tar -xzf files_var_www_example.com.tar.gz -C /tmp/restore
# затем замените нужные файлы в /var/www/example.com
```

Совет: раз в месяц восстанавливайте копию на тестовый сервер. Бэкап, из которого ни разу не восстанавливали, — это надежда, а не бэкап.

## Удаление

```bash
sudo bash install.sh --uninstall
```

Конфиг `/etc/site-guard` и бэкапы `/var/backups/site-guard` остаются — удалите вручную, если не нужны.

## Устройство

| Файл | Что делает |
|---|---|
| `monitor.py` | Проверки сайта, SSL, диска, свежести бэкапа. Состояние — в `/var/lib/site-guard/state.json` |
| `backup.sh` | Дамп БД, архив файлов, контрольные суммы, ротация, rsync |
| `systemd/*.timer` | Расписание: мониторинг раз в 5 минут, бэкап в 03:30 |
| `install.sh` | Установка, включение, удаление |

## Безопасность

- Конфиг с токеном доступен только root (`chmod 600`).
- Бэкапы создаются с правами `700/600` — в них данные пользователей сайта.
- Копия на том же сервере не спасёт, если сервер умрёт целиком. Используйте `REMOTE_TARGET`.
