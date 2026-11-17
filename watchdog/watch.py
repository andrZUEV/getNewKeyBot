#!/usr/bin/env python3
"""Взаимный сторож для пары серверов.

Каждый сервер делает три вещи:

1. принимает пинги от партнёра (`GET /ping/<токен>`);
2. сам пингует партнёра по публичному адресу раз в `ping_every` секунд;
3. если пингов нет дольше `alert_after` секунд — шлёт тревогу в Telegram,
   а когда связь возвращается — сообщает об этом.

Пинги идут по публичному HTTPS, а не через туннель, поэтому проверяется
не только живой процесс, но и доступность сервера снаружи.

Зависимости: только стандартная библиотека Python 3.
"""

import hmac
import json
import os
import ssl
import subprocess
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

CONF = os.environ.get("VPNWATCH_CONF", "/opt/vpnwatch/config.json")
NOTIFY = os.environ.get("VPNWATCH_NOTIFY", "/usr/local/bin/tg-notify.sh")


def log(msg):
    print("%s %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg), flush=True)


def load_conf():
    with open(CONF, encoding="utf-8") as f:
        return json.load(f)


def notify(text):
    """Отправить сообщение в Telegram (скрипт берёт токен из своего env)."""
    try:
        subprocess.run([NOTIFY, text], check=False, timeout=25,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        log("уведомление отправлено")
    except Exception as e:  # noqa: BLE001 - сторож не должен падать из-за этого
        log("не смог отправить уведомление: %s" % e)


def http_get(url, timeout=15):
    """Один GET. Сертификат проверяется: так мы заодно ловим истёкший TLS."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "vpnwatch/1"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            r.read(128)
        return True
    except Exception as e:  # noqa: BLE001
        # URL не печатаем целиком: в нём токен
        log("GET %s: %s" % (url.split("?")[0].split("/ping/")[0], e))
        return False


class State:
    """Общее состояние: когда был последний пинг и что с партнёром."""

    def __init__(self):
        self.lock = threading.Lock()
        self.last_ping = 0.0
        self.alerted = False
        self.partner_ok = None
        self.started = time.time()

    def ping(self):
        with self.lock:
            self.last_ping = time.time()

    def age(self):
        """Сколько секунд назад был пинг (None, если ещё ни одного)."""
        with self.lock:
            if not self.last_ping:
                return None
            return time.time() - self.last_ping

    def uptime(self):
        return time.time() - self.started


STATE = State()


class Handler(BaseHTTPRequestHandler):
    server_version = "vpnwatch"
    sys_version = ""

    def log_message(self, fmt, *args):  # noqa: D401 - молчим в журнал
        pass

    def _send(self, code, body, ctype="text/plain; charset=utf-8"):
        data = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(data)
        except Exception:  # noqa: BLE001 - клиент мог отвалиться
            pass

    def do_GET(self):
        conf = load_conf()
        path = self.path.split("?")[0]
        token_in = str(conf.get("token_in") or "")

        if path.startswith("/ping/"):
            got = path[len("/ping/"):]
            # Сравнение с постоянным временем: токен не должен утекать по таймингу
            if token_in and hmac.compare_digest(got, token_in):
                STATE.ping()
                self._send(200, "ok\n")
            else:
                self._send(404, "not found\n")
            return

        if path == "/status":
            age = STATE.age()
            if age is None:
                age_txt = "ещё не было"
            else:
                age_txt = "%.0f с назад" % age
            partner = conf.get("partner_name") or "партнёр"
            partner_url = (conf.get("partner_ping") or "").split("/ping/")[0]
            state = "неизвестно"
            if STATE.partner_ok is True:
                state = "доступен"
            elif STATE.partner_ok is False:
                state = "НЕ доступен"
            body = (
                "<!doctype html><html lang=\"ru\"><meta charset=\"utf-8\">"
                "<title>vpnwatch</title>"
                "<style>body{font:16px system-ui,sans-serif;margin:2rem;"
                "max-width:40rem}h1{font-size:1.2rem}dt{font-weight:600;"
                "margin-top:.7rem}dd{margin:.2rem 0 0 0}</style>"
                "<h1>Сторож — %s</h1><dl>"
                "<dt>Последний пинг от %s</dt><dd>%s</dd>"
                "<dt>Партнёр (%s)</dt><dd>%s</dd>"
                "<dt>Сторож работает</dt><dd>%.0f мин</dd>"
                "</dl></body></html>"
                % (conf.get("role", "?"), partner, age_txt,
                   partner_url or "не задан", state, STATE.uptime() / 60)
            )
            self._send(200, body, "text/html; charset=utf-8")
            return

        self._send(404, "not found\n")


def watchdog(conf):
    """Следит за возрастом пинга и шлёт тревогу/восстановление."""
    alert_after = float(conf.get("alert_after", 600))
    partner = conf.get("partner_name") or "партнёр"
    role = conf.get("role", "?")
    while True:
        age = STATE.age()
        if age is None:
            # С момента старта пингов ещё не было — даём такой же грейс
            age = STATE.uptime()
        if age > alert_after:
            if not STATE.alerted:
                log("АЛЕРТ: нет пингов %.0f с (порог %.0f)" % (age, alert_after))
                notify("\U0001F534 %s: нет связи с %s уже %.0f мин.\n"
                       "Проверьте сервер: ping, панель, журнал vpnwatch."
                       % (role, partner, age / 60))
                STATE.alerted = True
        else:
            if STATE.alerted:
                log("восстановление связи")
                notify("\U0001F7E2 %s: связь с %s восстановлена."
                       % (role, partner))
                STATE.alerted = False
        time.sleep(15)


def pinger(conf):
    """Пингует партнёра и внешний наблюдатель."""
    every = float(conf.get("ping_every", 120))
    partner_url = conf.get("partner_ping") or ""
    hc_url = conf.get("hc_ping_url") or ""
    while True:
        if partner_url:
            ok = http_get(partner_url)
            STATE.partner_ok = ok
            if not ok:
                log("пинг партнёра не прошёл")
        if hc_url:
            http_get(hc_url)
        time.sleep(every)


def main():
    conf = load_conf()
    port = int(conf.get("listen_port", 9443))
    cert = conf.get("cert")
    key = conf.get("key")
    if not cert or not key:
        log("в конфиге нет путей к сертификату/ключу")
        return 1

    threading.Thread(target=watchdog, args=(conf,), daemon=True).start()
    threading.Thread(target=pinger, args=(conf,), daemon=True).start()

    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_cert_chain(cert, key)

    httpd = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    httpd.daemon_threads = True
    httpd.socket = ctx.wrap_socket(httpd.socket, server_side=True)

    log("vpnwatch: роль %s, порт %d, партнёр %s"
        % (conf.get("role", "?"), port, conf.get("partner_name", "?")))
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
