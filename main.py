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

# ============ БАЗА ДАННЫХ ============
def init_db():
    conn = sqlite3.connect("users.db")
    conn.execute("CREATE TABLE IF NOT EXISTS users (user_id INTEGER PRIMARY KEY, accepted INTEGER DEFAULT 0, city TEXT DEFAULT NULL)")
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
PROMPT_QA = """Ты — эксперт-юрист РФ. Отвечай СТРОГО по структуре: 📌 КРАТКИЙ ВЕРДИКТ, 📖 ПРАВОВОЕ ОБОСНОВАНИЕ, 💡 ПЛАН ДЕЙСТВИЙ, ⚠️ РИСКИ И СРОКИ. Используй актуальные кодексы и законы РФ."""

PROMPT_DOC_GEN = """Ты — юрист-делопроизводитель. Твоя задача — писать ТОЛЬКО текст документа.
НИКОГДА не пиши, что ты не можешь создать файл.
ОБЯЗАТЕЛЬНО оберни весь текст документа в теги <DOC> и </DOC>.
Пример: <DOC>ДОГОВОР...</DOC>
Если данных нет — ставь прочерки (____). Если данные есть — заполняй."""

# ============ ГЕНЕРАЦИЯ PDF ============
def create_pdf_gost(text: str, filename: str):
    try:
        # Убираем лишний мусор
        text = text.replace('<DOC>', '').replace('</DOC>', '').replace('**', '').replace('#', '').strip()
        pdf = FPDF()
        pdf.add_page()
        pdf.set_margins(20, 20, 10)
        
        font_path = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
        if os.path.exists(font_path):
            pdf.add_font('DejaVu', '', font_path, uni=True)
            pdf.set_font('DejaVu', '', 11)
        else: pdf.set_font("Arial", size=11)
        
        lines = text.split('\n')
        first = True
        for line in lines:
            l = line.strip()
            if not l: pdf.ln(4); continue
            if first and len(l) < 100:
                pdf.set_font(pdf.font_family, '', 14)
                pdf.multi_cell(0, 10, l.upper(), align='C')
                pdf.set_font(pdf.font_family, '', 11); pdf.ln(5); first = False
            else:
                pdf.multi_cell(0, 7, "      " + l, align='J')
                first = False
        pdf.output(filename)
        return True
    except: return False

# ============ ОБРАБОТКА МЕДИА ============
async def process_voice(f_id):
    ogg, wav = f"v_{f_id}.ogg", f"v_{f_id}.wav"
    await bot.download_file((await bot.get_file(f_id)).file_path, ogg)
    os.system(f"ffmpeg -y -i {ogg} -ar 16000 -ac 1 {wav} > /dev/null 2>&1")
    r = sr.Recognizer()
    try:
        with sr.AudioFile(wav) as s: return r.recognize_google(r.record(s), language="ru-RU")
    except: return None
    finally: [os.remove(x) for x in [ogg, wav] if os.path.exists(x)]

async def process_ocr(f_id):
    p = f"p_{f_id}.jpg"
    await bot.download_file((await bot.get_file(f_id)).file_path, p)
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
        [InlineKeyboardButton(text="💼 Профильная консультация", callback_data="mode_prof")],
        [InlineKeyboardButton(text="📍 Сменить город", callback_data="change_city")]
    ])
def back_kb(): return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 В главное меню", callback_data="back")]])

# ============ ЛОГИКА ИНТЕРФЕЙСА ============
async def send_start_msg(m, u):
    welcome = ("⚖️ <b>Правовой AI-Консультант</b>\n\nПрофессиональный сервис экспресс-анализа юридических ситуаций, "
               "проверки документов и подготовки правовых решений на базе искусственного интеллекта.\n\n"
               "🎙 <i>Принимаю текстовые и голосовые сообщения!</i>\n⚡️ <i>Анализ ситуаций за секунды • Работа 24/7</i>")
    await m.answer(welcome, parse_mode="HTML")
    await asyncio.sleep(2.5)
    try:
        await m.answer_document(FSInputFile("agreement.pdf"), caption="📄 Соглашение")
        await m.answer_document(FSInputFile("faq.pdf"), caption="❓ FAQ")
    except: pass
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✅ Соглашаюсь", callback_data="accept"), InlineKeyboardButton(text="❌ Не соглашаюсь", callback_data="decline")]])
    await m.answer("📋 <b>Условия использования сервиса</b>", reply_markup=kb, parse_mode="HTML")

@dp.message(CommandStart())
async def cmd_start(m: Message, state: FSMContext):
    u = get_db_user(m.from_user.id)
    if u["accepted"]:
        await m.answer("👋 <b>С возвращением!</b>", parse_mode="HTML")
        await state.set_state(BotStates.main_menu)
        return await m.answer("📋 <b>ГЛАВНОЕ МЕНЮ</b>\n\nВыберите нужный раздел (доступен текст и голос 🎙):", reply_markup=main_kb(), parse_mode="HTML")
    await send_start_msg(m, u)

@dp.callback_query(F.data == "decline")
async def decline(c: CallbackQuery):
    await c.message.edit_text("😔 Доступ ограничен. Вы можете передумать:", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="📄 Вернуться к соглашению", callback_data="back_start")]]))

@dp.callback_query(F.data == "back_start")
async def back_start(c: CallbackQuery):
    await c.message.delete(); await send_start_msg(c.message, None)

@dp.callback_query(F.data == "accept")
async def accept(c: CallbackQuery, state: FSMContext):
    update_db(c.from_user.id, "accepted", 1)
    await c.message.edit_text("✅ <b>Условия приняты.</b>")
    await c.message.answer("🏙 <b>Укажите город:</b>", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🚫 Пропустить", callback_data="skip")]]))
    await state.set_state(BotStates.waiting_for_city)

@dp.callback_query(F.data == "skip")
async def skip(c: CallbackQuery, state: FSMContext):
    update_db(c.from_user.id, "city", "Не указан"); await c.message.delete()
    await state.set_state(BotStates.main_menu)
    await c.message.answer("📋 <b>ГЛАВНОЕ МЕНЮ</b>", reply_markup=main_kb(), parse_mode="HTML")

@dp.message(BotStates.waiting_for_city)
async def city_in(m: Message, state: FSMContext):
    update_db(m.from_user.id, "city", m.text.strip())
    await m.answer(f"📍 Ваш город: <b>{m.text}</b>", parse_mode="HTML")
    await state.set_state(BotStates.main_menu)
    await m.answer("📋 <b>ГЛАВНОЕ МЕНЮ</b>", reply_markup=main_kb(), parse_mode="HTML")

@dp.callback_query(F.data == "back")
async def back(c: CallbackQuery, state: FSMContext):
    await state.set_state(BotStates.main_menu)
    await c.message.answer("📋 <b>ГЛАВНОЕ МЕНЮ</b>", reply_markup=main_kb(), parse_mode="HTML")

@dp.callback_query(F.data.startswith("mode_"))
async def set_mode(c: CallbackQuery, state: FSMContext):
    if c.data == "mode_prof":
        return await c.message.edit_text("💼 <b>РЕЖИМ: Профильная консультация</b>\n\n_ [⚠️в разработке🛠️]", reply_markup=back_kb(), parse_mode="HTML")
    
    m_map = {"mode_qa": BotStates.qa_mode, "mode_doc_gen": BotStates.doc_gen_mode, "mode_doc_analyze": BotStates.doc_analyze_mode}
    t_map = {"mode_qa": "⚖️ Юридический вопрос\n\nОпишите вашу ситуацию текстом или голосом 🎙.", "mode_doc_gen": "📝 Создание документа\n\nПришлите фото или PDF образца, либо напишите название документа.", "mode_doc_analyze": "🔍 Разбор документа\n\nПришлите фото, PDF или текст документа."}
    await state.set_state(m_map[c.data])
    await c.message.edit_text(f"<b>РЕЖИМ: {t_map[c.data]}</b>", reply_markup=back_kb(), parse_mode="HTML")

@dp.callback_query(F.data == "change_city")
async def ch_city(c: CallbackQuery, state: FSMContext):
    await c.message.edit_text("🏙 Введите название нового города:"); await state.set_state(BotStates.waiting_for_city)

# ============ ГЛАВНЫЙ ОБРАБОТЧИК ============
@dp.message(F.text | F.voice | F.photo | F.document)
async def handle(m: Message, state: FSMContext):
    u = get_db_user(m.from_user.id)
    st = await state.get_state()
    if not u["accepted"] or not st or st == BotStates.main_menu.state:
        return await m.answer("Выберите раздел в меню 👇", reply_markup=main_kb())

    status = await m.answer("🔍 <i>Обрабатываю...</i>", parse_mode="HTML")
    inp = m.text or ""
    if m.voice:
        inp = await process_voice(m.voice.file_id)
        if inp: await m.answer(f"🎙 <b>Вы сказали:</b>\n<i>{inp}</i>", parse_mode="HTML")
    elif m.photo or m.document:
        inp = await process_ocr(m.photo[-1].file_id if m.photo else m.document.file_id)
    
    if not inp: return await status.edit_text("⚠️ Данные не распознаны.", reply_markup=back_kb())

    try:
        # 1. РЕЖИМ ВОПРОСА
        if st == BotStates.qa_mode.state:
            res = await asyncio.to_thread(giga.chat, {"messages": [{"role": "system", "content": PROMPT_QA + f"\nГород: {u['city']}"}, {"role": "user", "content": inp}]})
            ans = res.choices[0].message.content
            await status.edit_text(ans + "\n\n<i>ℹ️ Справочно. Юрист: /lawyer</i>", reply_markup=back_kb(), parse_mode="HTML")

        # 2. РЕЖИМ СОЗДАНИЯ (ФОРСИРОВАННЫЙ PDF)
        elif st == BotStates.doc_gen_mode.state:
            # Стираем слова-триггеры "PDF" из запроса к ИИ
            clean_inp = re.sub(r'(?i)(pdf|пдф|файл|ворд|word|в формате)', '', inp).strip()
            res = await asyncio.to_thread(giga.chat, {"messages": [{"role": "system", "content": PROMPT_DOC_GEN}, {"role": "user", "content": clean_inp}]})
            ans = res.choices[0].message.content
            
            if "<DOC>" in ans or len(ans) > 200:
                f_name = f"doc_{m.from_user.id}.pdf"
                if await asyncio.to_thread(create_pdf_gost, ans, f_name):
                    await status.delete()
                    await m.answer_document(FSInputFile(f_name), caption="📄 <b>Ваш документ готов.</b>", reply_markup=back_kb())
                    return os.remove(f_name)
            await status.edit_text(ans, reply_markup=back_kb())

        # 3. РЕЖИМ РАЗБОРА
        elif st == BotStates.doc_analyze_mode.state:
            res = await asyncio.to_thread(giga.chat, {"messages": [{"role": "system", "content": "Ты юрист. Найди риски в тексте."}, {"role": "user", "content": inp}]})
            await status.edit_text(f"🔍 <b>Анализ:</b>\n\n{res.choices[0].message.content}", reply_markup=back_kb(), parse_mode="HTML")

    except: await status.edit_text("⚠️ Ошибка AI.", reply_markup=back_kb())

async def main():
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__": asyncio.run(main())