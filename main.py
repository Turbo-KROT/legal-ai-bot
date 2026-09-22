import asyncio
import logging
import os
import re
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
    model="GigaChat-2",
    verify_ssl_certs=False
)

db = {}

# ============ ГЛОБАЛЬНАЯ ЗАЩИТА ОТ ПАДЕНИЙ ============
@dp.errors()
async def global_error_handler(event: ErrorEvent):
    """Глотает ЛЮБЫЕ ошибки, чтобы бот никогда не выключался"""
    logging.critical(f"Критическая ошибка перехвачена и подавлена: {event.exception}")
    # Бот остается жить и работать автономно

# ============ СОСТОЯНИЯ ============
class BotStates(StatesGroup):
    waiting_for_city = State()
    main_menu = State()
    qa_mode = State()
    doc_gen_mode = State()
    doc_analyze_mode = State()

# ============ ЖЕСТКИЕ ПРОМПТЫ ============
PROMPT_QA = """Ты — высококвалифицированный юридический AI-консультант РФ.
Отвечай СТРОГО по структуре:
📌 КРАТКИЙ ВЕРДИКТ:
📖 ПРАВОВОЕ ОБОСНОВАНИЕ: (ссылки на законы, ст. Конституции, ФЗ)
💡 ПЛАН ДЕЙСТВИЙ:
⚠️ РИСКИ И СРОКИ:"""

PROMPT_DOC_GEN = """Ты — профессиональный делопроизводитель РФ. Создаешь юридические документы.
ВНИМАНИЕ! Твой ответ ДОЛЖЕН начинаться с одного из кодовых слов: "ЧАТ:" или "ФАЙЛ:".

ПРАВИЛА ЛОГИКИ СТРОГО:
1. Запрос ПУСТОГО образца -> пиши "ФАЙЛ:" и сразу текст пустого документа (с прочерками).
2. Запрос составить по данным (но данных нет) -> пиши "ЧАТ:" и перечисли, что нужно прислать.
3. Клиент прислал данные -> пиши "ЧАТ:", сформируй анкету из его данных и ОБЯЗАТЕЛЬНО спроси: "Все верно без ошибок?".
4. Клиент ответил "Да" (подтвердил анкету) -> пиши "ФАЙЛ:" и текст готового ЗАПОЛНЕННОГО документа.
5. Запрос документа текстом -> пиши "ЧАТ:" и выдай текст.
Во всех остальных случаях выдачи документов используй "ФАЙЛ:".

ФОРМАТ ДОКУМЕНТА (после ФАЙЛ:):
Первая строка — Название документа. Далее текст. Без лишних слов."""

# ============ ФУНКЦИИ ============
def get_user(user_id: int):
    if user_id not in db: db[user_id] = {"accepted": False, "city": None, "history": []}
    return db[user_id]

def clean_markdown_and_symbols(text: str) -> str:
    text = text.replace('**', '').replace('*', '').replace('__', '').replace('#', '')
    # Оставляем только буквы, цифры и базовую пунктуацию (включая №, §, %)
    text = re.sub(r'[^\w\s.,!?:;()\-—"«»№%§]+', '', text, flags=re.UNICODE)
    return text.strip()

# ============ ВЕРСТКА PDF (ГОСТ) ============
def create_pdf(text, filename):
    clean_text = clean_markdown_and_symbols(text)
    pdf = FPDF()
    pdf.add_page()
    pdf.set_auto_page_break(auto=True, margin=15)
    
    font_path = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
    has_font = os.path.exists(font_path)
    if has_font: pdf.add_font('DejaVu', '', font_path)
    
    lines = clean_text.split('\n')
    title_done = False
    
    for line in lines:
        c_line = line.strip()
        if not c_line:
            pdf.ln(5)
            continue
            
        if not title_done and len(c_line) < 100:
            # ЗАГОЛОВОК: Центр, 14 шрифт
            if has_font: pdf.set_font('DejaVu', '', 14)
            else: pdf.set_font('Arial', '', 14)
            pdf.multi_cell(0, 8, c_line, align='C')
            title_done = True
            pdf.ln(5)
        else:
            # ОСНОВНОЙ ТЕКСТ: Ширина, 12 шрифт, абзацный отступ (6 пробелов), полуторный интервал
            if has_font: pdf.set_font('DejaVu', '', 12)
            else: pdf.set_font('Arial', '', 12)
            pdf.multi_cell(0, 8, "      " + c_line, align='J')

    pdf.output(filename)

# ============ МЕДИА ============
async def transcribe_voice(file_id: str) -> str:
    try:
        file = await bot.get_file(file_id)
        ogg_path, wav_path = f"v_{file_id}.ogg", f"v_{file_id}.wav"
        await bot.download_file(file.file_path, ogg_path)
        proc = await asyncio.create_subprocess_exec("ffmpeg", "-y", "-i", ogg_path, "-ar", "16000", "-ac", "1", wav_path, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        await proc.communicate()
        
        recognizer = sr.Recognizer()
        with sr.AudioFile(wav_path) as source:
            text = recognizer.recognize_google(recognizer.record(source), language="ru-RU")
        
        for p in [ogg_path, wav_path]:
            if os.path.exists(p): os.remove(p)
        return text
    except: return None

async def extract_text_from_photo(file_id: str) -> str:
    try:
        p_path = f"p_{file_id}.jpg"
        await bot.download_file((await bot.get_file(file_id)).file_path, p_path)
        text = await asyncio.to_thread(pytesseract.image_to_string, Image.open(p_path).convert('L'), lang='rus+eng')
        os.remove(p_path)
        return text.strip()
    except: return ""

async def extract_text_from_pdf(file_id: str) -> str:
    try:
        pdf_p = f"d_{file_id}.pdf"
        await bot.download_file((await bot.get_file(file_id)).file_path, pdf_p)
        text = "".join([p.get_text() for p in fitz.open(pdf_p)])
        os.remove(pdf_p)
        return text.strip()
    except: return ""

# ============ МЕНЮ ============
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
    except: pass

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Соглашаюсь", callback_data="accept"), InlineKeyboardButton(text="❌ Не соглашаюсь", callback_data="decline")]
    ])
    await message.answer("📋 <b>Условия использования сервиса</b>\n\nНажимая «Соглашаюсь», вы принимаете условия.", reply_markup=kb, parse_mode="HTML")

@dp.callback_query(F.data == "decline")
async def decline(c: CallbackQuery): await c.message.edit_text("😔 Доступ ограничен. Нажмите /start")

@dp.callback_query(F.data == "accept")
async def accept(c: CallbackQuery, state: FSMContext):
    get_user(c.from_user.id)["accepted"] = True
    await c.message.answer("🏙 <b>Укажите ваш город:</b>", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🚫 Пропустить", callback_data="skip_city")]]), parse_mode="HTML")
    await state.set_state(BotStates.waiting_for_city)

@dp.callback_query(F.data == "skip_city")
async def skip_city(c: CallbackQuery, state: FSMContext):
    get_user(c.from_user.id)["city"] = "Не указан"
    await state.set_state(BotStates.main_menu)
    await c.message.answer("📋 <b>ГЛАВНОЕ МЕНЮ</b>", reply_markup=get_main_menu_kb(), parse_mode="HTML")

@dp.message(BotStates.waiting_for_city)
async def city_input(m: Message, state: FSMContext):
    get_user(m.from_user.id)["city"] = m.text.strip()
    await state.set_state(BotStates.main_menu)
    await m.answer("📋 <b>ГЛАВНОЕ МЕНЮ</b>", reply_markup=get_main_menu_kb(), parse_mode="HTML")

@dp.callback_query(F.data == "back_to_menu")
async def back_to_menu(c: CallbackQuery, state: FSMContext):
    await c.message.delete()
    get_user(c.from_user.id)["history"] = []
    await state.set_state(BotStates.main_menu)
    await c.message.answer("📋 <b>ГЛАВНОЕ МЕНЮ</b>", reply_markup=get_main_menu_kb(), parse_mode="HTML")

@dp.callback_query(F.data == "change_city")
async def ch_city(c: CallbackQuery, state: FSMContext):
    await c.message.edit_text("🏙 Введите название нового города:")
    await state.set_state(BotStates.waiting_for_city)

@dp.callback_query(F.data.startswith("mode_"))
async def modes(c: CallbackQuery, state: FSMContext):
    states = {"mode_qa": BotStates.qa_mode, "mode_doc_gen": BotStates.doc_gen_mode, "mode_doc_analyze": BotStates.doc_analyze_mode}
    await state.set_state(states[c.data])
    txt = "⚖️ Юридический вопрос" if c.data == "mode_qa" else "📝 Создание документа" if c.data == "mode_doc_gen" else "🔍 Разбор документа"
    await c.message.edit_text(f"<b>РЕЖИМ: {txt}</b>\n\nПринимаю текст, голос и фото.", reply_markup=get_back_kb(), parse_mode="HTML")

# ============ ЦЕНТРАЛЬНЫЙ ОБРАБОТЧИК ============
@dp.message(F.text | F.voice | F.photo | F.document)
async def handle_input(message: Message, state: FSMContext):
    user = get_user(message.from_user.id)
    curr_state = await state.get_state()
    
    if not user["accepted"] or curr_state == BotStates.main_menu.state:
        return await message.answer("Пожалуйста, выберите режим в меню 👇", reply_markup=get_main_menu_kb())

    status = await message.answer("⌛ <i>Обрабатываю данные...</i>", parse_mode="HTML")
    input_text = message.text or ""
    
    try:
        if message.voice:
            await status.edit_text("🎙 <i>Распознаю голос...</i>", parse_mode="HTML")
            input_text = await transcribe_voice(message.voice.file_id)
            if input_text: await message.answer(f"🎙 <b>Вы сказали:</b>\n«<i>{input_text}</i>»", parse_mode="HTML")
        elif message.photo:
            await status.edit_text("📸 <i>Сканирую текст с фото...</i>", parse_mode="HTML")
            input_text = await extract_text_from_photo(message.photo[-1].file_id)
        elif message.document:
            await status.edit_text("📄 <i>Читаю PDF-файл...</i>", parse_mode="HTML")
            input_text = await extract_text_from_pdf(message.document.file_id)

        if not input_text:
            return await status.edit_text("⚠️ Не удалось разобрать данные. Попробуйте еще раз.", reply_markup=get_back_kb())

        # РЕЖИМ: ВОПРОСЫ
        if curr_state == BotStates.qa_mode.state:
            user["history"].append({"role": "user", "content": input_text})
            res = giga.chat({"messages": [{"role": "system", "content": PROMPT_QA + f"\nГород: {user['city']}"}] + user["history"][-6:]})
            ans = res.choices[0].message.content
            user["history"].append({"role": "assistant", "content": ans})
            await status.edit_text(ans + "\n\n<i>ℹ️ Связь с юристом: /lawyer</i>", reply_markup=get_back_kb(), parse_mode="HTML")

        # РЕЖИМ: СОЗДАНИЕ ДОКУМЕНТА (ДВУХКОНТУРНЫЙ)
        elif curr_state == BotStates.doc_gen_mode.state:
            user["history"].append({"role": "user", "content": input_text})
            res = giga.chat({"messages": [{"role": "system", "content": PROMPT_DOC_GEN}] + user["history"][-6:]})
            ans = res.choices[0].message.content.strip()
            
            # Контур 1: Нейросеть выдает готовый файл
            if ans.startswith("ФАЙЛ:") or "ДОКУМЕНТ_ГОТОВ" in ans:
                clean_text = ans.replace("ФАЙЛ:", "").replace("ДОКУМЕНТ_ГОТОВ", "").strip()
                pdf_name = f"doc_{message.from_user.id}.pdf"
                
                pdf_success = await asyncio.to_thread(create_pdf, clean_text, pdf_name)
                
                await status.delete()
                if os.path.exists(pdf_name):
                    await message.answer_document(FSInputFile(pdf_name), caption="📄 <b>Ваш документ готов.</b>", reply_markup=get_back_kb(), parse_mode="HTML")
                    os.remove(pdf_name)
                else:
                    await message.answer(f"📄 <b>Документ готов:</b>\n\n{clean_text}", reply_markup=get_back_kb(), parse_mode="HTML")
            
            # Контур 2: Нейросеть общается в чате (анкета, вопросы)
            else:
                ans_chat = ans.replace("ЧАТ:", "").strip()
                user["history"].append({"role": "assistant", "content": ans})
                await status.edit_text(ans_chat, reply_markup=get_back_kb(), parse_mode="HTML")

        # РЕЖИМ: РАЗБОР
        elif curr_state == BotStates.doc_analyze_mode.state:
            res = giga.chat({"messages": [{"role": "system", "content": "Найди правовые риски, скрытые комиссии и ошибки в тексте."}, {"role": "user", "content": input_text}]})
            await status.edit_text(f"🔍 <b>Результат анализа:</b>\n\n{res.choices[0].message.content}", reply_markup=get_back_kb(), parse_mode="HTML")

    except Exception as e:
        logging.error(f"Logic Error: {e}")
        try: await status.edit_text("⚠️ Ошибка сервера. Попробуйте еще раз.", reply_markup=get_back_kb())
        except: pass

async def main():
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())