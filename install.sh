#!/bin/bash
# Установка бота на сервер. Запускать от root из корня репозитория.
set -e
DEST=/opt/vpnbot

echo "==> каталог $DEST"
mkdir -p "$DEST/data/keys"
cp bot.py .env.example "$DEST/"

if [ ! -f "$DEST/.env" ]; then
    cp .env.example "$DEST/.env"
    chmod 600 "$DEST/.env"
    echo "    создан $DEST/.env — заполните BOT_TOKEN и ADMIN_IDS"
else
    echo "    $DEST/.env уже существует, не трогаю"
fi

echo "==> служба"
cp vpnbot.service /etc/systemd/system/vpnbot.service
systemctl daemon-reload
systemctl enable vpnbot

echo "==> проверки"
command -v python3 >/dev/null || { echo "python3 не найден"; exit 1; }
command -v xray >/dev/null || echo "ВНИМАНИЕ: xray не найден в PATH"
python3 -m py_compile "$DEST/bot.py" && echo "    синтаксис в порядке"

echo ""
echo "Готово. Дальше:"
echo "  1) nano $DEST/.env      — вписать BOT_TOKEN и ADMIN_IDS"
echo "  2) systemctl restart vpnbot"
echo "  3) journalctl -u vpnbot -f"