import nest_asyncio
nest_asyncio.apply()

import aiohttp
import asyncio
import uuid
import urllib.parse
import os
import json
import datetime
import aiomax
from aiomax import Bot

# ============================================================
# КОНФИГУРАЦИЯ
# ============================================================

from secrets import MAX_TOKEN, GIGACHAT_CREDENTIALS
MODEL_NAME = "GigaChat-3-Ultra"
VIP_FILE = "vip.txt"
RESULTS_FILE = "results.txt"

DEBUG_NAMES = False

OAUTH_TIMEOUT = aiohttp.ClientTimeout(total=30, sock_connect=15)
GIGACHAT_TIMEOUT = aiohttp.ClientTimeout(total=30, sock_connect=10)

ROLE_PROMPTS = {
    "teacher": (
        "Ты опытный методист и помощник учителя. Помогай составлять инструктажи, "
        "планы уроков, задания и методические материалы. Отвечай профессионально и структурированно. "
        "Но понимай что количество токенов у тебя ограничено, отвечай по делу, без воды но доброжелательно."
    ),
    "student": (
        "Ты доброжелательный ИИ-репетитор для школьника. Объясняй материал просто и понятно, "
        "с примерами. Помогай с домашними заданиями, но не давай готовых ответов — направляй к решению. "
        "Но понимай что количество токенов у тебя ограничено, отвечай по делу, без воды но доброжелательно."
    ),
}

# ============================================================
# СЛОВАРЬ УРОКОВ
# ============================================================

lessons = {
    "физика": "https://drive.google.com/file/d/1lpKCsDmLqqDG_FWwOkBtq6ENE9UWxD3p/view?usp=drive_link",
}

# ============================================================
# ИНИЦИАЛИЗАЦИЯ БОТА И ХРАНИЛИЩА СОСТОЯНИЙ
# ============================================================

bot = Bot(MAX_TOKEN, default_format="markdown", use_certificate=True)

user_states = {}
pending_users = {}
active_tests = {}
greeted_admins = set()  # админы, которым меню уже показано в этой сессии


def set_user_state(user_id: int, state: str | None, data: dict | None = None):
    if state is None:
        user_states.pop(user_id, None)
    else:
        user_states[user_id] = {"state": state, "data": data or {}}


def get_user_state(user_id: int) -> str | None:
    entry = user_states.get(user_id)
    return entry["state"] if entry else None


def get_user_data(user_id: int) -> dict | None:
    entry = user_states.get(user_id)
    return entry["data"] if entry else None


def update_user_data(user_id: int, updates: dict):
    entry = user_states.get(user_id)
    if entry:
        entry["data"].update(updates)


def clear_user_state(user_id: int):
    user_states.pop(user_id, None)


# ============================================================
# ЛОГИРОВАНИЕ
# ============================================================

def _short(text: str, limit: int = 200) -> str:
    text = str(text).replace("\n", "\\n")
    if len(text) > limit:
        return text[:limit] + "…"
    return text


def log_incoming(user_name: str, user_id: int, text: str):
    print(f"📥 [{user_name} (id={user_id})] {_short(text)}")


def log_outgoing(recipient: str, text: str):
    print(f"📤 [→ {recipient}] {_short(text)}")


# ============================================================
# ХРАНИЛИЩЕ РОЛЕЙ
# ============================================================

def init_vip_file():
    if not os.path.exists(VIP_FILE):
        with open(VIP_FILE, "w", encoding="utf-8"):
            pass


def get_user_role(user_id: int) -> str | None:
    if not os.path.exists(VIP_FILE):
        return None
    with open(VIP_FILE, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or ":" not in line:
                continue
            uid_str, role = line.split(":", 1)
            try:
                if int(uid_str) == user_id:
                    return role.strip()
            except ValueError:
                continue
    return None


def set_user_role(user_id: int, role: str):
    lines = []
    found = False
    if os.path.exists(VIP_FILE):
        with open(VIP_FILE, "r", encoding="utf-8") as f:
            for line in f:
                stripped = line.strip()
                if not stripped or ":" not in stripped:
                    lines.append(line.rstrip("\n"))
                    continue
                uid_str, _ = stripped.split(":", 1)
                try:
                    if int(uid_str) == user_id:
                        lines.append(f"{user_id}:{role}")
                        found = True
                        continue
                except ValueError:
                    pass
                lines.append(line.rstrip("\n"))
    if not found:
        lines.append(f"{user_id}:{role}")
    with open(VIP_FILE, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + ("\n" if lines else ""))


def get_all_admins() -> list[int]:
    admins = []
    if not os.path.exists(VIP_FILE):
        return admins
    with open(VIP_FILE, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or ":" not in line:
                continue
            uid_str, role = line.split(":", 1)
            if role.strip() == "admin":
                try:
                    admins.append(int(uid_str))
                except ValueError:
                    continue
    return admins


# ============================================================
# GIGACHAT API
# ============================================================

async def get_sber_oauth_token() -> str | None:
    url = "https://ngw.devices.sberbank.ru:9443/api/v2/oauth"
    headers = {
        "Authorization": f"Basic {GIGACHAT_CREDENTIALS}",
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept": "application/json",
        "RqUID": str(uuid.uuid4()),
    }
    form_data = urllib.parse.urlencode({"scope": "GIGACHAT_API_PERS"})

    async with aiohttp.ClientSession(timeout=OAUTH_TIMEOUT) as session:
        try:
            async with session.post(
                url, headers=headers, data=form_data,
                ssl=False, allow_redirects=False,
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return data.get("access_token")
                err_text = await resp.text()
                print(f"Ответ Сбера (Статус {resp.status}): {err_text}")
                return None
        except Exception as e:
            print(f"Исключение при авторизации в Сбере: {e}")
            return None


async def ask_gigachat(user_message: str, system_prompt: str) -> str:
    token = await get_sber_oauth_token()
    if not token:
        return "Не удалось пройти авторизацию в системе GigaChat API."

    url = "https://api.giga.chat/v1/chat/completions"
    headers = {
        "Content-Type": "application/json; charset=utf-8",
        "Authorization": f"Bearer {token}",
    }
    payload = {
        "model": MODEL_NAME,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message},
        ],
        "temperature": 0.5,
    }

    async with aiohttp.ClientSession(timeout=GIGACHAT_TIMEOUT) as session:
        try:
            async with session.post(url, headers=headers, json=payload, ssl=False) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return data["choices"][0]["message"]["content"]
                err_text = await resp.text()
                return f"Сбер вернул ошибку {resp.status}: {err_text}"
        except Exception as e:
            return f"Ошибка сети при генерации ответа: {e}"


# ============================================================
# ИМЯ ПОЛЬЗОВАТЕЛЯ
# ============================================================

def _name_from_user(user) -> str | None:
    if user is None:
        return None
    if DEBUG_NAMES:
        fields = {}
        for a in ("user_id", "name", "username", "first_name", "last_name", "display_name", "is_bot"):
            fields[a] = getattr(user, a, None)
        print(f"[DEBUG] user fields: {fields}")

    first = getattr(user, "first_name", None)
    last = getattr(user, "last_name", None)
    if first or last:
        parts = [str(p).strip() for p in (first, last) if p and str(p).strip()]
        if parts:
            return " ".join(parts)

    name = getattr(user, "name", None)
    if name:
        return str(name)

    username = getattr(user, "username", None)
    if username:
        return str(username)

    return None


def _looks_like_user(obj) -> bool:
    if obj is None:
        return False
    if getattr(obj, "is_bot", False):
        return False
    return (
        hasattr(obj, "user_id")
        or hasattr(obj, "first_name")
        or hasattr(obj, "username")
        or hasattr(obj, "name")
    )


def _sender_from_cb(cb):
    for attr in ("user", "sender", "from_user", "from"):
        obj = getattr(cb, attr, None)
        if _looks_like_user(obj):
            return obj

    clicker_id = getattr(cb, "user_id", None) or getattr(cb, "from_user_id", None)
    msg = getattr(cb, "message", None)
    if msg is not None:
        sender = getattr(msg, "sender", None)
        if _looks_like_user(sender):
            sender_id = getattr(sender, "user_id", None)
            if sender_id and clicker_id and sender_id == clicker_id:
                return sender
    return None


async def _fetch_user_from_api(user_id: int):
    for method_name in ("get_user", "get_user_info", "get_user_by_id", "fetch_user", "get_member"):
        method = getattr(bot, method_name, None)
        if method is None:
            continue
        try:
            result = method(user_id)
            if hasattr(result, "__await__"):
                result = await result
            if result is not None:
                if DEBUG_NAMES:
                    print(f"[DEBUG] {method_name}({user_id}) → {result}")
                return result
        except Exception as e:
            if DEBUG_NAMES:
                print(f"[DEBUG] {method_name}({user_id}) failed: {e}")
    return None


async def resolve_user_display_name(cb, user_id: int | None = None) -> str:
    sender = _sender_from_cb(cb)
    if sender is not None:
        name = _name_from_user(sender)
        if name:
            return name

    if user_id is None:
        user_id = extract_callback_user_id(cb)
    if user_id:
        user_obj = await _fetch_user_from_api(user_id)
        if user_obj is not None:
            name = _name_from_user(user_obj)
            if name:
                return name
        return f"Ученик {user_id}"

    return "Ученик"


def extract_user_name(message) -> str:
    sender = getattr(message, "sender", None)
    if sender is not None:
        name = _name_from_user(sender)
        if name:
            return name
    return "Неизвестный"


# ============================================================
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# ============================================================

def extract_user_id(message) -> int | None:
    return (
        getattr(message.sender, "user_id", None)
        or getattr(message, "user_id", None)
    )


def extract_callback_user_id(cb) -> int | None:
    uid = getattr(cb, "user_id", None) or getattr(cb, "from_user_id", None)
    if uid:
        return uid
    msg = getattr(cb, "message", None)
    if msg is not None:
        sender = getattr(msg, "sender", None)
        if sender is not None:
            uid = getattr(sender, "user_id", None)
            if uid:
                return uid
    return None


async def notify_all_students(text: str):
    if not os.path.exists(VIP_FILE):
        return 0, 0

    student_ids = []
    with open(VIP_FILE, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or ":" not in line:
                continue
            uid_str, role = line.split(":", 1)
            if role.strip() == "student":
                try:
                    student_ids.append(int(uid_str))
                except ValueError:
                    continue

    success = 0
    failed = 0
    for uid in student_ids:
        try:
            await bot.send_message(text=text, user_id=uid)
            log_outgoing(f"ученик {uid}", text)
            success += 1
            await asyncio.sleep(0.05)
        except Exception as e:
            print(f"Не удалось отправить ученику {uid}: {e}")
            failed += 1
    return success, failed


# ============================================================
# УДАЛЕНИЕ КНОПОК ПОСЛЕ CALLBACK
# ============================================================

async def clear_buttons_after_callback(cb, text: str) -> bool:
    """
    Ответить на callback и попытаться убрать кнопки с исходного сообщения.
    Пробует несколько способов (разные версии aiomax поддерживают разное).
    """
    # Способ 1: keyboard=None — в большинстве версий означает "без клавиатуры"
    try:
        await cb.answer(text=text, keyboard=None)
        print("🔘 [buttons] cleared via keyboard=None")
        return True
    except TypeError as e:
        print(f"🔘 [buttons] keyboard=None: TypeError {e}")
    except Exception as e:
        print(f"🔘 [buttons] keyboard=None failed: {e}")

    # Способ 2: attachments=[] (не работает в некоторых версиях)
    try:
        await cb.answer(text=text, attachments=[])
        print("🔘 [buttons] cleared via attachments=[]")
        return True
    except Exception as e:
        print(f"🔘 [buttons] attachments=[] failed: {e}")

    # Способ 3: оба вместе
    try:
        await cb.answer(text=text, keyboard=None, attachments=[])
        print("🔘 [buttons] cleared via keyboard=None + attachments=[]")
        return True
    except Exception as e:
        print(f"🔘 [buttons] both failed: {e}")

    # Способ 4: прямое редактирование/удаление сообщения через bot
    msg = getattr(cb, "message", None)
    msg_id = None
    if msg is not None:
        msg_id = (
            getattr(msg, "message_id", None)
            or getattr(msg, "id", None)
            or getattr(getattr(msg, "body", None), "mid", None)
        )
    if msg_id:
        for method_name in ("edit_message", "edit", "update_message"):
            method = getattr(bot, method_name, None)
            if method is None:
                continue
            try:
                await method(message_id=msg_id, text=text, attachments=[])
                print(f"🔘 [buttons] cleared via bot.{method_name}")
                return True
            except TypeError:
                try:
                    await method(msg_id, text=text)
                    print(f"🔘 [buttons] cleared via bot.{method_name}(pos)")
                    return True
                except Exception as e:
                    print(f"🔘 [buttons] bot.{method_name} (pos) failed: {e}")
            except Exception as e:
                print(f"🔘 [buttons] bot.{method_name} failed: {e}")

        for method_name in ("delete_message", "remove_message"):
            method = getattr(bot, method_name, None)
            if method is None:
                continue
            try:
                await method(message_id=msg_id)
                print(f"🔘 [buttons] message deleted via bot.{method_name}")
                admin_id = extract_callback_user_id(cb)
                if admin_id:
                    try:
                        await bot.send_message(text=text, user_id=admin_id)
                    except Exception as e:
                        print(f"🔘 [buttons] send confirmation failed: {e}")
                return True
            except Exception as e:
                print(f"🔘 [buttons] bot.{method_name} failed: {e}")

    # Фолбэк: хотя бы текст
    try:
        await cb.answer(text=text)
        print("🔘 [buttons] text-only (buttons may remain)")
    except Exception as e:
        print(f"🔘 [buttons] text-only failed: {e}")
    return False


# ============================================================
# ЗАЯВКА НОВОГО ПОЛЬЗОВАТЕЛЯ
# ============================================================

async def notify_admins_about_new_user(user_id: int, user_name: str):
    admin_ids = get_all_admins()
    if not admin_ids:
        print("⚠ Нет ни одного администратора (id:admin в vip.txt). Никто не получит уведомление.")
        return

    text = (
        f"🔔 Новый пользователь!\n\n"
        f"👤 Имя: **{user_name}**\n"
        f"🆔 ID: `{user_id}`\n\n"
        f"Кем его сделать?"
    )
    for admin_id in admin_ids:
        try:
            kb = aiomax.buttons.KeyboardBuilder()
            kb.add(aiomax.buttons.CallbackButton("📚 Ученик", f"admin_assign:{user_id}:student"))
            kb.add(aiomax.buttons.CallbackButton("👨‍🏫 Учитель", f"admin_assign:{user_id}:teacher"))
            await bot.send_message(text=text, user_id=admin_id, keyboard=kb)
            log_outgoing(f"админ {admin_id}", text)
            await asyncio.sleep(0.05)
        except Exception as e:
            print(f"Не удалось уведомить админа {admin_id}: {e}")


async def try_request_access(message, user_id: int, user_name: str) -> bool:
    if user_id in pending_users:
        await message.reply(
            text="⏳ Ваша заявка уже на рассмотрении у администратора. Ожидайте."
        )
        log_outgoing(user_name, "уже в ожидании")
        return True

    pending_users[user_id] = user_name
    await message.reply(
        text="👋 Привет! Ваша заявка отправлена администратору.\n"
             "Ожидайте, пока вам назначат роль (ученик или учитель)."
    )
    log_outgoing(user_name, "заявка отправлена админам")
    await notify_admins_about_new_user(user_id, user_name)
    return True


# ============================================================
# КЛАВИАТУРЫ
# ============================================================

def build_teacher_menu():
    kb = aiomax.buttons.KeyboardBuilder()
    kb.add(aiomax.buttons.CallbackButton("📚 Материалы", "materials_menu"))
    kb.add(aiomax.buttons.CallbackButton("📝 Тест", "test_menu"))
    kb.add(aiomax.buttons.CallbackButton("📊 Результаты", "results_view"))
    return kb


def build_back_keyboard():
    kb = aiomax.buttons.KeyboardBuilder()
    kb.add(aiomax.buttons.CallbackButton("⬅ Обратно в меню", "back_to_menu"))
    return kb


def build_lessons_keyboard(prefix: str):
    if not lessons:
        return None
    kb = aiomax.buttons.KeyboardBuilder()
    count = 0
    for key in lessons:
        if not key:
            continue
        kb.add(aiomax.buttons.CallbackButton(key, f"{prefix}:{key}"))
        count += 1
        if count % 3 == 0:
            kb.row()
    return kb


def build_question_keyboard(index: int):
    kb = aiomax.buttons.KeyboardBuilder()
    kb.add(aiomax.buttons.CallbackButton("✅ Верно", f"answer:{index}:true"))
    kb.add(aiomax.buttons.CallbackButton("❌ Неверно", f"answer:{index}:false"))
    return kb


# ============================================================
# ГЕНЕРАЦИЯ ТЕСТА
# ============================================================

async def generate_test(subject: str) -> list[dict] | None:
    prompt = (
        f"Составь тест из 10 вопросов по теме «{subject}». "
        "Каждый вопрос должен иметь два варианта ответа: «Верно» и «Неверно». "
        "Укажи правильный ответ для каждого вопроса. "
        "Верни ответ строго в формате JSON-массива объектов с полями: "
        "\"question\" (текст вопроса) и \"correct\" (строка \"true\" или \"false\"). "
        "Не добавляй никаких пояснений, только JSON."
    )
    system = "Ты — составитель тестов. Отвечай только валидным JSON."
    raw = await ask_gigachat(prompt, system)

    start = raw.find("[")
    end = raw.rfind("]")
    if start == -1 or end == -1:
        print(f"Не удалось найти JSON в ответе: {raw[:300]}")
        return None
    try:
        data = json.loads(raw[start:end + 1])
    except json.JSONDecodeError as e:
        print(f"Ошибка парсинга JSON: {e}\nОтвет: {raw[:500]}")
        return None

    if not isinstance(data, list) or len(data) != 10:
        print(f"Ожидался список из 10 вопросов, получено: type={type(data)}")
        return None
    for item in data:
        if not isinstance(item, dict) or "question" not in item or "correct" not in item:
            print("Неверная структура вопроса")
            return None
    return data


# ============================================================
# КОММЕНТАРИЙ К РЕЗУЛЬТАТУ
# ============================================================

def get_result_comment(score: int, total: int) -> str:
    pct = (score / total) * 100 if total else 0
    if pct >= 90:
        return "🏆 Превосходно! Ты отлично знаешь материал!"
    elif pct >= 70:
        return "👍 Отличная работа! Ты хорошо усвоил тему."
    elif pct >= 50:
        return "🙂 Неплохо! Есть небольшие пробелы, но ты на верном пути."
    elif pct >= 30:
        return "📖 Стоит повторить материал — и результат улучшится."
    else:
        return "💪 Не расстраивайся! Повтори тему и попробуй снова."


# ============================================================
# СОХРАНЕНИЕ РЕЗУЛЬТАТОВ
# ============================================================

def save_result(user_id: int, user_name: str, subject: str, score: int, total: int):
    date_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    line = f"{user_id}|{user_name}|{subject}|{score}|{total}|{date_str}\n"
    with open(RESULTS_FILE, "a", encoding="utf-8") as f:
        f.write(line)


# ============================================================
# ОСНОВНОЙ ОБРАБОТЧИК СООБЩЕНИЙ
# ============================================================

@bot.on_message()
async def ai_handler(message):
    try:
        user_text = message.content
        user_id = extract_user_id(message)
        user_name = extract_user_name(message)

        if not user_text or str(user_text).strip() == "":
            log_incoming(user_name, user_id or 0, "<нетекстовое сообщение>")
            await message.reply(text="Я понимаю только текст! Пожалуйста, напиши мне вопрос 📝")
            log_outgoing(user_name, "Я понимаю только текст!")
            return

        if not user_id:
            log_incoming(user_name, 0, str(user_text))
            print("Не удалось получить user_id, игнорируем сообщение.")
            await message.reply(text="Не удалось определить ваш ID. Попробуйте позже.")
            return

        user_text = str(user_text)
        log_incoming(user_name, user_id, user_text)

        role = get_user_role(user_id)

        # Новый пользователь — отправляем заявку админам
        if role is None:
            await try_request_access(message, user_id, user_name)
            return

        # Админ: при первом сообщении в сессии показываем меню с приветствием
        if role == "admin" and user_id not in greeted_admins:
            greeted_admins.add(user_id)
            if user_text.strip().lower() not in ("/menu", "меню", "/start", "старт"):
                kb = build_teacher_menu()
                await message.reply(
                    text=(
                        "👑 Добро пожаловать, администратор!\n\n"
                        "Вам доступны все функции учителя: материалы, тесты, результаты. "
                        "Также вы получаете уведомления о новых пользователях и назначаете им роли.\n\n"
                        "📋 Меню:"
                    ),
                    keyboard=kb,
                )
                log_outgoing(user_name, "приветствие + меню администратора")

        if get_user_state(user_id) == "answering":
            answer_text = "Пожалуйста, отвечайте на вопросы кнопками «✅ Верно» / «❌ Неверно»."
            await message.reply(text=answer_text)
            log_outgoing(user_name, answer_text)
            return

        # Команда /start — меню для teacher/admin
        if role in ("teacher", "admin") and user_text.strip().lower() in ("/start", "старт"):
            kb = build_teacher_menu()
            await message.reply(text="📋 Меню:", keyboard=kb)
            log_outgoing(user_name, "меню по /start")
            return

        # Команда /menu — меню для teacher/admin
        if role in ("teacher", "admin") and user_text.strip().lower() in ("/menu", "меню"):
            kb = build_teacher_menu()
            await message.reply(text="📋 Меню:", keyboard=kb)
            log_outgoing(user_name, "меню по /menu")
            return

        # /test — быстрый выбор предмета для теста
        if role in ("teacher", "admin") and user_text.strip().lower() in ("/test", "тест", "задать тест"):
            kb = build_lessons_keyboard(prefix="test_subject")
            if kb is None:
                await message.reply(text="📭 Список уроков пуст. Добавь предметы в словарь `lessons`.")
                log_outgoing(user_name, "Список уроков пуст")
            else:
                await message.reply(text="📝 Выберите предмет для теста:", keyboard=kb)
                log_outgoing(user_name, "Выбор предмета для теста")
            return

        # Обычный ответ через GigaChat
        if role in ("teacher", "admin"):
            system_prompt = ROLE_PROMPTS["teacher"]
        else:
            system_prompt = ROLE_PROMPTS["student"]

        ai_response = await ask_gigachat(user_text, system_prompt=system_prompt)
        await message.reply(text=ai_response)
        log_outgoing(user_name, ai_response)

    except Exception as e:
        print(f"Критическая ошибка в боте: {e}")


# ============================================================
# НАЗНАЧЕНИЕ РОЛИ АДМИНОМ
# ============================================================

@bot.on_button_callback(lambda data: data.payload.startswith("admin_assign:"))
async def admin_assign_callback(cb: aiomax.Callback):
    try:
        parts = cb.payload.split(":")
        if len(parts) != 3:
            await clear_buttons_after_callback(cb, "❌ Некорректные данные.")
            return
        _, new_user_id_str, new_role = parts
        try:
            new_user_id = int(new_user_id_str)
        except ValueError:
            await clear_buttons_after_callback(cb, "❌ Некорректный ID пользователя.")
            return

        if new_role not in ("teacher", "student"):
            await clear_buttons_after_callback(cb, "❌ Некорректная роль.")
            return

        admin_id = extract_callback_user_id(cb)
        if not admin_id or get_user_role(admin_id) != "admin":
            await clear_buttons_after_callback(cb, "⛔ Только администратор может назначать роли.")
            return

        if get_user_role(new_user_id) is not None:
            pending_users.pop(new_user_id, None)
            await clear_buttons_after_callback(
                cb,
                f"⚠ Пользователю {new_user_id} уже назначена роль.",
            )
            return

        # Назначаем роль
        set_user_role(new_user_id, new_role)
        pending_users.pop(new_user_id, None)
        role_ru = "Учитель" if new_role == "teacher" else "Ученик"

        await clear_buttons_after_callback(
            cb,
            f"✅ Пользователь {new_user_id} назначен: **{role_ru}**.",
        )
        print(f"👑 Админ {admin_id} назначил пользователю {new_user_id} роль: {new_role}")

        # Уведомляем пользователя
        try:
            await bot.send_message(
                text=f"✅ Вам назначена роль: **{role_ru}**. Теперь можете пользоваться ботом.",
                user_id=new_user_id,
            )
            log_outgoing(f"новый {role_ru.lower()} {new_user_id}", f"назначена роль {role_ru}")

            if new_role == "teacher":
                kb = build_teacher_menu()
                await bot.send_message(
                    text="📋 Ваше меню учителя:",
                    user_id=new_user_id,
                    keyboard=kb,
                )
        except Exception as e:
            print(f"Не удалось уведомить пользователя {new_user_id}: {e}")
    except Exception as e:
        print(f"Ошибка в admin_assign_callback: {e}")
        try:
            await clear_buttons_after_callback(cb, "❌ Ошибка при назначении роли.")
        except Exception:
            pass


# ============================================================
# МЕНЮ «МАТЕРИАЛЫ»
# ============================================================

@bot.on_button_callback(lambda data: data.payload == "materials_menu")
async def materials_menu_callback(cb: aiomax.Callback):
    try:
        kb = build_lessons_keyboard(prefix="lesson")
        if kb is None:
            await cb.answer(text="📭 Список уроков пуст.", keyboard=build_back_keyboard())
            return
        await cb.answer(text="📚 Выберите предмет:", keyboard=kb)
    except Exception as e:
        print(f"Ошибка в меню «Материалы»: {e}")
        try:
            await cb.answer(text="❌ Не удалось открыть меню материалов.", keyboard=build_back_keyboard())
        except Exception:
            pass


# ============================================================
# РАССЫЛКА МАТЕРИАЛОВ
# ============================================================

@bot.on_button_callback(lambda data: data.payload.startswith("lesson:"))
async def lesson_callback(cb: aiomax.Callback):
    try:
        lesson_key = cb.payload.split(":", 1)[1]
        link = lessons.get(lesson_key, "").strip()

        if not link:
            await cb.answer(
                text=f"⚠ Урок «{lesson_key}» найден, но ссылка не задана.",
                keyboard=build_back_keyboard(),
            )
            return

        broadcast_text = f"📚 Новый урок: **{lesson_key}**\n🔗 {link}"
        ok, fail = await notify_all_students(broadcast_text)

        await cb.answer(
            text=f"✅ Отправлено: {ok}, не доставлено: {fail}.",
            keyboard=build_back_keyboard(),
        )
    except Exception as e:
        print(f"Ошибка в обработчике кнопки урока: {e}")
        try:
            await cb.answer(
                text="❌ Произошла ошибка при рассылке.",
                keyboard=build_back_keyboard(),
            )
        except Exception:
            pass


# ============================================================
# ВОЗВРАТ В МЕНЮ
# ============================================================

@bot.on_button_callback(lambda data: data.payload == "back_to_menu")
async def back_to_menu_callback(cb: aiomax.Callback):
    try:
        kb = build_teacher_menu()
        await cb.answer(text="📋 Меню:", keyboard=kb)
    except Exception as e:
        print(f"Ошибка возврата в меню: {e}")


# ============================================================
# МЕНЮ «ТЕСТ»
# ============================================================

@bot.on_button_callback(lambda data: data.payload == "test_menu")
async def test_menu_callback(cb: aiomax.Callback):
    try:
        kb = build_lessons_keyboard(prefix="test_subject")
        if kb is None:
            await cb.answer(text="📭 Список уроков пуст.", keyboard=build_back_keyboard())
            return
        await cb.answer(text="📝 Выберите предмет для теста:", keyboard=kb)
    except Exception as e:
        print(f"Ошибка в меню «Тест»: {e}")
        try:
            await cb.answer(text="❌ Не удалось открыть меню тестов.", keyboard=build_back_keyboard())
        except Exception:
            pass


# ============================================================
# ГЕНЕРАЦИЯ И РАССЫЛКА ТЕСТА
# ============================================================

@bot.on_button_callback(lambda data: data.payload.startswith("test_subject:"))
async def test_subject_callback(cb: aiomax.Callback):
    try:
        subject = cb.payload.split(":", 1)[1]
        await cb.answer(text=f"⏳ Составляю тест по предмету «{subject}»...")

        questions = await generate_test(subject)
        if questions is None:
            await cb.answer(
                text="❌ Не удалось составить тест. Попробуйте позже.",
                keyboard=build_back_keyboard(),
            )
            return

        active_tests[subject] = questions

        broadcast_text = (
            f"📝 Доступен тест по предмету **{subject}**!\n"
            f"Нажмите «▶ Начать тест», чтобы пройти 10 вопросов."
        )

        if not os.path.exists(VIP_FILE):
            await cb.answer(text="⚠ Файл с учениками не найден.", keyboard=build_back_keyboard())
            return

        student_ids = []
        with open(VIP_FILE, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or ":" not in line:
                    continue
                uid_str, role = line.split(":", 1)
                if role.strip() == "student":
                    try:
                        student_ids.append(int(uid_str))
                    except ValueError:
                        continue

        success = 0
        failed = 0
        for uid in student_ids:
            try:
                kb = aiomax.buttons.KeyboardBuilder()
                kb.add(aiomax.buttons.CallbackButton("▶ Начать тест", f"start_test:{subject}"))
                await bot.send_message(text=broadcast_text, user_id=uid, keyboard=kb)
                log_outgoing(f"ученик {uid}", broadcast_text)
                success += 1
                await asyncio.sleep(0.05)
            except Exception as e:
                print(f"Не удалось отправить тест ученику {uid}: {e}")
                failed += 1

        await cb.answer(
            text=f"✅ Тест разослан: {success}, не доставлено: {failed}.",
            keyboard=build_back_keyboard(),
        )
    except Exception as e:
        print(f"Ошибка в обработчике предмета теста: {e}")
        try:
            await cb.answer(text="❌ Ошибка при создании теста.", keyboard=build_back_keyboard())
        except Exception:
            pass


# ============================================================
# СТАРТ ТЕСТА УЧЕНИКОМ
# ============================================================

@bot.on_button_callback(lambda data: data.payload.startswith("start_test:"))
async def start_test_callback(cb: aiomax.Callback):
    try:
        subject = cb.payload.split(":", 1)[1]
        questions = active_tests.get(subject)
        if not questions:
            await cb.answer(text="❌ Тест по этому предмету ещё не создан.")
            return

        user_id = extract_callback_user_id(cb)
        if not user_id:
            await cb.answer(text="❌ Не удалось определить ваш ID.")
            return

        user_name = await resolve_user_display_name(cb, user_id)

        set_user_state(
            user_id,
            "answering",
            {
                "subject": subject,
                "questions": questions,
                "index": 0,
                "score": 0,
                "user_name": user_name,
            },
        )

        first_q = questions[0]
        kb = build_question_keyboard(0)
        await cb.answer(
            text=f"Вопрос 1/10:\n\n{first_q['question']}",
            keyboard=kb,
        )
    except Exception as e:
        print(f"Ошибка при старте теста: {e}")
        try:
            await cb.answer(text="❌ Не удалось начать тест.")
        except Exception:
            pass


# ============================================================
# ОТВЕТЫ НА ВОПРОСЫ ТЕСТА
# ============================================================

@bot.on_button_callback(lambda data: data.payload.startswith("answer:"))
async def answer_callback(cb: aiomax.Callback):
    try:
        parts = cb.payload.split(":")
        if len(parts) != 3:
            await clear_buttons_after_callback(cb, "❌ Некорректные данные ответа.")
            return
        _, index_str, answer_str = parts
        index = int(index_str)
        user_answer = answer_str == "true"

        user_id = extract_callback_user_id(cb)
        if not user_id:
            await clear_buttons_after_callback(cb, "❌ Не удалось определить ваш ID.")
            return

        state = get_user_state(user_id)
        if state != "answering":
            await clear_buttons_after_callback(cb, "⚠ Вы сейчас не проходите тест.")
            return

        data = get_user_data(user_id)
        if data is None:
            await clear_buttons_after_callback(cb, "⚠ Данные теста потеряны. Начните заново.")
            clear_user_state(user_id)
            return

        questions = data["questions"]
        current_index = data["index"]
        score = data["score"]

        if index != current_index:
            await clear_buttons_after_callback(cb, "⚠ Этот вопрос уже неактуален.")
            return

        correct = questions[index]["correct"].lower() == "true"
        if user_answer == correct:
            score += 1

        next_index = index + 1
        if next_index < len(questions):
            update_user_data(user_id, {"index": next_index, "score": score})
            next_q = questions[next_index]
            kb = build_question_keyboard(next_index)
            await cb.answer(
                text=f"Вопрос {next_index + 1}/10:\n\n{next_q['question']}",
                keyboard=kb,
            )
        else:
            subject = data["subject"]
            total = len(questions)
            user_name = data.get("user_name", "Ученик")
            save_result(user_id, user_name, subject, score, total)
            clear_user_state(user_id)

            pct = round(score / total * 100) if total else 0
            comment = get_result_comment(score, total)
            final_text = (
                f"🏁 Тест завершён!\n\n"
                f"📊 Ваш результат: **{score} из {total}** ({pct}%)\n\n"
                f"{comment}"
            )
            await clear_buttons_after_callback(cb, final_text)
            log_outgoing(user_name, f"результат теста: {score}/{total}")
    except Exception as e:
        print(f"Ошибка в обработчике ответа: {e}")
        try:
            await clear_buttons_after_callback(cb, "❌ Произошла ошибка при обработке ответа.")
        except Exception:
            pass


# ============================================================
# ПРОСМОТР РЕЗУЛЬТАТОВ (ДЛЯ УЧИТЕЛЯ / АДМИНА)
# ============================================================

@bot.on_button_callback(lambda data: data.payload == "results_view")
async def results_view_callback(cb: aiomax.Callback):
    try:
        if not os.path.exists(RESULTS_FILE):
            await cb.answer(text="📭 Пока нет результатов.", keyboard=build_back_keyboard())
            return

        rows = []
        with open(RESULTS_FILE, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                parts = line.split("|")
                if len(parts) < 6:
                    continue
                uid = int(parts[0])
                name = parts[1]
                subject = parts[2]
                score = int(parts[3])
                total = int(parts[4])
                date = parts[5]
                rows.append((uid, name, subject, score, total, date))

        if not rows:
            await cb.answer(text="📭 Пока нет результатов.", keyboard=build_back_keyboard())
            return

        text_lines = ["📊 **Результаты тестов:**\n"]
        for uid, name, subject, score, total, date in rows[-20:]:
            pct = round(score / total * 100) if total else 0
            text_lines.append(
                f"👤 **{name}**\n"
                f"   📚 {subject} — **{score}/{total}** ({pct}%)\n"
                f"   🕒 {date}\n"
            )

        if len(rows) > 20:
            text_lines.append(f"… и ещё {len(rows) - 20} записей.")

        await cb.answer(text="\n".join(text_lines), keyboard=build_back_keyboard())
    except Exception as e:
        print(f"Ошибка при просмотре результатов: {e}")
        try:
            await cb.answer(text="❌ Не удалось прочитать результаты.", keyboard=build_back_keyboard())
        except Exception:
            pass


# ============================================================
# ТОЧКА ВХОДА
# ============================================================

def main():
    init_vip_file()
    print("ИИ-агент для мессенджера MAX запущен.")
    print(f"Уроков в словаре: {len(lessons)}")

    admins = get_all_admins()
    if admins:
        print(f"Администраторы (id): {admins}")
    else:
        print("⚠ Внимание: ни один пользователь не назначен администратором.")
        print("  Добавьте в vip.txt строку вида: <user_id>:admin")

    bot.run()


if __name__ == "__main__":
    main()