# getNewKeyBot

Telegram-бот для выдачи и отзыва ключей **VLESS + WebSocket + TLS** (Xray).
Только стандартная библиотека Python — зависимостей нет.

## Возможности

- **Добавить клиента** — спрашивает имя, генерирует UUID, выдаёт три ссылки
- **Список клиентов** — кнопки с именами
- **Карточка клиента** — новые ключи, переименование, удаление (с подтверждением)
- Ключи сохраняются текстовыми файлами: `data/keys/<имя>.txt`
- Доступ только для указанных Telegram ID
- Работает через long polling: входящие порты не нужны, боту всё равно, заблокирован ли IP сервера

## Схема

```
Клиент ──> Москва:443 (проброс TCP) ──> Прага:443 (TLS) ──> интернет
       ──> Прага:443 напрямую
       ──> Прага:443 (второй IP)
```

Три точки входа, один ключ. Выход всегда в Праге.
Сертификат на московский домен лежит на Праге, поэтому TLS сквозной — Москва видит только шифрованный поток.

## Установка

```bash
git clone git@github.com:andrZUEV/getNewKeyBot.git /tmp/vpnbot
cd /tmp/vpnbot
bash install.sh
nano /opt/vpnbot/.env
systemctl restart vpnbot
journalctl -u vpnbot -f
```

## Конфигурация

Всё в файле `.env` рядом с `bot.py`. Шаблон — `.env.example`.

| Переменная | Назначение |
|---|---|
| `BOT_TOKEN` | токен от [@BotFather](https://t.me/BotFather) |
| `ADMIN_IDS` | Telegram ID через запятую. Пусто = доступ всем (не для прода) |
| `XRAY_CONFIG` | путь к конфигу Xray, по умолчанию `/usr/local/etc/xray/config.json` |
| `XRAY_SERVICE` | имя systemd-юнита Xray, по умолчанию `xray` |
| `KEYS_DIR` | каталог для файлов с ключами, относительно каталога бота |
| `DOMAIN_PRIMARY` | основной адрес (Прага) |
| `DOMAIN_ALT` | резервный адрес (Прага, второй IP) |
| `DOMAIN_MOSCOW` | адрес входа через Москву |

Свой Telegram ID можно узнать у [@userinfobot](https://t.me/userinfobot) или командой `/id` в боте.

## Требования

- Python 3.8+ (стандартная библиотека)
- `xray` в PATH, конфиг VLESS+WS+TLS, клиенты в `inbounds[0].settings.clients`
- systemd, права root (бот перезапускает службу `xray`)

## Файлы

```
bot.py             — бот
.env.example       — шаблон конфигурации (публикуется)
.env               — реальные секреты (в .gitignore)
vpnbot.service     — systemd-юнит
install.sh         — установка
data/keys/         — текстовые файлы с ключами (в .gitignore)
```

## Безопасность

- `.env` не попадает в git (`.gitignore`)
- Бот не открывает портов — только исходящие запросы к Telegram
- Список `ADMIN_IDS` обязателен в проде