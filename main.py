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

class BotStates(StatesGroup):
    waiting_for_city = State()
    main_menu = State()
    qa_mode = State()
    doc_gen_mode = State()
    doc_analyze_mode = State()

# ============ СИСТЕМНЫЕ ПРОМПТЫ ============
PROMPT_QA = """Ты — юридический AI-консультант РФ. Отвечай СТРОГО по структуре:
📌 КРАТКИЙ ВЕРДИКТ:
📖 ПРАВОВОЕ ОБОСНОВАНИЕ:
💡 ПЛАН ДЕЙСТВИЙ:
⚠️ РИСКИ И СРОКИ:"""

PROMPT_DOC_GEN = """Ты — делопроизводитель РФ.
Если пользователь прислал ТЕКСТ ОБРАЗЦА (из фото/файла) и свои ДАННЫЕ — составь готовый документ на основе образца, вставив данные.
Если данных нет — напиши текстом, что именно нужно заполнить.
Если готов выдать документ, начни с кодового слова ДОКУМЕНТ_ГОТОВ."""

# ============ ФУНКЦИИ ОБРАБОТКИ ФАЙЛОВ (OCR) ============
async def extract_text_from_photo(file_id: str) -> str:
    file = await bot.get_file(file_id)
    photo_path = f"photo_{file_id}.jpg"
    await bot.download_file(file.file_path, photo_path)
    
    try:
        text = await asyncio.to_thread(pytesseract.image_to_string, Image.open(photo_path), lang='rus')
        return text
    finally:
        if os.path.exists(photo_path): os.remove(photo_path)

async def extract_text_from_pdf(file_id: str) -> str:
    file = await bot.get_file(file_id)
    pdf_path = f"doc_{file_id}.pdf"
    await bot.download_file(file.file_path, pdf_path)
    
    text = ""
    try:
        doc = fitz.open(pdf_path)
        for page in doc:
            text += page.get_text()
        return text
    finally:
        if os.path.exists(pdf_path): os.remove(pdf_path)

# ============ ВЕРСТКА PDF ============
def create_pdf(text, filename):
    pdf = FPDF()
    pdf.add_page()
    font_path = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
    if os.path.exists(font_path):
        pdf.add_font('DejaVu', '', font_path, uni=True)
        pdf.set_font('DejaVu', '', 11)
    else:
        pdf.set_font("Arial", size=11)
        
    lines = text.strip().split('\n')
    title_processed = False
    for line in lines:
        clean_line = line.strip()
        if not clean_line:
            pdf.ln(5)
            continue
        if not title_processed:
            if os.path.exists(font_path): pdf.set_font('DejaVu', '', 14)
            pdf.multi_cell(0, 8, clean_line, align='C')
            title_processed = True
            pdf.ln(5)
        else:
            if os.path.exists(font_path): pdf.set_font('DejaVu', '', 11)
            pdf.multi_cell(0, 6, "    " + clean_line, align='J')
    pdf.output(filename)

# ============ ГОЛОСОВОЙ МОДУЛЬ ============
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
        proc = await asyncio.create_subprocess_exec("ffmpeg", "-y", "-i", ogg_path, wav_path, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        await proc.communicate()
        return await asyncio.to_thread(recognize_audio, wav_path)
    except: return None
    finally:
        for p in [ogg_path, wav_path]:
            if os.path.exists(p): os.remove(p)

def get_user(user_id: int):
    if user_id not in db:
        db[user_id] = {"accepted": False, "city": None, "history": []}
    return db[user_id]

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
    await message.answer("⚖️ <b>Правовой AI-Консультант</b>\n\n🎙 <i>Принимаю текст, голос и фото документов!</i>", parse_mode="HTML")
    await asyncio.sleep(2.5)
    try:
        await message.answer_document(FSInputFile("agreement.pdf"), caption="📄 Соглашение")
        await message.answer_document(FSInputFile("faq.pdf"), caption="❓ FAQ")
    except: pass
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✅ Соглашаюсь", callback_data="accept")]])
    await message.answer("📋 Примите условия использования:", reply_markup=kb, parse_mode="HTML")

@dp.callback_query(F.data == "accept")
async def process_accept(callback: CallbackQuery, state: FSMContext):
    get_user(callback.from_user.id)["accepted"] = True
    await callback.message.answer("🏙 Укажите ваш город (или нажмите пропустить):", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🚫 Пропустить", callback_data="skip_city")]]))
    await state.set_state(BotStates.waiting_for_city)

@dp.callback_query(F.data == "skip_city")
async def skip_city(callback: CallbackQuery, state: FSMContext):
    get_user(callback.from_user.id)["city"] = "Не указан"
    await show_main_menu(callback.from_user.id, state)

@dp.message(BotStates.waiting_for_city)
async def city_input(message: Message, state: FSMContext):
    get_user(message.from_user.id)["city"] = message.text
    await show_main_menu(message.from_user.id, state)

async def show_main_menu(user_id: int, state: FSMContext):
    db[user_id]["history"] = []
    await bot.send_message(user_id, "📋 <b>ГЛАВНОЕ МЕНЮ</b>\n\nВыберите раздел:", reply_markup=get_main_menu_kb(), parse_mode="HTML")
    await state.set_state(BotStates.main_menu)

@dp.callback_query(F.data == "mode_qa")
async def mode_qa(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_text("⚖️ <b>РЕЖИМ: Вопрос</b>\n\nЗадайте вопрос текстом или голосом 🎙", reply_markup=get_back_kb(), parse_mode="HTML")
    await state.set_state(BotStates.qa_mode)

@dp.callback_query(F.data == "mode_doc_gen")
async def mode_doc_gen(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_text("📝 <b>РЕЖИМ: Создание</b>\n\nПришлите <b>фото или PDF</b> образца, либо напишите название документа.", reply_markup=get_back_kb(), parse_mode="HTML")
    await state.set_state(BotStates.doc_gen_mode)

@dp.callback_query(F.data == "mode_doc_analyze")
async def mode_doc_analyze(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_text("🔍 <b>РЕЖИМ: Разбор</b>\n\nПришлите <b>фото или PDF</b> документа для анализа рисков.", reply_markup=get_back_kb(), parse_mode="HTML")
    await state.set_state(BotStates.doc_analyze_mode)

@dp.callback_query(F.data.in_(["back_to_menu", "change_city"]))
async def nav(callback: CallbackQuery, state: FSMContext):
    if callback.data == "change_city":
        await callback.message.edit_text("🏙 Введите новый город:")
        await state.set_state(BotStates.waiting_for_city)
    else:
        await callback.message.delete()
        await show_main_menu(callback.from_user.id, state)

# ============ ГЛАВНЫЙ ОБРАБОТЧИК (ТЕКСТ, ГОЛОС, ФОТО, ФАЙЛ) ============
@dp.message(F.text | F.voice | F.photo | F.document)
async def handle_input(message: Message, state: FSMContext):
    user = get_user(message.from_user.id)
    curr_state = await state.get_state()
    
    if not user["accepted"] or curr_state == BotStates.main_menu.state:
        await message.answer("Пожалуйста, выберите режим в меню 👇", reply_markup=get_main_menu_kb())
        return

    status = await message.answer("⌛ <i>Обрабатываю данные...</i>", parse_mode="HTML")
    
    input_text = ""
    # 1. Если текст
    if message.text:
        input_text = message.text
    # 2. Если голос
    elif message.voice:
        await status.edit_text("🎙 <i>Распознаю голос...</i>", parse_mode="HTML")
        input_text = await transcribe_voice(message.voice.file_id)
    # 3. Если фото
    elif message.photo:
        await status.edit_text("📸 <i>Сканирую текст с фото...</i>", parse_mode="HTML")
        input_text = f"[ТЕКСТ С ВАШЕГО ФОТО]:\n{await extract_text_from_photo(message.photo[-1].file_id)}"
    # 4. Если PDF
    elif message.document and message.document.mime_type == "application/pdf":
        await status.edit_text("📄 <i>Читаю PDF-файл...</i>", parse_mode="HTML")
        input_text = f"[ТЕКСТ ИЗ ВАШЕГО PDF]:\n{await extract_text_from_pdf(message.document.file_id)}"
    
    if not input_text:
        await status.edit_text("⚠️ Не удалось получить данные. Попробуйте еще раз.")
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
                pdf_name = f"res_{message.from_user.id}.pdf"
                create_pdf(ans.replace("ДОКУМЕНТ_ГОТОВ", "").strip(), pdf_name)
                await status.delete()
                await message.answer_document(FSInputFile(pdf_name), caption="📄 Документ заполнен и готов.", reply_markup=get_back_kb())
                os.remove(pdf_name)
            else:
                await status.edit_text(ans, reply_markup=get_back_kb())

        elif curr_state == BotStates.doc_analyze_mode.state:
            res = giga.chat({"messages": [{"role": "system", "content": "Ты юрист. Найди риски и ошибки в тексте документа."}, {"role": "user", "content": input_text}]})
            await status.edit_text(f"🔍 <b>Анализ:</b>\n\n{res.choices[0].message.content}", reply_markup=get_back_kb(), parse_mode="HTML")

    except Exception as e:
        logging.error(f"Error: {e}")
        await status.edit_text("⚠️ Ошибка AI. Попробуйте кратко описать задачу текстом.", reply_markup=get_back_kb())

async def main():
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())