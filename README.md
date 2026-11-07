# VPN Key Bot

Telegram-бот для управления ключами VLESS + WebSocket + TLS (Xray).
Только стандартная библиотека Python — зависимостей нет.

## Что умеет

- **Добавить клиента** — спрашивает имя, создаёт UUID, выдаёт три ссылки
- **Список клиентов** — кнопки с именами
- **Карточка клиента** — новые ключи, переименование, удаление
- Ключи сохраняются текстовыми файлами в `data/keys/<имя>.txt`
- Доступ только для указанных Telegram ID

## Схема

```
Клиент -> Москва:443 (проброс TCP) -> Прага:443 (TLS) -> интернет
        -> Прага:443 напрямую
        -> Прага:443 (второй IP)
```

Три точки входа, один ключ. Выход всегда в Праге.

## Установка на сервер

```bash
git clone <repo> /tmp/vpnbot && cd /tmp/vpnbot
bash install.sh
nano /opt/vpnbot/config.json     # token + admins
systemctl restart vpnbot
journalctl -u vpnbot -f
```

Конфиг бота:

```json
{
  "token": "123456:ABC-DEF...",
  "admins": [123456789],
  "xray_config": "/usr/local/etc/xray/config.json",
  "xray_service": "xray",
  "domains": {
    "primary": "195-110-217-179.sslip.io",
    "alt": "91-184-250-218.sslip.io",
    "moscow": "45-154-185-199.sslip.io"
  }
}
```

`admins` — список Telegram ID, кому разрешено. Пустой список = доступ всем (не оставлять так).
Свой ID бот покажет командой `/id`.

## Требования

- Python 3.8+ (стандартная библиотека)
- `xray` в PATH, конфиг в формате VLESS+WS+TLS, клиенты в `inbounds[0].settings.clients`
- systemd
- права root (перезапускает `xray` через systemctl)

## Безопасность

- Бот не слушает порты — работает через long polling, входящие соединения не нужны
- Токен и `config.json` не коммитятся (см. `.gitignore`)
- Список `admins` обязателен в проде