#!/usr/bin/env python3
"""Telegram-Р±РѕС‚ РґР»СЏ СѓРїСЂР°РІР»РµРЅРёСЏ РєР»СЋС‡Р°РјРё VLESS (Xray).

РўРѕР»СЊРєРѕ СЃС‚Р°РЅРґР°СЂС‚РЅР°СЏ Р±РёР±Р»РёРѕС‚РµРєР° вЂ” РЅРёРєР°РєРёС… Р·Р°РІРёСЃРёРјРѕСЃС‚РµР№.
РљР»СЋС‡Рё С…СЂР°РЅСЏС‚СЃСЏ С‚РµРєСЃС‚РѕРІС‹РјРё С„Р°Р№Р»Р°РјРё РІ data/keys/<РёРјСЏ>.txt
"""
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

BASE = os.path.dirname(os.path.abspath(__file__))
ENV_PATH = os.path.join(BASE, ".env")


def load_env(path):
    """Минимальный парсер .env: KEY=VALUE, комментарии через #."""
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

WAITING = {}  # user_id -> (action, data)


def api(method, **params):
    url = "https://api.telegram.org/bot%s/%s" % (TOKEN, method)
    body = json.dumps(params).encode()
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=65) as r:
            return json.load(r)
    except Exception as e:
        print("api error %s: %s" % (method, e), flush=True)
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
    bad = '/\\:*?"<>|'
    for ch in bad:
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
        f.write("# РџСЂР°РіР° (РѕСЃРЅРѕРІРЅРѕР№)\n%s\n\n" % l["primary"])
        f.write("# РџСЂР°РіР° (СЂРµР·РµСЂРІРЅС‹Р№ IP)\n%s\n\n" % l["alt"])
        f.write("# Р§РµСЂРµР· РњРѕСЃРєРІСѓ\n%s\n" % l["moscow"])
    return p


def remove_keys(name):
    p = os.path.join(KEYS_DIR, "%s.txt" % name)
    if os.path.exists(p):
        os.remove(p)


def fmt_links(name, l):
    return (
        "<b>%s</b>\n\n"
        "<b>РџСЂР°РіР°</b>\n<code>%s</code>\n\n"
        "<b>РџСЂР°РіР°-2</b>\n<code>%s</code>\n\n"
        "<b>РњРѕСЃРєРІР°</b>\n<code>%s</code>"
        % (name, l["primary"], l["alt"], l["moscow"])
    )


def kb_menu():
    return {"inline_keyboard": [
        [{"text": "вћ• Р”РѕР±Р°РІРёС‚СЊ РєР»РёРµРЅС‚Р°", "callback_data": "add"}],
        [{"text": "рџ“‹ РЎРїРёСЃРѕРє РєР»РёРµРЅС‚РѕРІ", "callback_data": "list"}],
    ]}


def kb_list():
    rows = [[{"text": "рџ‘¤ " + c.get("email", "?"), "callback_data": "c:" + c.get("email", "")}]
            for c in clients()]
    if not rows:
        rows = [[{"text": "(РїСѓСЃС‚Рѕ)", "callback_data": "menu"}]]
    rows.append([{"text": "в—ЂпёЏ Р’ РјРµРЅСЋ", "callback_data": "menu"}])
    return {"inline_keyboard": rows}


def kb_client(name):
    return {"inline_keyboard": [
        [{"text": "рџ”‘ Р’С‹РґР°С‚СЊ РЅРѕРІС‹Рµ РєР»СЋС‡Рё", "callback_data": "new:" + name}],
        [{"text": "вњЏпёЏ РџРµСЂРµРёРјРµРЅРѕРІР°С‚СЊ", "callback_data": "ren:" + name}],
        [{"text": "рџ—‘ РЈРґР°Р»РёС‚СЊ", "callback_data": "del:" + name}],
        [{"text": "в—ЂпёЏ Рљ СЃРїРёСЃРєСѓ", "callback_data": "list"}],
    ]}


def kb_confirm(name):
    return {"inline_keyboard": [
        [{"text": "вњ… Р”Р°, СѓРґР°Р»РёС‚СЊ", "callback_data": "delyes:" + name}],
        [{"text": "в—ЂпёЏ РћС‚РјРµРЅР°", "callback_data": "c:" + name}],
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
        send(chat, "Р”РѕСЃС‚СѓРї Р·Р°РєСЂС‹С‚. Р’Р°С€ ID: <code>%d</code>" % uid)
        return

    if text in ("/start", "/menu"):
        WAITING.pop(uid, None)
        send(chat, "РЈРїСЂР°РІР»РµРЅРёРµ РєР»СЋС‡Р°РјРё VPN\nР’С‹Р±РµСЂРёС‚Рµ РґРµР№СЃС‚РІРёРµ:", kb_menu())
        return

    if text == "/id":
        send(chat, "Р’Р°С€ ID: <code>%d</code>" % uid)
        return

    state = WAITING.pop(uid, None)
    if not state:
        send(chat, "РќРµ РїРѕРЅСЏР». /start", kb_menu())
        return

    action, data = state

    if action == "add":
        name = clean_name(text)
        if not name:
            send(chat, "РќРµРґРѕРїСѓСЃС‚РёРјРѕРµ РёРјСЏ.")
            return
        cfg = xload()
        cl = cfg["inbounds"][0]["settings"]["clients"]
        if any(c.get("email") == name for c in cl):
            send(chat, "РљР»РёРµРЅС‚ В«%sВ» СѓР¶Рµ РµСЃС‚СЊ." % name)
            return
        new_id = subprocess.run(["xray", "uuid"], capture_output=True, text=True).stdout.strip()
        cl.append({"id": new_id, "email": name})
        xsave(cfg)
        l = links_for(new_id, name)
        path = save_keys(name, l)
        send(chat, "РљР»СЋС‡Рё СЃРѕР·РґР°РЅС‹ Рё СЃРѕС…СЂР°РЅРµРЅС‹ РІ <code>%s</code>\n\n%s" % (path, fmt_links(name, l)), kb_client(name))

    elif action == "rename":
        old = data
        new = clean_name(text)
        if not new:
            send(chat, "РќРµРґРѕРїСѓСЃС‚РёРјРѕРµ РёРјСЏ.")
            return
        cfg = xload()
        cl = cfg["inbounds"][0]["settings"]["clients"]
        if any(c.get("email") == new for c in cl):
            send(chat, "РРјСЏ В«%sВ» Р·Р°РЅСЏС‚Рѕ." % new)
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
        send(chat, "РџРµСЂРµРёРјРµРЅРѕРІР°РЅ: В«%sВ» в†’ В«%sВ»" % (old, new), kb_client(new))


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
        edit(chat, mid, "РЈРїСЂР°РІР»РµРЅРёРµ РєР»СЋС‡Р°РјРё VPN\nР’С‹Р±РµСЂРёС‚Рµ РґРµР№СЃС‚РІРёРµ:", kb_menu())

    elif data == "list":
        edit(chat, mid, "<b>РљР»РёРµРЅС‚С‹</b>\nРќР°Р¶РјРёС‚Рµ РЅР° РёРјСЏ РґР»СЏ СѓРїСЂР°РІР»РµРЅРёСЏ:", kb_list())

    elif data == "add":
        WAITING[uid] = ("add", None)
        edit(chat, mid, "Р’РІРµРґРёС‚Рµ РёРјСЏ РЅРѕРІРѕРіРѕ РєР»РёРµРЅС‚Р° (РЅР°РїСЂРёРјРµСЂ: <i>С‚РµР»РµС„РѕРЅ</i>):")

    elif data.startswith("c:"):
        name = data[2:]
        found = None
        for c in clients():
            if c.get("email") == name:
                found = c
        if not found:
            edit(chat, mid, "РљР»РёРµРЅС‚ РЅРµ РЅР°Р№РґРµРЅ.", kb_list())
            return
        l = links_for(found["id"], name)
        edit(chat, mid, fmt_links(name, l) + "\n\n<b>РЈРїСЂР°РІР»РµРЅРёРµ:</b>", kb_client(name))

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
            edit(chat, mid, "РљР»РёРµРЅС‚ РЅРµ РЅР°Р№РґРµРЅ.", kb_list())
            return
        xsave(cfg)
        l = links_for(new_id, name)
        save_keys(name, l)
        edit(chat, mid, "рџ”‘ РЎС‚Р°СЂС‹Рµ РєР»СЋС‡Рё РѕС‚РѕР·РІР°РЅС‹.\n\n" + fmt_links(name, l), kb_client(name))

    elif data.startswith("ren:"):
        name = data[4:]
        WAITING[uid] = ("rename", name)
        edit(chat, mid, "Р’РІРµРґРёС‚Рµ РЅРѕРІРѕРµ РёРјСЏ РґР»СЏ В«%sВ»:" % name, kb_client(name))

    elif data.startswith("del:"):
        name = data[4:]
        edit(chat, mid, "РЈРґР°Р»РёС‚СЊ РєР»РёРµРЅС‚Р° В«%sВ»? Р”РѕСЃС‚СѓРї Р±СѓРґРµС‚ РѕС‚РѕР·РІР°РЅ." % name, kb_confirm(name))

    elif data.startswith("delyes:"):
        name = data[7:]
        cfg = xload()
        cfg["inbounds"][0]["settings"]["clients"] = [
            c for c in cfg["inbounds"][0]["settings"]["clients"] if c.get("email") != name
        ]
        xsave(cfg)
        remove_keys(name)
        edit(chat, mid, "рџ—‘ РЈРґР°Р»С‘РЅ: В«%sВ»" % name, kb_list())


def main():
    if not TOKEN or TOKEN.startswith("ВСТАВЬТЕ") or ":" not in TOKEN:
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