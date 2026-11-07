#!/bin/bash
# Установка бота на сервер. Запускать от root из корня репозитория.
set -e
DEST=/opt/vpnbot

echo "==> каталог $DEST"
mkdir -p "$DEST/data/keys"
cp bot.py config.example.json "$DEST/"
[ -f "$DEST/config.json" ] || cp config.example.json "$DEST/config.json"

echo "==> служба"
cp vpnbot.service /etc/systemd/system/vpnbot.service
systemctl daemon-reload
systemctl enable vpnbot

echo "==> проверка зависимостей"
command -v xray >/dev/null || echo "ВНИМАНИЕ: xray не найден"
command -v python3 >/dev/null || { echo "python3 не найден"; exit 1; }

echo ""
echo "Готово. Дальше:"
echo "  1) nano $DEST/config.json  — вписать токен и ваш telegram id"
echo "  2) systemctl restart vpnbot"
echo "  3) journalctl -u vpnbot -f"