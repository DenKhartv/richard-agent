import os
import json
import logging
import uuid
from datetime import datetime, date, timedelta
from pathlib import Path
import tempfile

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from anthropic import Anthropic
from openai import OpenAI
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

anthropic_client = Anthropic()
openai_client = OpenAI()

APP_URL = os.getenv("APP_URL", "")  # e.g. https://richard-agent.railway.app

TASKS_FILE = Path(__file__).parent / "tasks.json"
IDEAS_FILE = Path(__file__).parent / "ideas.json"
PROFILE_FILE = Path(__file__).parent / "denis_profile.md"
CONFIG_FILE = Path(__file__).parent / "config.json"

conversation_history = []

SYSTEM_PROMPT = """Ты — личный ИИ-агент Дениса по имени Ричард.

Твоя задача: помогать Денису планировать день, управлять задачами и не перегружаться.

Характер: умный, честный, прямой — как хороший друг. Не льстишь.

ВАЖНО — два типа вещей:
1. ЗАДАЧИ — конкретные дела с дедлайном (позвонить, сделать, отправить)
2. ИДЕИ — расплывчатые мысли, проекты без сроков ("было бы круто...", "думаю попробовать...", "идея для...")

Алгоритм при получении информации о планах:
1. Сначала разбери: что тут задача, а что идея?
2. Идеи → сразу сохрани через add_idea (не задавай вопросов)
3. Задачи → задай уточняющие вопросы ПЕРЕД тем как добавить.
   ВАЖНО: вопросы должны быть конкретными и явными, по каждой задаче отдельно.
   Пример правильного ответа:
   "Окей, несколько вопросов:
   1. Написать Вадиму — ты сказал "к вчеру", значит это уже просрочено? Когда именно нужно?
   2. Созвон с Вадимом — сегодня или на другой день?
   3. Лид-магнит с Антоном — это на сегодня или можно на завтра?
   4. ОП — договор и файлы — когда дедлайн?"

   НЕ пиши "жду ответы" без самих вопросов. Вопросы должны быть в том же сообщении.
4. Только после ответов Дениса → add_task для каждой задачи
5. Если узнал что-то новое о Денисе → update_profile

Принципы:
- Максимум 150 слов в ответе
- Говори по-русски, тепло но по делу
- Крупные задачи предлагай разбить или перенести
- Думай о реальной энергии человека, не об идеальном сценарии
- Профиль Дениса — это твоя долгосрочная память о нём. Обновляй его при каждой новой информации.

Текущее время: {current_time}

---
ПРОФИЛЬ ДЕНИСА (твоя долгосрочная память):
{profile}
---

Активные задачи:
{active_tasks}

Идеи (последние 5):
{recent_ideas}
"""

TOOLS = [
    {
        "name": "add_task",
        "description": "Добавить задачу в календарь Дениса. Вызывать ТОЛЬКО после того, как уточнены дата и приоритет.",
        "input_schema": {
            "type": "object",
            "properties": {
                "title": {
                    "type": "string",
                    "description": "Название задачи"
                },
                "date": {
                    "type": "string",
                    "description": "Дата в формате YYYY-MM-DD"
                },
                "priority": {
                    "type": "string",
                    "enum": ["high", "medium", "low"],
                    "description": "Приоритет: high=срочно, medium=важно, low=когда-нибудь"
                },
                "notes": {
                    "type": "string",
                    "description": "Дополнительные заметки"
                },
            },
            "required": ["title", "date", "priority"],
        },
    },
    {
        "name": "add_idea",
        "description": "Сохранить идею Дениса. Использовать для расплывчатых мыслей, проектов без сроков, 'было бы круто' и т.п.",
        "input_schema": {
            "type": "object",
            "properties": {
                "text": {
                    "type": "string",
                    "description": "Текст идеи"
                },
                "tags": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Теги для категоризации (например: ['проект', 'бизнес'] или ['личное'])"
                },
            },
            "required": ["text"],
        },
    },
    {
        "name": "update_profile",
        "description": "Обновить раздел профиля Дениса. Вызывай каждый раз, когда узнаёшь что-то новое о нём.",
        "input_schema": {
            "type": "object",
            "properties": {
                "section": {
                    "type": "string",
                    "enum": [
                        "Характер и стиль работы",
                        "Паттерны продуктивности",
                        "Предпочтения и антипатии",
                        "Текущие проекты",
                        "Как общаться с Денисом",
                        "Наблюдения"
                    ],
                    "description": "Какой раздел профиля обновить"
                },
                "content": {
                    "type": "string",
                    "description": "Новое содержимое раздела (полностью заменяет старое)"
                },
            },
            "required": ["section", "content"],
        },
    },
]


# ── Tasks ────────────────────────────────────────────────────────────────────

def load_tasks() -> dict:
    if TASKS_FILE.exists():
        return json.loads(TASKS_FILE.read_text(encoding="utf-8"))
    return {"tasks": []}


def save_tasks(data: dict):
    TASKS_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def add_task(title: str, task_date: str, priority: str, notes: str = "") -> dict:
    tasks_data = load_tasks()
    task = {
        "id": str(uuid.uuid4())[:8],
        "title": title,
        "date": task_date,
        "priority": priority,
        "notes": notes,
        "status": "pending",
        "created_at": datetime.now().isoformat(),
    }
    tasks_data["tasks"].append(task)
    save_tasks(tasks_data)
    return task


def get_active_tasks_summary() -> str:
    tasks_data = load_tasks()
    today = date.today().isoformat()
    active = [t for t in tasks_data["tasks"] if t["status"] == "pending" and t["date"] >= today]
    if not active:
        return "Нет активных задач"
    lines = []
    for t in sorted(active, key=lambda x: (x["date"], {"high": 0, "medium": 1, "low": 2}.get(x["priority"], 3)))[:10]:
        lines.append(f"• [{t['date']}] {t['title']} ({t['priority']})")
    return "\n".join(lines)


# ── Ideas ────────────────────────────────────────────────────────────────────

def load_ideas() -> dict:
    if IDEAS_FILE.exists():
        return json.loads(IDEAS_FILE.read_text(encoding="utf-8"))
    return {"ideas": []}


def save_ideas(data: dict):
    IDEAS_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def add_idea_to_file(text: str, tags: list = None) -> dict:
    ideas_data = load_ideas()
    idea = {
        "id": str(uuid.uuid4())[:8],
        "text": text,
        "tags": tags or [],
        "created_at": datetime.now().isoformat(),
    }
    ideas_data["ideas"].append(idea)
    save_ideas(ideas_data)
    return idea


def get_recent_ideas_summary() -> str:
    ideas_data = load_ideas()
    ideas = ideas_data.get("ideas", [])
    if not ideas:
        return "Нет сохранённых идей"
    recent = ideas[-5:]
    lines = []
    for i in recent:
        tags_str = f" [{', '.join(i['tags'])}]" if i.get("tags") else ""
        lines.append(f"• {i['text']}{tags_str}")
    return "\n".join(lines)


# ── Profile ──────────────────────────────────────────────────────────────────

def load_profile() -> str:
    if PROFILE_FILE.exists():
        return PROFILE_FILE.read_text(encoding="utf-8")
    return "(Профиль пока пустой — буду заполнять по мере общения)"


def update_profile_section(section: str, content: str):
    profile_text = load_profile()

    if profile_text.startswith("("):
        profile_text = _empty_profile()

    lines = profile_text.split("\n")
    new_lines = []
    inside_section = False
    section_replaced = False

    for line in lines:
        if line.strip() == f"## {section}":
            inside_section = True
            new_lines.append(line)
            new_lines.append(content)
            section_replaced = True
            continue

        if inside_section:
            if line.startswith("## "):
                inside_section = False
                new_lines.append(line)
        else:
            new_lines.append(line)

    if not section_replaced:
        new_lines.append(f"\n## {section}")
        new_lines.append(content)

    PROFILE_FILE.write_text("\n".join(new_lines), encoding="utf-8")


def _empty_profile() -> str:
    return """# Профиль Дениса

## Характер и стиль работы

## Паттерны продуктивности

## Предпочтения и антипатии

## Текущие проекты

## Как общаться с Денисом

## Наблюдения
"""


# ── Config (chat_id persistence) ─────────────────────────────────────────────

def save_chat_id(chat_id: int):
    config = {}
    if CONFIG_FILE.exists():
        config = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    config["denis_chat_id"] = chat_id
    CONFIG_FILE.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")


def load_chat_id() -> int | None:
    if CONFIG_FILE.exists():
        config = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        return config.get("denis_chat_id")
    return None


# ── Voice ────────────────────────────────────────────────────────────────────

async def transcribe_voice(file_path: str) -> str:
    with open(file_path, "rb") as f:
        transcript = openai_client.audio.transcriptions.create(
            model="whisper-1",
            file=f,
            language="ru",
        )
    return transcript.text


# ── Claude loop ───────────────────────────────────────────────────────────────

async def process_with_claude(user_message: str) -> str:
    global conversation_history

    now = datetime.now().strftime("%H:%M, %A %d %B %Y")
    profile = load_profile()
    active_tasks = get_active_tasks_summary()
    recent_ideas = get_recent_ideas_summary()

    system = SYSTEM_PROMPT.format(
        current_time=now,
        profile=profile,
        active_tasks=active_tasks,
        recent_ideas=recent_ideas,
    )

    conversation_history.append({"role": "user", "content": user_message})
    messages_to_send = conversation_history[-20:]

    added_tasks = []
    added_ideas = []
    updated_sections = []

    while True:
        response = anthropic_client.messages.create(
            model="claude-opus-4-6",
            max_tokens=1024,
            system=system,
            messages=messages_to_send,
            tools=TOOLS,
            thinking={"type": "adaptive"},
        )

        if response.stop_reason == "tool_use":
            tool_results = []
            for block in response.content:
                if block.type == "tool_use":
                    if block.name == "add_task":
                        task = add_task(
                            title=block.input["title"],
                            task_date=block.input["date"],
                            priority=block.input["priority"],
                            notes=block.input.get("notes", ""),
                        )
                        added_tasks.append(task["title"])
                        tool_results.append({
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": f"Задача добавлена: {task['title']} на {task['date']}",
                        })

                    elif block.name == "add_idea":
                        idea = add_idea_to_file(
                            text=block.input["text"],
                            tags=block.input.get("tags", []),
                        )
                        added_ideas.append(idea["text"])
                        tool_results.append({
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": f"Идея сохранена: {idea['text']}",
                        })

                    elif block.name == "update_profile":
                        update_profile_section(block.input["section"], block.input["content"])
                        updated_sections.append(block.input["section"])
                        tool_results.append({
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": f"Профиль обновлён: раздел «{block.input['section']}»",
                        })

            messages_to_send = list(messages_to_send) + [
                {"role": "assistant", "content": response.content},
                {"role": "user", "content": tool_results},
            ]
        else:
            break

    assistant_text = next(
        (block.text for block in response.content if block.type == "text"), ""
    )

    conversation_history.append({"role": "assistant", "content": assistant_text})

    suffix_parts = []
    if added_tasks:
        task_list = "\n".join(f"✅ {t}" for t in added_tasks)
        suffix_parts.append(f"*Добавил в календарь:*\n{task_list}")
    if added_ideas:
        idea_list = "\n".join(f"💡 {i}" for i in added_ideas)
        suffix_parts.append(f"*Сохранил идеи:*\n{idea_list}")
    if updated_sections:
        suffix_parts.append(f"_Обновил профиль: {', '.join(updated_sections)}_")

    if suffix_parts:
        assistant_text += "\n\n" + "\n".join(suffix_parts)

    return assistant_text


# ── Scheduled jobs ────────────────────────────────────────────────────────────

async def send_ideas_reminder(context: ContextTypes.DEFAULT_TYPE):
    chat_id = load_chat_id()
    if not chat_id:
        return

    ideas_data = load_ideas()
    ideas = ideas_data.get("ideas", [])
    if not ideas:
        return

    lines = ["💡 *Напоминание об идеях*\n", "Вот что у тебя накопилось:\n"]
    for i in ideas[-10:]:
        tags_str = f" _{', '.join(i['tags'])}_" if i.get("tags") else ""
        lines.append(f"• {i['text']}{tags_str}")

    lines.append("\n_Может, что-то уже пора превратить в задачу?_")
    await context.bot.send_message(chat_id=chat_id, text="\n".join(lines), parse_mode="Markdown")


# ── Handlers ──────────────────────────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    save_chat_id(update.effective_chat.id)
    await update.message.reply_text(
        "Привет, я Ричард 👋\n\n"
        "Отправь голосовое или текст — расскажи что планируешь, "
        "и я помогу с приоритетами, добавлю задачи и сохраню идеи.\n\n"
        "/today — задачи на сегодня\n"
        "/week — задачи на неделю\n"
        "/tasks — все активные задачи\n"
        "/ideas — все идеи\n"
        "/done ID — отметить выполненной\n"
        "/profile — что я знаю о тебе\n"
        "/reset — новый разговор"
    )


async def today_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tasks_data = load_tasks()
    today = date.today().isoformat()
    todays = [t for t in tasks_data["tasks"] if t["status"] == "pending" and t["date"] == today]

    if not todays:
        await update.message.reply_text("На сегодня задач нет 🎉")
        return

    priority_emoji = {"high": "🔴", "medium": "🟡", "low": "🟢"}
    lines = [f"*Сегодня, {today}:*\n"]
    for t in sorted(todays, key=lambda x: {"high": 0, "medium": 1, "low": 2}.get(x["priority"], 3)):
        emoji = priority_emoji.get(t["priority"], "⚪")
        lines.append(f"{emoji} `{t['id']}` {t['title']}")
        if t.get("notes"):
            lines.append(f"   _{t['notes']}_")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def week_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tasks_data = load_tasks()
    today = date.today()
    week_end = (today + timedelta(days=7)).isoformat()
    today_str = today.isoformat()

    week_tasks = [
        t for t in tasks_data["tasks"]
        if t["status"] == "pending" and today_str <= t["date"] <= week_end
    ]

    if not week_tasks:
        await update.message.reply_text("На эту неделю задач нет 🎉")
        return

    priority_emoji = {"high": "🔴", "medium": "🟡", "low": "🟢"}
    lines = ["*Задачи на 7 дней:*\n"]
    current_date = None
    for t in sorted(week_tasks, key=lambda x: (x["date"], {"high": 0, "medium": 1, "low": 2}.get(x["priority"], 3))):
        if t["date"] != current_date:
            current_date = t["date"]
            label = "Сегодня" if current_date == today_str else current_date
            lines.append(f"\n*{label}*")
        emoji = priority_emoji.get(t["priority"], "⚪")
        lines.append(f"{emoji} `{t['id']}` {t['title']}")

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def tasks_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tasks_data = load_tasks()
    today = date.today().isoformat()
    active = [t for t in tasks_data["tasks"] if t["status"] == "pending" and t["date"] >= today]

    if not active:
        await update.message.reply_text("Нет активных задач 🎉")
        return

    priority_emoji = {"high": "🔴", "medium": "🟡", "low": "🟢"}
    lines = ["*Активные задачи:*\n"]
    for t in sorted(active, key=lambda x: (x["date"], {"high": 0, "medium": 1, "low": 2}.get(x["priority"], 3))):
        emoji = priority_emoji.get(t["priority"], "⚪")
        lines.append(f"{emoji} `{t['id']}` {t['title']}")
        lines.append(f"   📅 {t['date']}")
        if t.get("notes"):
            lines.append(f"   _{t['notes']}_")
        lines.append("")

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def ideas_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ideas_data = load_ideas()
    ideas = ideas_data.get("ideas", [])

    if not ideas:
        await update.message.reply_text("Идей пока нет 💡\nПросто расскажи мне что-нибудь — я сам разберу что задача, а что идея.")
        return

    lines = [f"*Твои идеи ({len(ideas)}):*\n"]
    for i in reversed(ideas):
        tags_str = f" _{', '.join(i['tags'])}_" if i.get("tags") else ""
        created = i["created_at"][:10]
        lines.append(f"💡 `{i['id']}` {i['text']}{tags_str}")
        lines.append(f"   _{created}_")
        lines.append("")

    text = "\n".join(lines)
    if len(text) > 4000:
        text = text[:4000] + "\n...(обрезано)"
    await update.message.reply_text(text, parse_mode="Markdown")


async def done_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Укажи ID задачи: /done abc12345")
        return

    task_id = context.args[0]
    tasks_data = load_tasks()

    for task in tasks_data["tasks"]:
        if task["id"] == task_id:
            task["status"] = "done"
            task["done_at"] = datetime.now().isoformat()
            save_tasks(tasks_data)
            await update.message.reply_text(f"✅ Выполнено: *{task['title']}*", parse_mode="Markdown")
            return

    await update.message.reply_text(f"Задача `{task_id}` не найдена", parse_mode="Markdown")


async def profile_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    profile = load_profile()
    if profile.startswith("("):
        await update.message.reply_text("Профиль пока пустой — пообщаемся, и я начну тебя запоминать 🙂")
        return
    if len(profile) > 4000:
        profile = profile[:4000] + "\n...(обрезано)"
    await update.message.reply_text(profile, parse_mode="Markdown")


async def reset_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global conversation_history
    conversation_history = []
    await update.message.reply_text("История очищена. Начнём заново 🔄\n_(Профиль и задачи сохранены)_", parse_mode="Markdown")


def calendar_keyboard() -> InlineKeyboardMarkup | None:
    if not APP_URL:
        return None
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("📅 Открыть календарь", web_app={"url": APP_URL})
    ]])


async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    save_chat_id(update.effective_chat.id)
    await update.message.reply_text("🎙️ Слушаю...")

    voice = update.message.voice
    tg_file = await context.bot.get_file(voice.file_id)

    with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
        tmp_path = tmp.name

    try:
        await tg_file.download_to_drive(tmp_path)
        await update.message.reply_text("✍️ Расшифровываю...")

        transcript = await transcribe_voice(tmp_path)

        if not transcript.strip():
            await update.message.reply_text("Не смог разобрать голосовое. Попробуй ещё раз.")
            return

        await update.message.reply_text(f"_Услышал:_ {transcript}", parse_mode="Markdown")
        await update.message.reply_text("💭 Думаю...")

        reply = await process_with_claude(transcript)
        await update.message.reply_text(reply, parse_mode="Markdown", reply_markup=calendar_keyboard())

    except Exception as e:
        logger.error(f"Voice error: {e}")
        await update.message.reply_text("Что-то пошло не так. Попробуй ещё раз.")
    finally:
        Path(tmp_path).unlink(missing_ok=True)


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    save_chat_id(update.effective_chat.id)
    text = update.message.text
    if text.startswith("/"):
        return

    await update.message.reply_text("💭 Думаю...")
    try:
        reply = await process_with_claude(text)
        await update.message.reply_text(reply, parse_mode="Markdown", reply_markup=calendar_keyboard())
    except Exception as e:
        logger.error(f"Text error: {e}")
        await update.message.reply_text("Что-то пошло не так. Попробуй ещё раз.")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    token = os.getenv("TELEGRAM_TOKEN")
    if not token:
        print("❌ TELEGRAM_TOKEN не найден в .env")
        return

    anthropic_key = os.getenv("ANTHROPIC_API_KEY")
    if not anthropic_key or anthropic_key == "your_anthropic_key_here":
        print("❌ ANTHROPIC_API_KEY не найден в .env")
        return

    openai_key = os.getenv("OPENAI_API_KEY")
    if not openai_key:
        print("❌ OPENAI_API_KEY не найден в .env")
        return

    if not PROFILE_FILE.exists():
        PROFILE_FILE.write_text(_empty_profile(), encoding="utf-8")
        print("📝 Создан denis_profile.md")

    if not IDEAS_FILE.exists():
        IDEAS_FILE.write_text(json.dumps({"ideas": []}, ensure_ascii=False, indent=2), encoding="utf-8")
        print("💡 Создан ideas.json")

    app = Application.builder().token(token).build()

    # Commands
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("today", today_command))
    app.add_handler(CommandHandler("week", week_command))
    app.add_handler(CommandHandler("tasks", tasks_command))
    app.add_handler(CommandHandler("ideas", ideas_command))
    app.add_handler(CommandHandler("done", done_command))
    app.add_handler(CommandHandler("profile", profile_command))
    app.add_handler(CommandHandler("reset", reset_command))

    # Messages
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    # Reminder: ideas every 3 days (259200 seconds), first run after 3 days
    app.job_queue.run_repeating(
        send_ideas_reminder,
        interval=60 * 60 * 24 * 3,
        first=60 * 60 * 24 * 3,
    )

    print("✅ Ричард запущен! Открой Telegram и напиши боту.")
    app.run_polling()


if __name__ == "__main__":
    main()
