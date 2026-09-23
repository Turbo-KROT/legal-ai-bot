import asyncio
import logging
import os
import re
import urllib.request
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

# Самая мощная модель для юриспруденции
giga = GigaChat(
    credentials=GIGACHAT_CREDENTIALS,
    scope="GIGACHAT_API_PERS",
    model="GigaChat-2-Pro",
    verify_ssl_certs=False
)

db = {}

# ============ ГЛОБАЛЬНАЯ ЗАЩИТА ОТ ПАДЕНИЙ ============
@dp.errors()
async def global_error_handler(event: ErrorEvent):
    logging.critical(f"Ошибка подавлена: {event.exception}")

class BotStates(StatesGroup):
    waiting_for_city = State()
    main_menu = State()
    qa_mode = State()
    doc_gen_mode = State()
    doc_analyze_mode = State()

# ============ СИСТЕМНЫЕ ПРОМПТЫ ============
PROMPT_QA = """Ты — высококвалифицированный юридический AI-консультант по ВСЕМ отраслям права РФ. 
Давай глубокие ответы с указанием статей Кодексов, Конституции и ФЗ. 
Структура: 📌 КРАТКИЙ ВЕРДИКТ, 📖 ПРАВОВОЕ ОБОСНОВАНИЕ, 💡 ПЛАН ДЕЙСТВИЙ, ⚠️ РИСКИ."""

PROMPT_DOC_GEN = """Ты — профессиональный юрист-делопроизводитель РФ. 
Твоя задача — писать ТОЛЬКО текст документа.
НИКОГДА не пиши, что ты не можешь создать файл. Просто выдай текст. 
Начинай сразу с Названия документа. Используй (______) для пустых полей."""

# ============ ФУНКЦИИ ============
def get_user(user_id: int):
    if user_id not in db: db[user_id] = {"accepted": False, "city": None, "history": []}
    return db[user_id]

def sanitize_text_for_pdf(text: str) -> str:
    text = re.sub(r'\*\*(.*?)\*\*', r'\1', text)
    text = re.sub(r'\*(.*?)\*', r'\1', text)
    text = re.sub(r'#(.*?)\n', r'\1\n', text)
    emoji_pattern = re.compile("[" "\U00010000-\U0010FFFF" "\u2600-\u27BF\u2300-\u23FF\u2B00-\u2BFF\u2190-\u21FF" "]+", flags=re.UNICODE)
    text = emoji_pattern.sub("", text)
    replacements = {'—': '-', '–': '-', '…': '...', '«': '"', '»': '"', '“': '"', '”': '"', '‘': "'", '’': "'", '\xa0': ' ', '\t': '    '}
    for orig, repl in replacements.items(): text = text.replace(orig, repl)
    return text.strip()

def download_font():
    font_file = "DejaVuSans.ttf"
    if not os.path.exists(font_file):
        try:
            url = "https://github.com/matomo-org/travis-scripts/raw/master/fonts/DejaVuSans.ttf"
            urllib.request.urlretrieve(url, font_file)
        except: pass
    return font_file if os.path.exists(font_file) else None

def generate_pdf_file(text: str, filename: str) -> bool:
    try:
        clean_text = sanitize_text_for_pdf(text)
        pdf = FPDF()
        pdf.add_page()
        pdf.set_margins(20, 20, 10)
        pdf.set_auto_page_break(auto=True, margin=20)
        font_path = download_font()
        if font_path:
            pdf.add_font('DejaVu', '', font_path)
            pdf.set_font('DejaVu', '', 11)
        else: pdf.set_font("Arial", size=11)
            
        lines = clean_text.split('\n')
        title_done = False
        for line in lines:
            c_line = line.strip()
            if not c_line:
                pdf.ln(4); continue
            if not title_done and len(c_line) < 120:
                if font_path: pdf.set_font('DejaVu', '', 14)
                pdf.multi_cell(0, 8, c_line, align='C')
                if font_path: pdf.set_font('DejaVu', '', 11)
                pdf.ln(4); title_done = True
            else:
                pdf.multi_cell(0, 6, "      " + c_line, align='J')
        pdf.output(filename)
        return True
    except Exception as e:
        logging.error(f"PDF Error: {e}")
        return False

# ============ МЕДИА ============
async def transcribe_voice(file_id: str) -> str:
    file = await bot.get_file(file_id)
    ogg, wav = f"v_{file_id}.ogg", f"v_{file_id}.wav"
    try:
        await bot.download_file(file.file_path, ogg)
        proc = await asyncio.create_subprocess_exec("ffmpeg", "-y", "-i", ogg, "-ar", "16000", "-ac", "1", wav, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        await proc.communicate()
        recognizer = sr.Recognizer()
        with sr.AudioFile(wav) as source: return recognizer.recognize_google(recognizer.record(source), language="ru-RU")
    except: return None
    finally:
        for p in [ogg, wav]:
            if os.path.exists(p): os.remove(p)

async def extract_text_from_photo(file_id: str) -> str:
    p_path = f"p_{file_id}.jpg"
    await bot.download_file((await bot.get_file(file_id)).file_path, p_path)
    try:
        img = Image.open(p_path).convert('L')
        return pytesseract.image_to_string(img, lang='rus+eng').strip()
    except: return ""
    finally:
        if os.path.exists(p_path): os.remove(p_path)

async def extract_text_from_pdf(file_id: str) -> str:
    pdf_p = f"d_{file_id}.pdf"
    await bot.download_file((await bot.get_file(file_id)).file_path, pdf_p)
    try:
        doc = fitz.open(pdf_p)
        return "".join([p.get_text() for p in doc]).strip()
    except: return ""
    finally:
        if os.path.exists(pdf_p): os.remove(pdf_p)

# ============ КЛАВИАТУРЫ ============
def get_main_menu_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⚖️ Задать юридический вопрос", callback_data="mode_qa")],
        [InlineKeyboardButton(text="📝 Создание документа", callback_data="mode_doc_gen")],
        [InlineKeyboardButton(text="🔍 Разбор документа", callback_data="mode_doc_analyze")],
        [InlineKeyboardButton(text="📍 Сменить город", callback_data="change_city")]
    ])

def get_back_kb():
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 В главное меню", callback_data="back_to_menu")]])

# ============ ОБРАБОТЧИКИ ============
@dp.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    user = get_user(message.from_user.id)
    if user["accepted"]:
        await message.answer("👋 <b>С возвращением!</b>", parse_mode="HTML")
        await state.set_state(BotStates.main_menu)
        await message.answer("📋 <b>ГЛАВНОЕ МЕНЮ</b>", reply_markup=get_main_menu_kb(), parse_mode="HTML")
        return
    welcome = "⚖️ <b>Правовой AI-Консультант</b>\n\nПрофессиональный сервис экспресс-анализа...\n\n🎙 <i>Принимаю текст, голос и фото!</i>"
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
    get_user(c.from_user.id)["accepted"] = True
    await c.message.answer("🏙 <b>Укажите ваш город:</b>", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🚫 Пропустить", callback_data="skip_city")]]), parse_mode="HTML")
    await state.set_state(BotStates.waiting_for_city)

@dp.callback_query(F.data == "back_to_menu")
async def back_to_menu(c: CallbackQuery, state: FSMContext):
    get_user(c.from_user.id)["history"] = []
    await state.set_state(BotStates.main_menu)
    await c.message.answer("📋 <b>ГЛАВНОЕ МЕНЮ</b>", reply_markup=get_main_menu_kb(), parse_mode="HTML")

@dp.callback_query(F.data.startswith("mode_"))
async def modes(c: CallbackQuery, state: FSMContext):
    states = {"mode_qa": BotStates.qa_mode, "mode_doc_gen": BotStates.doc_gen_mode, "mode_doc_analyze": BotStates.doc_analyze_mode}
    await state.set_state(states[c.data])
    txt = "⚖️ Вопрос" if c.data == "mode_qa" else "📝 Создание документа" if c.data == "mode_doc_gen" else "🔍 Разбор документа"
    await c.message.edit_text(f"<b>РЕЖИМ: {txt}</b>\n\nПринимаю текст, голос и фото.", reply_markup=get_back_kb(), parse_mode="HTML")

# ============ ГЛАВНЫЙ ОБРАБОТЧИК ============
@dp.message(F.text | F.voice | F.photo | F.document)
async def handle_input(message: Message, state: FSMContext):
    user = get_user(message.from_user.id)
    curr_state = await state.get_state()
    if not user["accepted"] or curr_state == BotStates.main_menu.state:
        return await message.answer("Пожалуйста, выберите режим в меню 👇", reply_markup=get_main_menu_kb())

    status = await message.answer("🔍 <i>Анализирую данные...</i>", parse_mode="HTML")
    input_text = message.text or ""
    
    try:
        if message.voice:
            input_text = await transcribe_voice(message.voice.file_id)
            if input_text: await message.answer(f"🎙 <b>Вы сказали:</b>\n«<i>{input_text}</i>»", parse_mode="HTML")
        elif message.photo:
            input_text = f"Текст с фото:\n{await extract_text_from_photo(message.photo[-1].file_id)}"
        elif message.document:
            input_text = f"Текст из файла:\n{await extract_text_from_pdf(message.document.file_id)}"

        if not input_text: return await status.edit_text("⚠️ Не удалось разобрать данные.", reply_markup=get_back_kb())

        # РЕЖИМ 1: ВОПРОС
        if curr_state == BotStates.qa_mode.state:
            user["history"].append({"role": "user", "content": input_text})
            res = await asyncio.to_thread(giga.chat, {"messages": [{"role": "system", "content": PROMPT_QA + f"\nГород: {user['city']}"}] + user["history"][-6:]})
            ans = res.choices[0].message.content
            user["history"].append({"role": "assistant", "content": ans})
            await status.edit_text(ans + "\n\n<i>ℹ️ Связь с юристом: /lawyer</i>", reply_markup=get_back_kb(), parse_mode="HTML")

        # РЕЖИМ 2: СОЗДАНИЕ ДОКУМЕНТА (ПРИНУДИТЕЛЬНЫЙ PDF)
        elif curr_state == BotStates.doc_gen_mode.state:
            # ОЧИСТКА: убираем PDF-триггеры для ИИ
            clean_query = re.sub(r'(?i)(pdf|пдф|файл|ворд|word|в формате)', '', input_text).strip()
            
            user["history"].append({"role": "user", "content": clean_query})
            res = await asyncio.to_thread(giga.chat, {"messages": [{"role": "system", "content": PROMPT_DOC_GEN}] + user["history"][-6:]})
            ans = res.choices[0].message.content.strip()
            
            # Если ответ длинный (похож на документ), ПРИНУДИТЕЛЬНО делаем PDF
            if len(ans) > 300 or "ДОГОВОР" in ans.upper() or "ЗАЯВЛЕНИЕ" in ans.upper():
                pdf_name = f"doc_{message.from_user.id}.pdf"
                success = await asyncio.to_thread(generate_pdf_file, ans, pdf_name)
                await status.delete()
                if success and os.path.exists(pdf_name):
                    await message.answer_document(FSInputFile(pdf_name), caption="📄 <b>Ваш документ готов.</b>", reply_markup=get_back_kb(), parse_mode="HTML")
                    os.remove(pdf_name)
                else: await message.answer(f"📄 <b>Ваш документ:</b>\n\n{ans}", reply_markup=get_back_kb(), parse_mode="HTML")
            else:
                user["history"].append({"role": "assistant", "content": ans})
                await status.edit_text(ans, reply_markup=get_back_kb(), parse_mode="HTML")

        # РЕЖИМ 3: РАЗБОР
        elif curr_state == BotStates.doc_analyze_mode.state:
            res = await asyncio.to_thread(giga.chat, {"messages": [{"role": "system", "content": "Ты юрист РФ. Найди риски в тексте документа."}, {"role": "user", "content": input_text}]})
            await status.edit_text(f"🔍 <b>Анализ:</b>\n\n{res.choices[0].message.content}", reply_markup=get_back_kb(), parse_mode="HTML")

    except Exception as e:
        logging.error(f"Error: {e}")
        await status.edit_text("⚠️ Ошибка. Попробуйте еще раз.", reply_markup=get_back_kb())

async def main():
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())