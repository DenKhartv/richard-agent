"""
Единая точка входа: Flask (web) + Telegram bot в одном процессе.
Flask крутится в фоновом потоке, бот — в основном asyncio loop.
"""
import threading
import os
from dotenv import load_dotenv

load_dotenv()


def run_flask():
    from app import app
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)


def run_bot():
    from bot import main
    main()


if __name__ == "__main__":
    # Flask в отдельном потоке
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()

    # Бот в основном потоке
    run_bot()
