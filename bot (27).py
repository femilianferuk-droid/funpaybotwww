"""
Telegram-бот для генерации и редактирования Python-кода.

Стек:
- aiogram 3 (Telegram Bot API)
- Anthropic SDK с кастомным API-gateway (https://api.smartapi.shop)
- SQLite (aiosqlite) для хранения чатов и истории
- Код из ответов ИИ автоматически извлекается и сохраняется в .py файлы

Переменные окружения:
    BOT_TOKEN      — токен Telegram-бота (обязательно)
    SMARTAPI_KEY   — API-ключ AI-gateway (по умолчанию встроенный)

Запуск:
    export BOT_TOKEN=xxx
    pip install -r requirements.txt
    python bot.py
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from contextlib import asynccontextmanager
from html import escape
from pathlib import Path

import aiosqlite
import anthropic
from aiogram import Bot, Dispatcher, F, Router
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

# ====================== Конфигурация ======================

BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError(
        "Не задан BOT_TOKEN. Установи переменную окружения: export BOT_TOKEN=..."
    )

API_KEY = os.getenv(
    "SMARTAPI_KEY",
    "sk-smart-3XD55m5XyNjpez1edNzGkuaqvnnXs6qKm1pf5hQqHEA",
)
API_BASE_URL = "https://api.smartapi.shop"

DB_PATH = "data/bot.db"
WORKSPACE_DIR = Path("workspaces")
Path("data").mkdir(parents=True, exist_ok=True)
WORKSPACE_DIR.mkdir(parents=True, exist_ok=True)

AVAILABLE_MODELS = [
    "sonnet-4.6",
    "deepseek-v4-flash",
    "mimo-v2.5",
    "minimax-m3",
]

SYSTEM_PROMPT = """Ты — ассистент для написания и редактирования Python-кода. Отвечай на русском языке.

Когда создаёшь или изменяешь код, ВСЕГДА используй формат:
```python:имя_файла.py
полный код файла
```

Правила:
1. Каждый код-блок должен содержать ПОЛНЫЙ код файла (а не фрагмент).
2. Имя файла указывается после языка через двоеточие, обязательно с расширением .py.
3. Можно создавать несколько файлов в одном ответе.
4. При редактировании существующего файла выведи его полный обновлённый код.
5. Кратко поясни, что было сделано (1-3 предложения)."""

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
log = logging.getLogger(__name__)

# ====================== Клиенты ======================

bot = Bot(token=BOT_TOKEN, parse_mode=ParseMode.HTML)
dp = Dispatcher()
router = Router()
dp.include_router(router)

ai_client = anthropic.AsyncAnthropic(
    base_url=API_BASE_URL,
    api_key=API_KEY,
)

# ====================== БД ======================


@asynccontextmanager
async def get_db():
    """Контекстный менеджер для подключения к SQLite с включённым FK."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA foreign_keys = ON")
        yield db


async def init_db():
    async with get_db() as db:
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                current_chat_id INTEGER,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS chats (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                model TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (chat_id) REFERENCES chats(id) ON DELETE CASCADE
            )
            """
        )
        await db.commit()


async def ensure_user(user_id: int):
    async with get_db() as db:
        await db.execute(
            "INSERT OR IGNORE INTO users (user_id) VALUES (?)", (user_id,)
        )
        await db.commit()


async def get_user(user_id: int):
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
        return await cur.fetchone()


async def set_current_chat(user_id: int, chat_id: int):
    async with get_db() as db:
        await db.execute(
            "UPDATE users SET current_chat_id = ? WHERE user_id = ?",
            (chat_id, user_id),
        )
        await db.commit()


async def get_user_chats(user_id: int):
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM chats WHERE user_id = ? ORDER BY created_at DESC",
            (user_id,),
        )
        return await cur.fetchall()


async def get_chat(chat_id: int):
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM chats WHERE id = ?", (chat_id,))
        return await cur.fetchone()


async def create_chat(user_id: int, name: str, model: str | None = None) -> int:
    if model is None:
        model = AVAILABLE_MODELS[0]
    async with get_db() as db:
        cur = await db.execute(
            "INSERT INTO chats (user_id, name, model) VALUES (?, ?, ?)",
            (user_id, name, model),
        )
        chat_id = cur.lastrowid
        await db.execute(
            "INSERT INTO users (user_id, current_chat_id) VALUES (?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET current_chat_id = excluded.current_chat_id",
            (user_id, chat_id),
        )
        await db.commit()
        return chat_id


async def update_chat_model(chat_id: int, model: str):
    async with get_db() as db:
        await db.execute(
            "UPDATE chats SET model = ? WHERE id = ?", (model, chat_id)
        )
        await db.commit()


async def save_message(chat_id: int, role: str, content: str):
    async with get_db() as db:
        await db.execute(
            "INSERT INTO messages (chat_id, role, content) VALUES (?, ?, ?)",
            (chat_id, role, content),
        )
        await db.commit()


async def get_chat_messages(chat_id: int, limit: int = 50):
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT role, content FROM messages WHERE chat_id = ? "
            "ORDER BY id DESC LIMIT ?",
            (chat_id, limit),
        )
        rows = await cur.fetchall()
        return [
            {"role": r["role"], "content": r["content"]} for r in reversed(rows)
        ]


async def get_or_create_current_chat(user_id: int):
    user = await get_user(user_id)
    if user and user["current_chat_id"]:
        chat = await get_chat(user["current_chat_id"])
        if chat and chat["user_id"] == user_id:
            return chat
    chats = await get_user_chats(user_id)
    name = f"Чат {len(chats) + 1}"
    chat_id = await create_chat(user_id, name)
    return await get_chat(chat_id)


# ====================== Рабочая директория ======================


def get_user_workspace(user_id: int) -> Path:
    path = WORKSPACE_DIR / str(user_id)
    path.mkdir(parents=True, exist_ok=True)
    return path


def list_workspace_files(user_id: int) -> list[str]:
    ws = get_user_workspace(user_id)
    return sorted([p.name for p in ws.glob("*.py")])


# ====================== Извлечение и сохранение кода ======================

# Поддерживает формат ```python:file.py\n...``` (с двоеточием и именем файла).
CODE_BLOCK_RE = re.compile(
    r"```([\w+\-]*)(?::([^\s\n`]+))?\s*\n(.*?)```",
    re.DOTALL,
)


def extract_code_blocks(text: str) -> list[dict]:
    """Парсит блоки кода и возвращает список {lang, filename, code}."""
    blocks: list[dict] = []
    for m in CODE_BLOCK_RE.finditer(text):
        lang = (m.group(1) or "").lower()
        filename = (m.group(2) or "").strip()
        code = m.group(3)
        if not filename:
            # блок без имени файла — пропускаем
            continue
        blocks.append({"lang": lang, "filename": filename, "code": code})
    return blocks


def save_code_file(user_id: int, filename: str, code: str) -> Path | None:
    """Сохраняет код в .py файл в рабочей директории пользователя.

    Возвращает Path или None, если имя файла невалидно.
    """
    safe_name = Path(filename).name  # защита от попыток выйти за пределы папки
    if not safe_name or not safe_name.endswith(".py"):
        return None
    ws = get_user_workspace(user_id)
    path = ws / safe_name
    path.write_text(code, encoding="utf-8")
    return path


# ====================== Клавиатуры ======================


def chats_keyboard(chats, current_chat_id) -> InlineKeyboardMarkup:
    buttons: list[list[InlineKeyboardButton]] = []
    for chat in chats:
        marker = "✅ " if chat["id"] == current_chat_id else "💬 "
        title = chat["name"]
        if len(title) > 32:
            title = title[:29] + "..."
        buttons.append(
            [
                InlineKeyboardButton(
                    text=f"{marker}{title}",
                    callback_data=f"chat:select:{chat['id']}",
                )
            ]
        )
    buttons.append(
        [InlineKeyboardButton(text="➕ Новый чат", callback_data="chat:new")]
    )
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def models_keyboard(current_model: str) -> InlineKeyboardMarkup:
    buttons: list[list[InlineKeyboardButton]] = []
    for model in AVAILABLE_MODELS:
        marker = "✅ " if model == current_model else ""
        buttons.append(
            [
                InlineKeyboardButton(
                    text=f"{marker}{model}",
                    callback_data=f"model:select:{model}",
                )
            ]
        )
    return InlineKeyboardMarkup(inline_keyboard=buttons)


# ====================== Индикатор «Думаю...» ======================


class ThinkingIndicator:
    """Параллельно обновляет сообщение раз в секунду, показывая время раздумий ИИ."""

    def __init__(self, bot: Bot, chat_id: int, message_id: int):
        self.bot = bot
        self.chat_id = chat_id
        self.message_id = message_id
        self.start_time = time.time()
        self.stop_event = asyncio.Event()
        self.task: asyncio.Task | None = None

    async def _run(self):
        while not self.stop_event.is_set():
            elapsed = int(time.time() - self.start_time)
            try:
                await self.bot.edit_message_text(
                    chat_id=self.chat_id,
                    message_id=self.message_id,
                    text=f"⏳ Думаю... {elapsed}с",
                )
            except Exception:
                # сообщение могло быть удалено/изменено — игнорируем
                pass
            try:
                await asyncio.wait_for(self.stop_event.wait(), timeout=1.0)
                break
            except asyncio.TimeoutError:
                continue

    def start(self) -> None:
        self.task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        self.stop_event.set()
        if self.task:
            try:
                await asyncio.wait_for(self.task, timeout=2.0)
            except asyncio.TimeoutError:
                self.task.cancel()
                try:
                    await self.task
                except Exception:
                    pass


# ====================== Команды ======================


@router.message(CommandStart())
async def cmd_start(message: Message):
    user_id = message.from_user.id
    await ensure_user(user_id)
    chat = await get_or_create_current_chat(user_id)
    await message.answer(
        "👋 <b>Привет! Я бот для написания и редактирования Python-кода.</b>\n\n"
        f"Текущая модель: <code>{escape(chat['model'])}</code>\n"
        f"Текущий чат: <code>{escape(chat['name'])}</code>\n\n"
        "<b>Команды:</b>\n"
        "/new — создать новый чат\n"
        "/chats — список чатов и переключение\n"
        "/model — выбрать модель\n"
        "/files — показать файлы в рабочей директории\n"
        "/clear — очистить текущий чат\n"
        "/help — подробная справка\n\n"
        "Просто напиши запрос — я сгенерирую код и сохраню его в <code>.py</code> файлы."
    )


@router.message(Command("help"))
async def cmd_help(message: Message):
    await message.answer(
        "ℹ️ <b>Как пользоваться</b>\n\n"
        "1. Напиши запрос в свободной форме, например:\n"
        "   <i>«Сделай Flask-приложение с роутом /hello»</i>\n\n"
        "2. Я отвечу кодом, который автоматически сохранится в файл.\n\n"
        "3. Чтобы отредактировать файл, попроси изменить его — "
        "я выдам обновлённый код, и он перезапишет файл.\n\n"
        "<b>Команды:</b>\n"
        "/new — новый чат\n"
        "/chats — переключить чат\n"
        "/model — выбрать модель\n"
        "/files — список файлов\n"
        "/clear — очистить чат"
    )


@router.message(Command("new"))
async def cmd_new_chat(message: Message):
    user_id = message.from_user.id
    await ensure_user(user_id)
    chats = await get_user_chats(user_id)
    name = f"Чат {len(chats) + 1}"
    await create_chat(user_id, name)
    await message.answer(
        f"✨ Создан новый чат: <b>{escape(name)}</b>.\n"
        "Он автоматически выбран как текущий."
    )


@router.message(Command("chats"))
async def cmd_chats(message: Message):
    user_id = message.from_user.id
    await ensure_user(user_id)
    chats = await get_user_chats(user_id)
    if not chats:
        # нет ни одного чата — создадим первый и сразу покажем список
        await create_chat(user_id, "Чат 1")
        chats = await get_user_chats(user_id)
    user = await get_user(user_id)
    kb = chats_keyboard(chats, user["current_chat_id"] if user else None)
    await message.answer("📂 <b>Твои чаты:</b>", reply_markup=kb)


@router.message(Command("model"))
async def cmd_model(message: Message):
    user_id = message.from_user.id
    chat = await get_or_create_current_chat(user_id)
    kb = models_keyboard(chat["model"])
    await message.answer(
        f"Текущая модель: <code>{escape(chat['model'])}</code>\nВыбери модель:",
        reply_markup=kb,
    )


@router.message(Command("files"))
async def cmd_files(message: Message):
    user_id = message.from_user.id
    files = list_workspace_files(user_id)
    if not files:
        await message.answer(
            "📁 В рабочей директории пока нет <code>.py</code> файлов."
        )
        return
    text = "📁 <b>Файлы в рабочей директории:</b>\n\n" + "\n".join(
        f"• <code>{escape(f)}</code>" for f in files
    )
    await message.answer(text)


@router.message(Command("clear"))
async def cmd_clear(message: Message):
    user_id = message.from_user.id
    chat = await get_or_create_current_chat(user_id)
    async with get_db() as db:
        await db.execute("DELETE FROM messages WHERE chat_id = ?", (chat["id"],))
        await db.commit()
    await message.answer(f"🧹 Чат <b>{escape(chat['name'])}</b> очищен.")


# ====================== Callback'и ======================


@router.callback_query(F.data.startswith("chat:"))
async def on_chat_callback(callback: CallbackQuery):
    user_id = callback.from_user.id
    parts = callback.data.split(":", 2)
    action = parts[1]

    if action == "new":
        chats = await get_user_chats(user_id)
        name = f"Чат {len(chats) + 1}"
        await create_chat(user_id, name)
        await callback.message.edit_text(
            f"✨ Создан и выбран чат: <b>{escape(name)}</b>."
        )
    elif action == "select":
        chat_id = int(parts[2])
        chat = await get_chat(chat_id)
        if not chat or chat["user_id"] != user_id:
            await callback.answer("Чат не найден.", show_alert=True)
            return
        await set_current_chat(user_id, chat_id)
        await callback.message.edit_text(
            f"✅ Выбран чат: <b>{escape(chat['name'])}</b>\n"
            f"Модель: <code>{escape(chat['model'])}</code>"
        )
    await callback.answer()


@router.callback_query(F.data.startswith("model:"))
async def on_model_callback(callback: CallbackQuery):
    user_id = callback.from_user.id
    model = callback.data.split(":", 2)[2]
    if model not in AVAILABLE_MODELS:
        await callback.answer("Неизвестная модель.", show_alert=True)
        return
    chat = await get_or_create_current_chat(user_id)
    await update_chat_model(chat["id"], model)
    await callback.message.edit_text(
        f"✅ Для чата <b>{escape(chat['name'])}</b> "
        f"установлена модель <code>{escape(model)}</code>."
    )
    await callback.answer()


# ====================== Главный обработчик сообщений ======================

active_requests: set = set()


@router.message(F.text)
async def handle_text(message: Message):
    user_id = message.from_user.id
    if user_id in active_requests:
        await message.answer(
            "⚠️ Подожди, предыдущий запрос ещё обрабатывается..."
        )
        return

    await ensure_user(user_id)
    chat = await get_or_create_current_chat(user_id)
    user_text = message.text or ""

    # сохраняем сообщение пользователя в историю
    await save_message(chat["id"], "user", user_text)

    # получаем историю чата
    history = await get_chat_messages(chat["id"])

    # отправляем «Думаю...» и запускаем таймер
    thinking_msg = await message.answer("⏳ Думаю... 0с")
    indicator = ThinkingIndicator(
        message.bot, message.chat.id, thinking_msg.message_id
    )
    indicator.start()

    active_requests.add(user_id)
    try:
        # дополняем system prompt списком файлов пользователя
        files = list_workspace_files(user_id)
        files_section = ""
        if files:
            files_section = (
                "\n\nФайлы в рабочей директории пользователя:\n"
                + "\n".join(f"- {f}" for f in files)
            )
        system = SYSTEM_PROMPT + files_section

        # запрос к ИИ
        try:
            response = await ai_client.messages.create(
                model=chat["model"],
                max_tokens=4096,
                system=system,
                messages=history,
            )
        except anthropic.APIError as e:
            await indicator.stop()
            await thinking_msg.edit_text(f"❌ Ошибка API: {escape(str(e))}")
            return
        except Exception as e:
            log.exception("Ошибка запроса к ИИ")
            await indicator.stop()
            await thinking_msg.edit_text(
                f"❌ Ошибка запроса: {escape(str(e))}"
            )
            return

        # собираем текстовую часть ответа
        text_parts: list[str] = []
        for block in response.content:
            if getattr(block, "type", None) == "text":
                text_parts.append(block.text)
        response_text = "\n".join(text_parts).strip()

        if not response_text:
            await indicator.stop()
            await thinking_msg.edit_text("❌ ИИ вернул пустой ответ.")
            return

        # сохраняем ответ ассистента в историю
        await save_message(chat["id"], "assistant", response_text)
        # останавливаем таймер
        await indicator.stop()

        # извлекаем и сохраняем код
        code_blocks = extract_code_blocks(response_text)
        saved: list[Path] = []
        for blk in code_blocks:
            try:
                path = save_code_file(user_id, blk["filename"], blk["code"])
                if path:
                    saved.append(path)
            except Exception:
                log.exception("Ошибка сохранения файла %s", blk["filename"])

        # готовим текст для чата (без блоков кода, экранируем HTML)
        text_for_chat = CODE_BLOCK_RE.sub("", response_text).strip()
        text_for_chat = escape(text_for_chat) if text_for_chat else ""

        files_summary = ""
        if saved:
            files_summary = "\n\n📦 <b>Сохранённые файлы:</b>\n" + "\n".join(
                f"  • <code>{escape(p.name)}</code> "
                f"({p.stat().st_size} байт)"
                for p in saved
            )

        full_text = (text_for_chat or "<i>(код без пояснений)</i>") + files_summary

        # отправляем основной текст (с разбиением на части по 4000 символов)
        if len(full_text) <= 4000:
            await thinking_msg.edit_text(full_text)
        else:
            await thinking_msg.edit_text(full_text[:4000])
            remaining = full_text[4000:]
            while remaining:
                await message.answer(remaining[:4000])
                remaining = remaining[4000:]

        # отправляем файлы документами
        for path in saved:
            try:
                doc = FSInputFile(str(path), filename=path.name)
                await message.answer_document(
                    document=doc, caption=f"📄 {escape(path.name)}"
                )
            except Exception:
                log.exception("Не удалось отправить документ %s", path)

    except Exception as e:
        log.exception("Необработанная ошибка")
        try:
            await indicator.stop()
            await thinking_msg.edit_text(f"❌ Ошибка: {escape(str(e))}")
        except Exception:
            pass
    finally:
        active_requests.discard(user_id)


@router.message()
async def handle_other(message: Message):
    """Заглушка для нетекстовых сообщений."""
    if message.text is None:
        await message.answer(
            "Отправь текстовое сообщение с описанием задачи ✍️"
        )


# ====================== main ======================


async def main():
    await init_db()
    log.info("Бот запускается...")
    try:
        await dp.start_polling(bot)
    finally:
        await ai_client.close()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
