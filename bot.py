#!/usr/bin/env python3
"""Telegram-бот для управления ключами VLESS (Xray).

Только стандартная библиотека — никаких зависимостей.
Настройки читаются из .env рядом с bot.py.
Ключи сохраняются текстовыми файлами в data/keys/<имя>.txt
"""
import json
import os
import secrets
import socket
import subprocess
import sys
import time
import urllib.parse
import urllib.error
import urllib.request

# Telegram стабильно доступен по IPv4, а IPv6-маршрут периодически
# зависает в SYN-SENT на десятки секунд. Принудительно используем IPv4.
_orig_getaddrinfo = socket.getaddrinfo


def _getaddrinfo_ipv4(host, port, family=0, type=0, proto=0, flags=0):
    return _orig_getaddrinfo(host, port, socket.AF_INET, type, proto, flags)


socket.getaddrinfo = _getaddrinfo_ipv4

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
    "s2": ENV.get("DOMAIN_SERVER2", ""),
    "s2b": ENV.get("DOMAIN_SERVER2_ALT", ""),
    "s1": ENV.get("DOMAIN_SERVER1", ""),
}

# Порядок и подписи серверов в выдаче.
# Пустой домен = сервер не настроен/выведен из эксплуатации: он просто
# не попадает в ключи, вместо того чтобы выдавать нерабочую ссылку.
SERVER_ORDER = (
    ("s1", "Сервер 1"),
    ("s2", "Сервер 2"),
    ("s2b", "Сервер 2 (2-й IP)"),
)

# --- массовое добавление ---
MAX_BULK = 50          # больше за раз не создаём (и Telegram, и xray)
SUFFIX_LEN = 4         # сколько случайных символов добавляется к префиксу
# без похожих символов (i/l/1/o/0), чтобы имя было легко продиктовать
SUFFIX_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"
NAME_MAX_BYTES = 48    # ограничение, чтобы callback_data влезал в лимит Telegram
TELEGRAM_TEXT_LIMIT = 3800   # с запасом от лимита 4096

WAITING = {}


def api(method, **params):
    url = "https://api.telegram.org/bot%s/%s" % (TOKEN, method)
    body = json.dumps(params).encode()
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=(40 if method == "getUpdates" else 20)) as r:
            _res = json.load(r)
            if method != "getUpdates":
                print("api ok: %s" % method, flush=True)
            return _res
    except urllib.error.HTTPError as e:
        try:
            text = e.read().decode("utf-8", "replace")
        except Exception:
            text = ""
        if "message is not modified" in text:
            print("api %s: not modified -> ok" % method, flush=True)
            return {"ok": True, "ignored": True}
        if "query is too old" in text:
            print("api %s: QUERY TOO OLD" % method, flush=True)
            return {"ok": False, "ignored": True}
        print("api error %s: %s | %s" % (method, e, text), flush=True)
        return {"ok": False}
    except Exception as e:
        print("api error %s: %s" % (method, e), flush=True)
        return {"ok": False}


def xload():
    with open(XRAY, encoding="utf-8") as f:
        return json.load(f)


def xsave(cfg):
    """Записать конфиг атомарно и только после проверки, что xray его примет.

    Раньше json.dump писал прямо в целевой файл: обрыв в момент записи
    оставлял битый config.json, и xray больше не поднимался. Теперь сначала
    пишем во временный файл, проверяем его `xray run -test` и лишь потом
    подменяем атомарным os.replace().
    """
    tmp = XRAY + ".tmp.json"   # расширение важно: xray определяет формат по нему
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)
    res = subprocess.run(["xray", "run", "-test", "-config", tmp], capture_output=True)
    if res.returncode != 0:
        try:
            os.remove(tmp)
        except OSError:
            pass
        text = (res.stderr or res.stdout).decode("utf-8", "replace")
        # отбрасываем баннер xray, оставляем суть
        lines = [x.strip() for x in text.splitlines() if x.strip()]
        err = " | ".join(lines[-2:])[:300] or "неизвестная ошибка"
        raise RuntimeError("xray не принял конфигурацию: %s" % err)
    os.replace(tmp, XRAY)
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
    for key, _label in SERVER_ORDER:
        d = (DOM.get(key) or "").strip()
        if not d:
            continue  # адрес не задан — не выдаём мёртвую ссылку
        out[key] = (
            "vless://%s@%s:443?encryption=none&security=tls&sni=%s"
            "&fp=chrome&type=ws&host=%s&path=%%2Fws#%s"
            % (uid, d, d, d, name.replace(" ", "%20"))
        )
    return out


def save_keys(name, l):
    os.makedirs(KEYS_DIR, exist_ok=True)
    p = os.path.join(KEYS_DIR, "%s.txt" % name)
    with open(p, "w", encoding="utf-8") as f:
        f.write("# %s\n\n" % name)
        for key, label in SERVER_ORDER:
            if key in l:
                f.write("# %s\n%s\n\n" % (label, l[key]))
    return p


def remove_keys(name):
    p = os.path.join(KEYS_DIR, "%s.txt" % name)
    if os.path.exists(p):
        os.remove(p)


def fmt_links(name, l):
    for key, _label in SERVER_ORDER:
        if key in l:
            return l[key]
    return "Ключи не настроены."


def fmt_all(name, l):
    parts = ["<b>%s</b>" % name]
    for key, label in SERVER_ORDER:
        if key in l:
            parts.append("<b>%s</b>\n<code>%s</code>" % (label, l[key]))
    return "\n\n".join(parts)


def _rand_suffix(n=SUFFIX_LEN):
    return "".join(secrets.choice(SUFFIX_ALPHABET) for _ in range(n))


def _truncate_bytes(s, limit):
    """Обрезать строку до limit байт UTF-8, не разорвав символ."""
    b = s.encode("utf-8")
    if len(b) <= limit:
        return s
    return b[:limit].decode("utf-8", "ignore")


def bulk_names(prefix, count, existing):
    """Имена вида «префикс-a3k9», гарантированно уникальные.

    Хвост случайный, но проверяем и на уже существующие имена, и внутри
    пачки: коллизия на 4 символах маловероятна, но не невозможна.
    """
    taken = {(e or "").lower() for e in existing}
    base_limit = max(1, NAME_MAX_BYTES - (1 + SUFFIX_LEN))
    base = _truncate_bytes(clean_name(prefix), base_limit).strip() or "device"

    out = []
    guard = 0
    while len(out) < count:
        guard += 1
        if guard > count * 200:
            raise RuntimeError("не удалось подобрать уникальные имена")
        name = "%s-%s" % (base, _rand_suffix())
        if name.lower() in taken:
            continue
        taken.add(name.lower())
        out.append(name)
    return out


def parse_bulk(text):
    """'телефон 5' -> ('телефон', 5, None). Количество — последнее слово."""
    parts = (text or "").split()
    if len(parts) < 2:
        return None, 0, "нужен формат: префикс и количество"
    if not parts[-1].isdigit():
        return None, 0, "количество должно быть числом в конце"
    count = int(parts[-1])
    prefix = clean_name(" ".join(parts[:-1]))
    if not prefix:
        return None, 0, "пустой префикс"
    if count < 1 or count > MAX_BULK:
        return None, 0, "количество должно быть от 1 до %d" % MAX_BULK
    return prefix, count, None


def send_keys_chunked(chat, keys, header=None):
    """Отправить ключи порциями, разделяя их пустой строкой.

    Telegram не принимает сообщения длиннее 4096 символов, а одна ссылка
    vless:// занимает ~200, поэтому большая пачка режется на несколько
    сообщений. Формат внутри сообщения: ключ, пустая строка, ключ, ...

    Возвращает (доставлено, не доставлено), чтобы вызывающий код мог
    честно сообщить о сбое, а не сделать вид, что всё ушло.
    """
    chunks = []
    cur = ""
    for k in keys:
        piece = k + "\n\n"
        if cur and len(cur) + len(piece) > TELEGRAM_TEXT_LIMIT:
            chunks.append(cur.rstrip())
            cur = ""
        cur += piece
    if cur.strip():
        chunks.append(cur.rstrip())

    sent = failed = 0
    for i, ch in enumerate(chunks):
        if header and i == 0:
            ch = header + "\n\n" + ch
        if send(chat, ch, None, parse_mode=None).get("ok"):
            sent += 1
        else:
            failed += 1
    return sent, failed


def kb_menu():
    return {"inline_keyboard": [
        [{"text": "➕ Добавить клиента", "callback_data": "add"}],
        [{"text": "➕ Добавить несколько", "callback_data": "addmany"}],
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
        [{"text": "🌐 Другие адреса", "callback_data": "all:" + name}],
        [{"text": "🗑 Удалить", "callback_data": "del:" + name}],
        [{"text": "◀️ К списку", "callback_data": "list"}],
    ]}


def kb_confirm(name):
    return {"inline_keyboard": [
        [{"text": "✅ Да, удалить", "callback_data": "delyes:" + name}],
        [{"text": "◀️ Отмена", "callback_data": "c:" + name}],
    ]}


def send(chat_id, text, keyboard=None, parse_mode="HTML", retries=4):
    """Отправка с повторами.

    Сеть до api.telegram.org периодически рвётся (Connection reset by peer),
    и раньше ошибка отправки молча игнорировалась — админ мог не получить
    ключи вовсе. Теперь пробуем несколько раз с нарастающей паузой.
    """
    params = {"chat_id": chat_id, "text": text,
              "disable_web_page_preview": True}
    if parse_mode:
        params["parse_mode"] = parse_mode
    if keyboard:
        params["reply_markup"] = keyboard

    res = {"ok": False}
    for attempt in range(retries):
        res = api("sendMessage", **params)
        if res.get("ok"):
            return res
        if attempt < retries - 1:
            time.sleep(1 + attempt)
    print("send failed after %d attempts" % retries, flush=True)
    return res


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
        send(chat, fmt_links(name, l), kb_client(name))

    elif action == "addmany":
        prefix, count, err = parse_bulk(text)
        if err:
            send(chat, "Не понял: %s.\nПример: <code>телефон 5</code>" % err)
            return

        cfg = xload()
        cl = cfg["inbounds"][0]["settings"]["clients"]
        try:
            names = bulk_names(prefix, count, [c.get("email", "") for c in cl])
        except RuntimeError as e:
            send(chat, "Не получилось: %s" % e)
            return

        added = []
        for name in names:
            uid_new = subprocess.run(
                ["xray", "uuid"], capture_output=True, text=True
            ).stdout.strip()
            if not uid_new:
                # конфиг на диск ещё не писали — ничего не сломано
                send(chat, "Ошибка: <code>xray uuid</code> не вернул значение. "
                           "Ничего не изменено.")
                return
            cl.append({"id": uid_new, "email": name})
            added.append((name, uid_new))

        try:
            xsave(cfg)   # один перезапуск xray на всю пачку
        except Exception as e:
            send(chat, "Конфигурация не применена, старые ключи целы.\n<code>%s</code>"
                       % str(e)[:300])
            return

        keys = []
        for name, uid_new in added:
            l = links_for(uid_new, name)
            save_keys(name, l)
            keys.append(fmt_links(name, l))

        # В ответ идут только ключи (как и договаривались): имя устройства
        # видно в самом ключе после #.
        sent_msgs, failed_msgs = send_keys_chunked(
            chat, keys, header="Готово: добавлено %d" % len(added))

        if failed_msgs:
            send(chat,
                 "⚠️ <b>%d из %d</b> сообщений с ключами не доставлено "
                 "(сеть до Telegram).\n\n"
                 "Клиенты созданы, ключи сохранены. Забрать их можно:\n"
                 "• «📋 Список клиентов» → нужное имя;\n"
                 "• файлы на сервере: <code>%s</code>"
                 % (failed_msgs, sent_msgs + failed_msgs, KEYS_DIR),
                 kb_menu())

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

    elif data == "addmany":
        WAITING[uid] = ("addmany", None)
        edit(chat, mid,
             "<b>Массовое добавление</b>\n\n"
             "Отправьте <b>префикс</b> и <b>количество</b> через пробел, например:\n"
             "<code>телефон 5</code>\n\n"
             "Каждому подключению добавится случайный хвост:\n"
             "<code>телефон-a3k9</code>, <code>телефон-p7mq</code>, …\n\n"
             "Префикс может содержать пробелы: <code>Махов офис 3</code>.\n"
             "За один раз — до %d." % MAX_BULK)

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
        edit(chat, mid, fmt_links(name, l), kb_client(name))

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
        edit(chat, mid, fmt_links(name, l), kb_client(name))

    elif data.startswith("ren:"):
        name = data[4:]
        WAITING[uid] = ("rename", name)
        edit(chat, mid, "Введите новое имя для «%s»:" % name, kb_client(name))

    elif data.startswith("all:"):
        name = data[4:]
        found = None
        for c in clients():
            if c.get("email") == name:
                found = c
        if not found:
            edit(chat, mid, "Клиент не найден.", kb_list())
            return
        edit(chat, mid, fmt_all(name, links_for(found["id"], name)), kb_client(name))

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
    _r0 = api("getUpdates", offset=-1, limit=1)
    if _r0.get("ok") and _r0.get("result"):
        offset = _r0["result"][-1]["update_id"] + 1
        print("пропускаю старые обновления до %d" % (offset - 1), flush=True)
    while True:
        params = {"timeout": 25}
        if offset is not None:
            params["offset"] = offset
        res = api("getUpdates", **params)
        if not res.get("ok"):
            time.sleep(3)
            continue
        for upd in res.get("result", []):
            offset = upd["update_id"] + 1
            if "callback_query" in upd:
                print("CB data=%r from=%s" % (upd["callback_query"].get("data"),
                                              upd["callback_query"]["from"]["id"]), flush=True)
            elif "message" in upd:
                print("MSG text=%r from=%s" % ((upd["message"].get("text") or "")[:40],
                                               upd["message"]["from"]["id"]), flush=True)
            try:
                if "message" in upd:
                    handle_message(upd["message"])
                elif "callback_query" in upd:
                    handle_callback(upd["callback_query"])
            except Exception as e:
                print("handler error: %s" % e, flush=True)


if __name__ == "__main__":
    main()