
    
  
    if d["image_b64"]: parts.append("🖼 Картинка прикреплена")
    if d["sound_b64"]: parts.append("🔊 Звук прикреплён")
    kb = {"inline_keyboard": [[
        {"text": "📨 Отправить всем",         "callback_data": "send_all"},
        {"text": "👤 Выбрать получателей",    "callback_data": "pick_users"},
    ], [
        {"text": "🗑 Сбросить черновик",       "callback_data": "cancel"},
    ]]}
    say(chat_id, "\n".join(parts)[:4000], reply_markup=kb, parse_mode="Markdown")


def handle_callback(cb: dict):
    uid     = int(cb.get("from", {}).get("id", 0))
    chat_id = int(cb["message"]["chat"]["id"])
    msg_id  = int(cb["message"]["message_id"])
    data    = cb.get("data", "")
    try:
        tg("answerCallbackQuery", callback_query_id=cb["id"])
    except Exception:
        pass

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
                         targets=sel)
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
            tg("editMessageText", chat_id=chat_id, message_id=msg_id, text="Отменено.")
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

