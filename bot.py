"""
FunPay Telegram Bot
Использует неофициальный API: https://github.com/LIMBODS/FunPayAPI
"""

import asyncio
import logging
import os
import threading
import time
from typing import Optional

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)

# ──────────────────────────────────────────────────────────────────────────────
# Настройки
# ──────────────────────────────────────────────────────────────────────────────
# Токен берётся из переменной окружения BOT_TOKEN
TELEGRAM_BOT_TOKEN = os.getenv("BOT_TOKEN")

if not TELEGRAM_BOT_TOKEN:
    logger_text = (
        "❌ Переменная окружения BOT_TOKEN не задана.\n"
        "Установите её перед запуском, например:\n"
        "  • Linux/macOS: export BOT_TOKEN='ваш_токен'\n"
        "  • Windows (PowerShell): $env:BOT_TOKEN='ваш_токен'\n"
        "  • Windows (cmd): set BOT_TOKEN=ваш_токен\n"
        "  • .env-файл: BOT_TOKEN=ваш_токен"
    )
    raise RuntimeError(logger_text)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("FunPayBot")

# ──────────────────────────────────────────────────────────────────────────────
# Хранилище данных (in-memory, на каждого пользователя Telegram)
# ──────────────────────────────────────────────────────────────────────────────
# user_data[tg_user_id] = {
#   "accounts": {golden_key: {"acc": Account, "username": str, "id": int, "user_agent": str}},
#   "pending_key": str | None,         # ключ, ждущий user-agent
#   "notifications_enabled": bool,
#   "listener_thread": threading.Thread | None,
#   "listener_stop": threading.Event | None,
# }
user_data: dict[int, dict] = {}


def get_user(tg_id: int) -> dict:
    if tg_id not in user_data:
        user_data[tg_id] = {
            "accounts": {},
            "pending_key": None,
            "notifications_enabled": False,
            "listener_thread": None,
            "listener_stop": None,
        }
    return user_data[tg_id]


# ──────────────────────────────────────────────────────────────────────────────
# FSM
# ──────────────────────────────────────────────────────────────────────────────
class AddKey(StatesGroup):
    waiting_for_key = State()


# ──────────────────────────────────────────────────────────────────────────────
# Клавиатуры
# ──────────────────────────────────────────────────────────────────────────────
def main_keyboard(tg_id: int) -> InlineKeyboardMarkup:
    data = get_user(tg_id)
    notif_status = "🔔 Уведомления: ВКЛ" if data["notifications_enabled"] else "🔕 Уведомления: ВЫКЛ"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Добавить Golden Key", callback_data="add_key")],
        [InlineKeyboardButton(text=notif_status,            callback_data="toggle_notifications")],
        [InlineKeyboardButton(text="💬 Чаты",               callback_data="show_chats")],
        [InlineKeyboardButton(text="👤 Подключённые аккаунты", callback_data="show_accounts")],
    ])


def back_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 Назад", callback_data="back_main")]
    ])


def skip_ua_keyboard() -> InlineKeyboardMarkup:
    """Клавиатура для шага ввода user-agent: пропустить или ввести вручную."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⏭ Использовать стандартный", callback_data="ua_default")],
        [InlineKeyboardButton(text="🔙 Отмена", callback_data="back_main")],
    ])


def accounts_keyboard(tg_id: int) -> InlineKeyboardMarkup:
    data = get_user(tg_id)
    buttons = []
    for gk, info in data["accounts"].items():
        label = f"🗑 Удалить {info['username']}"
        buttons.append([InlineKeyboardButton(text=label, callback_data=f"del_acc:{gk}")])
    buttons.append([InlineKeyboardButton(text="🔙 Назад", callback_data="back_main")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def chats_keyboard(chats_list: list) -> InlineKeyboardMarkup:
    """chats_list — список types.ChatShortcut"""
    buttons = []
    for chat in chats_list[:15]:   # показываем не больше 15
        label = f"💬 {chat.name}"
        buttons.append([InlineKeyboardButton(text=label, callback_data=f"open_chat:{chat.id}")])
    buttons.append([InlineKeyboardButton(text="🔙 Назад", callback_data="back_main")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


# ──────────────────────────────────────────────────────────────────────────────
# Вспомогательные функции FunPay
# ──────────────────────────────────────────────────────────────────────────────
# User-Agent реального браузера. Без него FunPayAPI шлёт дефолтный
# `python-requests/2.28.1` и FunPay возвращает «пустую» страницу
# (капча/проверка), на которой нет блока авторизации → ошибка авторизации.
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0.0.0 Safari/537.36"
)


def try_add_account(golden_key: str, user_agent: str) -> tuple[bool, str, Optional[object]]:
    """Пробуем авторизоваться по golden_key. Возвращает (успех, сообщение, объект Account)."""
    try:
        from FunPayAPI import Account
        from FunPayAPI.common import exceptions as fp_exceptions

        acc = Account(golden_key, user_agent=user_agent).get()
        return True, f"Аккаунт **{acc.username}** (ID: {acc.id}) успешно добавлен!", acc
    except fp_exceptions.UnauthorizedError:
        return (
            False,
            "Ошибка авторизации: FunPay не принял golden_key.\n"
            "Частые причины:\n"
            "• user-agent не совпадает с тем, из которого копировался ключ;\n"
            "• ключ истёк или был перевыпущен при повторном входе;\n"
            "• сервер FunPay блокирует IP/VPS (особенно RU).\n"
            "Перекопируйте golden_key и user-agent в одном браузере.",
            None,
        )
    except fp_exceptions.RequestFailedError as e:
        return (
            False,
            f"Ошибка запроса к FunPay (статус {e.status_code}).\n"
            "Попробуйте позже или проверьте соединение / VPN.",
            None,
        )
    except Exception as e:
        return False, f"Ошибка авторизации: {e}", None


def normalize_golden_key(raw: str) -> tuple[bool, str]:
    """Чистим ввод: убираем пробелы/переносы, режем по ';' (если скопировали всю cookie-строку)."""
    if not raw:
        return False, ""
    s = raw.strip()
    # Если прислали кусок cookie-строки вида "golden_key=abc...; PHPSESSID=..."
    if "golden_key" in s and "=" in s:
        for part in s.split(";"):
            part = part.strip()
            if part.startswith("golden_key="):
                s = part.split("=", 1)[1]
                break
    # Удаляем любые пробелы/переносы внутри (на всякий случай)
    s = s.replace(" ", "").replace("\n", "").replace("\r", "").replace("\t", "")
    return True, s


def get_all_chats(tg_id: int) -> list:
    """Возвращает все ChatShortcut по всем аккаунтам пользователя."""
    data = get_user(tg_id)
    result = []
    for gk, info in data["accounts"].items():
        try:
            acc = info["acc"]
            chats = acc.get_chats()
            result.extend(chats)
        except Exception as e:
            logger.warning(f"Не удалось получить чаты для {info['username']}: {e}")
    return result


def get_chat_messages(tg_id: int, chat_id: int) -> tuple[str, list]:
    """
    Ищет в каком аккаунте есть chat_id и возвращает (username_аккаунта, список Message).
    """
    data = get_user(tg_id)
    for gk, info in data["accounts"].items():
        try:
            acc = info["acc"]
            messages = acc.get_chat_history(chat_id)
            return info["username"], messages
        except Exception:
            continue
    return "", []


# ──────────────────────────────────────────────────────────────────────────────
# Фоновый listener уведомлений
# ──────────────────────────────────────────────────────────────────────────────
def _listener_thread(tg_id: int, bot_token: str, stop_event: threading.Event):
    """Запускается в отдельном потоке для каждого пользователя."""
    import asyncio as _asyncio
    from FunPayAPI import Runner
    from FunPayAPI.common.enums import EventTypes

    data = get_user(tg_id)
    if not data["accounts"]:
        return

    # Берём первый (или все) аккаунты
    # Для простоты слушаем только первый аккаунт; можно расширить
    first_key = next(iter(data["accounts"]))
    info = data["accounts"][first_key]
    acc = info["acc"]

    # Используем user-agent, с которым ключ был добавлен.
    # Это критично: FunPay режет авторизацию, если UA не совпадает.
    if not acc.user_agent:
        acc.user_agent = info.get("user_agent") or DEFAULT_USER_AGENT

    try:
        runner = Runner(acc)
    except Exception as e:
        logger.error(f"Не удалось создать Runner: {e}")
        return

    _bot = Bot(token=bot_token)

    async def send_notify(text: str):
        try:
            await _bot.send_message(tg_id, text, parse_mode="HTML")
        except Exception as ex:
            logger.warning(f"Не удалось отправить уведомление: {ex}")

    loop = _asyncio.new_event_loop()

    for event in runner.listen(requests_delay=5):
        if stop_event.is_set():
            break
        if event.type is EventTypes.NEW_MESSAGE:
            msg = event.message
            # Не уведомляем об собственных сообщениях
            if msg.author_id == acc.id:
                continue
            text = (
                f"📨 <b>Новое сообщение на FunPay</b>\n"
                f"👤 Отправитель: <b>{msg.author}</b>\n"
                f"💬 Чат ID: <code>{msg.chat_id}</code>\n"
                f"📝 Текст: {msg.text or '[изображение]'}"
            )
            loop.run_until_complete(send_notify(text))

    loop.run_until_complete(_bot.session.close())
    loop.close()


def start_listener(tg_id: int, bot_token: str):
    data = get_user(tg_id)
    # Если уже запущен — ничего не делаем
    if data["listener_thread"] and data["listener_thread"].is_alive():
        return

    stop_event = threading.Event()
    t = threading.Thread(
        target=_listener_thread,
        args=(tg_id, bot_token, stop_event),
        daemon=True,
        name=f"funpay_listener_{tg_id}",
    )
    data["listener_stop"] = stop_event
    data["listener_thread"] = t
    t.start()
    logger.info(f"Listener запущен для tg_id={tg_id}")


def stop_listener(tg_id: int):
    data = get_user(tg_id)
    if data["listener_stop"]:
        data["listener_stop"].set()
    data["listener_thread"] = None
    data["listener_stop"] = None
    logger.info(f"Listener остановлен для tg_id={tg_id}")


# ──────────────────────────────────────────────────────────────────────────────
# Хэндлеры
# ──────────────────────────────────────────────────────────────────────────────
async def cmd_start(message: Message):
    tg_id = message.from_user.id
    get_user(tg_id)  # инициализация
    await message.answer(
        "👋 <b>FunPay Manager Bot</b>\n\n"
        "Управляй своими аккаунтами FunPay прямо из Telegram.\n"
        "Добавь Golden Key и получай уведомления о новых сообщениях!",
        reply_markup=main_keyboard(tg_id),
        parse_mode="HTML",
    )


# ── Добавить ключ ──────────────────────────────────────────────────────────────
async def cb_add_key(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_text(
        "🔑 <b>Введите Golden Key</b>\n\n"
        "1) Откройте <code>funpay.com</code> и войдите в аккаунт.\n"
        "2) <code>F12</code> → <i>Application</i> → <i>Cookies</i> → <i>https://funpay.com</i>.\n"
        "3) Найдите куку <code>golden_key</code> и скопируйте её <b>значение</b>.\n\n"
        "Отправьте ключ одним сообщением. Если скопировали всю строку куки — "
        "бот сам вытащит из неё <code>golden_key</code>.",
        reply_markup=back_keyboard(),
        parse_mode="HTML",
    )
    await state.set_state(AddKey.waiting_for_key)
    await callback.answer()


async def process_golden_key(message: Message, state: FSMContext):
    tg_id = message.from_user.id
    data = get_user(tg_id)

    ok_raw, raw = normalize_golden_key(message.text)
    if not ok_raw or not raw:
        await message.answer(
            "❌ Пусто. Пришлите golden_key текстом.",
            reply_markup=back_keyboard(),
        )
        return

    # Валидация: golden_key — 32 символа [a-z0-9]
    if not (32 <= len(raw) <= 64) or not all(c.isalnum() for c in raw):
        await message.answer(
            "❌ Это не похоже на golden_key (ожидается 32 символа a-z/0-9).\n"
            "Скопируйте значение куки <b>golden_key</b> ещё раз.",
            reply_markup=back_keyboard(),
            parse_mode="HTML",
        )
        return

    # Если такой ключ уже добавлен — просто выходим
    if raw in data["accounts"]:
        await message.answer(
            "⚠️ Этот golden_key уже добавлен.",
            reply_markup=main_keyboard(tg_id),
        )
        await state.clear()
        return

    # Сохраняем ключ во временное хранилище и просим user-agent
    data["pending_key"] = raw
    await message.answer(
        "🌐 <b>Шаг 2/2 — User-Agent</b>\n\n"
        "FunPay сравнивает user-agent с тем, из которого был выдан golden_key. "
        "Если не совпадёт — авторизация режется.\n\n"
        "<b>Откуда взять:</b>\n"
        "• В том же браузере, где копировали ключ, откройте:\n"
        "  <a href=\"https://whatmyuseragent.com\">whatmyuseragent.com</a> и нажмите <b>Copy</b>.\n"
        "• Или в этом же браузере нажмите <code>F12</code> → вкладка <i>Console</i> → "
        "выполните <code>navigator.userAgent</code> и скопируйте строку.\n\n"
        "Отправьте user-agent текстом или нажмите <b>«Использовать стандартный»</b> — "
        "тогда возьмём встроенный (Chrome/126).",
        reply_markup=skip_ua_keyboard(),
        parse_mode="HTML",
        disable_web_page_preview=True,
    )
    await state.set_state(AddKey.waiting_for_user_agent)


async def process_user_agent(message: Message, state: FSMContext):
    """Второй шаг: получаем user-agent, пробуем авторизоваться."""
    tg_id = message.from_user.id
    data = get_user(tg_id)
    pending = data.get("pending_key")
    if not pending:
        await message.answer(
            "Сессия добавления сбросилась. Начните заново: ➕ Добавить Golden Key.",
            reply_markup=main_keyboard(tg_id),
        )
        await state.clear()
        return

    ua = (message.text or "").strip()
    if not ua or len(ua) < 10:
        await message.answer(
            "❌ User-agent слишком короткий. Пришлите полную строку или нажмите «Использовать стандартный».",
            reply_markup=skip_ua_keyboard(),
        )
        return

    await message.answer("⏳ Проверяю ключ + user-agent...")
    ok, text, acc = await asyncio.get_event_loop().run_in_executor(
        None, try_add_account, pending, ua
    )

    if ok:
        data["accounts"][pending] = {
            "acc": acc,
            "username": acc.username,
            "id": acc.id,
            "user_agent": ua,
        }
        data["pending_key"] = None
        await message.answer(
            f"✅ {text}\n\nАккаунт добавлен!",
            reply_markup=main_keyboard(tg_id),
            parse_mode="Markdown",
        )
    else:
        # Не сбрасываем pending — пусть пользователь попробует другой UA
        await message.answer(
            f"❌ {text}\n\n"
            "Можете прислать <b>другой user-agent</b> (из того же браузера, где брали ключ) "
            "или нажмите <b>«Использовать стандартный»</b>.",
            reply_markup=skip_ua_keyboard(),
            parse_mode="HTML",
        )
        return

    await state.clear()


async def cb_ua_default(callback: CallbackQuery, state: FSMContext):
    """Пользователь выбрал стандартный user-agent."""
    tg_id = callback.from_user.id
    data = get_user(tg_id)
    pending = data.get("pending_key")
    if not pending:
        await callback.message.edit_text(
            "Сессия добавления сбросилась. Начните заново: ➕ Добавить Golden Key.",
            reply_markup=main_keyboard(tg_id),
        )
        await state.clear()
        await callback.answer()
        return

    await callback.message.edit_text("⏳ Проверяю ключ со стандартным user-agent...")
    ok, text, acc = await asyncio.get_event_loop().run_in_executor(
        None, try_add_account, pending, DEFAULT_USER_AGENT
    )

    if ok:
        data["accounts"][pending] = {
            "acc": acc,
            "username": acc.username,
            "id": acc.id,
            "user_agent": DEFAULT_USER_AGENT,
        }
        data["pending_key"] = None
        await callback.message.edit_text(
            f"✅ {text}\n\nАккаунт добавлен!",
            reply_markup=main_keyboard(tg_id),
            parse_mode="Markdown",
        )
    else:
        await callback.message.edit_text(
            f"❌ {text}\n\n"
            "Скорее всего, user-agent не совпадает с тем, из которого выдан ключ. "
            "Отправьте <b>user-agent из того же браузера</b>, где брали golden_key.",
            reply_markup=skip_ua_keyboard(),
            parse_mode="HTML",
        )
        return

    await state.clear()
    await callback.answer()


# ── Уведомления ────────────────────────────────────────────────────────────────
async def cb_toggle_notifications(callback: CallbackQuery):
    tg_id = callback.from_user.id
    data = get_user(tg_id)

    if not data["accounts"]:
        await callback.answer("⚠️ Сначала добавьте хотя бы один аккаунт!", show_alert=True)
        return

    data["notifications_enabled"] = not data["notifications_enabled"]

    if data["notifications_enabled"]:
        start_listener(tg_id, TELEGRAM_BOT_TOKEN)
        status_text = "🔔 Уведомления <b>включены</b>!\nВы будете получать сообщения от FunPay."
    else:
        stop_listener(tg_id)
        status_text = "🔕 Уведомления <b>выключены</b>."

    await callback.message.edit_text(
        status_text,
        reply_markup=main_keyboard(tg_id),
        parse_mode="HTML",
    )
    await callback.answer()


# ── Чаты ───────────────────────────────────────────────────────────────────────
async def cb_show_chats(callback: CallbackQuery):
    tg_id = callback.from_user.id
    data = get_user(tg_id)

    if not data["accounts"]:
        await callback.answer("⚠️ Сначала добавьте хотя бы один аккаунт!", show_alert=True)
        return

    await callback.message.edit_text("⏳ Загружаю список чатов...")

    chats = await asyncio.get_event_loop().run_in_executor(None, get_all_chats, tg_id)

    if not chats:
        await callback.message.edit_text(
            "💬 Чатов не найдено.",
            reply_markup=back_keyboard(),
        )
        await callback.answer()
        return

    lines = ["💬 <b>Активные чаты FunPay:</b>\n"]
    for chat in chats[:15]:
        unread = "🔵 " if chat.unread else ""
        last = chat.last_message_text[:60] + "..." if len(chat.last_message_text) > 60 else chat.last_message_text
        lines.append(f"{unread}<b>{chat.name}</b>\n   └ {last}")

    await callback.message.edit_text(
        "\n".join(lines),
        reply_markup=chats_keyboard(chats),
        parse_mode="HTML",
    )
    await callback.answer()


async def cb_open_chat(callback: CallbackQuery):
    """Показывает последние сообщения конкретного чата."""
    tg_id = callback.from_user.id
    chat_id = int(callback.data.split(":")[1])

    await callback.message.edit_text("⏳ Загружаю историю чата...")

    acc_name, messages = await asyncio.get_event_loop().run_in_executor(
        None, get_chat_messages, tg_id, chat_id
    )

    if not messages:
        await callback.message.edit_text(
            "📭 Сообщений не найдено.",
            reply_markup=back_keyboard(),
        )
        await callback.answer()
        return

    lines = [f"💬 <b>Чат #{chat_id}</b> (аккаунт: {acc_name})\n"]
    for msg in messages[-10:]:  # последние 10 сообщений
        author = msg.author or "Неизвестно"
        text = msg.text or "[изображение]"
        text = text[:120] + "..." if len(text) > 120 else text
        lines.append(f"<b>{author}:</b> {text}")

    await callback.message.edit_text(
        "\n".join(lines),
        reply_markup=back_keyboard(),
        parse_mode="HTML",
    )
    await callback.answer()


# ── Аккаунты ───────────────────────────────────────────────────────────────────
async def cb_show_accounts(callback: CallbackQuery):
    tg_id = callback.from_user.id
    data = get_user(tg_id)

    if not data["accounts"]:
        await callback.message.edit_text(
            "👤 <b>Подключённые аккаунты</b>\n\nАккаунтов нет. Добавьте Golden Key.",
            reply_markup=back_keyboard(),
            parse_mode="HTML",
        )
        await callback.answer()
        return

    lines = ["👤 <b>Подключённые аккаунты FunPay:</b>\n"]
    for gk, info in data["accounts"].items():
        short_key = gk[:8] + "..." + gk[-4:]
        lines.append(
            f"• <b>{info['username']}</b> (ID: {info['id']})\n"
            f"  Ключ: <code>{short_key}</code>"
        )

    await callback.message.edit_text(
        "\n".join(lines),
        reply_markup=accounts_keyboard(tg_id),
        parse_mode="HTML",
    )
    await callback.answer()


async def cb_del_account(callback: CallbackQuery):
    tg_id = callback.from_user.id
    data = get_user(tg_id)
    golden_key = callback.data.split(":", 1)[1]

    removed_name = data["accounts"].get(golden_key, {}).get("username", "?")
    data["accounts"].pop(golden_key, None)

    # Если аккаунтов не осталось — выключаем уведомления
    if not data["accounts"] and data["notifications_enabled"]:
        data["notifications_enabled"] = False
        stop_listener(tg_id)

    await callback.message.edit_text(
        f"🗑 Аккаунт <b>{removed_name}</b> удалён.",
        reply_markup=main_keyboard(tg_id),
        parse_mode="HTML",
    )
    await callback.answer()


# ── Назад ──────────────────────────────────────────────────────────────────────
async def cb_back_main(callback: CallbackQuery, state: FSMContext):
    tg_id = callback.from_user.id
    await state.clear()
    await callback.message.edit_text(
        "🏠 <b>Главное меню</b>",
        reply_markup=main_keyboard(tg_id),
        parse_mode="HTML",
    )
    await callback.answer()


# ──────────────────────────────────────────────────────────────────────────────
# Точка входа
# ──────────────────────────────────────────────────────────────────────────────
async def main():
    bot = Bot(token=TELEGRAM_BOT_TOKEN)
    dp = Dispatcher(storage=MemoryStorage())

    # Регистрация хэндлеров
    dp.message.register(cmd_start, Command("start"))
    dp.message.register(process_golden_key,  AddKey.waiting_for_key)
    dp.message.register(process_user_agent, AddKey.waiting_for_user_agent)

    dp.callback_query.register(cb_add_key,               F.data == "add_key")
    dp.callback_query.register(cb_ua_default,            F.data == "ua_default")
    dp.callback_query.register(cb_toggle_notifications,  F.data == "toggle_notifications")
    dp.callback_query.register(cb_show_chats,            F.data == "show_chats")
    dp.callback_query.register(cb_show_accounts,         F.data == "show_accounts")
    dp.callback_query.register(cb_back_main,             F.data == "back_main")
    dp.callback_query.register(cb_open_chat,             F.data.startswith("open_chat:"))
    dp.callback_query.register(cb_del_account,           F.data.startswith("del_acc:"))

    logger.info("Бот запускается...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
