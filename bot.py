
    
  
#!/usr/bin/env python3
"""
Бот рассылки Universal.

Что умеет:
  • Рассылка текста, картинки и/или звука всем пользователям плагина.
  • Выборочная рассылка по конкретным людям из списка.
  • Плагин сам регистрируется на сервере: /api/register  (POST).
  • Список всех зарегистрированных пользователей виден в боте.

Запуск:
    BOT_TOKEN=123:ABC python3 bot.py

Переменные окружения:
    BOT_TOKEN   токен бота (обязательно)
    PORT        порт HTTP-сервера (по умолчанию 8080)
    DATA_FILE   файл с текущим сообщением (data.json)
    USERS_FILE  файл с пользователями плагина (users.json)
"""
import base64, hashlib, json, os, threading, time, urllib.error, urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

TOKEN    = os.environ.get("BOT_TOKEN", "").strip()
PORT     = int(os.environ.get("PORT", "8080"))
DATA_FILE  = os.environ.get("DATA_FILE",  "data.json")
USERS_FILE = os.environ.get("USERS_FILE", "users.json")

# Heartbeat window: the plugin registers every ~2 seconds while it is running.
HEARTBEAT_TTL = int(os.environ.get("HEARTBEAT_TTL", "20"))

# ------------------------------------------------------------------ хранилище
_lock  = threading.Lock()

# текущее сообщение
_state = {
    "id": 0, "title": "", "text": "",
    "image": "",   # base64 JPEG/PNG (или пусто)
    "sound": "",   # base64 MP3/OGG  (или пусто)
    "targets": [], # [] = всем; иначе список строк user_id
    "ts": 0,
}

# зарегистрированные пользователи плагина
# ключ — строка user_id, значение — {"username": ..., "first_name": ..., "ts": ...}
_users: dict = {}

# черновики: chat_id -> {"title", "text", "image_b64", "sound_b64", "targets"}
_drafts: dict = {}


def _load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        if isinstance(d, type(default)):
            return d
    except FileNotFoundError:
        pass
    except Exception as exc:
        print("load", path, exc)
    return default


def _save_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    os.replace(tmp, path)


def load_all():
    _state.update(_load_json(DATA_FILE,  _state))
    _users.update(_load_json(USERS_FILE, {}))


# ------------------------------------------------------------------ публикация
def publish(title: str, text: str,
            image_b64: str = "", sound_b64: str = "",
            targets: list | None = None) -> int:
    """Новое сообщение. targets=None → всем; targets=["123","456"] → только им."""
    with _lock:
        _state["id"]      = max(int(_state["id"]) + 1, int(time.time()))
        _state["title"]   = title[:120]
        _state["text"]    = text[:4000]
        _state["image"]   = image_b64
        _state["sound"]   = sound_b64
        _state["targets"] = [str(t) for t in (targets or [])]
        _state["ts"]      = int(time.time())
        _save_json(DATA_FILE, _state)
        return _state["id"]


# ------------------------------------------------------------------ HTTP API
def _api_body_for(user_id: str) -> bytes:
    with _lock:
        tgts = _state.get("targets") or []
        if tgts and str(user_id) not in tgts:
            # не для этого пользователя — пустой ответ (id=0 → плагин игнорирует)
            return json.dumps({"id": 0, "title": "", "text": "", "image": "", "sound": ""},
                              ensure_ascii=False).encode()
        return json.dumps({
            "id":    int(_state["id"]),
            "title": _state["title"],
            "text":  _state["text"],
            "image": _state.get("image", ""),
            "sound": _state.get("sound", ""),
        }, ensure_ascii=False).encode()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a): pass

    def do_GET(self):
        path = self.path.split("?", 1)[0].rstrip("/")

        if path == "/api":
            # плагин передаёт свой user_id через заголовок или query-param
            uid = ""
            qs  = self.path.split("?", 1)[1] if "?" in self.path else ""
            for part in qs.split("&"):
                if part.startswith("uid="):
                    uid = part[4:]
            uid = uid or self.headers.get("X-User-Id", "")
            body = _api_body_for(uid)
            etag = '"%s"' % hashlib.md5(body).hexdigest()
            if self.headers.get("If-None-Match", "").strip() == etag:
                self._send(304, b"", etag=etag)
                return

