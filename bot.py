import base64, hashlib, json, os, threading, time, urllib.error, urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

TOKEN      = os.environ.get("BOT_TOKEN", "").strip()
PORT       = int(os.environ.get("PORT", "8080"))
DATA_FILE  = os.environ.get("DATA_FILE",  "data.json")
USERS_FILE = os.environ.get("USERS_FILE", "users.json")
ADMIN_IDS  = {int(x) for x in os.environ.get("ADMIN_IDS", "").replace(" ", "").split(",") if x.strip().isdigit()}

# Плагин опрашивает сервер раз в минуту (ANNOUNCE_INTERVAL = 60),
# поэтому окно "онлайн" с запасом на пропущенные опросы.
HEARTBEAT_TTL = int(os.environ.get("HEARTBEAT_TTL", "150"))

# ------------------------------------------------------------------ хранилище
_lock  = threading.Lock()

_state = {
    "id": 0, "title": "", "text": "",
    "image": "", "sound": "", "volume": 100,
    "targets": [], "ts": 0,
}
_users: dict = {}
_drafts: dict = {}
_last_users_save = 0.0


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
    _state.update(_load_json(DATA_FILE, _state))
    _users.update(_load_json(USERS_FILE, {}))


def publish(title, text, image_b64="", sound_b64="", targets=None, volume=100) -> int:
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


def _touch_user(uid: str):
    """Heartbeat: обновляем ts известного пользователя. На диск пишем не чаще раза в 5 минут."""
    global _last_users_save
    if not uid:
        return
    now = int(time.time())
    with _lock:
        info = _users.get(uid)
        if info is None:
            return
        info["ts"] = now
        if now - _last_users_save >= 300:
            _last_users_save = now
            try:
                _save_json(USERS_FILE, _users)
            except Exception as exc:
                print("save users:", exc)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a): pass

    def do_GET(self):
        path = self.path.split("?", 1)[0].rstrip("/")

        if path == "/api":
            uid = ""
            qs  = self.path.split("?", 1)[1] if "?" in self.path else ""
            for part in qs.split("&"):
                if part.startswith("uid="):
                    uid = part[4:]
            uid = uid or self.headers.get("X-User-Id", "")
            _touch_user(uid)
            body = _api_body_for(uid)
            etag = '"%s"' % hashlib.md5(body).hexdigest()
            if self.headers.get("If-None-Match", "").strip() == etag:
                self._send(304, b"", etag=etag)
                return
            self._send(200, body,
                       content_type="application/json; charset=utf-8",
                       extra={"Cache-Control": "no-cache", "ETag": etag})

        elif path == "/api/users":
            with _lock:
                body = json.dumps(_users, ensure_ascii=False).encode()
            self._send(200, body, content_type="application/json; charset=utf-8")
        else:
            self._send(200, b"ok", content_type="text/plain")

    def do_POST(self):
        path = self.path.split("?", 1)[0].rstrip("/")
        if path == "/api/register":
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

    def _send(self, code, body, content_type="text/plain", etag="", extra=None):
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


def download_file(file_id: str) -> bytes:
    info = tg("getFile", file_id=file_id)
    url  = "https://api.telegram.org/file/bot%s/%s" % (TOKEN, info["file_path"])
    with urllib.request.urlopen(url, timeout=30) as r:
        return r.read()


def say(chat_id: int, text: str, **extra):
    try:
        return tg("sendMessage", chat_id=chat_id, text=text, **extra)
    except Exception as exc:
        print("sendMessage:", exc)


def edit_screen(chat_id: int, msg_id: int, text: str, keyboard=None):
    params = {"chat_id": chat_id, "message_id": msg_id, "text": text}
    if keyboard is not None:
        params["reply_markup"] = keyboard
    try:
        tg("editMessageText", **params)
    except Exception:
        say(chat_id, text, **({"reply_markup": keyboard} if keyboard else {}))


# ------------------------------------------------------------------ пользователи
def users_list():
    with _lock:
        rows = list(_users.items())
    result = []
    for uid, info in rows:
        name  = info.get("first_name", "").strip()
        uname = info.get("username", "").strip()
        label = name or ("@" + uname if uname else "id" + uid)
        if uname:
            label += " (@%s)" % uname
        result.append((uid, label))
    return sorted(result, key=lambda x: x[1].lower())


def user_is_active(uid: str, now=None) -> bool:
    now = int(now or time.time())
    with _lock:
        info = _users.get(str(uid), {})
        try:
            last_seen = int(info.get("ts", 0) or 0)
        except Exception:
            last_seen = 0
    return last_seen > 0 and (now - last_seen) <= HEARTBEAT_TTL


def active_user_ids():
    now = int(time.time())
    with _lock:
        rows = list(_users.items())
    return [str(uid) for uid, info in rows
            if int(info.get("ts", 0) or 0) > 0
            and now - int(info.get("ts", 0) or 0) <= HEARTBEAT_TTL]


def user_status_label(uid: str) -> str:
    return "🟢 онлайн" if user_is_active(uid) else "⚪ офлайн"


def users_keyboard(selected=None) -> dict:
    """Inline-список людей с галочками (выбор получателей)."""
    rows = []
    selected = selected or set()
    for uid, label in users_list():
        mark   = "✅ " if uid in selected else ""
        status = "🟢 " if user_is_active(uid) else "⚪ "
        rows.append([{"text": mark + status + label, "callback_data": "usr:" + uid}])
    rows.append([{"text": "✔️ Готово", "callback_data": "back_editor"}])
    return {"inline_keyboard": rows}


# ------------------------------------------------------------------ нижняя клавиатура
BTN_NEW, BTN_USERS, BTN_STATUS = "📨 Новая рассылка", "👥 Пользователи", "📊 Статус"
BTN_CLEAR, BTN_HELP            = "🧹 Снять сообщение", "❓ Помощь"

BTN_TEXT, BTN_PHOTO, BTN_SOUND = "✏️ Текст", "🖼 Фото", "🔊 Звук"
BTN_VOL, BTN_PREVIEW, BTN_PICK = "🔉 Громкость", "👀 Предпросмотр", "👥 Получатели"
BTN_SEND_ALL, BTN_SEND_SEL     = "🚀 Всем онлайн", "📨 Выбранным"
BTN_CANCEL                     = "❌ Отмена"


def _reply_kb(rows) -> dict:
    return {"keyboard": [[{"text": t} for t in row] for row in rows],
            "resize_keyboard": True, "is_persistent": True}


def main_kb() -> dict:
    return _reply_kb([[BTN_NEW, BTN_USERS], [BTN_STATUS, BTN_CLEAR], [BTN_HELP]])


def editor_kb() -> dict:
    return _reply_kb([
        [BTN_TEXT, BTN_PHOTO], [BTN_SOUND, BTN_VOL],
        [BTN_PREVIEW, BTN_PICK], [BTN_SEND_ALL, BTN_SEND_SEL],
        [BTN_CANCEL],
    ])


def volume_keyboard(volume: int) -> dict:
    row = []
    for value in (0, 25, 50, 75, 100):
        mark = " ✓" if value == volume else ""
        row.append({"text": "%d%%%s" % (value, mark), "callback_data": "vol:%d" % value})
    return {"inline_keyboard": [row,
            [{"text": "−10", "callback_data": "vol:-10"}, {"text": "+10", "callback_data": "vol:+10"}],
            [{"text": "↩️ Назад", "callback_data": "back_editor"}]]}


HELP = (
    "📢 Рассылка Universal\n\n"
    "Всё управление — кнопками внизу.\n"
    "«📨 Новая рассылка» → добавьте текст / фото / звук → «🚀 Всем онлайн» "
    "или выберите людей через «👥 Получатели» и нажмите «📨 Выбранным».\n"
    "Заголовок и текст можно прислать одним сообщением: Заголовок || Текст\n\n"
    "Команды тоже работают: /menu /users /status /clear"
)


# ------------------------------------------------------------------ черновик
def _draft(chat_id: int) -> dict:
    return _drafts.setdefault(chat_id, {
        "title": "", "text": "", "image_b64": "", "sound_b64": "",
        "volume": 100, "selected": set(), "pending": ""
    })


def _editor_text(d: dict) -> str:
    sel = d.get("selected") or set()
    return "\n".join([
        "📝 Настройка рассылки",
        "Текст: %s" % ("✅" if d.get("text") else "—"),
        "Фото: %s" % ("✅" if d.get("image_b64") else "—"),
        "Звук: %s" % ("✅" if d.get("sound_b64") else "—"),
        "Громкость: %d%%" % int(d.get("volume", 100)),
        "Получатели: %s" % ("выбрано %d" % len(sel) if sel else "не выбраны (кнопка «Всем онлайн»)"),
    ])


def show_editor(chat_id: int, prefix: str = ""):
    say(chat_id, (prefix + "\n\n" if prefix else "") + _editor_text(_draft(chat_id)),
        reply_markup=editor_kb())


def show_main(chat_id: int, text: str = "📢 Главное меню"):
    say(chat_id, text, reply_markup=main_kb())


def show_preview(chat_id: int):
    d = _draft(chat_id)
    parts = ["👀 Предпросмотр:"]
    if d.get("title"): parts.append(d["title"])
    if d.get("text"):  parts.append(d["text"])
    if d.get("image_b64"): parts.append("🖼 Картинка прикреплена")
    if d.get("sound_b64"): parts.append("🔊 Звук прикреплён, громкость: %d%%" % int(d.get("volume", 100)))
    if not any((d.get("text"), d.get("image_b64"), d.get("sound_b64"))):
        parts.append("Пока ничего не добавлено.")
    say(chat_id, "\n".join(parts)[:4000], reply_markup=editor_kb())


def do_publish(chat_id: int, only_selected: bool) -> str:
    """Публикует черновик. Возвращает текст результата."""
    d = _draft(chat_id)
    if not (d.get("text") or d.get("image_b64") or d.get("sound_b64")):
        return "ℹ️ Черновик пуст. Добавьте текст, фото или звук."
    if only_selected:
        sel = list(d.get("selected") or set())
        if not sel:
            return "Никто не выбран. Нажмите «👥 Получатели» или «🚀 Всем онлайн»."
        offline = [t for t in sel if not user_is_active(t)]
        if offline:
            labels = dict(users_list())
            return ("⛔ Не опубликовано: сейчас офлайн — " +
                    ", ".join(labels.get(t, "id" + t) for t in offline) +
                    ".\nПлагин может быть выключен, приложение закрыто или нет интернета.")
    else:
        sel = active_user_ids()
        if not sel:
            return "⛔ Не опубликовано: сейчас нет пользователей онлайн."
    new_id = publish(d.get("title", ""), d.get("text", ""),
                     d.get("image_b64", ""), d.get("sound_b64", ""),
                     targets=sel, volume=int(d.get("volume", 100)))
    _drafts.pop(chat_id, None)
    return ("✅ Опубликовано для %d польз. (№%d). Плагин получит сообщение при ближайшем опросе "
            "(до минуты); фактический показ на устройстве не гарантирован.") % (len(sel), new_id)


# ------------------------------------------------------------------ действия
def action_new(chat_id):
    _drafts[chat_id] = {"title": "", "text": "", "image_b64": "", "sound_b64": "",
                        "volume": 100, "selected": set(), "pending": ""}
    show_editor(chat_id)


def action_users(chat_id):
    rows = users_list()
    if not rows:
        show_main(chat_id, "Пока нет зарегистрированных пользователей.")
        return
    online = sum(1 for uid, _ in rows if user_is_active(uid))
    lines = ["👥 Пользователи плагина: %d (онлайн %d)" % (len(rows), online)]
    for uid, label in rows[:50]:
        lines.append("• %s — %s  [%s]" % (label, user_status_label(uid), uid))
    say(chat_id, "\n".join(lines)[:4000])


def action_status(chat_id):
    with _lock:
        cur = dict(_state)
    if cur["text"] or cur["image"] or cur["sound"]:
        parts = ["Сейчас опубликовано (№%d):" % cur["id"]]
        if cur["title"]: parts.append(cur["title"])
        if cur["text"]:  parts.append(cur["text"])
        if cur["image"]: parts.append("🖼 Картинка есть")
        if cur["sound"]: parts.append("🔊 Звук есть, громкость %d%%" % int(cur.get("volume", 100)))
        tgts = cur.get("targets") or []
        parts.append("Получатели: %s" % (", ".join(tgts) if tgts else "все"))
        say(chat_id, "\n".join(parts)[:4000])
    else:
        say(chat_id, "Сейчас ничего не показывается.")


def action_clear(chat_id):
    publish("", "", "", "", [], volume=100)
    say(chat_id, "✅ Сообщение снято.")


def action_prompt(chat_id, kind):
    d = _draft(chat_id)
    d["pending"] = kind
    say(chat_id, {
        "text":  "✏️ Отправьте текст одним сообщением.\nС заголовком: Заголовок || Текст",
        "photo": "🖼 Отправьте фото (можно с подписью — она станет текстом).",
        "sound": "🔊 Отправьте аудиофайл или голосовое.",
    }[kind], reply_markup=editor_kb())


def action_volume(chat_id):
    v = int(_draft(chat_id).get("volume", 100))
    say(chat_id, "🔉 Громкость звука: %d%%" % v, reply_markup=volume_keyboard(v))


def action_pick(chat_id):
    if not users_list():
        say(chat_id, "Пока нет зарегистрированных пользователей. Дождитесь, когда кто-то запустит плагин.",
            reply_markup=editor_kb())
        return
    say(chat_id, "Выберите получателей (нажмите, чтобы отметить):",
        reply_markup=users_keyboard(_draft(chat_id).get("selected") or set()))


def action_cancel(chat_id):
    _drafts.pop(chat_id, None)
    show_main(chat_id, "Отменено.")


def finish_publish(chat_id, only_selected):
    result = do_publish(chat_id, only_selected)
    if result.startswith("✅"):
        show_main(chat_id, result)
    else:
        say(chat_id, result, reply_markup=editor_kb() if chat_id in _drafts else main_kb())


BUTTONS = {
    BTN_NEW: action_new, BTN_USERS: action_users, BTN_STATUS: action_status,
    BTN_CLEAR: action_clear, BTN_HELP: lambda c: say(c, HELP, reply_markup=main_kb()),
    BTN_TEXT: lambda c: action_prompt(c, "text"),
    BTN_PHOTO: lambda c: action_prompt(c, "photo"),
    BTN_SOUND: lambda c: action_prompt(c, "sound"),
    BTN_VOL: action_volume, BTN_PREVIEW: show_preview, BTN_PICK: action_pick,
    BTN_SEND_ALL: lambda c: finish_publish(c, False),
    BTN_SEND_SEL: lambda c: finish_publish(c, True),
    BTN_CANCEL: action_cancel,
}
COMMANDS = {"/menu": lambda c: show_main(c), "/start": lambda c: show_main(c),
            "/help": BUTTONS[BTN_HELP], "/users": action_users,
            "/status": action_status, "/clear": action_clear}


def allowed(user_id: int) -> bool:
    return not ADMIN_IDS or int(user_id) in ADMIN_IDS


# ------------------------------------------------------------------ обработчики
def handle_message(msg: dict):
    chat_id = int(msg["chat"]["id"])
    if not allowed(msg.get("from", {}).get("id", 0)):
        return
    text    = (msg.get("text") or "").strip()
    photo   = msg.get("photo")
    audio   = msg.get("audio") or msg.get("voice") or msg.get("document")
    caption = (msg.get("caption") or "").strip()

    # кнопки нижней клавиатуры и команды — всегда в приоритете
    action = BUTTONS.get(text) or COMMANDS.get(text.split("@")[0])
    if action:
        if chat_id in _drafts:
            _drafts[chat_id]["pending"] = ""
        action(chat_id)
        return

    d = _draft(chat_id)
    pending = d.get("pending", "")

    if photo:
        try:
            d["image_b64"] = base64.b64encode(download_file(photo[-1]["file_id"])).decode()
            if caption:
                d["text"] = caption
            d["pending"] = ""
            show_editor(chat_id, "✅ Фото добавлено.")
        except Exception as exc:
            say(chat_id, "Не удалось скачать фото: " + str(exc))
        return

    if audio and not text:
        file_id = audio.get("file_id", "") if isinstance(audio, dict) else ""
        if not file_id:
            say(chat_id, "Не могу получить файл.")
            return
        try:
            d["sound_b64"] = base64.b64encode(download_file(file_id)).decode()
            d["pending"] = ""
            show_editor(chat_id, "✅ Звук добавлен.")
        except Exception as exc:
            say(chat_id, "Не удалось скачать аудио: " + str(exc))
        return

    if text.startswith("/send"):
        body = text[5:].strip()
        if body:
            d["text"] = body
        d["pending"] = ""
        show_preview(chat_id)
        return

    if text.startswith("/"):
        say(chat_id, HELP, reply_markup=main_kb())
        return

    if text:
        if "||" in text:
            title, body = text.split("||", 1)
            d["title"], d["text"] = title.strip(), body.strip()
        else:
            d["text"] = text
        d["pending"] = ""
        show_editor(chat_id, "✅ Текст добавлен.")
        return

    show_main(chat_id, "Используйте кнопки внизу.")


def handle_callback(cb: dict):
    if not allowed(cb.get("from", {}).get("id", 0)):
        return
    chat_id = int(cb["message"]["chat"]["id"])
    msg_id  = int(cb["message"]["message_id"])
    data    = cb.get("data", "")
    try:
        tg("answerCallbackQuery", callback_query_id=cb["id"])
    except Exception:
        pass

    if data.startswith("vol:"):
        d = _draft(chat_id)
        value, current = data[4:], int(_draft(chat_id).get("volume", 100))
        if value == "+10":   current += 10
        elif value == "-10": current -= 10
        else:
            try: current = int(value)
            except ValueError: current = 100
        d["volume"] = max(0, min(100, current))
        edit_screen(chat_id, msg_id, "🔉 Громкость звука: %d%%" % d["volume"], volume_keyboard(d["volume"]))
        return

    if data.startswith("usr:"):
        sel = _draft(chat_id).setdefault("selected", set())
        target = data[4:]
        sel.discard(target) if target in sel else sel.add(target)
        try:
            tg("editMessageReplyMarkup", chat_id=chat_id, message_id=msg_id,
               reply_markup=users_keyboard(sel))
        except Exception:
            pass
        return

    if data == "back_editor":
        d = _draft(chat_id)
        d["pending"] = ""
        edit_screen(chat_id, msg_id, _editor_text(d))
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
    if not ADMIN_IDS:
        print("ВНИМАНИЕ: ADMIN_IDS не задан — бот отвечает любому пользователю.")
    if not TOKEN:
        print("BOT_TOKEN не задан — работает только HTTP API.")
        while True:
            time.sleep(3600)
    run_bot()
