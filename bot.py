"""Private Telegram chatbot using Sign in with ChatGPT token sharing.
Python 3.12+, python-telegram-bot 22.x. No API key required.
"""
import asyncio
import base64
import mimetypes
import json
import logging
import os
import sqlite3
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes, MessageHandler, filters

BOT_TOKEN = os.environ['TELEGRAM_BOT_TOKEN']
ALLOWED_ID = int(os.environ['ALLOWED_TELEGRAM_ID'])
CREDS = Path(os.environ.get('OPENAI_CREDENTIALS_FILE', '/opt/my-gpt-bot/private/credentials.json'))
MODEL = os.environ.get('OPENAI_MODEL', 'gpt-6.1-sol')
AVAILABLE_MODELS = {
    'gpt-6.1-sol': 'GPT-6.1 Sol',
    'gpt-6-astra': 'GPT-6 Astra',
    'gpt-6.1-terra': 'GPT-6.1 Terra',
    'gpt-6-sol': 'GPT-6 Sol',
    'gpt-6-luna': 'GPT-6 Luna',
    'gpt-5.6-sol': 'GPT-5.6 Sol',
    'gpt-5.6-terra': 'GPT-5.6 Terra',
    'gpt-5.6-luna': 'GPT-5.6 Luna',
}
if MODEL not in AVAILABLE_MODELS:
    MODEL = 'gpt-6.1-sol'
API_URL = 'https://api.openai.com/v1/responses'
TOKEN_URL = 'https://auth.openai.com/api/accounts/oauth/token'
RESOURCE = 'https://api.openai.com/v1'
log = logging.getLogger('my-gpt-bot')

# run_polling handlers are sequential by default; refresh protected against overlap as well.
refresh_lock = asyncio.Lock()
DB_PATH = Path(os.environ.get('CHAT_DB_FILE', '/opt/my-gpt-bot/private/chat.db'))
HISTORY_LIMIT = 12
MAX_FILE_BYTES = 19_000_000  # Leave headroom under Telegram cloud Bot API 20 MB download cap
SUPPORTED_DOCS = {".pdf": "application/pdf", ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document", ".txt": "text/plain"}
SUPPORTED_IMAGES = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp"}




def connect_db():
    con = sqlite3.connect(DB_PATH, timeout=10)
    con.execute('PRAGMA busy_timeout=10000')
    con.execute('PRAGMA foreign_keys=ON')
    return con


def init_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with connect_db() as con:
        con.execute('''CREATE TABLE IF NOT EXISTS users (
            telegram_id INTEGER PRIMARY KEY,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )''')
        con.execute('''CREATE TABLE IF NOT EXISTS conversations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            telegram_id INTEGER NOT NULL REFERENCES users(telegram_id),
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )''')
        con.execute('''CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            conversation_id INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
            role TEXT NOT NULL CHECK(role IN ('user','assistant')),
            content TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )''')
        # MYGPT_V27_ACCESS: access list, independent from conversation data.
        con.execute('''CREATE TABLE IF NOT EXISTS authorized_users (
            telegram_id INTEGER PRIMARY KEY,
            role TEXT NOT NULL CHECK(role IN ('admin', 'user')),
            added_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )''')
        con.execute('INSERT OR IGNORE INTO authorized_users(telegram_id,role) VALUES (?,?)', (390640171, 'admin'))
        con.execute('INSERT OR IGNORE INTO authorized_users(telegram_id,role) VALUES (?,?)', (948655400, 'user'))
        con.execute('CREATE INDEX IF NOT EXISTS ix_messages_conversation ON messages(conversation_id,id)')
        cols = {row[1] for row in con.execute('PRAGMA table_info(users)')}
        if 'selected_model' not in cols:
            con.execute("ALTER TABLE users ADD COLUMN selected_model TEXT NOT NULL DEFAULT 'gpt-6.1-sol'")
        if 'active_conversation_id' not in cols:
            con.execute('ALTER TABLE users ADD COLUMN active_conversation_id INTEGER')
        con.execute('''UPDATE users SET active_conversation_id=(SELECT MAX(id) FROM conversations WHERE telegram_id=users.telegram_id) WHERE active_conversation_id IS NULL''')
    os.chmod(DB_PATH, 0o600)


def active_conversation(con, telegram_id):
    con.execute('INSERT OR IGNORE INTO users(telegram_id) VALUES (?)', (telegram_id,))
    row = con.execute('''SELECT c.id FROM users u JOIN conversations c ON c.id=u.active_conversation_id AND c.telegram_id=u.telegram_id WHERE u.telegram_id=?''', (telegram_id,)).fetchone()
    if row:
        return row[0]
    row = con.execute('SELECT id FROM conversations WHERE telegram_id=? ORDER BY id DESC LIMIT 1', (telegram_id,)).fetchone()
    cid = row[0] if row else con.execute('INSERT INTO conversations(telegram_id) VALUES (?)', (telegram_id,)).lastrowid
    con.execute('UPDATE users SET active_conversation_id=? WHERE telegram_id=?', (cid, telegram_id))
    return cid


def list_conversations(telegram_id):
    with connect_db() as con:
        current = active_conversation(con, telegram_id)
        rows = con.execute('''SELECT c.id, c.created_at, COUNT(m.id),
            COALESCE((SELECT substr(content,1,55) FROM messages WHERE conversation_id=c.id AND role='user' ORDER BY id ASC LIMIT 1),'Пустой диалог')
            FROM conversations c LEFT JOIN messages m ON m.conversation_id=c.id
            WHERE c.telegram_id=? GROUP BY c.id ORDER BY c.id DESC LIMIT 20''', (telegram_id,)).fetchall()
    return current, rows


def pick_conversation(telegram_id, cid):
    with connect_db() as con:
        exists = con.execute('SELECT 1 FROM conversations WHERE id=? AND telegram_id=?', (cid, telegram_id)).fetchone()
        if not exists:
            return False
        con.execute('UPDATE users SET active_conversation_id=? WHERE telegram_id=?', (cid, telegram_id))
    return True


def delete_conversation(telegram_id, cid):
    with connect_db() as con:
        if not con.execute('SELECT 1 FROM conversations WHERE id=? AND telegram_id=?', (cid, telegram_id)).fetchone():
            return False
        con.execute('DELETE FROM conversations WHERE id=? AND telegram_id=?', (cid, telegram_id))
        current = con.execute('SELECT active_conversation_id FROM users WHERE telegram_id=?', (telegram_id,)).fetchone()
        if current and current[0] == cid:
            alternative = con.execute('SELECT id FROM conversations WHERE telegram_id=? ORDER BY id DESC LIMIT 1', (telegram_id,)).fetchone()
            if alternative:
                new_id = alternative[0]
            else:
                new_id = con.execute('INSERT INTO conversations(telegram_id) VALUES (?)', (telegram_id,)).lastrowid
            con.execute('UPDATE users SET active_conversation_id=? WHERE telegram_id=?', (new_id, telegram_id))
    return True


def history_markup(telegram_id):
    current, rows = list_conversations(telegram_id)
    buttons = []
    for cid, created, count, preview in rows[:10]:
        label = ('✅ ' if cid == current else '') + f'#{cid} · {preview[:28]} ({count})'
        buttons.append([InlineKeyboardButton(label, callback_data=f'history:open:{cid}')])
    buttons.append([InlineKeyboardButton('⚙️ Настройки', callback_data='menu:settings')])
    return InlineKeyboardMarkup(buttons)


def dialog_markup(cid):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton('✅ Продолжить диалог', callback_data=f'history:select:{cid}')],
        [InlineKeyboardButton('🗑 Удалить диалог', callback_data=f'history:askdelete:{cid}')],
        [InlineKeyboardButton('⬅️ К списку', callback_data='history:list')],
    ])


def user_model(telegram_id):
    with connect_db() as con:
        con.execute('INSERT OR IGNORE INTO users(telegram_id) VALUES (?)', (telegram_id,))
        row = con.execute('SELECT selected_model FROM users WHERE telegram_id=?', (telegram_id,)).fetchone()
    value = row[0] if row else MODEL
    return value if value in AVAILABLE_MODELS else MODEL


def set_user_model(telegram_id, model):
    if model not in AVAILABLE_MODELS:
        raise ValueError('Unknown model')
    with connect_db() as con:
        con.execute('INSERT OR IGNORE INTO users(telegram_id) VALUES (?)', (telegram_id,))
        con.execute('UPDATE users SET selected_model=? WHERE telegram_id=?', (model, telegram_id))


def settings_markup():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton('🧠 Выбрать модель', callback_data='menu:model')],
        [InlineKeyboardButton('🆕 Новый диалог', callback_data='menu:new')],
        [InlineKeyboardButton('📚 История', callback_data='menu:history'),
         InlineKeyboardButton('ℹ️ Статус', callback_data='menu:status')],
        [InlineKeyboardButton('🖼 Изображения (скоро)', callback_data='menu:images')],
        [InlineKeyboardButton('❔ Помощь', callback_data='menu:help')],
    ])


def history_stats(telegram_id):
    with connect_db() as con:
        dialogs = con.execute('SELECT COUNT(*) FROM conversations WHERE telegram_id=?', (telegram_id,)).fetchone()[0]
        messages = con.execute('SELECT COUNT(*) FROM messages m JOIN conversations c ON m.conversation_id=c.id WHERE c.telegram_id=?', (telegram_id,)).fetchone()[0]
    return dialogs, messages


def model_markup(current):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(('✅ ' if key == current else '') + label, callback_data='model:' + key)]
        for key, label in AVAILABLE_MODELS.items()
    ])


def load_history(telegram_id):
    with connect_db() as con:
        cid = active_conversation(con, telegram_id)
        rows = con.execute('SELECT role, content FROM (SELECT id, role, content FROM messages WHERE conversation_id=? ORDER BY id DESC LIMIT ?) ORDER BY id ASC', (cid, HISTORY_LIMIT)).fetchall()
    return cid, [{'role': role, 'content': text} for role, text in rows]


def save_exchange(cid, prompt, answer):
    with connect_db() as con:
        con.executemany('INSERT INTO messages(conversation_id,role,content) VALUES (?,?,?)', [(cid, 'user', prompt), (cid, 'assistant', answer)])


def start_new_conversation(telegram_id):
    with connect_db() as con:
        con.execute('INSERT OR IGNORE INTO users(telegram_id) VALUES (?)', (telegram_id,))
        cid = con.execute('INSERT INTO conversations(telegram_id) VALUES (?)', (telegram_id,)).lastrowid
        con.execute('UPDATE users SET active_conversation_id=? WHERE telegram_id=?', (cid, telegram_id))


def allowed(update: Update) -> bool:
    if not update.effective_user or not update.effective_chat or update.effective_chat.type != 'private':
        return False
    try:
        with connect_db() as con:
            return con.execute('SELECT 1 FROM authorized_users WHERE telegram_id=?',
                               (update.effective_user.id,)).fetchone() is not None
    except sqlite3.Error:
        log.exception('Access control DB error')
        return False



def load_creds():
    with CREDS.open('r', encoding='utf-8') as f:
        data = json.load(f)
    if not {'access_token', 'refresh_token', 'client_id', 'saved_at', 'expires_in'} <= data.keys():
        raise RuntimeError('Incomplete OAuth credentials')
    if 'chatgpt.tokens.use.direct' not in data.get('scopes', []):
        raise RuntimeError('Required subscription scope missing')
    return data


def atomic_save_creds(data):
    # Atomic replacement, owner-only permissions, same filesystem as original file.
    fd, name = tempfile.mkstemp(prefix='.creds-', dir=str(CREDS.parent))
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(name, CREDS)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def refresh_if_needed():
    d = load_creds()
    saved = datetime.fromisoformat(d['saved_at'].replace('Z', '+00:00')).timestamp()
    expiry = saved + int(d['expires_in'] or 3600)
    if time.time() < expiry - 300:
        return d['access_token']
    form = {'grant_type': 'refresh_token', 'client_id': d['client_id'],
            'refresh_token': d['refresh_token'], 'resource': RESOURCE}
    req = urllib.request.Request(TOKEN_URL,
          data=urllib.parse.urlencode(form).encode('utf-8'),
          headers={'Content-Type': 'application/x-www-form-urlencoded'}, method='POST')
    with urllib.request.urlopen(req, timeout=35) as r:
        t = json.load(r)
    if not t.get('access_token') or not t.get('refresh_token'):
        raise RuntimeError('Token refresh returned incomplete tokens')
    d.update(access_token=t['access_token'], refresh_token=t['refresh_token'],
             expires_in=t.get('expires_in', 3600),
             saved_at=datetime.now(timezone.utc).isoformat())
    if 'scope' in t:
        d['scopes'] = t['scope'].split() if isinstance(t['scope'], str) else t['scope']
    if 'id_token' in t:
        d['id_token'] = t['id_token']
    atomic_save_creds(d)
    return d['access_token']


def request_gpt(access_token, messages, model):
    payload = {'model': model, 'input': messages, 'store': False, 'stream': True}
    request = urllib.request.Request(API_URL,
        data=json.dumps(payload, ensure_ascii=False).encode('utf-8'),
        headers={'Authorization': 'Bearer ' + access_token,
                 'Content-Type': 'application/json', 'Accept': 'text/event-stream'},
        method='POST')
    deltas = []
    completed = None
    failed = None
    events_seen = set()
    with urllib.request.urlopen(request, timeout=150) as response:
        for raw in response:
            line = raw.decode('utf-8', 'replace').strip()
            if not line.startswith('data: '):
                continue
            try:
                event = json.loads(line[6:])
            except (ValueError, TypeError):
                continue
            kind = event.get('type', '')
            events_seen.add(kind)
            if kind == 'response.output_text.delta':
                deltas.append(event.get('delta', ''))
            elif kind == 'response.completed':
                completed = event.get('response', {})
                break
            elif kind in ('response.failed', 'response.incomplete', 'error'):
                failed = event.get('error') or event.get('response', {}).get('error') or kind
                break
    if failed or completed is None:
        raise RuntimeError('Model response incomplete or failed; events=' + ','.join(sorted(events_seen)))
    texts = []
    for item in completed.get('output', []):
        for content in item.get('content', []):
            if content.get('type') == 'output_text':
                texts.append(content.get('text', ''))
    # Prefer final assembled text; use streamed deltas when final output is absent.
    result = ''.join(texts).strip() or ''.join(deltas).strip()
    if not result:
        output_types = [item.get('type', '?') for item in completed.get('output', [])]
        raise RuntimeError('Model returned no text; status=%s; output_types=%s; events=%s' % (
            completed.get('status'), output_types, ','.join(sorted(events_seen))))
    return result


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if allowed(update) and update.message:
        await update.message.reply_text('Привет! Я Мой GPT. Напиши вопрос или открой меню /settings.', reply_markup=settings_markup())


async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if allowed(update) and update.message:
        start_new_conversation(update.effective_user.id)
        await update.message.reply_text('Начат новый диалог. Предыдущие сообщения сохранены в базе, но больше не входят в контекст.')


async def new_dialog(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await reset(update, context)


async def settings_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if allowed(update) and update.message:
        await update.message.reply_text(
            '⚙️ Настройки «Мой GPT»\nВыбери действие:',
            reply_markup=settings_markup(),
        )


async def menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query:
        return
    if not allowed(update):
        await query.answer('Нет доступа', show_alert=True)
        return
    action = (query.data or '').removeprefix('menu:')
    uid = update.effective_user.id
    if action == 'settings':
        text = '⚙️ Настройки «Мой GPT»\nВыбери действие:'
        markup = settings_markup()
    elif action == 'model':
        current = user_model(uid)
        text = '🧠 Выбери модель. Сейчас: ' + AVAILABLE_MODELS[current]
        markup = model_markup(current)
    elif action == 'new':
        start_new_conversation(uid)
        text = '🆕 Новый диалог начат. Предыдущая история сохранена.'
        markup = settings_markup()
    elif action == 'history':
        dialogs, messages = history_stats(uid)
        text = f'📚 Диалоги: {dialogs} · Сообщения: {messages}\nВыбери диалог ниже (показаны последние 10 из максимум 20):'
        markup = history_markup(uid)
    elif action == 'status':
        current = user_model(uid)
        text = 'ℹ️ Бот работает.\nМодель: ' + AVAILABLE_MODELS[current] + '\nИстория сохраняется в SQLite.\nДоступ пока только для владельца.'
        markup = settings_markup()
    elif action == 'images':
        text = '🖼 Генерацию изображений пока не подключали. Проверим доступный и разрешённый способ отдельно.'
        markup = settings_markup()
    elif action == 'help':
        text = '❔ Команды: /settings, /model, /new, /reset, /start, /history. Пришли фото или PDF/DOCX/TXT с подписью-вопросом — проанализирую.'
        markup = settings_markup()
    else:
        await query.answer('Неизвестная команда', show_alert=True)
        return
    await query.answer()
    await query.edit_message_text(text, reply_markup=markup)


async def history_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if allowed(update) and update.message:
        await update.message.reply_text('📚 Сохранённые диалоги:', reply_markup=history_markup(update.effective_user.id))


async def history_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query:
        return
    if not allowed(update):
        await query.answer('Нет доступа', show_alert=True)
        return
    parts = (query.data or '').split(':')
    action = parts[1] if len(parts) > 1 else ''
    uid = update.effective_user.id
    if action == 'list':
        await query.answer()
        await query.edit_message_text('📚 Сохранённые диалоги:', reply_markup=history_markup(uid))
        return
    try:
        cid = int(parts[2])
    except (IndexError, TypeError, ValueError):
        await query.answer('Неверная команда', show_alert=True)
        return
    with connect_db() as con:
        record = con.execute('SELECT created_at FROM conversations WHERE id=? AND telegram_id=?', (cid, uid)).fetchone()
    if not record:
        await query.answer('Диалог не найден', show_alert=True)
        return
    if action == 'open':
        await query.answer()
        await query.edit_message_text(f'Диалог #{cid} от {record[0]}\nВыбери действие:', reply_markup=dialog_markup(cid))
    elif action == 'select':
        if pick_conversation(uid, cid):
            await query.answer('Диалог выбран')
            await query.edit_message_text(f'✅ Диалог #{cid} выбран. Теперь сообщения продолжат его историю.', reply_markup=history_markup(uid))
    elif action == 'askdelete':
        await query.answer()
        markup = InlineKeyboardMarkup([
            [InlineKeyboardButton('❗ Да, удалить навсегда', callback_data=f'history:delete:{cid}')],
            [InlineKeyboardButton('Отмена', callback_data=f'history:open:{cid}')],
        ])
        await query.edit_message_text(f'Удалить диалог #{cid} и все его сообщения? Восстановить их через бот нельзя.', reply_markup=markup)
    elif action == 'delete':
        deleted = delete_conversation(uid, cid)
        await query.answer('Удалено' if deleted else 'Диалог не найден')
        await query.edit_message_text('🗑 Диалог удалён.' if deleted else 'Диалог не найден.', reply_markup=history_markup(uid))
    else:
        await query.answer('Неизвестная команда', show_alert=True)


async def model_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed(update) or not update.message:
        return
    current = user_model(update.effective_user.id)
    await update.message.reply_text(
        'Выбери модель GPT. Сейчас: ' + AVAILABLE_MODELS[current],
        reply_markup=model_markup(current),
    )


async def model_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query:
        return
    if not allowed(update):
        await query.answer('Нет доступа', show_alert=True)
        return
    model = (query.data or '').removeprefix('model:')
    if model not in AVAILABLE_MODELS:
        await query.answer('Неизвестная модель', show_alert=True)
        return
    set_user_model(update.effective_user.id, model)
    await query.answer('Модель выбрана')
    await query.edit_message_text(
        'Текущая модель: ' + AVAILABLE_MODELS[model],
        reply_markup=model_markup(model),
    )


async def analyze_attachment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed(update) or not update.message:
        return
    msg = update.message
    is_photo = bool(msg.photo)
    doc = msg.document
    if not is_photo and not doc:
        return
    caption = (msg.caption or '').strip()[:4000]
    if is_photo:
        media = msg.photo[-1]
        mime = 'image/jpeg'  # Telegram photo objects are delivered as JPEG
        filename = 'photo.jpg'
        default_question = 'Опиши изображение и выдели важные детали. Если это скриншот ошибки, объясни её.'
    else:
        filename = (doc.file_name or 'document').split('/')[-1].split('\\')[-1]
        ext = Path(filename).suffix.lower()
        if ext in SUPPORTED_DOCS:
            mime = SUPPORTED_DOCS[ext]
        elif ext in SUPPORTED_IMAGES:
            mime = SUPPORTED_IMAGES[ext]
        else:
            await msg.reply_text('Поддерживаются PDF, DOCX, TXT, JPG, PNG и WebP. Для других форматов пока используй PDF или TXT.')
            return
        media = doc
        default_question = 'Проанализируй файл и кратко опиши основные сведения, затем перечисли важные замечания.'
    if media.file_size is not None and media.file_size > MAX_FILE_BYTES:
        await msg.reply_text('Файл больше 19 МБ. Обычный Telegram Bot API позволяет скачивать вложения не более 20 МБ. Для больших PDF позже понадобится отдельный Bot API Server.')
        return
    await msg.reply_text('🔎 Анализирую вложение…')
    try:
        remote_file = await context.bot.get_file(media.file_id)
        # Download to disk first: avoids holding both the Telegram download buffer
        # and a second full copy in memory on this small VPS.
        with tempfile.TemporaryDirectory(prefix='mygpt-attachment-') as temp_dir:
            path = Path(temp_dir) / 'upload.bin'
            await remote_file.download_to_drive(custom_path=path)
            actual_size = path.stat().st_size
            if actual_size > MAX_FILE_BYTES:
                await msg.reply_text('Файл превышает лимит 19 МБ.')
                return
            # The API accepts inline Base64 input; still requires RAM for encoding.
            encoded = base64.b64encode(path.read_bytes()).decode('ascii')
        question = caption or default_question
        part = {'type': 'input_image', 'image_url': 'data:' + mime + ';base64,' + encoded} if mime.startswith('image/') else {'type': 'input_file', 'filename': filename, 'file_data': 'data:' + mime + ';base64,' + encoded}
        cid, history = load_history(update.effective_user.id)
        model = user_model(update.effective_user.id)
        messages = history + [{'role': 'user', 'content': [{'type': 'input_text', 'text': question}, part]}]
        async with refresh_lock:
            token = await asyncio.to_thread(refresh_if_needed)
            answer = await asyncio.to_thread(request_gpt, token, messages, model)
    except urllib.error.HTTPError as ex:
        log.warning('Attachment analysis HTTP error: %s', ex.code)
        await msg.reply_text('Не удалось проанализировать вложение (HTTP %s). Возможно, выбранная модель не поддерживает этот формат.' % ex.code)
        return
    except Exception as ex:
        log.warning('Attachment analysis failed: %s: %s', type(ex).__name__, str(ex))
        await msg.reply_text('Не удалось обработать вложение. Проверь журнал службы.')
        return
    # Persist a textual placeholder only: do not retain original file or base64 in SQLite.
    save_exchange(cid, '[Вложение: ' + filename[:80] + '] ' + question, answer)
    for pos in range(0, len(answer), 3900):
        await msg.reply_text(answer[pos:pos + 3900])


async def message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed(update) or not update.message or not update.message.text:
        return
    prompt = update.message.text[:8000]
    try:
        cid, history = load_history(update.effective_user.id)
        model = user_model(update.effective_user.id)
    except Exception:
        log.exception('SQLite read error')
        await update.message.reply_text('Ошибка чтения истории. Проверь журнал службы.')
        return
    messages = history + [{'role': 'user', 'content': prompt}]
    try:
        async with refresh_lock:
            token = await asyncio.to_thread(refresh_if_needed)
            answer = await asyncio.to_thread(request_gpt, token, messages, model)
    except urllib.error.HTTPError as ex:
        log.warning('OpenAI HTTP error: %s', ex.code)
        answer = 'Не удалось получить ответ GPT (HTTP %s). Проверь журналы службы.' % ex.code
    except Exception as ex:
        log.warning('GPT request failed: %s: %s', type(ex).__name__, str(ex))
        answer = 'Временная ошибка подключения к GPT. Попробуй позже.'
    else:
        try:
            save_exchange(cid, prompt, answer)
        except Exception:
            log.exception('SQLite write error')
    for pos in range(0, len(answer), 3900):
        await update.message.reply_text(answer[pos:pos + 3900])



# MYGPT_V27_ADMIN_COMMANDS
async def access_users_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed(update) or not update.message:
        return
    if update.effective_user.id != 390640171:
        await update.message.reply_text('Управление пользователями доступно только администратору.')
        return
    with connect_db() as con:
        records = con.execute('SELECT telegram_id, role FROM authorized_users ORDER BY role, telegram_id').fetchall()
    await update.message.reply_text('Разрешённые аккаунты:\n' + '\n'.join(
        f'{uid} — {"администратор" if role == "admin" else "пользователь"}' for uid, role in records))


async def access_add_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed(update) or not update.message:
        return
    if update.effective_user.id != 390640171:
        await update.message.reply_text('Команда доступна только администратору.')
        return
    if len(context.args) != 1 or not context.args[0].isdigit() or int(context.args[0]) < 1:
        await update.message.reply_text('Формат: /adduser 123456789. Добавляй только свои аккаунты.')
        return
    uid = int(context.args[0])
    with connect_db() as con:
        exists = con.execute('SELECT 1 FROM authorized_users WHERE telegram_id=?', (uid,)).fetchone()
        if not exists:
            con.execute('INSERT INTO authorized_users(telegram_id,role) VALUES (?,?)', (uid, 'user'))
    await update.message.reply_text('Аккаунт уже разрешён.' if exists else f'Аккаунт {uid} добавлен.')


async def access_remove_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed(update) or not update.message:
        return
    if update.effective_user.id != 390640171:
        await update.message.reply_text('Команда доступна только администратору.')
        return
    if len(context.args) != 1 or not context.args[0].isdigit():
        await update.message.reply_text('Формат: /removeuser 123456789')
        return
    uid = int(context.args[0])
    if uid == 390640171:
        await update.message.reply_text('Основного администратора удалить нельзя.')
        return
    with connect_db() as con:
        cur = con.execute('DELETE FROM authorized_users WHERE telegram_id=? AND role<>?', (uid, 'admin'))
    await update.message.reply_text(f'Доступ аккаунта {uid} отключён.' if cur.rowcount else 'Аккаунт не найден.')


async def access_status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed(update) or not update.message:
        return
    with connect_db() as con:
        count = con.execute('SELECT COUNT(*) FROM authorized_users').fetchone()[0]
        own = con.execute('SELECT role FROM authorized_users WHERE telegram_id=?',
                          (update.effective_user.id,)).fetchone()
    await update.message.reply_text(
        f'Мой GPT 2.7\nСтатус: работает\nВаша роль: {own[0] if own else "нет"}\nРазрешённых аккаунтов: {count}')


def main():
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    logging.getLogger('httpx').setLevel(logging.WARNING)
    logging.getLogger('telegram').setLevel(logging.WARNING)
    load_creds()  # refuse to start without permission
    init_db()
    app = Application.builder().token(BOT_TOKEN).concurrent_updates(False).build()
    app.add_handler(CommandHandler('start', start))
    app.add_handler(CommandHandler('users', access_users_command))
    app.add_handler(CommandHandler('adduser', access_add_command))
    app.add_handler(CommandHandler('removeuser', access_remove_command))
    app.add_handler(CommandHandler('status', access_status_command))
    app.add_handler(CommandHandler('reset', reset))
    app.add_handler(CommandHandler('new', new_dialog))
    app.add_handler(CommandHandler('model', model_command))
    app.add_handler(CommandHandler('settings', settings_command))
    app.add_handler(CommandHandler('history', history_command))
    app.add_handler(CallbackQueryHandler(history_callback, pattern=r'^history:'))
    app.add_handler(CallbackQueryHandler(menu_callback, pattern=r'^menu:'))
    app.add_handler(CallbackQueryHandler(model_callback, pattern=r'^model:'))
    app.add_handler(MessageHandler(filters.PHOTO | filters.Document.ALL, analyze_attachment))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message))
    log.info('My GPT started')
    app.run_polling()


if __name__ == '__main__':
    main()
