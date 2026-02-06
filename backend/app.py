"""
Web UI для чата с AI-менеджером турагентства
Flask + Server-Sent Events для streaming
"""

import asyncio
import os
import time
import uuid
import logging
from datetime import datetime
from flask import Flask, render_template, request, Response, jsonify, stream_with_context, g
from flask_cors import CORS
from werkzeug.exceptions import HTTPException
try:
    # если запускают из папки backend (python app.py)
    from yandex_handler import YandexGPTHandler
except ImportError:
    # если запускают из корня (python -m backend.app)
    from backend.yandex_handler import YandexGPTHandler
import json
import queue
import threading

app = Flask(__name__, template_folder='templates', static_folder='static')
CORS(app)

# === ЛОГИРОВАНИЕ ===
def _setup_logging() -> logging.Logger:
    """
    Единая настройка логирования в консоль.
    Управление:
      - LOG_LEVEL=DEBUG|INFO|WARNING|ERROR (по умолчанию INFO)
    """
    logger = logging.getLogger("mgp_bot")

    level_name = os.getenv("LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    logger.setLevel(level)

    if logger.handlers:
        handler = logger.handlers[0]
    else:
        handler = logging.StreamHandler()
        logger.addHandler(handler)

    handler.setLevel(level)
    formatter = logging.Formatter(
        fmt="%(asctime)s.%(msecs)03d %(levelname)s [%(name)s] %(message)s",
        datefmt="%H:%M:%S",
    )
    handler.setFormatter(formatter)

    # WerkZeug: по умолчанию скрываем access-логи (они дублируют наши -> / <-).
    # При необходимости можно включить обратно через WERKZEUG_LOG_LEVEL=INFO.
    werk_logger = logging.getLogger("werkzeug")
    werk_level_name = os.getenv("WERKZEUG_LOG_LEVEL", "WARNING").upper()
    werk_level = getattr(logging, werk_level_name, logging.WARNING)
    werk_logger.setLevel(werk_level)
    if not werk_logger.handlers:
        werk_logger.addHandler(handler)
    else:
        # на случай, если handler уже был, приведём его к одному формату
        for h in werk_logger.handlers:
            h.setLevel(werk_level)
            h.setFormatter(formatter)

    return logger


logger = _setup_logging()


def log(msg: str, level: str = "INFO"):
    """Совместимость со старым логгером (level=INFO/OK/WARN/ERROR/MSG/FUNC)."""
    level_map = {
        "INFO": logging.INFO,
        "OK": logging.INFO,
        "WARN": logging.WARNING,
        "ERROR": logging.ERROR,
        "MSG": logging.INFO,
        "FUNC": logging.DEBUG,
    }
    py_level = level_map.get(level, logging.INFO)
    logger.log(py_level, f"[{level}] {msg}")

# Глобальный handler (для простоты — один на всех, в production нужно по сессиям)
handlers = {}

def get_handler(session_id: str) -> YandexGPTHandler:
    """Получить или создать handler для сессии"""
    if session_id not in handlers:
        handlers[session_id] = YandexGPTHandler()
    return handlers[session_id]


@app.route('/')
def index():
    """Главная страница с чатом"""
    return render_template('chat.html')

@app.route('/favicon.ico')
def favicon():
    """Чтобы не засорять логи 404-ками от браузера."""
    return ("", 204)

@app.before_request
def _log_request_start():
    g._req_start = time.perf_counter()
    g.request_id = uuid.uuid4().hex[:8]
    logger.info("-> %s %s rid=%s ip=%s", request.method, request.path, g.request_id, request.remote_addr)


@app.after_request
def _log_request_end(response):
    try:
        duration_ms = int((time.perf_counter() - getattr(g, "_req_start", time.perf_counter())) * 1000)
    except Exception:
        duration_ms = -1
    rid = getattr(g, "request_id", "-")
    logger.info("<- %s %s %s %dms rid=%s", request.method, request.path, response.status_code, duration_ms, rid)
    # удобно дергать request-id из фронта при разборе багов
    response.headers["X-Request-Id"] = rid
    return response


@app.errorhandler(Exception)
def _handle_unexpected_error(e: Exception):
    # не ломаем штатные HTTP ошибки (404/405 и т.п.)
    if isinstance(e, HTTPException):
        return e

    rid = getattr(g, "request_id", "-")
    logger.exception("Unhandled exception rid=%s path=%s", rid, request.path)
    if request.path.startswith("/api/"):
        return jsonify({"error": str(e), "request_id": rid}), 500
    return "Internal Server Error", 500


@app.route('/api/chat', methods=['POST'])
def chat():
    """Обычный chat без streaming"""
    data = request.json
    message = data.get('message', '')
    session_id = data.get('session_id', 'default')
    
    if not message:
        return jsonify({'error': 'Empty message'}), 400
    
    handler = get_handler(session_id)
    
    try:
        # Запускаем async функцию
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        response = loop.run_until_complete(handler.chat(message))
        loop.close()
        
        return jsonify({'response': response})
    except Exception as e:
        logger.exception("chat error session_id=%s", session_id)
        return jsonify({'error': str(e)}), 500


@app.route('/api/chat/stream', methods=['POST'])
def chat_stream():
    """Chat со streaming через SSE"""
    data = request.json
    message = data.get('message', '')
    session_id = data.get('session_id', 'default')
    
    log(f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━", "INFO")
    log(f"📨 Новое сообщение от {session_id[:8]}...", "MSG")
    log(f"   └─ \"{message[:100]}{'...' if len(message) > 100 else ''}\"", "MSG")
    
    if not message:
        log("❌ Пустое сообщение!", "ERROR")
        return jsonify({'error': 'Empty message'}), 400
    
    handler = get_handler(session_id)
    log(f"📊 Модель: {handler.model}", "INFO")
    log(f"📊 История: {len(handler.input_list)} сообщений", "INFO")
    
    def generate():
        token_queue = queue.Queue()
        result = {'response': '', 'error': None}
        token_count = [0]  # Счётчик токенов
        
        def on_token(token):
            token_queue.put(('token', token))
            token_count[0] += 1
        
        def run_chat():
            try:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                log("🚀 Отправляю запрос в YandexGPT...", "INFO")
                response = loop.run_until_complete(
                    handler.chat_stream(message, on_token=on_token)
                )
                loop.close()
                result['response'] = response
                log(f"✅ Ответ получен: {len(response)} символов, {token_count[0]} токенов", "OK")
                log(f"   └─ \"{response[:150]}{'...' if len(response) > 150 else ''}\"", "OK")
                token_queue.put(('done', response))
            except Exception as e:
                result['error'] = str(e)
                logger.exception("stream chat error session_id=%s", session_id)
                log(f"❌ ОШИБКА: {e}", "ERROR")
                token_queue.put(('error', str(e)))
        
        # Запускаем в отдельном потоке
        thread = threading.Thread(target=run_chat)
        thread.start()
        
        # Стримим токены
        while True:
            try:
                event_type, data = token_queue.get(timeout=60)
                
                if event_type == 'token':
                    yield f"data: {json.dumps({'type': 'token', 'content': data})}\n\n"
                elif event_type == 'done':
                    yield f"data: {json.dumps({'type': 'done', 'content': data})}\n\n"
                    break
                elif event_type == 'error':
                    yield f"data: {json.dumps({'type': 'error', 'content': data})}\n\n"
                    break
            except queue.Empty:
                log("⏳ Таймаут ожидания...", "WARN")
                yield f"data: {json.dumps({'type': 'ping'})}\n\n"
        
        thread.join()
    
    return Response(
        stream_with_context(generate()),
        mimetype='text/event-stream',
        headers={
            'Cache-Control': 'no-cache',
            'X-Accel-Buffering': 'no'
        }
    )


@app.route('/api/reset', methods=['POST'])
def reset():
    """Сбросить историю диалога"""
    data = request.json or {}
    session_id = data.get('session_id', 'default')
    
    if session_id in handlers:
        handlers[session_id].reset()
        log(f"🔄 Сессия {session_id[:8]}... сброшена", "WARN")
    
    return jsonify({'status': 'ok'})


@app.route('/api/status')
def status():
    """Статус сервера"""
    return jsonify({
        'status': 'running',
        'sessions': len(handlers)
    })


if __name__ == '__main__':
    from dotenv import load_dotenv
    load_dotenv()
    
    model = os.getenv("YANDEX_MODEL", "yandexgpt")
    folder = os.getenv("YANDEX_FOLDER_ID", "???")
    
    print("\n" + "="*50)
    print("🚀 AI ТУРМЕНЕДЖЕР - Web UI")
    print("="*50)
    print(f"📍 URL: http://localhost:8080")
    print(f"🤖 Модель: {model}")
    print(f"📁 Folder: {folder[:8]}...")
    print("="*50 + "\n")
    
    app.run(host='0.0.0.0', port=8080, debug=True, threaded=True)
