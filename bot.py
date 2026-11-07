#!/usr/bin/env python3
"""Telegram-бот для управления ключами VLESS (Xray).

Только стандартная библиотека — никаких зависимостей.
Настройки читаются из .env рядом с bot.py.
Ключи сохраняются текстовыми файлами в data/keys/<имя>.txt
"""
import json
import os
import subprocess
import sys
import time
import urllib.parse
import urllib.request

BASE = os.path.dirname(os.path.abspath(__file__))
ENV_PATH = os.path.join(BASE, ".env")


def load_env(path):
    env = {}
    if not os.path.exists(path):
        return env
    with open(path, encoding="utf-8") as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip().strip('"').strip("'")
    return env


ENV = load_env(ENV_PATH)

TOKEN = ENV.get("BOT_TOKEN", "").strip()
ADMINS = set(
    int(x) for x in ENV.get("ADMIN_IDS", "").replace(" ", "").split(",")
    if x.strip().lstrip("-").isdigit()
)
XRAY = ENV.get("XRAY_CONFIG", "/usr/local/etc/xray/config.json")
SERVICE = ENV.get("XRAY_SERVICE", "xray")

_kd = ENV.get("KEYS_DIR", "data/keys")
KEYS_DIR = _kd if os.path.isabs(_kd) else os.path.join(BASE, _kd)

DOM = {
    "primary": ENV.get("DOMAIN_PRIMARY", ""),
    "alt": ENV.get("DOMAIN_ALT", ""),
    "moscow": ENV.get("DOMAIN_MOSCOW", ""),
}

WAITING = {}


def api(method, **params):
    url = "https://api.telegram.org/bot%s/%s" % (TOKEN, method)
    body = json.dumps(params).encode()
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=65) as r:
            return json.load(r)
        if "message is not modified" in body:
            return {"ok": True, "ignored": True}
        if "query is too old" in body:
            return {"ok": False, "ignored": True}
        print("api error %s: %s | %s" % (method, e, body), flush=True)
        return {"ok": False}


def xload():
    with open(XRAY, encoding="utf-8") as f:
        return json.load(f)


def xsave(cfg):
    with open(XRAY, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)
    subprocess.run(["systemctl", "restart", SERVICE], check=False)
    time.sleep(1)


def clients():
    return xload()["inbounds"][0]["settings"]["clients"]


def clean_name(s):
    s = (s or "").strip()
    for ch in '/\\:*?"<>|':
        s = s.replace(ch, "")
    return s[:40]


def links_for(uid, name):
    out = {}
    for key in ("primary", "alt", "moscow"):
        d = DOM[key]
        out[key] = (
            "vless://%s@%s:443?encryption=none&security=tls&sni=%s"
            "&fp=chrome&type=ws&host=%s&path=%%2Fws#%s"
            % (uid, d, d, d, urllib.parse.quote(name))
        )
    return out


def save_keys(name, l):
    os.makedirs(KEYS_DIR, exist_ok=True)
    p = os.path.join(KEYS_DIR, "%s.txt" % name)
    with open(p, "w", encoding="utf-8") as f:
        f.write("# %s\n\n" % name)
        f.write("# Прага (основной)\n%s\n\n" % l["primary"])
        f.write("# Прага (резервный IP)\n%s\n\n" % l["alt"])
        f.write("# Через Москву\n%s\n" % l["moscow"])
    return p


def remove_keys(name):
    p = os.path.join(KEYS_DIR, "%s.txt" % name)
    if os.path.exists(p):
        os.remove(p)


def fmt_links(name, l):
    return (
        "<b>%s</b>\n\n"
        "<b>Прага</b>\n<code>%s</code>\n\n"
        "<b>Прага-2</b>\n<code>%s</code>\n\n"
        "<b>Москва</b>\n<code>%s</code>"
        % (name, l["primary"], l["alt"], l["moscow"])
    )


def kb_menu():
    return {"inline_keyboard": [
        [{"text": "➕ Добавить клиента", "callback_data": "add"}],
        [{"text": "📋 Список клиентов", "callback_data": "list"}],
    ]}


def kb_list():
    rows = [[{"text": "👤 " + c.get("email", "?"), "callback_data": "c:" + c.get("email", "")}]
            for c in clients()]
    if not rows:
        rows = [[{"text": "(пусто)", "callback_data": "menu"}]]
    rows.append([{"text": "◀️ В меню", "callback_data": "menu"}])
    return {"inline_keyboard": rows}


def kb_client(name):
    return {"inline_keyboard": [
        [{"text": "🔑 Выдать новые ключи", "callback_data": "new:" + name}],
        [{"text": "✏️ Переименовать", "callback_data": "ren:" + name}],
        [{"text": "🗑 Удалить", "callback_data": "del:" + name}],
        [{"text": "◀️ К списку", "callback_data": "list"}],
    ]}


def kb_confirm(name):
    return {"inline_keyboard": [
        [{"text": "✅ Да, удалить", "callback_data": "delyes:" + name}],
        [{"text": "◀️ Отмена", "callback_data": "c:" + name}],
    ]}


def send(chat_id, text, keyboard=None):
    params = {"chat_id": chat_id, "text": text, "parse_mode": "HTML",
              "disable_web_page_preview": True}
    if keyboard:
        params["reply_markup"] = keyboard
    return api("sendMessage", **params)


def edit(chat_id, message_id, text, keyboard=None):
    params = {"chat_id": chat_id, "message_id": message_id, "text": text,
              "parse_mode": "HTML", "disable_web_page_preview": True}
    if keyboard:
        params["reply_markup"] = keyboard
    return api("editMessageText", **params)


def is_admin(uid):
    return (not ADMINS) or (uid in ADMINS)


def handle_message(msg):
    uid = msg["from"]["id"]
    chat = msg["chat"]["id"]
    text = (msg.get("text") or "").strip()

    if not is_admin(uid):
        send(chat, "Доступ закрыт. Ваш ID: <code>%d</code>" % uid)
        return

    if text in ("/start", "/menu"):
        WAITING.pop(uid, None)
        send(chat, "Управление ключами VPN\nВыберите действие:", kb_menu())
        return

    if text == "/id":
        send(chat, "Ваш ID: <code>%d</code>" % uid)
        return

    state = WAITING.pop(uid, None)
    if not state:
        send(chat, "Не понял. /start", kb_menu())
        return

    action, data = state

    if action == "add":
        name = clean_name(text)
        if not name:
            send(chat, "Недопустимое имя.")
            return
        cfg = xload()
        cl = cfg["inbounds"][0]["settings"]["clients"]
        if any(c.get("email") == name for c in cl):
            send(chat, "Клиент «%s» уже есть." % name)
            return
        new_id = subprocess.run(["xray", "uuid"], capture_output=True, text=True).stdout.strip()
        cl.append({"id": new_id, "email": name})
        xsave(cfg)
        l = links_for(new_id, name)
        path = save_keys(name, l)
        send(chat, "Ключи созданы и сохранены в <code>%s</code>\n\n%s" % (path, fmt_links(name, l)), kb_client(name))

    elif action == "rename":
        old = data
        new = clean_name(text)
        if not new:
            send(chat, "Недопустимое имя.")
            return
        cfg = xload()
        cl = cfg["inbounds"][0]["settings"]["clients"]
        if any(c.get("email") == new for c in cl):
            send(chat, "Имя «%s» занято." % new)
            return
        for c in cl:
            if c.get("email") == old:
                c["email"] = new
        xsave(cfg)
        old_file = os.path.join(KEYS_DIR, "%s.txt" % old)
        if os.path.exists(old_file):
            os.remove(old_file)
        for c in cl:
            if c.get("email") == new:
                save_keys(new, links_for(c["id"], new))
        send(chat, "Переименован: «%s» → «%s»" % (old, new), kb_client(new))


def handle_callback(cb):
    uid = cb["from"]["id"]
    chat = cb["message"]["chat"]["id"]
    mid = cb["message"]["message_id"]
    data = cb.get("data", "")
    api("answerCallbackQuery", callback_query_id=cb["id"])

    if not is_admin(uid):
        return

    if data == "menu":
        WAITING.pop(uid, None)
        edit(chat, mid, "Управление ключами VPN\nВыберите действие:", kb_menu())

    elif data == "list":
        edit(chat, mid, "<b>Клиенты</b>\nНажмите на имя для управления:", kb_list())

    elif data == "add":
        WAITING[uid] = ("add", None)
        edit(chat, mid, "Введите имя нового клиента (например: <i>телефон</i>):")

    elif data.startswith("c:"):
        name = data[2:]
        found = None
        for c in clients():
            if c.get("email") == name:
                found = c
        if not found:
            edit(chat, mid, "Клиент не найден.", kb_list())
            return
        l = links_for(found["id"], name)
        edit(chat, mid, fmt_links(name, l) + "\n\n<b>Управление:</b>", kb_client(name))

    elif data.startswith("new:"):
        name = data[4:]
        cfg = xload()
        cl = cfg["inbounds"][0]["settings"]["clients"]
        new_id = subprocess.run(["xray", "uuid"], capture_output=True, text=True).stdout.strip()
        ok = False
        for c in cl:
            if c.get("email") == name:
                c["id"] = new_id
                ok = True
        if not ok:
            edit(chat, mid, "Клиент не найден.", kb_list())
            return
        xsave(cfg)
        l = links_for(new_id, name)
        save_keys(name, l)
        edit(chat, mid, "🔑 Старые ключи отозваны.\n\n" + fmt_links(name, l), kb_client(name))

    elif data.startswith("ren:"):
        name = data[4:]
        WAITING[uid] = ("rename", name)
        edit(chat, mid, "Введите новое имя для «%s»:" % name, kb_client(name))

    elif data.startswith("del:"):
        name = data[4:]
        edit(chat, mid, "Удалить клиента «%s»? Доступ будет отозван." % name, kb_confirm(name))

    elif data.startswith("delyes:"):
        name = data[7:]
        cfg = xload()
        cfg["inbounds"][0]["settings"]["clients"] = [
            c for c in cfg["inbounds"][0]["settings"]["clients"] if c.get("email") != name
        ]
        xsave(cfg)
        remove_keys(name)
        edit(chat, mid, "🗑 Удалён: «%s»" % name, kb_list())


def main():
    if not TOKEN or ":" not in TOKEN:
        print("ОШИБКА: в .env не задан BOT_TOKEN", flush=True)
        sys.exit(1)
    print("bot started", flush=True)
    offset = None
    while True:
        params = {"timeout": 50}
        if offset is not None:
            params["offset"] = offset
        res = api("getUpdates", **params)
        if not res.get("ok"):
            time.sleep(3)
            continue
        for upd in res.get("result", []):
            offset = upd["update_id"] + 1
            try:
                if "message" in upd:
                    handle_message(upd["message"])
                elif "callback_query" in upd:
                    handle_callback(upd["callback_query"])
            except Exception as e:
                print("handler error: %s" % e, flush=True)


if __name__ == "__main__":
    main()