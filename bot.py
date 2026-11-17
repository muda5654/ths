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
    "volume": 100, # громкость звука в процентах: 0..100
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
            targets: list | None = None, volume: int = 100) -> int:
    """Новое сообщение. targets=None → всем; targets=["123","456"] → только им."""
    with _lock:
        _state["id"]      = max(int(_state["id"]) + 1, int(time.time()))
        _state["title"]   = title[:120]
        _state["text"]    = text[:4000]
        _state["image"]   = image_b64
        _state["sound"]   = sound_b64
        _state["volume"]  = max(0, min(100, int(volume)))
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
            return json.dumps({"id": 0, "title": "", "text": "", "image": "", "sound": "", "volume": 100},
                              ensure_ascii=False).encode()
        return json.dumps({
            "id":    int(_state["id"]),
            "title": _state["title"],
            "text":  _state["text"],
            "image": _state.get("image", ""),
            "sound": _state.get("sound", ""),
            "volume": int(_state.get("volume", 100)),
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
            self._send(200, body,
                       content_type="application/json; charset=utf-8",
                       extra={"Cache-Control": "no-cache", "ETag": etag})

        elif path == "/api/users":
            # список зарегистрированных пользователей (для бота и отладки)
            with _lock:
                body = json.dumps(_users, ensure_ascii=False).encode()
            self._send(200, body, content_type="application/json; charset=utf-8")

        else:
            self._send(200, b"ok", content_type="text/plain")

    def do_POST(self):
        path = self.path.split("?", 1)[0].rstrip("/")

        if path == "/api/register":
            # плагин регистрирует пользователя при каждом запуске
            length = int(self.headers.get("Content-Length", 0))
            raw    = self.rfile.read(length)
            try:
                data = json.loads(raw)
                uid  = str(data.get("user_id", "")).strip()
                if uid:
                    with _lock:
                        _users[uid] = {
                            "username":   str(data.get("username",   "") or ""),
                            "first_name": str(data.get("first_name", "") or ""),
                            "ts": int(time.time()),
                        }
                        _save_json(USERS_FILE, _users)
                self._send(200, b'{"ok":true}', content_type="application/json")
            except Exception as exc:
                print("register:", exc)
                self._send(400, b'{"ok":false}')
        else:
            self._send(404, b"not found")

    def _send(self, code: int, body: bytes,
              content_type: str = "text/plain",
              etag: str = "",
              extra: dict | None = None):
        self.send_response(code)
        if content_type:
            self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        if etag:
            self.send_header("ETag", etag)
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if body:
            self.wfile.write(body)


def run_http():
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()


# ------------------------------------------------------------------ Telegram
def tg(method: str, **params):
    req = urllib.request.Request(
        "https://api.telegram.org/bot%s/%s" % (TOKEN, method),
        data=json.dumps(params).encode(),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=40) as r:
        return json.loads(r.read()).get("result")


def tg_file(method: str, chat_id: int, field: str, file_id_or_bytes,
            is_bytes: bool = False, **extra):
    """Отправка файла: либо по file_id (строка), либо bytes → multipart."""
    if not is_bytes:
        return tg(method, chat_id=chat_id, **{field: file_id_or_bytes}, **extra)
    # multipart upload
    import io, random, string
    boundary = "---" + "".join(random.choices(string.ascii_lowercase, k=16))
    buf = io.BytesIO()
    def w(s): buf.write(s.encode() if isinstance(s, str) else s)
    for k, v in [("chat_id", str(chat_id))] + [(k2, str(v2)) for k2, v2 in extra.items()]:
        w("--%s\r\n" % boundary)
        w('Content-Disposition: form-data; name="%s"\r\n\r\n' % k)
        w("%s\r\n" % v)
    w("--%s\r\n" % boundary)
    w('Content-Disposition: form-data; name="%s"; filename="file"\r\n\r\n' % field)
    w(file_id_or_bytes)
    w("\r\n--%s--\r\n" % boundary)
    req = urllib.request.Request(
        "https://api.telegram.org/bot%s/%s" % (TOKEN, method),
        data=buf.getvalue(),
        headers={"Content-Type": "multipart/form-data; boundary=" + boundary})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read()).get("result")


def download_file(file_id: str) -> bytes:
    info = tg("getFile", file_id=file_id)
    path = info["file_path"]
    url  = "https://api.telegram.org/file/bot%s/%s" % (TOKEN, path)
    with urllib.request.urlopen(url, timeout=30) as r:
        return r.read()


def say(chat_id: int, text: str, **extra):
    try:
        return tg("sendMessage", chat_id=chat_id, text=text, **extra)
    except Exception as exc:
        print("sendMessage:", exc)


# ------------------------------------------------------------------ user list helpers
def users_list() -> list[tuple[str, str]]:
    """Возвращает [(user_id, label), ...], отсортировано по имени."""
    with _lock:
        rows = list(_users.items())
    result = []
    for uid, info in rows:
        name = info.get("first_name", "").strip()
        uname = info.get("username", "").strip()
        label = name or ("@" + uname if uname else "id" + uid)
        if uname:
            label += " (@%s)" % uname
        result.append((uid, label))
    return sorted(result, key=lambda x: x[1].lower())


def user_is_active(uid: str, now: int | None = None) -> bool:
    """True только при свежем heartbeat; offline и отключённый плагин не различимы."""
    now = int(now or time.time())
    with _lock:
        info = _users.get(str(uid), {})
        try:
            last_seen = int(info.get("ts", 0) or 0)
        except Exception:
            last_seen = 0
    return last_seen > 0 and (now - last_seen) <= HEARTBEAT_TTL


def active_user_ids() -> list[str]:
    now = int(time.time())
    with _lock:
        rows = list(_users.items())
    return [str(uid) for uid, info in rows
            if int(info.get("ts", 0) or 0) > 0
            and now - int(info.get("ts", 0) or 0) <= HEARTBEAT_TTL]


def user_status_label(uid: str) -> str:
    return "🟢 включён" if user_is_active(uid) else "⚪ выключен / нет связи"


def users_keyboard(selected: set[str] | None = None) -> dict:
    """Inline-клавиатура: список пользователей с чекбоксами."""
    rows = []
    selected = selected or set()
    for uid, label in users_list():
        mark = "✅ " if uid in selected else ""
        status = "🟢 " if user_is_active(uid) else "⚪ "
        rows.append([{"text": mark + status + label, "callback_data": "usr:" + uid}])
    rows.append([
        {"text": "📨 Отправить выбранным", "callback_data": "send_sel"},
        {"text": "📢 Всем",               "callback_data": "send_all"},
    ])
    rows.append([{"text": "❌ Отмена", "callback_data": "cancel"}])
    return {"inline_keyboard": rows}


# ------------------------------------------------------------------ bot logic
HELP = (
    "📢 Рассылка Universal\n\n"
    "Нажмите кнопку «Отправить рассылку», затем добавьте текст, фото или звук.\n"
    "Громкость звука настраивается от 0 до 100%.\n\n"
    "/users — список пользователей\n"
    "/status — текущая публикация\n"
    "/clear — снять сообщение\n"
    "/menu — открыть меню"
)


def main_keyboard() -> dict:
    return {"inline_keyboard": [[
        {"text": "📨 Отправить рассылку", "callback_data": "new_broadcast"}
    ]]}


def editor_keyboard() -> dict:
    return {"inline_keyboard": [
        [
            {"text": "✏️ Добавить текст", "callback_data": "add_text"},
            {"text": "🖼 Добавить фото", "callback_data": "add_photo"},
        ],
        [
            {"text": "🔊 Добавить звук", "callback_data": "add_sound"},
            {"text": "🔉 Настройка громкости", "callback_data": "volume_menu"},
        ],
        [
            {"text": "👀 Предпросмотр", "callback_data": "preview"},
            {"text": "👥 Получатели", "callback_data": "pick_users"},
        ],
        [
            {"text": "🚀 Опубликовать всем", "callback_data": "send_all"},
            {"text": "❌ Отмена", "callback_data": "cancel"},
        ],
    ]}


def volume_keyboard(volume: int) -> dict:
    values = [0, 25, 50, 75, 100]
    row = []
    for value in values:
        mark = " ✓" if value == volume else ""
        row.append({"text": "%d%%%s" % (value, mark), "callback_data": "vol:%d" % value})
    return {"inline_keyboard": [row, [
        {"text": "−10", "callback_data": "vol:-10"},
        {"text": "+10", "callback_data": "vol:+10"},
    ], [{"text": "↩️ Назад", "callback_data": "back_editor"}]]}


def _draft(chat_id: int) -> dict:
    return _drafts.setdefault(chat_id, {
        "title": "", "text": "", "image_b64": "", "sound_b64": "",
        "volume": 100, "selected": set(), "pending": ""
    })


def _editor_text(d: dict) -> str:
    parts = ["📝 Настройка рассылки"]
    parts.append("Текст: %s" % ("добавлен" if d.get("text") else "не добавлен"))
    parts.append("Фото: %s" % ("добавлено" if d.get("image_b64") else "не добавлено"))
    parts.append("Звук: %s" % ("добавлен" if d.get("sound_b64") else "не добавлен"))
    parts.append("Громкость: %d%%" % int(d.get("volume", 100)))
    parts.append("\nВыберите действие:")
    return "\n".join(parts)


def show_editor(chat_id: int):
    d = _draft(chat_id)
    say(chat_id, _editor_text(d), reply_markup=editor_keyboard())


def edit_screen(chat_id: int, msg_id: int, text: str, keyboard: dict | None = None):
    params = {"chat_id": chat_id, "message_id": msg_id, "text": text}
    if keyboard is not None:
        params["reply_markup"] = keyboard
    try:
        tg("editMessageText", **params)
    except Exception:
        say(chat_id, text, **({"reply_markup": keyboard} if keyboard else {}))


def handle_message(msg: dict):
    chat_id = int(msg["chat"]["id"])
    text    = (msg.get("text") or "").strip()
    photo   = msg.get("photo")
    audio   = msg.get("audio") or msg.get("voice") or msg.get("document")
    caption = (msg.get("caption") or "").strip()

    if text in ("/start", "/menu"):
        say(chat_id, "📢 Панель управления рассылкой", reply_markup=main_keyboard())
        return
    if text == "/help":
        say(chat_id, HELP, reply_markup=main_keyboard())
        return

    if text == "/status":
        with _lock:
            cur = dict(_state)
        if cur["text"] or cur["image"] or cur["sound"]:
            parts = ["Сейчас опубликовано (№%d):" % cur["id"]]
            if cur["title"]: parts.append(cur["title"])
            if cur["text"]: parts.append(cur["text"])
            if cur["image"]: parts.append("🖼 Картинка есть")
            if cur["sound"]: parts.append("🔊 Звук есть, громкость %d%%" % int(cur.get("volume", 100)))
            tgts = cur.get("targets") or []
            parts.append("Получатели: %s" % (", ".join(tgts) if tgts else "все"))
            say(chat_id, "\n".join(parts), reply_markup=main_keyboard())
        else:
            say(chat_id, "Сейчас ничего не показывается.", reply_markup=main_keyboard())
        return

    if text == "/clear":
        publish("", "", "", "", [], volume=100)
        say(chat_id, "✅ Сообщение снято.", reply_markup=main_keyboard())
        return

    if text == "/users":
        rows = users_list()
        if not rows:
            say(chat_id, "Пока нет зарегистрированных пользователей.", reply_markup=main_keyboard())
        else:
            lines = ["👥 Пользователи плагина (%d):" % len(rows)]
            for uid, label in rows[:50]:
                lines.append("• %s — %s  [%s]" % (label, user_status_label(uid), uid))
            say(chat_id, "\n".join(lines), reply_markup=main_keyboard())
        return

    d = _draft(chat_id)
    pending = d.get("pending", "")

    if photo:
        file_id = photo[-1]["file_id"]
        try:
            raw = download_file(file_id)
            d["image_b64"] = base64.b64encode(raw).decode()
            if caption:
                d["text"] = caption
            d["pending"] = ""
            say(chat_id, "✅ Фото добавлено.\n\n" + _editor_text(d), reply_markup=editor_keyboard())
        except Exception as exc:
            say(chat_id, "Не удалось скачать фото: " + str(exc))
        return

    if audio and not text:
        file_id = audio.get("file_id", "") if isinstance(audio, dict) else ""
        if not file_id:
            say(chat_id, "Не могу получить файл.")
            return
        try:
            raw = download_file(file_id)
            d["sound_b64"] = base64.b64encode(raw).decode()
            d["pending"] = ""
            say(chat_id, "✅ Звук добавлен.\n\n" + _editor_text(d), reply_markup=editor_keyboard())
        except Exception as exc:
            say(chat_id, "Не удалось скачать аудио: " + str(exc))
        return

    if pending == "text" and text:
        if "||" in text:
            title, body = text.split("||", 1)
            d["title"] = title.strip()
            d["text"] = body.strip()
        else:
            d["text"] = text
        d["pending"] = ""
        say(chat_id, "✅ Текст добавлен.\n\n" + _editor_text(d), reply_markup=editor_keyboard())
        return

    if text.startswith("/send"):
        body = text[5:].strip()
        if body:
            d["text"] = body
        d["pending"] = ""
        _show_preview(chat_id, d)
        return

    if text.startswith("/"):
        say(chat_id, HELP, reply_markup=main_keyboard())
        return

    if text:
        d["text"] = text
        _show_preview(chat_id, d)
        return

    say(chat_id, "Используйте кнопки меню, чтобы собрать рассылку.", reply_markup=main_keyboard())


def _show_preview(chat_id: int, d: dict):
    parts = ["👀 Предпросмотр:"]
    if d.get("title"): parts.append(d["title"])
    if d.get("text"): parts.append(d["text"])
    if d.get("image_b64"): parts.append("🖼 Картинка прикреплена")
    if d.get("sound_b64"):
        parts.append("🔊 Звук прикреплён, громкость: %d%%" % int(d.get("volume", 100)))
    if not any((d.get("text"), d.get("image_b64"), d.get("sound_b64"))):
        parts.append("Пока ничего не добавлено.")
    kb = {"inline_keyboard": [
        [{"text": "↩️ К настройкам", "callback_data": "back_editor"}],
        [{"text": "🚀 Опубликовать всем", "callback_data": "send_all"},
         {"text": "👥 Выбрать получателей", "callback_data": "pick_users"}],
        [{"text": "❌ Отмена", "callback_data": "cancel"}],
    ]}
    say(chat_id, "\n".join(parts)[:4000], reply_markup=kb)


def handle_callback(cb: dict):
    uid     = int(cb.get("from", {}).get("id", 0))
    chat_id = int(cb["message"]["chat"]["id"])
    msg_id  = int(cb["message"]["message_id"])
    data    = cb.get("data", "")
    try:
        tg("answerCallbackQuery", callback_query_id=cb["id"])
    except Exception:
        pass

    if data == "new_broadcast":
        _drafts[chat_id] = {
            "title": "", "text": "", "image_b64": "", "sound_b64": "",
            "volume": 100, "selected": set(), "pending": ""
        }
        edit_screen(chat_id, msg_id, _editor_text(_draft(chat_id)), editor_keyboard())
        return

    if data in ("add_text", "add_photo", "add_sound"):
        d = _draft(chat_id)
        d["pending"] = {"add_text": "text", "add_photo": "photo", "add_sound": "sound"}[data]
        prompts = {
            "add_text": "✏️ Отправьте текст одним сообщением.\nМожно указать заголовок так: Заголовок || Текст",
            "add_photo": "🖼 Теперь отправьте фотографию. Можно добавить подпись к фото.",
            "add_sound": "🔊 Теперь отправьте аудио или голосовое сообщение.",
        }
        edit_screen(chat_id, msg_id, prompts[data], {"inline_keyboard": [[
            {"text": "↩️ Назад", "callback_data": "back_editor"}
        ]]})
        return

    if data == "volume_menu":
        d = _draft(chat_id)
        volume = int(d.get("volume", 100))
        edit_screen(chat_id, msg_id, "🔉 Настройка громкости\nТекущее значение: %d%%" % volume,
                    volume_keyboard(volume))
        return

    if data.startswith("vol:"):
        d = _draft(chat_id)
        value = data[4:]
        current = int(d.get("volume", 100))
        if value == "+10":
            current += 10
        elif value == "-10":
            current -= 10
        else:
            try:
                current = int(value)
            except ValueError:
                current = 100
        d["volume"] = max(0, min(100, current))
        edit_screen(chat_id, msg_id,
                    "🔉 Настройка громкости\nТекущее значение: %d%%" % d["volume"],
                    volume_keyboard(d["volume"]))
        return

    if data == "back_editor":
        d = _draft(chat_id)
        d["pending"] = ""
        edit_screen(chat_id, msg_id, _editor_text(d), editor_keyboard())
        return

    if data == "preview":
        _show_preview(chat_id, _draft(chat_id))
        return

    # выбор пользователя (тоггл чекбокса)
    if data.startswith("usr:"):
        target_uid = data[4:]
        sel = _draft(chat_id).setdefault("selected", set())
        if target_uid in sel:
            sel.discard(target_uid)
        else:
            sel.add(target_uid)
        try:
            tg("editMessageReplyMarkup",
               chat_id=chat_id, message_id=msg_id,
               reply_markup=users_keyboard(sel))
        except Exception:
            pass
        return

    if data == "pick_users":
        d = _draft(chat_id)
        sel = d.get("selected") or set()
        kb  = users_keyboard(sel)
        if not users_list():
            try:
                tg("editMessageText", chat_id=chat_id, message_id=msg_id,
                   text="Пока нет зарегистрированных пользователей. "
                        "Дождитесь, когда кто-то запустит плагин.")
            except Exception:
                pass
            return
        try:
            tg("editMessageText", chat_id=chat_id, message_id=msg_id,
               text="Выберите получателей (нажмите чтобы отметить):",
               reply_markup=kb)
        except Exception:
            pass
        return

    if data in ("send_all", "send_sel"):
        d = _draft(chat_id)
        if not (d.get("text") or d.get("image_b64") or d.get("sound_b64")):
            say(chat_id, "ℹ️ Этот черновик уже опубликован или пуст. Создайте новое сообщение.")
            return
        if data == "send_sel":
            sel = list(d.get("selected") or set())
            if not sel:
                say(chat_id, "Никто не выбран. Нажмите «Всем» или отметьте пользователей.")
                return
            inactive = [target for target in sel if not user_is_active(target)]
            if inactive:
                labels = dict(users_list())
                names = [labels.get(target, "id" + target) for target in inactive]
                say(chat_id, "⛔ Не опубликовано: у выбранных пользователей плагин сейчас не на связи: "
                             + ", ".join(names)
                             + ". Это может означать, что плагин выключен, приложение закрыто или нет интернета.")
                return
        else:
            sel = active_user_ids()
            if not sel:
                say(chat_id, "⛔ Не опубликовано: сейчас нет пользователей с активным heartbeat плагина.")
                return
        new_id = publish(d.get("title", ""), d.get("text", ""),
                         d.get("image_b64", ""), d.get("sound_b64", ""),
                         targets=sel, volume=int(d.get("volume", 100)))
        who = "%d активным пользователям" % len(sel)
        result = ("✅ Объявление опубликовано на сервере для %s (№%d). "
                  "Это подтверждает публикацию, но не гарантирует фактический показ на устройстве.") % (who, new_id)
        _drafts.pop(chat_id, None)
        try:
            tg("editMessageText", chat_id=chat_id, message_id=msg_id, text=result)
        except Exception:
            say(chat_id, result)
        return

    if data == "cancel":
        _drafts.pop(chat_id, None)
        try:
            tg("editMessageText", chat_id=chat_id, message_id=msg_id,
               text="Отменено.", reply_markup=main_keyboard())
        except Exception:
            pass
        return


def run_bot():
    offset = 0
    while True:
        try:
            updates = tg("getUpdates", offset=offset, timeout=30,
                         allowed_updates=["message", "callback_query"]) or []
            for upd in updates:
                offset = upd["update_id"] + 1
                try:
                    if "message" in upd:
                        handle_message(upd["message"])
                    elif "callback_query" in upd:
                        handle_callback(upd["callback_query"])
                except Exception as exc:
                    print("update:", exc)
        except Exception as exc:
            print("getUpdates:", exc)
            time.sleep(3)


if __name__ == "__main__":
    load_all()
    threading.Thread(target=run_http, daemon=True).start()
    print("HTTP  http://0.0.0.0:%d/api" % PORT)
    if not TOKEN:
        print("BOT_TOKEN не задан — работает только HTTP API.")
        while True:
            time.sleep(3600)
    run_bot()
