import os
import time
import uuid
import wave
import sqlite3
import glob
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from openai import OpenAI
import uvicorn
import edge_tts

# ==========================================
# КОНФИГУРАЦИЯ — заполните перед запуском
# ==========================================
OPENAI_API_KEY = "YOUR_API_KEY_HERE"          # Ваш ключ OpenAI / ProxyAPI
OPENAI_BASE_URL = "https://openai.api.proxyapi.ru/v1"  # Или стандартный https://api.openai.com/v1
LOCAL_IP = "192.168.1.100"                   # IP этого компьютера в локальной сети
# ==========================================

# Инициализация приложения FastAPI
app = FastAPI(title="AI Local Server", description="Сервер поддержки принятия решений")
# Убедимся, что папка для статических аудиофайлов существует
os.makedirs("static_audio", exist_ok=True)
os.makedirs("temp_audio", exist_ok=True)
# Раздача статических аудиофайлов по пути /audio
app.mount("/audio", StaticFiles(directory="static_audio"), name="audio")
# Инициализация OpenAI клиента
client = OpenAI(
    api_key=OPENAI_API_KEY,
    base_url=OPENAI_BASE_URL
)
# ==========================================
# 1. БАЗА ДАННЫХ И ИНИЦИАЛИЗАЦИЯ
# ==========================================
def init_db():
    conn = sqlite3.connect("business.db")
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS inventory (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            item_name TEXT,
            quantity INTEGER,
            price REAL
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS chat_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            question TEXT,
            answer TEXT,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS device_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            level TEXT,
            message TEXT,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)
    cursor.execute("SELECT COUNT(*) FROM inventory")
    if cursor.fetchone()[0] == 0:
        mock_data = [
            ("Кофейные зерна (1кг)", 15, 1200.0),
            ("Молоко (1л)", 50, 90.0),
            ("Сахар (1кг)", 30, 80.0),
            ("Стаканчики бумажные", 500, 5.0),
            ("Сироп Ванильный", 5, 450.0)
        ]
        cursor.executemany("INSERT INTO inventory (item_name, quantity, price) VALUES (?, ?, ?)", mock_data)
        conn.commit()
        print("База данных business.db инициализирована начальными данными.")
    conn.close()
init_db()
def get_inventory_string():
    conn = sqlite3.connect("business.db")
    cursor = conn.cursor()
    cursor.execute("SELECT item_name, quantity, price FROM inventory")
    rows = cursor.fetchall()
    conn.close()
    
    if not rows:
        return "Склад пуст."
        
    lines = []
    for row in rows:
        lines.append(f"{row[0]}: {row[1]} шт., цена {row[2]} руб.")
    return "\n".join(lines)
# ==========================================
# 2. ОСНОВНОЙ ЭНДПОИНТ ДЛЯ ESP32
# ==========================================
@app.post("/api/process_audio")
async def process_audio(request: Request):
    try:
        # 1. Принимаем сырые PCM байты
        raw_pcm_bytes = await request.body()
        if not raw_pcm_bytes:
            return JSONResponse({"error": "No audio data received"}, status_code=400)
            
        t_start_total = time.perf_counter()
        t0 = time.perf_counter()
        
        # 1.5 Очистка старых MP3 файлов (старше 5 минут), чтобы не забивать диск сервера
        now = time.time()
        for f in glob.glob("static_audio/*.mp3"):
            try:
                if os.stat(f).st_mtime < now - 300:
                    os.remove(f)
            except Exception:
                pass

        print(f"Получено аудио: {len(raw_pcm_bytes)} байт.")
        request_id = uuid.uuid4().hex
        wav_filename = f"temp_audio/req_{request_id}.wav"
        mp3_filename = f"reply_{request_id}.mp3"
        mp3_filepath = f"static_audio/{mp3_filename}"
        # 2. Конвертируем в WAV
        with wave.open(wav_filename, "wb") as wav_file:
            wav_file.setnchannels(1)       
            wav_file.setsampwidth(2)       
            wav_file.setframerate(8000)    
            wav_file.writeframes(raw_pcm_bytes)
            
        t1 = time.perf_counter()
        print(f"[ПРОФИЛИРОВАНИЕ] Получение байт и конвертация в WAV: {t1 - t0:.2f} сек.")
        # 3. STT: Речь в текст (Whisper)
        t2 = time.perf_counter()
        print("Отправка аудио в Whisper STT...")
        with open(wav_filename, "rb") as audio_file:
            transcription = client.audio.transcriptions.create(
                model="whisper-1", 
                file=audio_file,
                language="ru"
            )
        user_text = transcription.text
        t3 = time.perf_counter()
        print(f"Распознанный текст: {user_text}")
        print(f"[ПРОФИЛИРОВАНИЕ] Whisper STT: {t3 - t2:.2f} сек.")
        # 4. Получаем данные со склада
        t4 = time.perf_counter()
        db_data = get_inventory_string()
        t5 = time.perf_counter()
        print(f"[ПРОФИЛИРОВАНИЕ] Обращение к SQLite (RAG-контекст): {t5 - t4:.2f} сек.")
        
        # 5. Запрос к ChatGPT
        t6 = time.perf_counter()
        print("Запрос к ChatGPT (LLM)...")
        system_prompt = (
            "Ты — голосовой AI-ассистент системы поддержки принятия решений (СППР) для персонала кофейни. "
            "Твоя главная задача — давать точные и быстрые ответы о состоянии склада, основываясь СТРОГО на предоставленной базе данных. "
            f"\nТекущие складские остатки:\n{db_data}\n\n"
            "СТРОГИЕ ПРАВИЛА:\n"
            "1. Отвечай кратко, емко и разговорным языком (максимум 2 предложения), так как твой ответ будет синтезирован в голос.\n"
            "2. Опирайся ТОЛЬКО на предоставленные данные. Если запрашиваемого товара нет в списке, честно ответь: «Этого товара нет в базе».\n"
            "3. Категорически запрещено придумывать цифры (галлюцинировать) или давать советы, не связанные со складом.\n"
            "4. Если остаток запрашиваемого товара меньше 10 единиц, обязательно предупреди бариста, что товар заканчивается."
        )
        
        completion = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_text}
            ],
            max_tokens=60
        )
        llm_reply = completion.choices[0].message.content
        t7 = time.perf_counter()
        print(f"Ответ нейросети: {llm_reply}")
        print(f"[ПРОФИЛИРОВАНИЕ] Генерация ответа (LLM GPT-4o-mini): {t7 - t6:.2f} сек.")
        t8 = time.perf_counter()
        conn = sqlite3.connect("business.db")
        cursor = conn.cursor()
        cursor.execute("INSERT INTO chat_history (question, answer) VALUES (?, ?)", (user_text, llm_reply))
        conn.commit()
        conn.close()
        t9 = time.perf_counter()
        print(f"[ПРОФИЛИРОВАНИЕ] Сохранение логов в БД: {t9 - t8:.2f} сек.")
        # 6. НОВАЯ СИСТЕМА TTS (Microsoft Edge - Бесплатно и без лимитов)
        t10 = time.perf_counter()
        print("Генерация аудио ответа (TTS Edge)...")
        communicate = edge_tts.Communicate(llm_reply, "ru-RU-SvetlanaNeural", rate="+20%")
        await communicate.save(mp3_filepath)
        t11 = time.perf_counter()
        print(f"[ПРОФИЛИРОВАНИЕ] Синтез речи (Edge TTS): {t11 - t10:.2f} сек.")
        if os.path.exists(wav_filename):
            os.remove(wav_filename)
        final_url = f"http://{LOCAL_IP}:8000/audio/{mp3_filename}"
        print(f"Готово! URL ответа: {final_url}")
        
        t_end_total = time.perf_counter()
        total_time = t_end_total - t_start_total
        profiling_msg = f"⏱ Общее время: {total_time:.2f} сек. (STT: {t3-t2:.2f}s, RAG: {t5-t4:.2f}s, LLM: {t7-t6:.2f}s, TTS: {t11-t10:.2f}s)"
        print(f"[ПРОФИЛИРОВАНИЕ] {profiling_msg}\n")
        
        conn = sqlite3.connect("business.db")
        cursor = conn.cursor()
        cursor.execute("INSERT INTO device_logs (level, message) VALUES (?, ?)", ("PROFILING", profiling_msg))
        conn.commit()
        conn.close()
        
        return JSONResponse({"url": final_url})
    except Exception as e:
        print(f"Ошибка сервера: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)
# ==========================================
# 3. DASHBOARD
# ==========================================
@app.post("/api/log")
async def receive_log(request: Request):
    try:
        data = await request.json()
        level = data.get("level", "INFO")
        message = data.get("message", "No message")
        conn = sqlite3.connect("business.db")
        cursor = conn.cursor()
        cursor.execute("INSERT INTO device_logs (level, message) VALUES (?, ?)", (level, message))
        conn.commit()
        conn.close()
        return JSONResponse({"status": "success"})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

@app.post("/api/clear_logs")
async def clear_logs():
    conn = sqlite3.connect("business.db")
    cursor = conn.cursor()
    cursor.execute("DELETE FROM device_logs")
    conn.commit()
    conn.close()
    return JSONResponse({"status": "cleared"})

@app.post("/api/clear_history")
async def clear_history():
    conn = sqlite3.connect("business.db")
    cursor = conn.cursor()
    cursor.execute("DELETE FROM chat_history")
    conn.commit()
    conn.close()
    return JSONResponse({"status": "cleared"})

@app.get("/api/get_chats")
async def get_chats():
    conn = sqlite3.connect("business.db")
    cursor = conn.cursor()
    cursor.execute("SELECT question, answer, timestamp FROM chat_history ORDER BY id DESC")
    rows = cursor.fetchall()
    conn.close()
    chats = [{"question": r[0], "answer": r[1], "timestamp": r[2]} for r in rows]
    return JSONResponse({"chats": chats})

@app.get("/api/get_logs")
async def get_logs():
    conn = sqlite3.connect("business.db")
    cursor = conn.cursor()
    cursor.execute("SELECT level, message, timestamp FROM device_logs ORDER BY id DESC")
    rows = cursor.fetchall()
    conn.close()
    logs = [{"level": r[0], "message": r[1], "timestamp": r[2]} for r in rows]
    return JSONResponse({"logs": logs})

@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard():
    html_content = """
    <!DOCTYPE html>
    <html lang="ru">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Панель управления СППР</title>
        <style>
            body { font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; background-color: #f5f7f9; margin: 0; padding: 20px; color: #333; }
            .nav-bar { display: flex; gap: 10px; margin-bottom: 20px; }
            .nav-btn { text-decoration: none; padding: 10px 20px; border-radius: 6px; font-weight: bold; color: white; background: #0d47a1; transition: background 0.3s; }
            .nav-btn:hover { background: #1565c0; }
            .nav-btn.active { background: #ff4757; cursor: default; }
            .container { max-width: 800px; margin: 0 auto; background: white; border-radius: 12px; box-shadow: 0 4px 6px rgba(0,0,0,0.1); padding: 20px; }
            .header-flex { display: flex; justify-content: space-between; align-items: center; border-bottom: 2px solid #eee; padding-bottom: 15px; margin-bottom: 20px; }
            .header-flex h1 { border: none; padding: 0; margin: 0; }
            .btn-clear { background: #ff4757; color: white; border: none; padding: 10px 15px; border-radius: 6px; cursor: pointer; font-weight: bold; transition: background 0.3s; }
            .btn-clear:hover { background: #ff6b81; }
            .message-group { margin-bottom: 20px; padding-bottom: 15px; border-bottom: 1px dashed #eee; }
            .time { font-size: 0.8em; color: #888; text-align: center; margin-bottom: 10px; }
            .message { padding: 12px 18px; border-radius: 18px; margin-bottom: 10px; max-width: 80%; line-height: 1.4; }
            .user-message { background-color: #e3f2fd; color: #0d47a1; margin-left: auto; border-bottom-right-radius: 4px; }
            .ai-message { background-color: #f1f8e9; color: #33691e; margin-right: auto; border-bottom-left-radius: 4px; }
            .empty { text-align: center; color: #777; padding: 40px 0; font-style: italic; }
        </style>
        <script>
            function clearHistory() {
                if(confirm('Вы уверены, что хотите полностью очистить историю диалогов?')) {
                    fetch('/api/clear_history', {method: 'POST'})
                    .then(response => fetchChats());
                }
            }

            function fetchChats() {
                fetch('/api/get_chats')
                    .then(response => response.json())
                    .then(data => {
                        const container = document.getElementById('chat-container');
                        if (data.chats.length === 0) {
                            container.innerHTML = "<div class='empty'>Пока нет истории диалогов. Запишите первое аудио!</div>";
                            return;
                        }
                        let html = "";
                        data.chats.forEach(chat => {
                            html += `
                            <div class="message-group">
                                <div class="time">${chat.timestamp}</div>
                                <div class="message user-message"><strong>Вы:</strong> ${chat.question}</div>
                                <div class="message ai-message"><strong>AI:</strong> ${chat.answer}</div>
                            </div>
                            `;
                        });
                        container.innerHTML = html;
                    })
                    .catch(err => console.error("Ошибка при получении чатов:", err));
            }

            setInterval(fetchChats, 3000);
            window.onload = fetchChats;
        </script>
    </head>
    <body>
        <div class="container">
            <div class="nav-bar">
                <a href="/dashboard" class="nav-btn active">Дашборд (Чаты)</a>
                <a href="/monitoring" class="nav-btn">Мониторинг ESP32</a>
            </div>
            <div class="header-flex">
                <h1>История диалогов</h1>
                <button class="btn-clear" onclick="clearHistory()">Очистить историю</button>
            </div>
            <div id="chat-container">
                <div class='empty'>Загрузка...</div>
            </div>
        </div>
    </body>
    </html>
    """
    return HTMLResponse(content=html_content)

@app.get("/monitoring", response_class=HTMLResponse)
async def monitoring():
    html_content = """
    <!DOCTYPE html>
    <html lang="ru">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Мониторинг ESP32</title>
        <style>
            body { font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; background-color: #f5f7f9; margin: 0; padding: 20px; color: #333; }
            .nav-bar { display: flex; gap: 10px; margin-bottom: 20px; }
            .nav-btn { text-decoration: none; padding: 10px 20px; border-radius: 6px; font-weight: bold; color: white; background: #0d47a1; transition: background 0.3s; }
            .nav-btn:hover { background: #1565c0; }
            .nav-btn.active { background: #ff4757; cursor: default; }
            .container { max-width: 800px; margin: 0 auto; background: white; border-radius: 12px; box-shadow: 0 4px 6px rgba(0,0,0,0.1); padding: 20px; }
            .header-flex { display: flex; justify-content: space-between; align-items: center; border-bottom: 2px solid #eee; padding-bottom: 15px; margin-bottom: 20px; }
            .header-flex h1 { border: none; padding: 0; margin: 0; }
            .btn-clear { background: #ff4757; color: white; border: none; padding: 10px 15px; border-radius: 6px; cursor: pointer; font-weight: bold; transition: background 0.3s; }
            .btn-clear:hover { background: #ff6b81; }
            .log-item { display: flex; align-items: center; padding: 10px; border-bottom: 1px solid #eee; }
            .log-item:last-child { border-bottom: none; }
            .log-time { font-size: 0.85em; color: #888; width: 150px; flex-shrink: 0; }
            .log-level { font-weight: bold; padding: 4px 8px; border-radius: 4px; font-size: 0.85em; width: 80px; text-align: center; margin-right: 15px; flex-shrink: 0;}
            .level-INFO { background-color: #e3f2fd; color: #0d47a1; }
            .level-WARNING { background-color: #fff3e0; color: #e65100; }
            .level-ERROR { background-color: #ffebee; color: #c62828; }
            .level-PROFILING { background-color: #f3e5f5; color: #6a1b9a; }
            .log-message { flex-grow: 1; word-break: break-word; }
            .empty { text-align: center; color: #777; padding: 40px 0; font-style: italic; }
        </style>
        <script>
            function clearLogs() {
                if(confirm('Вы уверены, что хотите полностью очистить логи устройства?')) {
                    fetch('/api/clear_logs', {method: 'POST'})
                    .then(response => fetchLogs());
                }
            }

            function fetchLogs() {
                fetch('/api/get_logs')
                    .then(response => response.json())
                    .then(data => {
                        const container = document.getElementById('log-container');
                        if (data.logs.length === 0) {
                            container.innerHTML = "<div class='empty'>Логи устройства пусты.</div>";
                            return;
                        }
                        let html = "";
                        data.logs.forEach(log => {
                            const levelClass = "level-" + log.level.toUpperCase();
                            html += `
                            <div class="log-item">
                                <div class="log-time">${log.timestamp}</div>
                                <div class="log-level ${levelClass}">${log.level}</div>
                                <div class="log-message">${log.message}</div>
                            </div>
                            `;
                        });
                        container.innerHTML = html;
                    })
                    .catch(err => console.error("Ошибка при получении логов:", err));
            }

            setInterval(fetchLogs, 3000);
            window.onload = fetchLogs;
        </script>
    </head>
    <body>
        <div class="container">
            <div class="nav-bar">
                <a href="/dashboard" class="nav-btn">Дашборд (Чаты)</a>
                <a href="/monitoring" class="nav-btn active">Мониторинг ESP32</a>
            </div>
            <div class="header-flex">
                <h1>Логи устройства (ESP32)</h1>
                <button class="btn-clear" onclick="clearLogs()">Очистить логи</button>
            </div>
            <div id="log-container">
                <div class='empty'>Загрузка...</div>
            </div>
        </div>
    </body>
    </html>
    """
    return HTMLResponse(content=html_content)
if __name__ == "__main__":
    uvicorn.run("server:app", host="0.0.0.0", port=8000)