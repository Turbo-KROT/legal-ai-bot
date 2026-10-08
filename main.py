import asyncio
import logging
import os
import re
import sqlite3
import fitz
from PIL import Image
import pytesseract
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, 
    InlineKeyboardButton, FSInputFile, ErrorEvent
)
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from gigachat import GigaChat
import speech_recognition as sr
from fpdf import FPDF

from config import BOT_TOKEN, GIGACHAT_CREDENTIALS

logging.basicConfig(level=logging.INFO)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

giga = GigaChat(
    credentials=GIGACHAT_CREDENTIALS,
    scope="GIGACHAT_API_PERS",
    model="GigaChat-2-Pro",
    verify_ssl_certs=False
)

# ============ БАЗА ДАННЫХ SQLITE ============
def init_db():
    conn = sqlite3.connect("users.db")
    cursor = conn.cursor()
    cursor.execute("CREATE TABLE IF NOT EXISTS users (user_id INTEGER PRIMARY KEY, accepted INTEGER DEFAULT 0, city TEXT DEFAULT NULL)")
    conn.commit()
    conn.close()
init_db()

def get_db_user(user_id: int):
    conn = sqlite3.connect("users.db")
    cursor = conn.cursor()
    cursor.execute("SELECT accepted, city FROM users WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()
    conn.close()
    if not row:
        conn = sqlite3.connect("users.db")
        conn.execute("INSERT INTO users (user_id, accepted, city) VALUES (?, 0, NULL)", (user_id,))
        conn.commit(); conn.close()
        return {"accepted": False, "city": None}
    return {"accepted": bool(row[0]), "city": row[1]}

def update_db(user_id: int, field: str, value):
    conn = sqlite3.connect("users.db")
    conn.execute(f"UPDATE users SET {field} = ? WHERE user_id = ?", (value, user_id))
    conn.commit(); conn.close()

user_history = {}

@dp.errors()
async def global_error_handler(event: ErrorEvent):
    logging.critical(f"Ошибка подавлена: {event.exception}")

class BotStates(StatesGroup):
    waiting_for_city, main_menu, qa_mode, doc_gen_mode, doc_analyze_mode, prof_consult_mode = State(), State(), State(), State(), State(), State()

# ============ СИСТЕМНЫЕ ПРОМПТЫ ============
PROMPT_QA = """Ты — высококвалифицированный юридический AI-консультант по ВСЕМ отраслям права РФ (Гражданское, Уголовное, Административное, Трудовое, Семейное, Налоговое, Земельное, а также ГПК, УПК, АПК, КАС, Конституция, Указы Президента и Постановления).
Твоя цель — дать глубокую, понятную и практичную консультацию, опираясь на актуальное законодательство. Укажи четкий алгоритм действий.

ВНИМАНИЕ! Если пользователь просит СОСТАВИТЬ документ или бланк, ответь ОДНИМ СЛОВОМ: REDIRECT_DOC
Если просит проверить/проанализировать присланный документ, ответь ОДНИМ СЛОВОМ: REDIRECT_ANALYZE

Отвечай СТРОГО по следующей структуре (используй эмодзи):
📌 КРАТКИЙ ВЕРДИКТ: (Четкая суть ответа и главная правовая позиция)
📖 ПРАВОВОЕ ОБОСНОВАНИЕ: (Подробный разбор статей кодексов РФ, законов и прав гражданина)
💡 ПЛАН ДЕЙСТВИЙ: (Конкретные, пошаговые инструкции, что делать дальше)
⚠️ РИСКИ И СРОКИ: (Сроки давности, возможные подводные камни)"""

PROMPT_DOC_GEN = """Ты — высококвалифицированный юрист-делопроизводитель РФ. Твоя задача — создавать юридические документы.

ПРАВИЛА И ТЕГИ (ОБЯЗАТЕЛЬНО):
1. Если просят составить документ, образец, бланк или прислали данные для заполнения: 
   - Начни ответ строго с тега [ФАЙЛ].
   - Со следующей строки пиши ПОЛНЫЙ ТЕКСТ документа.
   - Места для заполнения оставляй прочерками: _______________________
2. Если данных не хватает и пользователь хочет, чтобы ты заполнил документ, либо просит тебя помочь с составлением:
   - Начни ответ строго с тега [ЧАТ].
   - Напиши текстом, какие именно данные (ФИО, паспорта, адреса) нужны для заполнения.
3. НИКОГДА не пиши, что ты текстовая модель или не можешь сделать PDF."""

PROMPT_ANALYZE = """Ты — объективный и адекватный юрист-аудитор РФ. Внимательно изучи присланный текст документа и дай ОБЪЕКТИВНУЮ оценку:
1. Если в документе есть ОПАСНЫЕ РИСКИ — НАЧНИ СРАЗУ С НИХ.
2. Если документ ХОРОШИЙ — подмети его грамотные стороны, укажи на мелкие нюансы и успокой клиента.
3. На уточняющие вопросы отвечай человеческим языком, сохраняя контекст документа."""

# ============ ВЕРСТКА PDF ПО ГОСТУ (БЕЗ ОШИБОК) ============
def sanitize_text_for_pdf(text: str) -> str:
    text = text.replace('**', '').replace('*', '').replace('###', '').replace('##', '').replace('#', '')
    text = text.replace('[ФАЙЛ]', '').replace('[ЧАТ]', '').strip()
    emoji_pattern = re.compile("[" "\U00010000-\U0010FFFF" "\u2600-\u27BF\u2300-\u23FF\u2B00-\u2BFF\u2190-\u21FF" "]+", flags=re.UNICODE)
    text = emoji_pattern.sub("", text)
    replacements = {'—': '-', '–': '-', '…': '...', '«': '"', '»': '"', '“': '"', '”': '"', '‘': "'", '’': "'", '\xa0': ' ', '\t': '    '}
    for orig, repl in replacements.items(): text = text.replace(orig, repl)
    return text.strip()

def generate_pdf_gost(text: str, filename: str) -> bool:
    try:
        clean_text = sanitize_text_for_pdf(text)
        if len(clean_text) < 50: return False
        
        pdf = FPDF()
        pdf.add_page()
        pdf.set_margins(20, 20, 10)
        pdf.set_auto_page_break(auto=True, margin=20)
        
        # Используем FreeSerif (Аналог Times New Roman)
        font_path = "/usr/share/fonts/truetype/freefont/FreeSerif.ttf"
        if os.path.exists(font_path):
            pdf.add_font('FreeSerif', '', font_path)
            pdf.set_font('FreeSerif', '', 12)
        else:
            pdf.set_font("Helvetica", size=12)
            
        lines = clean_text.split('\n')
        title_done = False
        
        for line in lines:
            c_line = line.strip()
            if not c_line:
                pdf.ln(5)
                continue
                
            if not title_done and len(c_line) < 120:
                if os.path.exists(font_path): pdf.set_font('FreeSerif', '', 14)
                pdf.multi_cell(0, 8, c_line.upper(), align='C')
                if os.path.exists(font_path): pdf.set_font('FreeSerif', '', 12)
                pdf.ln(5)
                title_done = True
            else:
                pdf.multi_cell(0, 6, "      " + c_line, align='J')
                
        pdf.output(filename)
        return True
    except Exception as e:
        logging.error(f"PDF Build Error: {e}", exc_info=True)
        return False

# ============ МЕДИА ============
def recognize_audio(wav_path):
    r = sr.Recognizer()
    with sr.AudioFile(wav_path) as source: return r.recognize_google(r.record(source), language="ru-RU")

async def process_media(message: Message):
    if message.text: return message.text
    if message.voice:
        f_id = message.voice.file_id
        ogg, wav = f"v_{f_id}.ogg", f"v_{f_id}.wav"
        await bot.download_file((await bot.get_file(f_id)).file_path, ogg)
        await (await asyncio.create_subprocess_exec("ffmpeg", "-y", "-i", ogg, "-ar", "16000", "-ac", "1", wav, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)).communicate()
        res = await asyncio.to_thread(recognize_audio, wav)
        for p in [ogg, wav]: 
            if os.path.exists(p): os.remove(p)
        return res
    if message.photo:
        p = f"p_{message.photo[-1].file_id}.jpg"
        await bot.download_file((await bot.get_file(message.photo[-1].file_id)).file_path, p)
        try: res = await asyncio.to_thread(pytesseract.image_to_string, Image.open(p).convert('L'), lang='rus+eng')
        except: res = ""
        os.remove(p); return res.strip()
    if message.document:
        d = f"d_{message.document.file_id}.pdf"
        await bot.download_file((await bot.get_file(message.document.file_id)).file_path, d)
        try: res = "".join([page.get_text() for page in fitz.open(d)])
        except: res = ""
        os.remove(d); return res.strip()
    return ""

# ============ КЛАВИАТУРЫ ============
def agree_kb(): return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✅ Соглашаюсь", callback_data="accept"), InlineKeyboardButton(text="❌ Не соглашаюсь", callback_data="decline")]])
def main_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⚖️ Задать юридический вопрос", callback_data="mode_qa")],
        [InlineKeyboardButton(text="📝 Создание документа", callback_data="mode_doc_gen")],
        [InlineKeyboardButton(text="🔍 Разбор документа", callback_data="mode_doc_analyze")],
        [InlineKeyboardButton(text="💼 Профильная консультация", callback_data="mode_prof")],
        [InlineKeyboardButton(text="📍 Сменить город", callback_data="change_city")]
    ])
def get_back_kb(): return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 В главное меню", callback_data="back_to_menu")]])
def redirect_kb(m_type): return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="➡️ Перейти в нужный раздел", callback_data=m_type)]])
def doc_ready_kb(is_filled=False):
    btn_text = "✏️ Внести исправления / Скорректировать" if is_filled else "📝 Заполнить этот образец"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=btn_text, callback_data="fill_template")],
        [InlineKeyboardButton(text="🔙 В главное меню", callback_data="back_to_menu")]
    ])

# ============ ИНТЕРФЕЙС ============
async def send_agreement(message_obj):
    welcome = ("⚖️ <b>Правовой AI-Консультант</b>\n\nПрофессиональный сервис экспресс-анализа юридических ситуаций, "
               "проверки документов и подготовки правовых решений на базе искусственного интеллекта.\n\n"
               "🎙 <i>Принимаю текстовые и голосовые сообщения!</i>\n⚡️ <i>Анализ ситуаций за секунды • Работа 24/7</i>")
    await message_obj.answer(welcome, parse_mode="HTML")
    await asyncio.sleep(2.5)
    try:
        await message_obj.answer_document(FSInputFile("agreement.pdf"), caption="📄 Пользовательское соглашение")
        await message_obj.answer_document(FSInputFile("faq.pdf"), caption="❓ Часто задаваемые вопросы (FAQ)")
    except: pass
    await message_obj.answer("📋 <b>Условия использования сервиса</b>\n\nНажимая «Соглашаюсь», вы принимаете условия.", reply_markup=agree_kb(), parse_mode="HTML")

@dp.message(CommandStart())
async def cmd_start(m: Message, state: FSMContext):
    u = get_db_user(m.from_user.id)
    if u["accepted"]:
        await m.answer("👋 <b>С возвращением!</b>", parse_mode="HTML")
        user_history[m.from_user.id] = {'h': [], 'rc': 0, 'doc_text': ""}
        await state.set_state(BotStates.main_menu)
        return await m.answer("📋 <b>ГЛАВНОЕ МЕНЮ</b>\n\nВыберите нужный раздел (доступен текст и голос 🎙):", reply_markup=main_kb(), parse_mode="HTML")
    await send_agreement(m)

@dp.callback_query(F.data == "decline")
async def process_decline(c: CallbackQuery):
    await c.message.edit_text("😔 Без принятия условий доступ к сервису ограничен. Вы можете передумать в любой момент:", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="📄 Вернуться к соглашению", callback_data="return_agree")]]), parse_mode="HTML")

@dp.callback_query(F.data == "return_agree")
async def ret_agree(c: CallbackQuery):
    await c.message.delete(); await send_agreement(c.message)

@dp.callback_query(F.data == "accept")
async def accept(c: CallbackQuery, state: FSMContext):
    set_db_accept(c.from_user.id)
    await c.message.edit_text("✅ <b>Условия использования приняты.</b>", parse_mode="HTML")
    msg = await c.message.answer("🏙 <b>Укажите ваш город</b> (для учета региональных законов):", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🚫 Не указывать (Пропустить)", callback_data="skip_city")]]), parse_mode="HTML")
    await state.set_state(BotStates.waiting_for_city); await state.update_data(prompt_msg_id=msg.message_id)

@dp.callback_query(F.data == "skip_city")
async def skip_city(c: CallbackQuery, state: FSMContext):
    set_db_city(c.from_user.id, "Не указан")
    await c.message.delete()
    user_history[c.from_user.id] = {'h': [], 'rc': 0, 'doc_text': ""}
    await state.set_state(BotStates.main_menu)
    await c.message.answer("📍 Город не указан.\n\n📋 <b>ГЛАВНОЕ МЕНЮ</b>\n\nВыберите нужный раздел (доступен текст и голос 🎙):", reply_markup=main_kb(), parse_mode="HTML")

@dp.message(BotStates.waiting_for_city)
async def city_in(m: Message, state: FSMContext):
    set_db_city(m.from_user.id, m.text.strip())
    data = await state.get_data()
    if 'prompt_msg_id' in data:
        try: await bot.delete_message(m.chat.id, data['prompt_msg_id'])
        except: pass
    try: await m.delete()
    except: pass
    user_history[m.from_user.id] = {'h': [], 'rc': 0, 'doc_text': ""}
    await state.set_state(BotStates.main_menu)
    await m.answer(f"📍 Ваш город: <b>{m.text.strip()}</b>\n\n📋 <b>ГЛАВНОЕ МЕНЮ</b>\n\nВыберите нужный раздел (доступен текст и голос 🎙):", reply_markup=main_kb(), parse_mode="HTML")

@dp.callback_query(F.data == "change_city")
async def ch_city(c: CallbackQuery, state: FSMContext):
    msg = await c.message.edit_text("🏙 Введите название нового города:"); await state.set_state(BotStates.waiting_for_city); await state.update_data(prompt_msg_id=msg.message_id)

@dp.callback_query(F.data == "back_to_menu")
async def back_menu(c: CallbackQuery, state: FSMContext):
    user_history[c.from_user.id] = {'h': [], 'rc': 0, 'doc_text': ""}
    await state.set_state(BotStates.main_menu)
    
    msg_text = c.message.text or c.message.caption or ""
    if "РЕЖИМ:" in msg_text:
        await c.message.edit_text("📋 <b>ГЛАВНОЕ МЕНЮ</b>\n\nВыберите нужный раздел (доступен текст и голос 🎙):", reply_markup=main_kb(), parse_mode="HTML")
    else:
        try: await c.message.edit_reply_markup(reply_markup=None)
        except: pass
        await c.message.answer("📋 <b>ГЛАВНОЕ МЕНЮ</b>\n\nВыберите нужный раздел (доступен текст и голос 🎙):", reply_markup=main_kb(), parse_mode="HTML")

@dp.callback_query(F.data.startswith("mode_"))
async def modes(c: CallbackQuery, state: FSMContext):
    if c.data == "mode_prof":
        return await c.message.edit_text("💼 <b>РЕЖИМ: Профильная консультация</b>\n\n<i>_ [⚠️в разработке🛠️]</i>", reply_markup=get_back_kb(), parse_mode="HTML")
    
    modes_map = {"mode_qa": BotStates.qa_mode, "mode_doc_gen": BotStates.doc_gen_mode, "mode_doc_analyze": BotStates.doc_analyze_mode}
    txt_map = {
        "mode_qa": "⚖️ <b>РЕЖИМ: Юридический вопрос</b>\n\nОпишите вашу ситуацию текстом или голосом 🎙.",
        "mode_doc_gen": "📝 <b>РЕЖИМ: Создание документа</b>\n\nПришлите <b>фото или PDF</b> образца, либо напишите название документа. Я могу задать вопросы для заполнения или прислать готовый бланк.",
        "mode_doc_analyze": "🔍 <b>РЕЖИМ: Разбор документа</b>\n\nПришлите <b>фото, PDF</b> или текст документа, и я найду в нем риски и ошибки."
    }
    await state.set_state(modes_map[c.data])
    await c.message.edit_text(txt_map[c.data], reply_markup=get_back_kb(), parse_mode="HTML")

@dp.callback_query(F.data == "fill_template")
async def fill_template_cb(c: CallbackQuery, state: FSMContext):
    uid = c.from_user.id
    status = await c.message.answer("💬 <i>Готовлю список необходимых данных...</i>", parse_mode="HTML")
    if uid not in user_history: user_history[uid] = {'h': [], 'rc': 0, 'doc_text': ""}
    
    user_history[uid]['h'].append({"role": "user", "content": "Напиши мне четким списком, какие именно данные (ФИО, паспорта, даты, суммы) от меня нужны для заполнения документа."})
    try:
        res = await asyncio.to_thread(giga.chat, {"messages": [{"role": "system", "content": PROMPT_DOC_GEN}] + user_history[uid]['h']})
        ans = clean_text_chat(res.choices[0].message.content)
        user_history[uid]['h'].append({"role": "assistant", "content": ans})
        
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🚫 Не заполнять отдельные поля", callback_data="skip_some_fields")],
            [InlineKeyboardButton(text="🔙 В главное меню", callback_data="back_to_menu")]
        ])
        await status.edit_text(ans, reply_markup=kb, parse_mode="HTML")
    except:
        await status.edit_text("⚠️ Напишите необходимые данные для заполнения в чат.", reply_markup=get_back_kb())

@dp.callback_query(F.data == "skip_some_fields")
async def skip_some_fields_cb(c: CallbackQuery):
    await c.message.answer("Понял! Напишите только те данные, которые хотите указать. В остальных местах останутся прочерки _________________.", reply_markup=get_back_kb())

# ============ ЦЕНТРАЛЬНАЯ ЛОГИКА ============
@dp.message(F.text | F.voice | F.photo | F.document)
async def handle(m: Message, state: FSMContext):
    user_id = m.from_user.id
    user = get_db_user(user_id)
    curr = await state.get_state()
    
    if not user["accepted"] or curr == BotStates.main_menu.state: 
        return await m.answer("Пожалуйста, выберите раздел в меню 👇", reply_markup=main_kb())

    status = await m.answer("🔍 <i>Обрабатываю данные...</i>", parse_mode="HTML")
    
    try:
        caption = m.caption.strip() if m.caption else ""
        input_text = await process_media(m)
        
        if m.voice and input_text: await m.answer(f"🎙 <b>Вы сказали:</b>\n«<i>{input_text}</i>»", parse_mode="HTML")
        if m.photo and input_text: input_text = f"Текст с фото документа:\n{input_text}"
        if m.document and input_text: input_text = f"Текст из PDF-документа:\n{input_text}"
        if caption: input_text += f"\n\nУказание пользователя: {caption}"
        
        if not input_text:
            return await status.edit_text("⚠️ Не удалось разобрать данные. Попробуйте повторить передачу.", reply_markup=get_back_kb())

        if user_id not in user_history: user_history[user_id] = {'h': [], 'rc': 0, 'doc_text': ""}

        # 1. ВОПРОСЫ
        if curr == BotStates.qa_mode.state:
            user_history[user_id]['h'].append({"role": "user", "content": input_text})
            if len(user_history[user_id]['h']) > 6: user_history[user_id]['h'] = user_history[user_id]['h'][-6:]
            
            res = await asyncio.to_thread(giga.chat, {"messages": [{"role": "system", "content": PROMPT_QA + f"\nГород пользователя: {user['city']}"}] + user_history[user_id]['h']})
            ans = clean_text_chat(res.choices[0].message.content)
            
            if "REDIRECT_DOC" in ans or "REDIRECT_ANALYZE" in ans:
                user_history[user_id]['rc'] += 1
                target = "mode_doc_gen" if "DOC" in ans else "mode_doc_analyze"
                if user_history[user_id]['rc'] > 2: return await status.edit_text("Вы задаете вопрос не по профилю раздела. Нажмите «В главное меню».", reply_markup=get_back_kb())
                return await status.edit_text("Данную функцию можно сделать, выбрав другой раздел. Нажмите кнопку ниже:", reply_markup=redirect_kb(target))
                
            user_history[user_id]['h'].append({"role": "assistant", "content": ans})
            is_first = len(user_history[user_id]['h']) <= 2
            footer = "\n\n<i>ℹ️ Информация носит справочный характер. Связь с юристом: /lawyer</i>" if is_first else ""
            await status.edit_text(ans + footer, reply_markup=get_back_kb(), parse_mode="HTML")

        # 2. ГЕНЕРАТОР (100% PDF)
        elif curr == BotStates.doc_gen_mode.state:
            # Очистка запроса от "Сделай PDF"
            clean_prompt_text = re.sub(r'(?i)\b(в\s+формате\s+)?(pdf|пдф|файл(ом)?|документ(ом)?|word|ворд)\b', '', input_text).strip()
            if len(clean_prompt_text) < 3: clean_prompt_text = input_text

            user_history[user_id]['h'].append({"role": "user", "content": clean_prompt_text})
            if len(user_history[user_id]['h']) > 6: user_history[user_id]['h'] = user_history[user_id]['h'][-6:]
            
            res = await asyncio.to_thread(giga.chat, {"messages": [{"role": "system", "content": PROMPT_DOC_GEN}] + user_history[user_id]['h']})
            ans = res.choices[0].message.content.strip()

            if "REDIRECT_QA" in ans:
                user_history[user_id]['rc'] += 1
                if user_history[user_id]['rc'] > 2: return await status.edit_text("Для ответов на вопросы выйдите в главное меню.", reply_markup=get_back_kb())
                return await status.edit_text("На этот вопрос я отвечу в разделе «Задать вопрос»:", reply_markup=redirect_kb("mode_qa"))

            has_filled_data = any(w in input_text.lower() for w in ['фио', 'паспорт', 'проживающий', 'рублей', 'заполнить', 'данные'])
            
            # РАЗДЕЛЕНИЕ НА ЧАТ И ФАЙЛ
            if "[ФАЙЛ]" in ans or len(ans) > 250:
                pdf_name = f"doc_{user_id}.pdf"
                pdf_success = await asyncio.to_thread(generate_pdf_gost, ans, pdf_name)
                
                await status.delete()
                if pdf_success and os.path.exists(pdf_name):
                    caption_txt = "📄 <b>Заполненный документ готов.</b>" if has_filled_data else "📄 <b>Ваш проект бланка готов.</b>"
                    await m.answer_document(FSInputFile(pdf_name), caption=caption_txt, reply_markup=doc_ready_kb(is_filled=has_filled_data), parse_mode="HTML")
                    os.remove(pdf_name)
                else:
                    await m.answer("⚠️ Не удалось сформировать PDF-файл. Пожалуйста, попробуйте отправить запрос еще раз.", reply_markup=get_back_kb())
            else:
                ans_chat = clean_text_chat(ans)
                user_history[user_id]['h'].append({"role": "assistant", "content": ans_chat})
                await status.edit_text(ans_chat, reply_markup=get_back_kb(), parse_mode="HTML")

        # 3. РАЗБОР ДОКУМЕНТА
        elif curr == BotStates.doc_analyze_mode.state:
            if m.photo or m.document or len(input_text) > 300:
                user_history[user_id]['doc_text'] = input_text
                full_prompt = f"ДОКУМЕНТ ДЛЯ АНАЛИЗА:\n{input_text}"
            else:
                doc_ctx = user_history[user_id].get('doc_text', '')
                full_prompt = f"КОНТЕКСТ ДОКУМЕНТА:\n{doc_ctx}\n\nВОПРОС КЛИЕНТА: {input_text}" if doc_ctx else input_text

            user_history[user_id]['h'].append({"role": "user", "content": full_prompt})
            if len(user_history[user_id]['h']) > 6: user_history[user_id]['h'] = user_history[user_id]['h'][-6:]

            res = await asyncio.to_thread(giga.chat, {"messages": [{"role": "system", "content": PROMPT_ANALYZE}] + user_history[user_id]['h']})
            ans = clean_text_chat(res.choices[0].message.content)
            user_history[user_id]['h'].append({"role": "assistant", "content": ans})
            
            await status.edit_text(f"🔍 <b>Результат анализа:</b>\n\n{ans}", reply_markup=get_back_kb(), parse_mode="HTML")

    except Exception as e:
        logging.error(f"AI Error: {e}")
        try: await status.edit_text("⚠️ Ошибка. Попробуйте сформулировать иначе.", reply_markup=get_back_kb())
        except: pass

async def main():
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())