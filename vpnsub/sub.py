#!/usr/bin/env python3
"""Сервис подписок для VLESS-ключей (формат v2ray-подписки).

Отдаёт по адресу /sub/<токен> base64-список ссылок vless:// для одного
клиента — в том виде, который понимают Hiddify, v2rayNG, NekoBox и т.п.
Токен — это и есть доступ: 24 случайных байта, неугадываемый URL.

Никаких админ-функций здесь нет. Панель управления остаётся отдельно
и слушает только адрес внутри туннеля.

Зависимостей нет — только стандартная библиотека.
"""
import base64
import json
import os
import ssl
import sys
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

CFG = os.environ.get("VPNSUB_CFG", "/usr/local/etc/xray/config.json")
SUBS = os.environ.get("VPNSUB_DB", "/opt/vpnsub/subs.json")
ENVF = os.environ.get("VPNSUB_ENV", "/opt/vpnbot/.env")
CERT = os.environ.get("VPNSUB_CERT", "/usr/local/etc/xray/cert/fullchain.pem")
KEY = os.environ.get("VPNSUB_KEY", "/usr/local/etc/xray/cert/privkey.pem")
BIND = os.environ.get("VPNSUB_BIND", "0.0.0.0")
PORT = int(os.environ.get("VPNSUB_PORT", "2096"))
# Публичные параметры REALITY (pbk/sid/sni/port). Приватного ключа здесь нет:
# он лежит только в конфиге Xray.
REALITY = os.environ.get("VPNSUB_REALITY", "/opt/vpnsub/reality.json")

# Порядок как в боте: Сервер 1, Сервер 2, второй адрес Сервера 2.
DOMAIN_KEYS = ("DOMAIN_SERVER1", "DOMAIN_SERVER2", "DOMAIN_SERVER2_ALT")

# Основной путь — московский вход, который пробрасывает трафик ВНУТРЬ
# туннеля до Праги. Прямые входы оставлены резервом.
TUNNEL_WS_PORT = int(os.environ.get("VPNSUB_TUNNEL_WS", "4443"))
TUNNEL_REALITY_PORT = int(os.environ.get("VPNSUB_TUNNEL_RZ", "8445"))


def log(msg):
    print(msg, flush=True)


def load_env(path):
    env = {}
    try:
        with open(path, encoding="utf-8") as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip('"').strip("'")
    except OSError:
        pass
    return env


def load_domains():
    env = load_env(ENVF)
    out, seen = [], set()
    for k in DOMAIN_KEYS:
        d = (env.get(k) or "").strip()
        if d and d not in seen:
            seen.add(d)
            out.append(d)
    return out


def load_domains_map():
    """Домены по ролям.

    Нумерация SERVER1/SERVER2 в .env исторически не совпадает с ролями
    (вход и выход), поэтому роли задаются явными переменными
    DOMAIN_ENTRY (Москва, точка входа) и DOMAIN_EXIT (Прага, выход).
    Если их нет — падаем на прежние имена.
    """
    env = load_env(ENVF)
    out = {}
    for k in DOMAIN_KEYS:
        d = (env.get(k) or "").strip()
        if d:
            out[k] = d
    entry = (env.get("DOMAIN_ENTRY") or "").strip()
    exit_ = (env.get("DOMAIN_EXIT") or "").strip()
    out["ENTRY"] = entry or out.get("DOMAIN_SERVER1") or ""
    out["EXIT"] = exit_ or out.get("DOMAIN_SERVER2") or ""
    return out


def load_subs():
    try:
        with open(SUBS, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def load_clients():
    with open(CFG, encoding="utf-8") as f:
        cfg = json.load(f)
    inbound = cfg["inbounds"][0]
    return inbound["settings"]["clients"], inbound


def load_reality():
    """Публичные параметры REALITY-входа, если он настроен."""
    try:
        with open(REALITY, encoding="utf-8") as f:
            r = json.load(f)
        if all(r.get(k) for k in ("port", "pbk", "sid", "sni")):
            return r
    except (OSError, ValueError):
        pass
    return None


def build_links(uid, name, inbound):
    """Ссылки на клиента.

    Порядок важен: клиент берёт первый рабочий профиль, поэтому сверху идут
    основные — через туннель Москва → Прага. Прямые входы лежат ниже как
    резерв на случай, если туннель недоступен.
    """
    ss = inbound.get("streamSettings", {})
    ws_port = int(inbound.get("port", 443))
    ws_path = ss.get("wsSettings", {}).get("path", "/ws")
    path = urllib.parse.quote(ws_path, safe="")   # "/ws" -> "%2Fws"
    base = name or ""
    r = load_reality()
    dm = load_domains_map()
    prague = dm.get("EXIT")            # выход, он же принимает напрямую
    moscow = dm.get("ENTRY")           # точка входа, через неё идёт туннель
    alt = dm.get("DOMAIN_SERVER2_ALT")

    def ws_link(dom, port, frag):
        return (
            "vless://%s@%s:%d?encryption=none&security=tls&sni=%s"
            "&fp=chrome&type=ws&host=%s&path=%s#%s"
            % (uid, dom, port, dom, dom, path,
               urllib.parse.quote(frag, safe=""))
        )

    def rz_link(dom, port, frag):
        return (
            "vless://%s@%s:%d?encryption=none&security=reality&sni=%s"
            "&fp=chrome&pbk=%s&sid=%s&type=tcp&headerType=none#%s"
            % (uid, dom, port, r["sni"], r["pbk"], r["sid"],
               urllib.parse.quote(frag, safe=""))
        )

    out = []

    # --- Основные: через туннель (вход Москва, выход Прага) ---
    if moscow:
        out.append(ws_link(moscow, TUNNEL_WS_PORT, base))
        if r:
            out.append(rz_link(moscow, TUNNEL_REALITY_PORT, base + "-reality"))

    # --- Резерв: напрямую в Прагу ---
    if prague:
        out.append(ws_link(prague, ws_port, base + "-прямой-прага"))
        if r:
            out.append(rz_link(prague, int(r["port"]),
                               base + "-прямой-прага-reality"))

    # --- Резерв: напрямую в Москву (без туннеля) ---
    if moscow:
        out.append(ws_link(moscow, ws_port, base + "-прямой-мск"))
        if r:
            out.append(rz_link(moscow, int(r["port"]),
                               base + "-прямой-мск-reality"))

    # --- Второй адрес Москвы, если он задан отдельно ---
    if alt and alt != moscow:
        out.append(ws_link(alt, ws_port, base + "-прямой-мск2"))
        if r:
            out.append(rz_link(alt, int(r["port"]),
                               base + "-прямой-мск2-reality"))

    return out


class H(BaseHTTPRequestHandler):
    server_version = "vpnsub"
    sys_version = ""

    def log_message(self, *a):
        pass

    def not_found(self):
        # Одинаковый ответ на любой неверный токен: не подсказываем,
        # существует ли клиент.
        self.send_response(404)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"not found")

    def do_GET(self):
        try:
            path = urllib.parse.urlparse(self.path).path
            if not path.startswith("/sub/"):
                self.not_found()
                return
            token = path[len("/sub/"):].strip()
            if not token or len(token) > 128:
                self.not_found()
                return

            subs = load_subs()
            email = None
            for name, rec in subs.items():
                if isinstance(rec, dict) and rec.get("token") == token:
                    email = name
                    break
            if not email:
                self.not_found()
                return

            clients, inbound = load_clients()
            client = next((c for c in clients if c.get("email") == email), None)
            if not client:
                # клиента удалили — токен перестал действовать
                self.not_found()
                return

            links = build_links(client["id"], email, inbound)
            if not links:
                self.not_found()
                return

            body = base64.b64encode("\n".join(links).encode()).decode()

            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            # Hiddify и другие клиенты показывают это как заголовок профиля
            self.send_header("Profile-Title", base64.b64encode(email.encode()).decode())
            # Учёт трафика xray не ведёт, поэтому нули
            self.send_header("subscription-userinfo",
                             "upload=0; download=0; total=0; expire=0")
            self.end_headers()
            self.wfile.write(body.encode())
        except Exception as e:
            log("error: %r" % (e,))
            try:
                self.send_response(500)
                self.end_headers()
            except Exception:
                pass


def main():
    if not os.path.exists(CERT) or not os.path.exists(KEY):
        log("FATAL: нет сертификата %s / %s" % (CERT, KEY))
        sys.exit(1)
    if not load_domains():
        log("FATAL: в %s не задан ни один DOMAIN_*" % ENVF)
        sys.exit(1)

    srv = ThreadingHTTPServer((BIND, PORT), H)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(CERT, KEY)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    srv.socket = ctx.wrap_socket(srv.socket, server_side=True)
    log("vpnsub on %s:%d, domains=%d, subs=%d"
        % (BIND, PORT, len(load_domains()), len(load_subs())))
    srv.serve_forever()


if __name__ == "__main__":
    main()
