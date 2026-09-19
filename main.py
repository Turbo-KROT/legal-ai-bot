import asyncio
import logging
import os
import re
import fitz  # PyMuPDF
from PIL import Image
import pytesseract
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    Message, 
    CallbackQuery, 
    InlineKeyboardMarkup, 
    InlineKeyboardButton,
    FSInputFile
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
    model="GigaChat-2",
    verify_ssl_certs=False
)

db = {}

# ============ СОСТОЯНИЯ БОТА ============
class BotStates(StatesGroup):
    waiting_for_city = State()
    main_menu = State()
    qa_mode = State()
    doc_gen_mode = State()
    doc_analyze_mode = State()

# ============ СИСТЕМНЫЕ ПРОМПТЫ ============
PROMPT_QA = """Ты — высококвалифицированный юридический AI-консультант по праву РФ.
Твоя цель — дать глубокий, максимально информативный ответ.
В вопросах о полиции ОБЯЗАТЕЛЬНО ссылайся на ФЗ № 3-ФЗ «О полиции» (ст. 8 — публичность).
Отвечай СТРОГО по структуре:
📌 КРАТКИЙ ВЕРДИКТ:
📖 ПРАВОВОЕ ОБОСНОВАНИЕ:
💡 ПЛАН ДЕЙСТВИЙ:
⚠️ РИСКИ И СРОКИ:"""

PROMPT_DOC_GEN = """Ты — опытный юрист РФ. Составь текст документа.
Если данных нет — сделай ПУСТОЙ ШАБЛОН с (____).
Если данные есть — заполни их.
ОБЯЗАТЕЛЬНО начни ответ с кодового слова ДОКУМЕНТ_ГОТОВ на первой строке.
ВЫВЕДИ ТОЛЬКО ТЕКСТ ДОКУМЕНТА без комментариев."""

# ============ ВПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ============
def get_user(user_id: int):
    if user_id not in db:
        db[user_id] = {"accepted": False, "city": None, "history": []}
    return db[user_id]

def sanitize_for_pdf(text: str) -> str:
    """Максимально жесткая очистка текста для PDF"""
    # Убираем Markdown
    text = text.replace('**', '').replace('*', '').replace('__', '').replace('#', '')
    
    # Убираем эмодзи
    text = re.sub(r'[^\x00-\x7F\u0400-\u04FF\s.,!?:;()\-—"«»]+', '', text)
    
    # Заменяем специфические символы на стандартные
    reps = {
        '—': '-', '–': '-', '−': '-', '…': '...', 
        '«': '"', '»': '"', '„': '"', '“': '"', '”': '"',
        '\xa0': ' ', '\t': '    '
    }
    for search, replace in reps.items():
        text = text.replace(search, replace)
    
    return text.strip()

def create_pdf(text, filename):
    clean_text = sanitize_for_pdf(text)
    pdf = FPDF()
    pdf.add_page()
    pdf.set_auto_page_break(auto=True, margin=15)
    
    # Шрифт
    font_path = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
    f_name = "Arial"
    if os.path.exists(font_path):
        pdf.add_font('DejaVu', '', font_path)
        f_name = 'DejaVu'
    
    pdf.set_font(f_name, '', 11)
    
    lines = clean_text.split('\n')
    is_first_line = True
    
    for line in lines:
        l = line.strip()
        if not l:
            pdf.ln(5)
            continue
            
        # Если это первая строка (заголовок) - центрируем
        if is_first_line and len(l) < 100:
            pdf.set_font(f_name, '', 14)
            pdf.multi_cell(0, 10, l, align='C')
            pdf.set_font(f_name, '', 11)
            pdf.ln(5)
            is_first_line = False
        else:
            pdf.multi_cell(0, 7, l, align='L')
            is_first_line = False
            
    pdf.output(filename)

# ============ ОБРАБОТКА МЕДИА ============
def recognize_audio(wav_path):
    recognizer = sr.Recognizer()
    with sr.AudioFile(wav_path) as source:
        audio_data = recognizer.record(source)
        return recognizer.recognize_google(audio_data, language="ru-RU")

async def transcribe_voice(file_id: str) -> str:
    file = await bot.get_file(file_id)
    ogg_path = f"v_{file_id}.ogg"
    wav_path = f"v_{file_id}.wav"
    try:
        await bot.download_file(file.file_path, ogg_path)
        proc = await asyncio.create_subprocess_exec("ffmpeg", "-y", "-i", ogg_path, "-ar", "16000", "-ac", "1", wav_path, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        await proc.communicate()
        return await asyncio.to_thread(recognize_audio, wav_path)
    except: return None
    finally:
        for p in [ogg_path, wav_path]:
            if os.path.exists(p): os.remove(p)

async def extract_text_from_photo(file_id: str) -> str:
    file = await bot.get_file(file_id)
    p_path = f"p_{file_id}.jpg"
    await bot.download_file(file.file_path, p_path)
    try:
        img = Image.open(p_path).convert('L')
        return pytesseract.image_to_string(img, lang='rus+eng').strip()
    except: return ""
    finally:
        if os.path.exists(p_path): os.remove(p_path)

async def extract_text_from_pdf(file_id: str) -> str:
    file = await bot.get_file(file_id)
    pdf_p = f"d_{file_id}.pdf"
    await bot.download_file(file.file_path, pdf_p)
    text = ""
    try:
        doc = fitz.open(pdf_p)
        for page in doc: text += page.get_text()
        return text.strip()
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
        await show_main_menu(message.from_user.id, state)
        return
    welcome = (
        "⚖️ <b>Правовой AI-Консультант</b>\n\n"
        "Профессиональный сервис экспресс-анализа юридических ситуаций, "
        "проверки документов и подготовки правовых решений на базе искусственного интеллекта.\n\n"
        "🎙 <i>Принимаю текстовые и голосовые сообщения!</i>\n"
        "⚡️ <i>Анализ ситуаций за секунды • Работа 24/7</i>"
    )
    await message.answer(welcome, parse_mode="HTML")
    await asyncio.sleep(2.5)
    try:
        await message.answer_document(FSInputFile("agreement.pdf"), caption="📄 Соглашение")
        await message.answer_document(FSInputFile("faq.pdf"), caption="❓ FAQ")
    except: pass
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✅ Соглашаюсь", callback_data="accept"), InlineKeyboardButton(text="❌ Не соглашаюсь", callback_data="decline")]])
    await message.answer("📋 <b>Условия использования сервиса</b>", reply_markup=kb, parse_mode="HTML")

@dp.callback_query(F.data == "decline")
async def process_decline(callback: CallbackQuery):
    await callback.message.edit_text("😔 Доступ ограничен. Напишите /start")

@dp.callback_query(F.data == "accept")
async def process_accept(callback: CallbackQuery, state: FSMContext):
    get_user(callback.from_user.id)["accepted"] = True
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🚫 Пропустить", callback_data="skip_city")]])
    await callback.message.answer("🏙 <b>Укажите ваш город:</b>", reply_markup=kb, parse_mode="HTML")
    await state.set_state(BotStates.waiting_for_city)

@dp.callback_query(F.data == "skip_city")
async def skip_city(callback: CallbackQuery, state: FSMContext):
    get_user(callback.from_user.id)["city"] = "Не указан"
    await show_main_menu(callback.from_user.id, state)

@dp.message(BotStates.waiting_for_city)
async def city_input(message: Message, state: FSMContext):
    get_user(message.from_user.id)["city"] = message.text.strip()
    await show_main_menu(message.from_user.id, state)

async def show_main_menu(user_id: int, state: FSMContext):
    db[user_id]["history"] = []
    await bot.send_message(user_id, "📋 <b>ГЛАВНОЕ МЕНЮ</b>\n\nВыберите раздел:", reply_markup=get_main_menu_kb(), parse_mode="HTML")
    await state.set_state(BotStates.main_menu)

@dp.callback_query(F.data == "back_to_menu")
async def back_to_menu_btn(callback: CallbackQuery, state: FSMContext):
    await callback.message.delete()
    await show_main_menu(callback.from_user.id, state)

@dp.callback_query(F.data == "mode_qa")
async def mode_qa(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_text("⚖️ <b>РЕЖИМ: Юридический вопрос</b>", reply_markup=get_back_kb(), parse_mode="HTML")
    await state.set_state(BotStates.qa_mode)

@dp.callback_query(F.data == "mode_doc_gen")
async def mode_doc_gen(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_text("📝 <b>РЕЖИМ: Создание документа</b>", reply_markup=get_back_kb(), parse_mode="HTML")
    await state.set_state(BotStates.doc_gen_mode)

@dp.callback_query(F.data == "mode_doc_analyze")
async def mode_doc_analyze(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_text("🔍 <b>РЕЖИМ: Разбор документа</b>", reply_markup=get_back_kb(), parse_mode="HTML")
    await state.set_state(BotStates.doc_analyze_mode)

@dp.callback_query(F.data == "change_city")
async def ch_city(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_text("🏙 Введите город:")
    await state.set_state(BotStates.waiting_for_city)

@dp.message(F.text | F.voice | F.photo | F.document)
async def handle_input(message: Message, state: FSMContext):
    user = get_user(message.from_user.id)
    curr_state = await state.get_state()
    if not user["accepted"] or curr_state == BotStates.main_menu.state:
        await message.answer("Выберите режим в меню 👇", reply_markup=get_main_menu_kb())
        return

    status = await message.answer("⌛ <i>Обрабатываю...</i>", parse_mode="HTML")
    input_text = ""
    
    if message.text: input_text = message.text
    elif message.voice:
        input_text = await transcribe_voice(message.voice.file_id)
        if input_text: await message.answer(f"🎙 <b>Вы сказали:</b>\n<i>{input_text}</i>", parse_mode="HTML")
    elif message.photo:
        input_text = await extract_text_from_photo(message.photo[-1].file_id)
        if input_text: input_text = f"Текст с фото:\n{input_text}"
    elif message.document:
        input_text = await extract_text_from_pdf(message.document.file_id)
        if input_text: input_text = f"Текст из файла:\n{input_text}"

    if not input_text:
        await status.edit_text("⚠️ Не удалось получить текст. Попробуйте еще раз.", reply_markup=get_back_kb())
        return

    try:
        if curr_state == BotStates.qa_mode.state:
            user["history"].append({"role": "user", "content": input_text})
            res = giga.chat({"messages": [{"role": "system", "content": PROMPT_QA + f"\nГород: {user['city']}"}] + user["history"][-6:]})
            ans = res.choices[0].message.content
            user["history"].append({"role": "assistant", "content": ans})
            await status.edit_text(ans + "\n\n<i>/lawyer - связь с юристом</i>", reply_markup=get_back_kb(), parse_mode="HTML")

        elif curr_state == BotStates.doc_gen_mode.state:
            res = giga.chat({"messages": [{"role": "system", "content": PROMPT_DOC_GEN}, {"role": "user", "content": input_text}]})
            ans = res.choices[0].message.content
            if "ДОКУМЕНТ_ГОТОВ" in ans:
                pdf_name = f"doc_{message.from_user.id}.pdf"
                create_pdf(ans.replace("ДОКУМЕНТ_ГОТОВ", "").strip(), pdf_name)
                await status.delete()
                await message.answer_document(FSInputFile(pdf_name), caption="📄 <b>Ваш документ готов.</b>", reply_markup=get_back_kb(), parse_mode="HTML")
                os.remove(pdf_name)
            else:
                await status.edit_text(ans, reply_markup=get_back_kb())

        elif curr_state == BotStates.doc_analyze_mode.state:
            res = giga.chat({"messages": [{"role": "system", "content": "Ты юрист РФ. Проанализируй текст, найди риски и ошибки."}, {"role": "user", "content": input_text}]})
            await status.edit_text(f"🔍 <b>Анализ:</b>\n\n{res.choices[0].message.content}", reply_markup=get_back_kb(), parse_mode="HTML")

    except Exception as e:
        logging.error(f"Error: {e}")
        await status.edit_text("⚠️ Ошибка AI. Попробуйте позже.", reply_markup=get_back_kb())

async def main():
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())