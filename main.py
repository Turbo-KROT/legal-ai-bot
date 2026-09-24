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
    cursor.execute("CREATE TABLE IF NOT EXISTS users (user_id INTEGER PRIMARY KEY, accepted INTEGER DEFAULT 0, city TEXT DEFAULT NULL)")
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
    else: res = {"accepted": bool(row[0]), "city": row[1]}
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

@dp.errors()
async def global_error_handler(event: ErrorEvent):
    logging.critical(f"Ошибка подавлена: {event.exception}")

class BotStates(StatesGroup):
    waiting_for_city, main_menu, qa_mode, doc_gen_mode, doc_analyze_mode = State(), State(), State(), State(), State()

# ============ СИСТЕМНЫЕ ПРОМПТЫ ============
PROMPT_QA = """Ты — высококвалифицированный юридический AI-консультант по ВСЕМ отраслям права РФ. 
Отвечай СТРОГО по структуре: 📌 КРАТКИЙ ВЕРДИКТ, 📖 ПРАВОВОЕ ОБОСНОВАНИЕ (со статьями), 💡 ПЛАН ДЕЙСТВИЙ, ⚠️ РИСКИ И СРОКИ."""

PROMPT_DOC_GEN = """Ты — юрист-делопроизводитель РФ. Составь текст документа.
Если данных нет — оставь прочерки (____). Если данные есть — заполни их.
ПИШИ ТОЛЬКО ТЕКСТ ДОКУМЕНТА. Без приветствий вроде 'Конечно', 'Вот ваш образец'. Начни сразу с заголовка."""

# ============ ВЕРСТКА PDF ПО ГОСТУ ============
def generate_pdf_gost(text: str, filename: str):
    try:
        # Убираем "мусор" нейросети в начале (приветствия)
        doc_match = re.search(r'(ДОГОВОР|ЗАЯВЛЕНИЕ|АКТ|ДАРСТВЕННАЯ|СОГЛАШЕНИЕ|ПРОТОКОЛ|УСТАВ|ПРЕТЕНЗИЯ|ИСКОВОЕ).*', text, re.IGNORECASE | re.DOTALL)
        clean_text = doc_match.group(0) if doc_match else text
        
        # Очистка от спецсимволов
        clean_text = clean_text.replace('**', '').replace('*', '').replace('#', '')
        
        pdf = FPDF()
        pdf.add_page()
        pdf.set_margins(20, 20, 10)
        
        # Загрузка шрифта
        font_path = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
        f_name = "Arial"
        if os.path.exists(font_path):
            pdf.add_font('DejaVu', '', font_path, uni=True)
            f_name = 'DejaVu'
        
        pdf.set_font(f_name, '', 11)
        lines = clean_text.split('\n')
        first = True
        
        for line in lines:
            l = line.strip()
            if not l: pdf.ln(4); continue
            if first and len(l) < 100:
                pdf.set_font(f_name, '', 14)
                pdf.multi_cell(0, 10, l.upper(), align='C')
                pdf.set_font(f_name, '', 11); pdf.ln(5); first = False
            else:
                pdf.multi_cell(0, 7, "      " + l, align='J')
                first = False
        pdf.output(filename)
        return True
    except: return False

# ============ МЕДИА ============
async def transcribe_voice(file_id):
    file = await bot.get_file(file_id)
    ogg, wav = f"v_{file_id}.ogg", f"v_{file_id}.wav"
    try:
        await bot.download_file(file.file_path, ogg)
        os.system(f"ffmpeg -y -i {ogg} -ar 16000 -ac 1 {wav} > /dev/null 2>&1")
        r = sr.Recognizer()
        with sr.AudioFile(wav) as s: return r.recognize_google(r.record(s), language="ru-RU")
    except: return None
    finally: [os.remove(f) for f in [ogg, wav] if os.path.exists(f)]

async def get_ocr(file_id):
    p = f"p_{file_id}.jpg"
    await bot.download_file((await bot.get_file(file_id)).file_path, p)
    try:
        t = await asyncio.to_thread(pytesseract.image_to_string, Image.open(p).convert('L'), lang='rus+eng')
        return t.strip()
    finally: os.remove(p)

# ============ КЛАВИАТУРЫ ============
def main_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⚖️ Задать юридический вопрос", callback_data="mode_qa")],
        [InlineKeyboardButton(text="📝 Создание документа", callback_data="mode_doc_gen")],
        [InlineKeyboardButton(text="🔍 Разбор документа", callback_data="mode_doc_analyze")],
        [InlineKeyboardButton(text="📍 Сменить город", callback_data="change_city")]
    ])
def back_kb(): return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 В главное меню", callback_data="back")]])

# ============ ОБРАБОТЧИКИ ============
@dp.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    u = get_db_user(message.from_user.id)
    if u["accepted"]:
        await message.answer("👋 <b>С возвращением!</b>", parse_mode="HTML")
        return await show_main_menu(message.from_user.id, state)
    
    welcome = ("⚖️ <b>Правовой AI-Консультант</b>\n\nПрофессиональный сервис экспресс-анализа юридических ситуаций, "
               "проверки документов и подготовки правовых решений на базе искусственного интеллекта.\n\n"
               "🎙 <i>Принимаю текстовые и голосовые сообщения!</i>\n⚡️ <i>Анализ ситуаций за секунды • Работа 24/7</i>")
    await message.answer(welcome, parse_mode="HTML")
    await asyncio.sleep(2.5)
    try:
        await message.answer_document(FSInputFile("agreement.pdf"), caption="📄 Соглашение")
        await message.answer_document(FSInputFile("faq.pdf"), caption="❓ FAQ")
    except: pass
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✅ Соглашаюсь", callback_data="accept"), InlineKeyboardButton(text="❌ Не соглашаюсь", callback_data="decline")]])
    await message.answer("📋 <b>Условия использования сервиса</b>", reply_markup=kb, parse_mode="HTML")

@dp.callback_query(F.data == "accept")
async def accept(c: CallbackQuery, state: FSMContext):
    set_db_accept(c.from_user.id)
    await c.message.answer("🏙 <b>Укажите ваш город:</b>", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🚫 Пропустить", callback_data="skip")]]), parse_mode="HTML")
    await state.set_state(BotStates.waiting_for_city)

@dp.callback_query(F.data.in_(["skip", "back", "change_city"]))
async def nav(c: CallbackQuery, state: FSMContext):
    if c.data == "change_city": await c.message.edit_text("🏙 Введите город:"); await state.set_state(BotStates.waiting_for_city)
    else: await show_main_menu(c.from_user.id, state)

@dp.message(BotStates.waiting_for_city)
async def city_in(m: Message, state: FSMContext):
    set_db_city(m.from_user.id, m.text.strip())
    await show_main_menu(m.from_user.id, state)

async def show_main_menu(uid, state):
    user_history[uid] = []
    await bot.send_message(uid, "📋 <b>ГЛАВНОЕ МЕНЮ</b>", reply_markup=main_kb(), parse_mode="HTML")
    await state.set_state(BotStates.main_menu)

@dp.callback_query(F.data.startswith("mode_"))
async def set_mode(c: CallbackQuery, state: FSMContext):
    modes = {"mode_qa": BotStates.qa_mode, "mode_doc_gen": BotStates.doc_gen_mode, "mode_doc_analyze": BotStates.doc_analyze_mode}
    await state.set_state(modes[c.data])
    t = "⚖️ Вопрос" if c.data == "mode_qa" else "📝 Создание" if c.data == "mode_doc_gen" else "🔍 Разбор"
    await c.message.edit_text(f"<b>РЕЖИМ: {t}</b>", reply_markup=back_kb(), parse_mode="HTML")

@dp.message(F.text | F.voice | F.photo | F.document)
async def handle(m: Message, state: FSMContext):
    u = get_db_user(m.from_user.id)
    curr = await state.get_state()
    if not u["accepted"] or curr == BotStates.main_menu.state: return await m.answer("Выберите режим 👇", reply_markup=main_kb())

    status = await m.answer("🔍 <i>Обрабатываю...</i>", parse_mode="HTML")
    inp = m.text or (await transcribe_voice(m.voice.file_id) if m.voice else await get_ocr(m.photo[-1].file_id if m.photo else m.document.file_id))
    if m.voice and inp: await m.answer(f"🎙 <b>Вы сказали:</b>\n<i>{inp}</i>", parse_mode="HTML")
    
    if not inp: return await status.edit_text("⚠️ Не удалось получить данные.", reply_markup=back_kb())

    try:
        # РЕЖИМ: ВОПРОСЫ
        if curr == BotStates.qa_mode.state:
            hist = user_history.get(m.from_user.id, [])
            hist.append({"role": "user", "content": inp})
            res = await asyncio.to_thread(giga.chat, {"messages": [{"role": "system", "content": PROMPT_QA + f"\nГород: {u['city']}"}] + hist[-6:]})
            ans = res.choices[0].message.content
            hist.append({"role": "assistant", "content": ans})
            user_history[m.from_user.id] = hist
            await status.edit_text(ans + "\n\n<i>ℹ️ /lawyer</i>", reply_markup=back_kb(), parse_mode="HTML")

        # РЕЖИМ: СОЗДАНИЕ ДОКУМЕНТОВ (ПРИНУДИТЕЛЬНО В PDF)
        elif curr == BotStates.doc_gen_mode.state:
            res = await asyncio.to_thread(giga.chat, {"messages": [{"role": "system", "content": PROMPT_DOC_GEN}, {"role": "user", "content": inp}]})
            ans = res.choices[0].message.content.strip()
            
            # Если это длинный юридический текст — ГАРАНТИРОВАННО ДЕЛАЕМ PDF
            if len(ans) > 150:
                f_name = f"doc_{m.from_user.id}.pdf"
                if await asyncio.to_thread(generate_pdf_gost, ans, f_name):
                    await status.delete()
                    await m.answer_document(FSInputFile(f_name), caption="📄 <b>Ваш документ в формате PDF готов.</b>", reply_markup=back_kb(), parse_mode="HTML")
                    os.remove(f_name)
                else: await status.edit_text(ans, reply_markup=back_kb())
            else: await status.edit_text(ans, reply_markup=back_kb())

        # РЕЖИМ: РАЗБОР
        elif curr == BotStates.doc_analyze_mode.state:
            res = await asyncio.to_thread(giga.chat, {"messages": [{"role": "system", "content": "Проанализируй риски в тексте документа."}, {"role": "user", "content": inp}]})
            await status.edit_text(f"🔍 <b>Анализ:</b>\n\n{res.choices[0].message.content}", reply_markup=back_kb(), parse_mode="HTML")

    except: await status.edit_text("⚠️ Ошибка. Попробуйте еще раз.", reply_markup=back_kb())

async def main():
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__": asyncio.run(main())