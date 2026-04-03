import os
import json
from datetime import datetime, date
from pathlib import Path
from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
from anthropic import Anthropic
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__, static_folder="static")
CORS(app)

client = Anthropic()

TASKS_FILE = Path(__file__).parent / "tasks.json"
IDEAS_FILE = Path(__file__).parent / "ideas.json"

SYSTEM_PROMPT = """Ты — личный планировщик-агент Дениса. Твоя главная задача: помочь ему начать день с ясной головой и реалистичным планом.

Когда Денис присылает голосовые планы на день — ты:
1. Кратко подтверждаешь, что услышал (1-2 предложения)
2. Анализируешь объём задач честно: если задач слишком много — говоришь прямо
3. Предлагаешь конкретный план: что сделать сегодня, что лучше перенести
4. Даёшь 1-2 приоритета на утро (самое важное делать на свежую голову)

Принципы:
- Будь честным, не льсти. Если план нереалистичный — скажи
- Думай о реальной энергии человека, не об идеальном сценарии
- Крупные задачи всегда предлагай разбить или перенести
- Короткий ответ лучше длинного. Максимум 150-200 слов
- Говори по-русски, тепло но по делу

Текущее время: {current_time}
"""

conversation_history = []


@app.route("/")
def index():
    return send_from_directory("static", "index.html")


@app.route("/api/chat", methods=["POST"])
def chat():
    data = request.json
    user_message = data.get("message", "").strip()

    if not user_message:
        return jsonify({"error": "Пустое сообщение"}), 400

    now = datetime.now().strftime("%H:%M, %A %d %B %Y")
    system = SYSTEM_PROMPT.format(current_time=now)

    conversation_history.append({"role": "user", "content": user_message})

    # Keep last 20 messages to avoid context overflow
    messages_to_send = conversation_history[-20:]

    response = client.messages.create(
        model="claude-opus-4-6",
        max_tokens=1024,
        system=system,
        messages=messages_to_send,
        thinking={"type": "adaptive"},
    )

    assistant_text = next(
        (block.text for block in response.content if block.type == "text"), ""
    )

    conversation_history.append({"role": "assistant", "content": assistant_text})

    return jsonify({
        "reply": assistant_text,
        "tokens_used": response.usage.output_tokens,
    })


@app.route("/api/reset", methods=["POST"])
def reset():
    conversation_history.clear()
    return jsonify({"status": "ok", "message": "История очищена"})


@app.route("/api/history", methods=["GET"])
def history():
    return jsonify({"history": conversation_history})


@app.route("/api/tasks", methods=["GET"])
def get_tasks():
    if TASKS_FILE.exists():
        data = json.loads(TASKS_FILE.read_text(encoding="utf-8"))
    else:
        data = {"tasks": []}
    today = date.today().isoformat()
    active = [t for t in data["tasks"] if t["status"] == "pending" and t["date"] >= today]
    return jsonify({"tasks": active})


@app.route("/api/ideas", methods=["GET"])
def get_ideas():
    if IDEAS_FILE.exists():
        data = json.loads(IDEAS_FILE.read_text(encoding="utf-8"))
    else:
        data = {"ideas": []}
    return jsonify(data)


@app.route("/api/tasks/<task_id>/done", methods=["POST"])
def mark_done(task_id):
    if not TASKS_FILE.exists():
        return jsonify({"error": "not found"}), 404
    data = json.loads(TASKS_FILE.read_text(encoding="utf-8"))
    for task in data["tasks"]:
        if task["id"] == task_id:
            task["status"] = "done"
            task["done_at"] = datetime.now().isoformat()
            TASKS_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            return jsonify({"status": "ok"})
    return jsonify({"error": "not found"}), 404


if __name__ == "__main__":
    key = os.getenv("ANTHROPIC_API_KEY")
    if not key:
        print("❌ ANTHROPIC_API_KEY не найден. Создай .env файл.")
        exit(1)
    port = int(os.getenv("PORT", 5000))
    debug = os.getenv("RAILWAY_ENVIRONMENT") is None
    print(f"✅ Сервер запущен на порту {port}")
    app.run(host="0.0.0.0", port=port, debug=debug)
