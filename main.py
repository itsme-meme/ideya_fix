import nest_asyncio
nest_asyncio.apply()

import aiohttp
import asyncio
import uuid
import urllib.parse
import os
import json
import datetime
import re
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
# УРОКИ И ТЕМЫ
# Структура: { урок: { тема: ссылка_на_изображение } }
# ============================================================

lessons = {
    "физика": {
        "общее": "https://drive.google.com/file/d/1bOzF5M1CZtMIzJLFROGl_GBJzC_nxvCs/view?usp=drive_link",
        "работа со стеклом": "https://drive.google.com/file/d/1RZUuUSNa9HuL4PsyiPt4pIVjx0zjt_DC/view?usp=drive_link",
        "работа с весами": "https://drive.google.com/file/d/1CVbvr5rhEMt-RsvGlN_YmXCS7AWYaaBB/view?usp=drive_link",
        "работа с горячими вещами": "https://drive.google.com/file/d/10uso0bIDi8mpe1JJ9a_PlJLoo-8Z4Bw1/view?usp=drive_link",
        "работа с электричеством": "https://drive.google.com/file/d/1PB-AIW29EVgn3wf1JI8evkcUHqXrjFTL/view?usp=drive_link",
        "работа с оптикой": "https://drive.google.com/file/d/1s3Giu33mZNPRd4QVvXyyWI2Ni8gf4YXX/view?usp=drive_link",
        "действия при ЧС": "https://drive.google.com/file/d/1IfDJ_v0PBNOscoiM-mn7eS1grvYxLZhh/view?usp=drive_link",
    },
    "математика": {
        "Алгебра": "https://drive.google.com/file/d/example_algebra/view",
        "Геометрия": "https://drive.google.com/file/d/example_geometry/view",
    },
}

# ============================================================
# ИНИЦИАЛИЗАЦИЯ БОТА И ХРАНИЛИЩА СОСТОЯНИЙ
# ============================================================

bot = Bot(MAX_TOKEN, default_format="markdown", use_certificate=True)

user_states = {}
pending_users = {}
active_tests = {}
greeted_admins = set()
pending_notifications = {}

# Кэш токена и file_id изображений
_token_cache = {"token": None, "expires": None}
_image_file_id_cache = {}  # url -> file_id


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

async def get_sber_oauth_token(force_refresh: bool = False) -> str | None:
    now = datetime.datetime.now()
    if (
        not force_refresh
        and _token_cache["token"]
        and _token_cache["expires"]
        and now < _token_cache["expires"]
    ):
        return _token_cache["token"]

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
                    token = data.get("access_token")
                    if token:
                        _token_cache["token"] = token
                        _token_cache["expires"] = now + datetime.timedelta(minutes=25)
                    return token
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


async def upload_file_to_gigachat(
    image_bytes: bytes,
    filename: str,
    content_type: str,
) -> str | None:
    """Загрузить файл в хранилище GigaChat. Возвращает file_id."""
    token = await get_sber_oauth_token()
    if not token:
        return None

    url = "https://api.giga.chat/v1/files"
    headers = {"Authorization": f"Bearer {token}"}

    form = aiohttp.FormData()
    form.add_field("purpose", "general")
    form.add_field("file", image_bytes, filename=filename, content_type=content_type)

    timeout = aiohttp.ClientTimeout(total=60, sock_connect=15)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        try:
            async with session.post(url, headers=headers, data=form, ssl=False) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return data.get("id")
                err = await resp.text()
                print(f"⬆ [upload] ошибка {resp.status}: {err[:300]}")
                return None
        except Exception as e:
            print(f"⬆ [upload] исключение: {e}")
            return None


async def ask_gigachat_with_image(
    prompt: str,
    system_prompt: str,
    file_id: str,
) -> str:
    """Отправить в GigaChat запрос с изображением (через attachments)."""
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
            {"role": "user", "content": prompt, "attachments": [file_id]},
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
# СКАЧИВАНИЕ ИЗОБРАЖЕНИЯ
# ============================================================

def _normalize_image_url(url: str) -> str:
    """Превращает Google Drive view-ссылки в прямые ссылки на скачивание."""
    url = url.strip()
    if "drive.google.com" in url and "/file/d/" in url:
        m = re.search(r"/file/d/([a-zA-Z0-9_-]+)", url)
        if m:
            file_id = m.group(1)
            return f"https://drive.google.com/uc?export=download&id={file_id}"
    return url


async def download_image(url: str) -> tuple[bytes, str, str] | None:
    """Скачать изображение. Возвращает (bytes, content_type, filename) или None."""
    normalized = _normalize_image_url(url)
    timeout = aiohttp.ClientTimeout(total=60, sock_connect=15)

    async with aiohttp.ClientSession(timeout=timeout) as session:
        try:
            async with session.get(normalized, allow_redirects=True, ssl=False) as resp:
                if resp.status != 200:
                    print(f"⬇ [image] HTTP {resp.status} для {normalized[:120]}")
                    return None

                content_type = (resp.headers.get("Content-Type") or "").split(";")[0].strip()
                data = await resp.read()

                if len(data) > 20 * 1024 * 1024:
                    print(f"⬇ [image] файл слишком большой: {len(data)} байт")
                    return None
                if len(data) < 100:
                    print(f"⬇ [image] файл слишком маленький: {len(data)} байт")
                    return None

                if not content_type.startswith("image/"):
                    print(
                        f"⬇ [image] Content-Type не изображение: {content_type}. "
                        f"Возможно, ссылка ведёт на страницу, а не на файл."
                    )
                    return None

                ext_map = {
                    "image/jpeg": "jpg",
                    "image/png": "png",
                    "image/webp": "webp",
                    "image/gif": "gif",
                    "image/bmp": "bmp",
                }
                ext = ext_map.get(content_type, "jpg")
                filename = f"topic_image.{ext}"
                return data, content_type, filename
        except Exception as e:
            print(f"⬇ [image] исключение: {e}")
            return None


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
    admin_id = extract_callback_user_id(cb)

    msg = getattr(cb, "message", None)
    msg_id = None
    if msg is not None:
        msg_id = (
            getattr(msg, "message_id", None)
            or getattr(msg, "id", None)
            or getattr(getattr(msg, "body", None), "mid", None)
        )

    if msg_id:
        deleted = False
        for method_name in ("delete_message", "remove_message"):
            method = getattr(bot, method_name, None)
            if method is None:
                continue
            try:
                await method(message_id=msg_id)
                print(f"🔘 [buttons] deleted message {msg_id} via bot.{method_name}")
                deleted = True
                break
            except Exception as e:
                print(f"🔘 [buttons] bot.{method_name} failed: {e}")

        if deleted and admin_id:
            try:
                await bot.send_message(text=text, user_id=admin_id)
                print(f"🔘 [buttons] sent new clean message to admin {admin_id}")
                return True
            except Exception as e:
                print(f"🔘 [buttons] send new failed: {e}")

    for kwargs in (
        {"text": text, "keyboard": None},
        {"text": text, "attachments": []},
        {"text": text, "keyboard": None, "attachments": []},
    ):
        try:
            await cb.answer(**kwargs)
            print(f"🔘 [buttons] fallback cb.answer({list(kwargs.keys())})")
            return True
        except Exception as e:
            print(f"🔘 [buttons] cb.answer failed: {e}")

    try:
        await cb.answer(text=text)
        print("🔘 [buttons] text-only (buttons may remain)")
    except Exception as e:
        print(f"🔘 [buttons] text-only failed: {e}")
    return False


# ============================================================
# УДАЛЕНИЕ УВЕДОМЛЕНИЯ У ВСЕХ АДМИНОВ
# ============================================================

def _extract_message_id(result) -> int | str | None:
    if result is None:
        return None
    if isinstance(result, (int, str)):
        return result
    for attr in ("message_id", "id", "mid"):
        val = getattr(result, attr, None)
        if val:
            return val
    body = getattr(result, "body", None)
    if body is not None:
        for attr in ("message_id", "mid", "id"):
            val = getattr(body, attr, None)
            if val:
                return val
    return None


async def hide_admin_notification_for_all(
    new_user_id: int,
    result_text: str,
    skip_admin_id: int | None = None,
):
    entries = pending_notifications.pop(new_user_id, [])
    if not entries:
        return

    for admin_id, msg_id in entries:
        if skip_admin_id is not None and admin_id == skip_admin_id:
            continue
        if not msg_id:
            print(f"🧹 [hide] нет message_id для админа {admin_id} — пропускаем")
            continue

        deleted = False
        for method_name in ("delete_message", "remove_message"):
            method = getattr(bot, method_name, None)
            if method is None:
                continue
            try:
                await method(message_id=msg_id)
                print(f"🧹 [hide] удалено сообщение {msg_id} у админа {admin_id}")
                deleted = True
                break
            except Exception as e:
                print(f"🧹 [hide] {method_name} для {admin_id}: {e}")

        if not deleted:
            print(
                f"🧹 [hide] ⚠ не удалось удалить сообщение {msg_id} у админа {admin_id}. "
                f"Проверьте, поддерживает ли ваша версия aiomax bot.delete_message."
            )


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
    pending_notifications.setdefault(user_id, [])
    for admin_id in admin_ids:
        try:
            kb = aiomax.buttons.KeyboardBuilder()
            kb.add(aiomax.buttons.CallbackButton("📚 Ученик", f"admin_assign:{user_id}:student"))
            kb.add(aiomax.buttons.CallbackButton("👨‍🏫 Учитель", f"admin_assign:{user_id}:teacher"))
            result = await bot.send_message(text=text, user_id=admin_id, keyboard=kb)
            msg_id = _extract_message_id(result)
            if msg_id:
                pending_notifications[user_id].append((admin_id, msg_id))
                print(f"🧷 [notify] сохранил message_id={msg_id} для админа {admin_id}")
            else:
                print(f"🧷 [notify] не удалось получить message_id для админа {admin_id}")
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
    """Кнопки уроков (верхний уровень lessons)."""
    if not lessons:
        return None
    kb = aiomax.buttons.KeyboardBuilder()
    count = 0
    for lesson_name in lessons:
        if not lesson_name:
            continue
        kb.add(aiomax.buttons.CallbackButton(lesson_name, f"{prefix}:{lesson_name}"))
        count += 1
        if count % 3 == 0:
            kb.row()
    return kb if count > 0 else None


def build_topics_keyboard(lesson_name: str, prefix: str, back_payload: str):
    """
    Кнопки тем для выбранного урока + кнопка «Назад».
    prefix: m_topic / t_topic
    back_payload: m_back / t_back
    """
    topics = lessons.get(lesson_name)
    if not isinstance(topics, dict) or not topics:
        return None

    kb = aiomax.buttons.KeyboardBuilder()
    count = 0
    for topic in topics:
        if not topic:
            continue
        kb.add(aiomax.buttons.CallbackButton(topic, f"{prefix}:{lesson_name}:{topic}"))
        count += 1
        if count % 3 == 0:
            kb.row()
    if count == 0:
        return None

    if count % 3 != 0:
        kb.row()
    kb.add(aiomax.buttons.CallbackButton("⬅ Назад", back_payload))
    return kb


def build_question_keyboard(index: int):
    kb = aiomax.buttons.KeyboardBuilder()
    kb.add(aiomax.buttons.CallbackButton("✅ Верно", f"answer:{index}:true"))
    kb.add(aiomax.buttons.CallbackButton("❌ Неверно", f"answer:{index}:false"))
    return kb


# ============================================================
# ГЕНЕРАЦИЯ ТЕСТА (ПО ТЕКСТУ) — не используется, оставлена на будущее
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
# ГЕНЕРАЦИЯ ТЕСТА (ПО ИЗОБРАЖЕНИЮ)
# ============================================================

async def generate_test_from_image(image_url: str, subject: str) -> list[dict] | None:
    """Скачать изображение → загрузить в GigaChat → сгенерировать тест по нему."""

    file_id = _image_file_id_cache.get(image_url)

    if not file_id:
        print(f"⬇ [test] скачиваю изображение по теме «{subject}»")
        dl = await download_image(image_url)
        if not dl:
            print("❌ [test] не удалось скачать изображение")
            return None
        image_bytes, content_type, filename = dl
        print(f"⬇ [test] скачано {len(image_bytes)} байт, тип {content_type}")

        print("⬆ [test] загружаю изображение в GigaChat")
        file_id = await upload_file_to_gigachat(image_bytes, filename, content_type)
        if not file_id:
            print("❌ [test] не удалось загрузить изображение в GigaChat")
            return None
        print(f"⬆ [test] file_id={file_id}")
        _image_file_id_cache[image_url] = file_id
    else:
        print(f"♻ [test] использую кэшированный file_id={file_id}")

    prompt = (
        f"Внимательно изучи изображение по теме «{subject}». "
        f"Составь тест из 10 вопросов на основе материала с изображения. "
        "Каждый вопрос должен иметь два варианта ответа: «Верно» и «Неверно». "
        "Укажи правильный ответ для каждого вопроса. "
        "Верни ответ строго в формате JSON-массива объектов с полями: "
        "\"question\" (текст вопроса) и \"correct\" (строка \"true\" или \"false\"). "
        "Не добавляй никаких пояснений, только JSON."
    )
    system = "Ты — составитель тестов. Отвечай только валидным JSON."
    raw = await ask_gigachat_with_image(prompt, system, file_id)

    if raw.startswith("Сбер вернул ошибку") or raw.startswith("Ошибка сети") or raw.startswith("Не удалось"):
        print(f"❌ [test] GigaChat вернул ошибку: {raw[:300]}")
        return None

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

        if role is None:
            await try_request_access(message, user_id, user_name)
            return

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

        if role in ("teacher", "admin") and user_text.strip().lower() in ("/start", "старт"):
            kb = build_teacher_menu()
            await message.reply(text="📋 Меню:", keyboard=kb)
            log_outgoing(user_name, "меню по /start")
            return

        if role in ("teacher", "admin") and user_text.strip().lower() in ("/menu", "меню"):
            kb = build_teacher_menu()
            await message.reply(text="📋 Меню:", keyboard=kb)
            log_outgoing(user_name, "меню по /menu")
            return

        if role in ("teacher", "admin") and user_text.strip().lower() in ("/test", "тест", "задать тест"):
            kb = build_lessons_keyboard(prefix="t_lesson")
            if kb is None:
                await message.reply(text="📭 Список уроков пуст. Добавь предметы в словарь `lessons`.")
                log_outgoing(user_name, "Список уроков пуст")
            else:
                await message.reply(text="📝 Выберите урок для теста:", keyboard=kb)
                log_outgoing(user_name, "Выбор урока для теста")
            return

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
                cb, f"⚠ Пользователю {new_user_id} уже назначена роль.",
            )
            return

        set_user_role(new_user_id, new_role)
        pending_users.pop(new_user_id, None)
        role_ru = "Учитель" if new_role == "teacher" else "Ученик"
        result_text = f"✅ Пользователь {new_user_id} назначен: **{role_ru}**."

        await hide_admin_notification_for_all(
            new_user_id, result_text, skip_admin_id=admin_id,
        )
        await clear_buttons_after_callback(cb, result_text)
        print(f"👑 Админ {admin_id} назначил пользователю {new_user_id} роль: {new_role}")

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
# МАТЕРИАЛЫ: МЕНЮ → УРОКИ
# ============================================================

@bot.on_button_callback(lambda data: data.payload == "materials_menu")
async def materials_menu_callback(cb: aiomax.Callback):
    try:
        kb = build_lessons_keyboard(prefix="m_lesson")
        if kb is None:
            await cb.answer(text="📭 Список уроков пуст.", keyboard=build_back_keyboard())
            return
        await cb.answer(text="📚 Выберите урок:", keyboard=kb)
    except Exception as e:
        print(f"Ошибка в меню «Материалы»: {e}")
        try:
            await cb.answer(text="❌ Не удалось открыть меню материалов.", keyboard=build_back_keyboard())
        except Exception:
            pass


# ============================================================
# МАТЕРИАЛЫ: УРОК → ТЕМЫ
# ============================================================

@bot.on_button_callback(lambda data: data.payload.startswith("m_lesson:"))
async def materials_lesson_callback(cb: aiomax.Callback):
    try:
        lesson_name = cb.payload.split(":", 1)[1]
        if lesson_name not in lessons:
            await cb.answer(text=f"⚠ Урок «{lesson_name}» не найден.", keyboard=build_back_keyboard())
            return

        kb = build_topics_keyboard(lesson_name, prefix="m_topic", back_payload="m_back")
        if kb is None:
            await cb.answer(
                text=f"📭 У урока «{lesson_name}» нет тем.",
                keyboard=build_back_keyboard(),
            )
            return
        await cb.answer(text=f"📚 Урок «{lesson_name}». Выберите тему:", keyboard=kb)
    except Exception as e:
        print(f"Ошибка в materials_lesson_callback: {e}")
        try:
            await cb.answer(text="❌ Ошибка.", keyboard=build_back_keyboard())
        except Exception:
            pass


# ============================================================
# МАТЕРИАЛЫ: ТЕМА → РАССЫЛКА
# ============================================================

@bot.on_button_callback(lambda data: data.payload.startswith("m_topic:"))
async def materials_topic_callback(cb: aiomax.Callback):
    try:
        parts = cb.payload.split(":", 2)
        if len(parts) != 3:
            await cb.answer(text="❌ Некорректные данные темы.", keyboard=build_back_keyboard())
            return
        _, lesson_name, topic = parts
        topics = lessons.get(lesson_name, {})
        link = topics.get(topic, "").strip() if isinstance(topics, dict) else ""

        if not link:
            await cb.answer(
                text=f"⚠ Тема «{topic}» найдена, но ссылка не задана.",
                keyboard=build_back_keyboard(),
            )
            return

        broadcast_text = f"📚 Новый урок: **{lesson_name} — {topic}**\n🔗 {link}"
        ok, fail = await notify_all_students(broadcast_text)

        await cb.answer(
            text=f"✅ Отправлено: {ok}, не доставлено: {fail}.",
            keyboard=build_back_keyboard(),
        )
    except Exception as e:
        print(f"Ошибка в materials_topic_callback: {e}")
        try:
            await cb.answer(text="❌ Ошибка при рассылке.", keyboard=build_back_keyboard())
        except Exception:
            pass


# ============================================================
# МАТЕРИАЛЫ: НАЗАД К УРОКАМ
# ============================================================

@bot.on_button_callback(lambda data: data.payload == "m_back")
async def materials_back_callback(cb: aiomax.Callback):
    try:
        kb = build_lessons_keyboard(prefix="m_lesson")
        if kb is None:
            await cb.answer(text="📭 Список уроков пуст.", keyboard=build_back_keyboard())
            return
        await cb.answer(text="📚 Выберите урок:", keyboard=kb)
    except Exception as e:
        print(f"Ошибка в materials_back_callback: {e}")


# ============================================================
# ВОЗВРАТ В ГЛАВНОЕ МЕНЮ
# ============================================================

@bot.on_button_callback(lambda data: data.payload == "back_to_menu")
async def back_to_menu_callback(cb: aiomax.Callback):
    try:
        kb = build_teacher_menu()
        await cb.answer(text="📋 Меню:", keyboard=kb)
    except Exception as e:
        print(f"Ошибка возврата в меню: {e}")


# ============================================================
# ТЕСТЫ: МЕНЮ → УРОКИ
# ============================================================

@bot.on_button_callback(lambda data: data.payload == "test_menu")
async def test_menu_callback(cb: aiomax.Callback):
    try:
        kb = build_lessons_keyboard(prefix="t_lesson")
        if kb is None:
            await cb.answer(text="📭 Список уроков пуст.", keyboard=build_back_keyboard())
            return
        await cb.answer(text="📝 Выберите урок для теста:", keyboard=kb)
    except Exception as e:
        print(f"Ошибка в меню «Тест»: {e}")
        try:
            await cb.answer(text="❌ Не удалось открыть меню тестов.", keyboard=build_back_keyboard())
        except Exception:
            pass


# ============================================================
# ТЕСТЫ: УРОК → ТЕМЫ
# ============================================================

@bot.on_button_callback(lambda data: data.payload.startswith("t_lesson:"))
async def tests_lesson_callback(cb: aiomax.Callback):
    try:
        lesson_name = cb.payload.split(":", 1)[1]
        if lesson_name not in lessons:
            await cb.answer(text=f"⚠ Урок «{lesson_name}» не найден.", keyboard=build_back_keyboard())
            return

        kb = build_topics_keyboard(lesson_name, prefix="t_topic", back_payload="t_back")
        if kb is None:
            await cb.answer(
                text=f"📭 У урока «{lesson_name}» нет тем.",
                keyboard=build_back_keyboard(),
            )
            return
        await cb.answer(
            text=f"📝 Урок «{lesson_name}». Выберите тему для теста:",
            keyboard=kb,
        )
    except Exception as e:
        print(f"Ошибка в tests_lesson_callback: {e}")
        try:
            await cb.answer(text="❌ Ошибка.", keyboard=build_back_keyboard())
        except Exception:
            pass


# ============================================================
# ТЕСТЫ: ТЕМА → ГЕНЕРАЦИЯ ПО ИЗОБРАЖЕНИЮ
# ============================================================

@bot.on_button_callback(lambda data: data.payload.startswith("t_topic:"))
async def tests_topic_callback(cb: aiomax.Callback):
    try:
        parts = cb.payload.split(":", 2)
        if len(parts) != 3:
            await cb.answer(text="❌ Некорректные данные темы.", keyboard=build_back_keyboard())
            return
        _, lesson_name, topic = parts
        subject = f"{lesson_name} — {topic}"

        topics = lessons.get(lesson_name, {})
        image_url = topics.get(topic, "").strip() if isinstance(topics, dict) else ""
        if not image_url:
            await cb.answer(
                text=f"⚠ У темы «{topic}» нет ссылки. Добавьте URL изображения в `lessons`.",
                keyboard=build_back_keyboard(),
            )
            return

        await cb.answer(text=f"⏳ Скачиваю изображение и составляю тест по теме «{subject}»...")

        questions = await generate_test_from_image(image_url, subject)
        if questions is None:
            await cb.answer(
                text=(
                    "❌ Не удалось составить тест по изображению.\n\n"
                    "Возможные причины:\n"
                    "• ссылка ведёт не на картинку (а на страницу документа);\n"
                    "• файл слишком большой (>20 МБ) или слишком маленький;\n"
                    "• Google Drive-файл требует авторизации;\n"
                    "• GigaChat временно недоступен.\n\n"
                    "Проверьте ссылку и попробуйте снова."
                ),
                keyboard=build_back_keyboard(),
            )
            return

        active_tests[subject] = questions

        broadcast_text = (
            f"📝 Доступен тест по теме **{subject}**!\n"
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
        print(f"Ошибка в tests_topic_callback: {e}")
        try:
            await cb.answer(text="❌ Ошибка при создании теста.", keyboard=build_back_keyboard())
        except Exception:
            pass


# ============================================================
# ТЕСТЫ: НАЗАД К УРОКАМ
# ============================================================

@bot.on_button_callback(lambda data: data.payload == "t_back")
async def tests_back_callback(cb: aiomax.Callback):
    try:
        kb = build_lessons_keyboard(prefix="t_lesson")
        if kb is None:
            await cb.answer(text="📭 Список уроков пуст.", keyboard=build_back_keyboard())
            return
        await cb.answer(text="📝 Выберите урок для теста:", keyboard=kb)
    except Exception as e:
        print(f"Ошибка в tests_back_callback: {e}")


# ============================================================
# СТАРТ ТЕСТА УЧЕНИКОМ
# ============================================================

@bot.on_button_callback(lambda data: data.payload.startswith("start_test:"))
async def start_test_callback(cb: aiomax.Callback):
    try:
        subject = cb.payload.split(":", 1)[1]
        questions = active_tests.get(subject)
        if not questions:
            await cb.answer(text="❌ Тест по этой теме ещё не создан.")
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
# ПРОСМОТР РЕЗУЛЬТАТОВ
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
    total_topics = sum(len(t) for t in lessons.values() if isinstance(t, dict))
    print(f"Всего тем: {total_topics}")

    admins = get_all_admins()
    if admins:
        print(f"Администраторы (id): {admins}")
    else:
        print("⚠ Внимание: ни один пользователь не назначен администратором.")
        print("  Добавьте в vip.txt строку вида: <user_id>:admin")

    bot.run()


if __name__ == "__main__":
    main()