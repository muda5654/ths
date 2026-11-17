#!/usr/bin/env python3
"""
Universal — Telegram-бот для рассылки по пользователям плагина.

Что делает один этот файл:
  1. Бот: вы пишете ему текст, он показывает предпросмотр с кнопкой «Отправить всем».
  2. API:  GET /api  — отдаёт плагину текущее сообщение (плагин опрашивает его каждые 2 секунды
     и показывает новое сообщение на весь экран).

Зависимостей нет — нужен только Python 3.8+.

Запуск:
    BOT_TOKEN=123:ABC ADMIN_IDS=8290877754 python3 bot.py

Переменные окружения:
    BOT_TOKEN   токен бота от @BotFather (обязательно)
    ADMIN_IDS   Telegram ID админов через запятую (по умолчанию 8290877754)
    PORT        порт HTTP-сервера (по умолчанию 8080; на Render/Railway задаётся сам)
    DATA_FILE   где хранить сообщение (по умолчанию data.json)
"""
import json
import os
import threading
import time
import urllib.error
import urllib.request
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

TOKEN = os.environ.get("BOT_TOKEN", "").strip()
ADMINS = {int(x) for x in os.environ.get("ADMIN_IDS", "8290877754").replace(" ", "").split(",") if x.strip().isdigit()}
PORT = int(os.environ.get("PORT", "8080"))
DATA_FILE = os.environ.get("DATA_FILE", "data.json")

_lock = threading.Lock()
_state = {"id": 0, "title": "", "text": "", "ts": 0, "history": []}
_drafts = {}   # admin_id -> (title, text)


# ------------------------------------------------------------------ хранилище
def load_state():
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            _state.update(data)
    except FileNotFoundError:
        pass
    except Exception as exc:
        print("Не удалось прочитать", DATA_FILE, exc)


def save_state():
    tmp = DATA_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(_state, f, ensure_ascii=False)
    os.replace(tmp, DATA_FILE)


def publish(title, text):
    """Новое сообщение: увеличиваем id — плагин покажет его один раз каждому пользователю."""
    with _lock:
        # id по времени: всегда растёт, даже если файл данных стёрся (бесплатный Render)
        _state["id"] = max(int(_state["id"]) + 1, int(time.time()))
        _state["title"] = title[:120]
        _state["text"] = text[:4000]
        _state["ts"] = int(time.time())
        if text:
            _state["history"].insert(0, {"id": _state["id"], "title": title[:120], "text": text[:4000], "ts": _state["ts"]})
            _state["history"] = _state["history"][:20]
        save_state()
        return _state["id"]


def api_body():
    with _lock:
        return json.dumps({"id": int(_state["id"]), "title": _state["title"], "text": _state["text"]},
                          ensure_ascii=False).encode("utf-8")


# ------------------------------------------------------------------ HTTP API для плагина
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        path = self.path.split("?", 1)[0].rstrip("/")
        if path == "/api":
            body = api_body()
            etag = '"%s"' % hashlib.md5(body).hexdigest()
            if self.headers.get("If-None-Match", "").strip() == etag:
                self.send_response(304)
                self.send_header("ETag", etag)
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("ETag", etag)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            body = b"ok"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)


def run_http():
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()


# ------------------------------------------------------------------ Telegram
def tg(method, **params):
    req = urllib.request.Request(
        "https://api.telegram.org/bot%s/%s" % (TOKEN, method),
        data=json.dumps(params).encode("utf-8"),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=40) as response:
        return json.loads(response.read().decode("utf-8")).get("result")


def say(chat_id, text, **extra):
    try:
        return tg("sendMessage", chat_id=chat_id, text=text, **extra)
    except Exception as exc:
        print("sendMessage:", exc)


HELP = (
    "📢 Рассылка Universal\n\n"
    "Просто напишите текст — я покажу предпросмотр, и после кнопки «Отправить всем» "
    "он появится на весь экран у всех пользователей плагина.\n\n"
    "Заголовок: напишите «Заголовок || Текст».\n\n"
    "/status — что показывается сейчас\n"
    "/history — последние сообщения\n"
    "/clear — снять сообщение"
)


def parse_draft(text):
    if "||" in text:
        title, body = text.split("||", 1)
        return title.strip(), body.strip()
    return "", text.strip()


def handle_message(msg):
    chat_id = msg["chat"]["id"]
    uid = msg.get("from", {}).get("id", 0)
    text = (msg.get("text") or "").strip()
    if uid not in ADMINS:
        say(chat_id, "⛔ Нет доступа. Ваш ID: %d" % uid)
        return
    if not text or text in ("/start", "/help"):
        say(chat_id, HELP)
        return
    if text == "/status":
        with _lock:
            cur = dict(_state)
        if cur["text"]:
            say(chat_id, "Сейчас показывается (№%d):\n\n%s%s" % (
                cur["id"], (cur["title"] + "\n\n") if cur["title"] else "", cur["text"]))
        else:
            say(chat_id, "Сейчас ничего не показывается.")
        return
    if text == "/history":
        with _lock:
            rows = list(_state["history"][:10])
        if not rows:
            say(chat_id, "История пуста.")
        else:
            say(chat_id, "\n\n".join("№%d · %s\n%s" % (
                r["id"], time.strftime("%d.%m %H:%M", time.localtime(r["ts"])), r["text"][:200]) for r in rows))
        return
    if text == "/clear":
        publish("", "")
        say(chat_id, "✅ Сообщение снято.")
        return
    if text.startswith("/send"):
        text = text[5:].strip()
    elif text.startswith("/"):
        say(chat_id, HELP)
        return
    title, body = parse_draft(text)
    if not body:
        say(chat_id, "Введите текст сообщения.")
        return
    _drafts[uid] = (title, body)
    preview = "👀 Предпросмотр:\n\n%s%s" % ((title + "\n\n") if title else "", body)
    say(chat_id, preview[:4000], reply_markup={"inline_keyboard": [[
        {"text": "📨 Отправить всем", "callback_data": "send"},
        {"text": "Отмена", "callback_data": "cancel"}]]})


def handle_callback(cb):
    uid = cb.get("from", {}).get("id", 0)
    chat_id = cb["message"]["chat"]["id"]
    msg_id = cb["message"]["message_id"]
    try:
        tg("answerCallbackQuery", callback_query_id=cb["id"])
    except Exception:
        pass
    if uid not in ADMINS:
        return
    draft = _drafts.pop(uid, None)
    if cb.get("data") == "send" and draft:
        new_id = publish(draft[0], draft[1])
        result = "✅ Отправлено (№%d). Пользователи получат сообщение в течение нескольких секунд." % new_id
    elif cb.get("data") == "send":
        result = "Черновик не найден. Напишите текст заново."
    else:
        result = "Отменено."
    try:
        tg("editMessageText", chat_id=chat_id, message_id=msg_id, text=result)
    except Exception:
        say(chat_id, result)


def run_bot():
    offset = 0
    while True:
        try:
            updates = tg("getUpdates", offset=offset, timeout=30, allowed_updates=["message", "callback_query"]) or []
            for upd in updates:
                offset = upd["update_id"] + 1
                try:
                    if "message" in upd:
                        handle_message(upd["message"])
                    elif "callback_query" in upd:
                        handle_callback(upd["callback_query"])
                except Exception as exc:
                    print("update error:", exc)
        except Exception as exc:
            print("getUpdates:", exc)
            time.sleep(3)


if __name__ == "__main__":
    load_state()
    threading.Thread(target=run_http, daemon=True).start()
    print("API: http://0.0.0.0:%d/api  |  админы: %s" % (PORT, sorted(ADMINS)))
    if not TOKEN:
        print("BOT_TOKEN не задан — работает только API (для проверки).")
        while True:
            time.sleep(3600)
    run_bot()
