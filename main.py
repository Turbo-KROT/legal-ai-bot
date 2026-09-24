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

# ============ БАЗА ДАННЫХ SQLITE (ВЕЧНАЯ ПАМЯТЬ) ============
def init_db():
    conn = sqlite3.connect("users.db")
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            accepted INTEGER DEFAULT 0,
            city TEXT DEFAULT NULL
        )
    """)
    conn.commit()
    conn.close()

init_db()

def get_db_user(user_id: int):
    conn = sqlite3.connect("users.db")
    cursor = conn.cursor()
    cursor.execute("SELECT accepted, city FROM users WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()
    if not row:
        cursor.execute("INSERT INTO users (user_id, accepted, city) VALUES (?, 0, NULL)", (user_id,))
        conn.commit()
        res = {"accepted": False, "city": None}
    else:
        res = {"accepted": bool(row[0]), "city": row[1]}
    conn.close()
    return res

def set_db_accept(user_id: int):
    conn = sqlite3.connect("users.db")
    cursor = conn.cursor()
    cursor.execute("UPDATE users SET accepted = 1 WHERE user_id = ?", (user_id,))
    conn.commit()
    conn.close()

def set_db_city(user_id: int, city: str):
    conn = sqlite3.connect("users.db")
    cursor = conn.cursor()
    cursor.execute("UPDATE users SET city = ? WHERE user_id = ?", (city, user_id))
    conn.commit()
    conn.close()

user_history = {}

# ============ ГЛОБАЛЬНАЯ ЗАЩИТА ОТ ПАДЕНИЙ ============
@dp.errors()
async def global_error_handler(event: ErrorEvent):
    logging.critical(f"Критическая ошибка подавлена: {event.exception}")

# ============ СОСТОЯНИЯ ============
class BotStates(StatesGroup):
    waiting_for_city = State()
    main_menu = State()
    qa_mode = State()
    doc_gen_mode = State()
    doc_analyze_mode = State()

# ============ СИСТЕМНЫЕ ПРОМПТЫ ============
PROMPT_QA = """Ты — высококвалифицированный юридический AI-консультант по ВСЕМ отраслям права РФ (Гражданское, Уголовное, Административное, Трудовое, Семейное, Налоговое, Земельное, а также ГПК, УПК, АПК, КАС, Конституция, Указы Президента и Постановления).
Твоя цель — дать глубокую, понятную и практичную консультацию, опираясь на актуальное законодательство. Укажи четкий алгоритм действий.

Отвечай СТРОГО по следующей структуре (используй эмодзи):

📌 КРАТКИЙ ВЕРДИКТ: (Четкая суть ответа и главная правовая позиция)
📖 ПРАВОВОЕ ОБОСНОВАНИЕ: (Подробный разбор статей кодексов РФ, законов и прав гражданина)
💡 ПЛАН ДЕЙСТВИЙ: (Конкретные, пошаговые инструкции, что делать дальше)
⚠️ РИСКИ И СРОКИ: (Сроки давности, возможные подводные камни)"""

PROMPT_DOC_GEN = """Ты — юридический делопроизводитель РФ.
Твоя ЕДИНСТВЕННАЯ задача — составлять полные, юридически грамотные тексты документов.
Если пользователь просит составить документ, прислать образец, бланк или дал данные — НАПИШИ ПОЛНЫЙ ТЕКСТ ДОКУМЕНТА.
Начинай сразу с Названия документа по центру (например, ДОГОВОР КУПЛИ-ПРОДАЖИ). Без приветствий, отговорок и лишних комментариев."""

# ============ ВПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ============
def sanitize_text_for_pdf(text: str) -> str:
    text = re.sub(r'\*\*(.*?)\*\*', r'\1', text)
    text = re.sub(r'\*(.*?)\*', r'\1', text)
    text = re.sub(r'#(.*?)\n', r'\1\n', text)
    emoji_pattern = re.compile("[" "\U00010000-\U0010FFFF" "\u2600-\u27BF\u2300-\u23FF\u2B00-\u2BFF\u2190-\u21FF" "]+", flags=re.UNICODE)
    text = emoji_pattern.sub("", text)
    replacements = {'—': '-', '–': '-', '…': '...', '«': '"', '»': '"', '“': '"', '”': '"', '‘': "'", '’': "'", '\xa0': ' ', '\t': '    '}
    for orig, repl in replacements.items():
        text = text.replace(orig, repl)
    return text.strip()

def generate_pdf_file(text: str, filename: str) -> bool:
    try:
        clean_text = sanitize_text_for_pdf(text)
        pdf = FPDF()
        pdf.add_page()
        pdf.set_margins(20, 20, 10)
        pdf.set_auto_page_break(auto=True, margin=20)
        
        # Системные шрифты Ubuntu
        font_candidates = [
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf"
        ]
        font_path = next((f for f in font_candidates if os.path.exists(f)), None)
        
        if font_path:
            pdf.add_font('DejaVu', '', font_path)
            pdf.set_font('DejaVu', '', 11)
        else:
            pdf.set_font("Arial", size=11)
            
        lines = clean_text.split('\n')
        title_done = False
        
        for line in lines:
            c_line = line.strip()
            if not c_line:
                pdf.ln(4)
                continue
                
            if not title_done and len(c_line) < 120:
                if font_path: pdf.set_font('DejaVu', '', 13)
                pdf.multi_cell(0, 7, c_line, align='C')
                if font_path: pdf.set_font('DejaVu', '', 11)
                pdf.ln(4)
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
    recognizer = sr.Recognizer()
    with sr.AudioFile(wav_path) as source:
        audio_data = recognizer.record(source)
        return recognizer.recognize_google(audio_data, language="ru-RU")

async def transcribe_voice(file_id: str) -> str:
    file = await bot.get_file(file_id)
    ogg_path, wav_path = f"v_{file_id}.ogg", f"v_{file_id}.wav"
    try:
        await bot.download_file(file.file_path, ogg_path)
        proc = await asyncio.create_subprocess_exec("ffmpeg", "-y", "-i", ogg_path, "-ar", "16000", "-ac", "1", wav_path, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        await proc.communicate()
        return await asyncio.to_thread(recognize_audio, wav_path)
    except Exception as e:
        logging.error(f"Voice Error: {e}")
        return None
    finally:
        for p in [ogg_path, wav_path]:
            if os.path.exists(p): os.remove(p)

def process_ocr_photo(photo_path: str) -> str:
    img = Image.open(photo_path).convert('L')
    try: return pytesseract.image_to_string(img, lang='rus+eng').strip()
    except: return pytesseract.image_to_string(img).strip()

async def extract_text_from_photo(file_id: str) -> str:
    file = await bot.get_file(file_id)
    photo_path = f"photo_{file_id}.jpg"
    await bot.download_file(file.file_path, photo_path)
    try: return await asyncio.to_thread(process_ocr_photo, photo_path)
    except: return ""
    finally:
        if os.path.exists(photo_path): os.remove(photo_path)

def process_pdf_extract(pdf_path: str) -> str:
    text = ""
    doc = fitz.open(pdf_path)
    for page in doc: text += page.get_text()
    return text.strip()

async def extract_text_from_pdf(file_id: str) -> str:
    file = await bot.get_file(file_id)
    pdf_path = f"doc_{file_id}.pdf"
    await bot.download_file(file.file_path, pdf_path)
    try: return await asyncio.to_thread(process_pdf_extract, pdf_path)
    except: return ""
    finally:
        if os.path.exists(pdf_path): os.remove(pdf_path)

# ============ КЛАВИАТУРЫ ============
def get_main_menu_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⚖️ Задать юридический вопрос", callback_data="mode_qa")],
        [InlineKeyboardButton(text="📝 Создание документа", callback_data="mode_doc_gen")],
        [InlineKeyboardButton(text="🔍 Разбор документа", callback_data="mode_doc_analyze")],
        [InlineKeyboardButton(text="📍 Сменить город", callback_data="change_city")]
    ])

def get_back_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 В главное меню", callback_data="back_to_menu")]
    ])

# ============ СТАРТ И СОГЛАШЕНИЕ ============
@dp.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    user = get_db_user(message.from_user.id)
    if user["accepted"]:
        await message.answer("👋 <b>С возвращением!</b>", parse_mode="HTML")
        await show_main_menu(message.from_user.id, state)
        return

    welcome_text = (
        "⚖️ <b>Правовой AI-Консультант</b>\n\n"
        "Профессиональный сервис экспресс-анализа юридических ситуаций, "
        "проверки документов и подготовки правовых решений на базе искусственного интеллекта.\n\n"
        "🎙 <i>Принимаю текстовые и голосовые сообщения!</i>\n"
        "⚡️ <i>Анализ ситуаций за секунды • Работа 24/7</i>"
    )
    await message.answer(welcome_text, parse_mode="HTML")
    await asyncio.sleep(2.5)
    
    try:
        await message.answer_document(FSInputFile("agreement.pdf"), caption="📄 Пользовательское соглашение")
        await message.answer_document(FSInputFile("faq.pdf"), caption="❓ Часто задаваемые вопросы (FAQ)")
    except Exception as e:
        logging.error(f"PDF Send Error: {e}")

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Соглашаюсь", callback_data="accept"), 
         InlineKeyboardButton(text="❌ Не соглашаюсь", callback_data="decline")]
    ])
    await message.answer("📋 <b>Условия использования сервиса</b>\n\nНажимая «Соглашаюсь», вы принимаете условия.", reply_markup=kb, parse_mode="HTML")

@dp.callback_query(F.data == "decline")
async def process_decline(callback: CallbackQuery):
    await callback.message.edit_text("😔 Без принятия условий доступ ограничен. Нажмите /start для повтора.")
    await callback.answer()

@dp.callback_query(F.data == "accept")
async def process_accept(callback: CallbackQuery, state: FSMContext):
    set_db_accept(callback.from_user.id)
    await callback.message.edit_text("✅ <b>Условия использования приняты.</b>", parse_mode="HTML")
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🚫 Не указывать (Пропустить)", callback_data="skip_city")]
    ])
    await callback.message.answer(
        "🏙 <b>Укажите ваш город</b> (для учета региональных законов):", 
        reply_markup=kb, 
        parse_mode="HTML"
    )
    await state.set_state(BotStates.waiting_for_city)
    await callback.answer()

# ============ ВВОД ГОРОДА ============
@dp.callback_query(F.data == "skip_city")
async def skip_city(callback: CallbackQuery, state: FSMContext):
    set_db_city(callback.from_user.id, "Не указан")
    await callback.message.edit_text("📍 Город не указан.")
    await show_main_menu(callback.from_user.id, state)

@dp.message(BotStates.waiting_for_city)
async def city_input(message: Message, state: FSMContext):
    set_db_city(message.from_user.id, message.text.strip())
    await message.answer(f"📍 Город сохранен: <b>{message.text}</b>", parse_mode="HTML")
    await show_main_menu(message.from_user.id, state)

@dp.callback_query(F.data == "change_city")
async def change_city_btn(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_text("🏙 Введите название нового города:")
    await state.set_state(BotStates.waiting_for_city)

# ============ ГЛАВНОЕ МЕНЮ ============
async def show_main_menu(user_id: int, state: FSMContext):
    user_history[user_id] = []
    await bot.send_message(
        user_id, 
        "📋 <b>ГЛАВНОЕ МЕНЮ</b>\n\nВыберите нужный раздел (доступен текст и голос 🎙):", 
        reply_markup=get_main_menu_kb(), 
        parse_mode="HTML"
    )
    await state.set_state(BotStates.main_menu)

@dp.callback_query(F.data == "back_to_menu")
async def back_to_menu_btn(callback: CallbackQuery, state: FSMContext):
    await callback.message.delete()
    await show_main_menu(callback.from_user.id, state)

# ============ РЕЖИМЫ ============
@dp.callback_query(F.data == "mode_qa")
async def mode_qa(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_text("⚖️ <b>РЕЖИМ: Юридический вопрос</b>\n\nОпишите вашу ситуацию текстом или голосом 🎙.", reply_markup=get_back_kb(), parse_mode="HTML")
    await state.set_state(BotStates.qa_mode)

@dp.callback_query(F.data == "mode_doc_gen")
async def mode_doc_gen(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_text("📝 <b>РЕЖИМ: Создание документа</b>\n\nПришлите <b>фото или PDF</b> образца, либо напишите название документа. Я могу задать вопросы для заполнения или прислать готовый бланк.", reply_markup=get_back_kb(), parse_mode="HTML")
    await state.set_state(BotStates.doc_gen_mode)

@dp.callback_query(F.data == "mode_doc_analyze")
async def mode_doc_analyze(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_text("🔍 <b>РЕЖИМ: Разбор документа</b>\n\nПришлите <b>фото, PDF</b> или текст документа, и я найду в нем риски и ошибки.", reply_markup=get_back_kb(), parse_mode="HTML")
    await state.set_state(BotStates.doc_analyze_mode)

# ============ ЕДИНЫЙ ОБРАБОТЧИК ВХОДЯЩИХ ============
@dp.message(F.text | F.voice | F.photo | F.document)
async def handle_input(message: Message, state: FSMContext):
    user_id = message.from_user.id
    user = get_db_user(user_id)
    curr_state = await state.get_state()
    
    if not user["accepted"] or curr_state == BotStates.main_menu.state:
        await message.answer("Пожалуйста, выберите режим в меню 👇", reply_markup=get_main_menu_kb())
        return

    status = await message.answer("🔍 <i>Анализирую данные...</i>", parse_mode="HTML")
    
    try:
        caption = message.caption.strip() if message.caption else ""
        input_text = ""
        
        if message.text:
            input_text = message.text
        elif message.voice:
            await status.edit_text("🎙 <i>Распознаю голос...</i>", parse_mode="HTML")
            input_text = await transcribe_voice(message.voice.file_id)
            if input_text: await message.answer(f"🎙 <b>Вы сказали:</b>\n«<i>{input_text}</i>»", parse_mode="HTML")
        elif message.photo:
            await status.edit_text("📸 <i>Сканирую текст с фото...</i>", parse_mode="HTML")
            ocr_text = await extract_text_from_photo(message.photo[-1].file_id)
            if not ocr_text or len(ocr_text) < 3:
                return await status.edit_text("⚠️ <b>Не удалось четко распознать текст с фото.</b>", reply_markup=get_back_kb(), parse_mode="HTML")
            input_text = f"Текст с фото документа:\n{ocr_text}"
            if caption: input_text += f"\n\nУказание пользователя: {caption}"
        elif message.document:
            await status.edit_text("📄 <i>Читаю PDF-файл...</i>", parse_mode="HTML")
            pdf_text = await extract_text_from_pdf(message.document.file_id)
            if not pdf_text or len(pdf_text) < 3:
                return await status.edit_text("⚠️ <b>Не удалось извлечь текст из файла.</b>", reply_markup=get_back_kb(), parse_mode="HTML")
            input_text = f"Текст из PDF-документа:\n{pdf_text}"
            if caption: input_text += f"\n\nУказание пользователя: {caption}"
        
        if not input_text:
            return await status.edit_text("⚠️ Не удалось разобрать данные. Попробуйте повторить передачу.", reply_markup=get_back_kb())

        if user_id not in user_history: user_history[user_id] = []

        # РЕЖИМ 1: ЮРИДИЧЕСКИЙ ВОПРОС
        if curr_state == BotStates.qa_mode.state:
            user_history[user_id].append({"role": "user", "content": input_text})
            if len(user_history[user_id]) > 6: user_history[user_id] = user_history[user_id][-6:]
            
            sys_prompt = PROMPT_QA + f"\nГород пользователя: {user['city']}"
            res = await asyncio.to_thread(giga.chat, {"messages": [{"role": "system", "content": sys_prompt}] + user_history[user_id]})
            ans = res.choices[0].message.content
            user_history[user_id].append({"role": "assistant", "content": ans})
            
            footer = "\n\n<i>ℹ️ Информация носит справочный характер. Связь с юристом: /lawyer</i>"
            await status.edit_text(ans + footer, reply_markup=get_back_kb(), parse_mode="HTML")

        # РЕЖИМ 2: СОЗДАНИЕ ДОКУМЕНТА (ПРИНУДИТЕЛЬНЫЙ СБОР В PDF)
        elif curr_state == BotStates.doc_gen_mode.state:
            clean_prompt_text = re.sub(r'(?i)\b(в\s+формате\s+)?(pdf|пдф|файл(ом)?|документ(ом)?|word|ворд)\b', '', input_text).strip()
            if len(clean_prompt_text) < 3: clean_prompt_text = input_text

            user_history[user_id].append({"role": "user", "content": clean_prompt_text})
            if len(user_history[user_id]) > 6: user_history[user_id] = user_history[user_id][-6:]
            
            res = await asyncio.to_thread(giga.chat, {"messages": [{"role": "system", "content": PROMPT_DOC_GEN}] + user_history[user_id]})
            ans = res.choices[0].message.content.strip()
            
            # Если ответ длинный (похож на документ) или пользователь не просил явным образом "текстом в чат"
            is_explicit_text = "текстом" in input_text.lower() or "в чат" in input_text.lower()
            
            # Всякий раз, когда формируется текст документа, превращаем в PDF
            if not is_explicit_text and len(ans) > 150:
                pdf_name = f"doc_{message.from_user.id}.pdf"
                pdf_success = await asyncio.to_thread(generate_pdf_file, ans, pdf_name)
                
                await status.delete()
                if pdf_success and os.path.exists(pdf_name):
                    await message.answer_document(
                        FSInputFile(pdf_name), 
                        caption="📄 <b>Ваш проект документа готов (формат PDF).</b>", 
                        reply_markup=get_back_kb(), 
                        parse_mode="HTML"
                    )
                    os.remove(pdf_name)
                else:
                    await message.answer(f"📄 <b>Ваш документ готов:</b>\n\n{ans}", reply_markup=get_back_kb(), parse_mode="HTML")
            else:
                user_history[user_id].append({"role": "assistant", "content": ans})
                await status.edit_text(ans, reply_markup=get_back_kb(), parse_mode="HTML")

        # РЕЖИМ 3: РАЗБОР ДОКУМЕНТА
        elif curr_state == BotStates.doc_analyze_mode.state:
            sys_prompt = "Ты опытный юрист РФ. Проанализируй предоставленный текст документа. Найди все правовые риски, скрытые комиссии, ошибки и ущемления прав пользователя. Выдай понятный и подробный отчет со ссылками на законы."
            res = await asyncio.to_thread(giga.chat, {"messages": [{"role": "system", "content": sys_prompt}, {"role": "user", "content": input_text}]})
            ans = res.choices[0].message.content
            await status.edit_text(f"🔍 <b>Результат правового анализа:</b>\n\n{ans}", reply_markup=get_back_kb(), parse_mode="HTML")

    except Exception as e:
        logging.error(f"AI Error: {e}")
        await status.edit_text("⚠️ Произошла ошибка. Попробуйте повторить запрос.", reply_markup=get_back_kb())

async def main():
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())