import asyncio
import logging
import os
import re
import sqlite3
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
    prof_consult_mode = State()

# ============ KEYWORD-ПАТТЕРНЫ ДЛЯ ПРИНУДИТЕЛЬНОЙ ГЕНЕРАЦИИ PDF ============
FORCE_PDF_PATTERNS = re.compile(
    r"(?i)("
    r"образец|бланк|шаблон|пример\s+документ|пример\s+договор|пример\s+заявлени|"
    r"пришли\s+(документ|договор|заявлени|дарствен|доверенност|иск|претензи|контракт)|"
    r"пришли\s+образец|пришли\s+бланк|пришли\s+шаблон|"
    r"составь|составить|сформируй|сформировать|подготовь|подготовить|"
    r"сделай\s+(документ|договор|заявлени|дарствен|доверенност|иск|претензи)|"
    r"напиши\s+(документ|договор|заявлени|дарствен|доверенност|иск|претензи)|"
    r"нужен\s+(договор|документ|бланк|образец|шаблон|заявлени|иск|претензи)|"
    r"нужна\s+(дарствен|доверенност|расписка|жалоба|претензи)|"
    r"хочу\s+(договор|документ|бланк|образец|дарствен|доверенност|иск|претензи)|"
    r"договор\s+купли|договор\s+аренд|договор\s+займ|договор\s+подряд|"
    r"трудовой\s+договор|договор\s+оказани|договор\s+поставк|"
    r"дарственн|доверенност|исковое\s+заявлени|претензи|расписк|"
    r"акт\s+приема|акт\s+выполнен|приказ\s+об|заявлени\s+на\s+(отпуск|увольнени|прием)"
    r")"
)

REFUSAL_PATTERNS = re.compile(
    r"(?i)("
    r"не\s+могу\s+(отправ|создат|сгенерир|сформир|прислат|загруз)|"
    r"не\s+имею\s+возможност.{0,20}(файл|pdf|документ)|"
    r"являюсь\s+текстов|текстов\w+\s+модел|"
    r"скопируйте\s+в\s+(word|ворд)|"
    r"не\s+поддерживаю\s+(файл|pdf|формат)|"
    r"обратитесь\s+к\s+юрист|"
    r"я\s+не\s+в\s+состояни.{0,20}(создат|сформир|отправ)"
    r")"
)

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

PROMPT_DOC_INTENT_DETECTOR = """Ты — классификатор намерений пользователя в системе генерации юридических документов.

Проанализируй сообщение пользователя и ответь СТРОГО ОДНИМ СЛОВОМ из списка:

NEED_PDF     — пользователь просит образец, бланк, шаблон, конкретный документ (договор, заявление, иск, доверенность, дарственную, расписку, акт и т.п.) или уже предоставил все данные для составления документа.
NEED_CLARIFY — пользователь хочет документ, но не указал ключевые данные (стороны, предмет, условия) и нужно задать уточняющие вопросы перед составлением.
NEED_TEXT    — пользователь явно просит ответить текстом в чат (написал "текстом", "в чат", "без PDF").
REDIRECT_QA  — пользователь задаёт общий правовой вопрос, не связанный с составлением документа.

ВАЖНО: отвечай ТОЛЬКО одним словом из списка выше. Никаких пояснений."""

PROMPT_DOC_GENERATOR = """Ты — профессиональный юрист-делопроизводитель РФ. Твоя единственная задача — написать полный текст юридического документа.

АБСОЛЮТНЫЕ ПРАВИЛА (нарушение недопустимо):
1. Начинай СРАЗУ с названия документа (например: ДОГОВОР КУПЛИ-ПРОДАЖИ ТРАНСПОРТНОГО СРЕДСТВА). Без приветствий.
2. ЗАПРЕЩЕНО писать фразы: "я не могу", "я не умею", "скопируйте в Word", "обратитесь к юристу", "как языковая модель", "как текстовый помощник".
3. Если данные не указаны — используй прочерки __________ в полях для заполнения.
4. Соблюдай структуру документа по стандартам РФ: название, дата, место, преамбула, предмет, права и обязанности, ответственность, реквизиты сторон.
5. Пиши полный документ, не сокращай и не обрывай на середине.
6. НИКАКИХ markdown-символов: никаких **, *, #, _, ~~. Только чистый текст."""

PROMPT_DOC_CLARIFY = """Ты — профессиональный юрист-делопроизводитель РФ. Тебе нужно составить документ, но сначала уточни детали.

Задай пользователю КОНКРЕТНЫЕ вопросы, необходимые для заполнения документа (ФИО сторон, паспортные данные, предмет договора, стоимость, сроки и т.п.).
Спрашивай вежливо и по-деловому. Не пиши лишнего текста — только вопросы."""

# ============ ВПОМОГАТЕЛЬНЫЕ ФУНКЦИИ И НАДЕЖНЫЙ PDF-ГЕНЕРАТОР ============
def download_font():
    font_file = "DejaVuSans.ttf"
    if not os.path.exists(font_file):
        try:
            logging.info("Загрузка шрифта DejaVuSans.ttf...")
            urllib.request.urlretrieve(
                "https://github.com/matomo-org/travis-scripts/raw/master/fonts/DejaVuSans.ttf",
                font_file
            )
        except Exception as e:
            logging.error(f"Ошибка загрузки шрифта: {e}")
    return font_file if os.path.exists(font_file) else None

def sanitize_text_for_pdf(text: str) -> str:
    """100% очистка текста от спецсимволов и разметки перед FPDF"""
    text = re.sub(r'\*\*(.*?)\*\*', r'\1', text)
    text = re.sub(r'\*(.*?)\*', r'\1', text)
    text = re.sub(r'#(.*?)\n', r'\1\n', text)
    text = text.replace('**', '').replace('*', '').replace('#', '').replace('_', ' ')
    
    # Удаляем любые эмодзи
    emoji_pattern = re.compile(
        "["
        "\U00010000-\U0010FFFF"
        "\u2600-\u27BF\u2300-\u23FF\u2B00-\u2BFF\u2190-\u21FF"
        "]+",
        flags=re.UNICODE
    )
    text = emoji_pattern.sub("", text)
    
    # Замена спецтипографики на базовые символы
    replacements = {
        '—': '-', '–': '-', '…': '...', '«': '"', '»': '"',
        '“': '"', '”': '"', '‘': "'", '’': "'", '\xa0': ' ',
        '\t': '    ', '№': 'N', '§': 'параграф '
    }
    for orig, repl in replacements.items():
        text = text.replace(orig, repl)
        
    # Оставляем только кириллицу, латиницу, цифры и базовую пунктуацию
    allowed_pattern = re.compile(r'[^a-zA-Zа-яА-Я0-9\s.,!?:;()"\'-/\n\\]')
    text = allowed_pattern.sub('', text)
    
    return text.strip()

def generate_pdf_file(text: str, filename: str) -> bool:
    """Безопасная генерация PDF, гарантирующая создание файла без падений"""
    try:
        clean_text = sanitize_text_for_pdf(text)
        if not clean_text:
            logging.error("Текст для PDF пуст после очистки!")
            return False

        pdf = FPDF(orientation='P', unit='mm', format='A4')
        pdf.set_margins(20, 20, 10)
        pdf.add_page()
        pdf.set_auto_page_break(auto=True, margin=20)

        font_path = download_font()
        if not font_path or not os.path.exists(font_path):
            font_candidates = [
                "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
                "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
                "DejaVuSans.ttf"
            ]
            font_path = next((f for f in font_candidates if os.path.exists(f)), None)

        font_name = "Arial"
        if font_path and os.path.exists(font_path):
            try:
                pdf.add_font('DejaVu', '', font_path)
                font_name = 'DejaVu'
            except Exception as fe:
                logging.error(f"Error adding DejaVu font: {fe}")

        pdf.set_font(font_name, size=11)

        lines = clean_text.split('\n')
        title_done = False

        for line in lines:
            c_line = line.strip()

            if not c_line:
                pdf.ln(3)
                continue

            if font_name == "Arial":
                c_line = c_line.encode('latin-1', 'replace').decode('latin-1')

            if not title_done and len(c_line) < 120:
                if font_name == "DejaVu":
                    pdf.set_font('DejaVu', '', 13)
                else:
                    pdf.set_font("Arial", size=13)
                pdf.multi_cell(0, 7, c_line, align='C')
                pdf.ln(4)
                title_done = True
                if font_name == "DejaVu":
                    pdf.set_font('DejaVu', '', 11)
                else:
                    pdf.set_font("Arial", size=11)
            else:
                pdf.multi_cell(0, 6, "      " + c_line, align='J')

        pdf.output(filename)
        logging.info(f"PDF успешно создан: {filename}")
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
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-y", "-i", ogg_path, "-ar", "16000", "-ac", "1", wav_path,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL
        )
        await proc.communicate()
        return await asyncio.to_thread(recognize_audio, wav_path)
    except Exception as e:
        logging.error(f"Voice Error: {e}")
        return None
    finally:
        for p in [ogg_path, wav_path]:
            if os.path.exists(p):
                os.remove(p)

def process_ocr_photo(photo_path: str) -> str:
    img = Image.open(photo_path).convert('L')
    try:
        return pytesseract.image_to_string(img, lang='rus+eng').strip()
    except Exception:
        return pytesseract.image_to_string(img).strip()

async def extract_text_from_photo(file_id: str) -> str:
    file = await bot.get_file(file_id)
    photo_path = f"photo_{file_id}.jpg"
    await bot.download_file(file.file_path, photo_path)
    try:
        return await asyncio.to_thread(process_ocr_photo, photo_path)
    except Exception:
        return ""
    finally:
        if os.path.exists(photo_path):
            os.remove(photo_path)

def process_pdf_extract(pdf_path: str) -> str:
    text = ""
    doc = fitz.open(pdf_path)
    for page in doc:
        text += page.get_text()
    return text.strip()

async def extract_text_from_pdf(file_id: str) -> str:
    file = await bot.get_file(file_id)
    pdf_path = f"doc_{file_id}.pdf"
    await bot.download_file(file.file_path, pdf_path)
    try:
        return await asyncio.to_thread(process_pdf_extract, pdf_path)
    except Exception:
        return ""
    finally:
        if os.path.exists(pdf_path):
            os.remove(pdf_path)

# ============ КЛАВИАТУРЫ ============
def agree_kb():
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Соглашаюсь", callback_data="accept"),
        InlineKeyboardButton(text="❌ Не соглашаюсь", callback_data="decline")
    ]])

def main_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⚖️ Задать юридический вопрос", callback_data="mode_qa")],
        [InlineKeyboardButton(text="📝 Создание документа", callback_data="mode_doc_gen")],
        [InlineKeyboardButton(text="🔍 Разбор документа", callback_data="mode_doc_analyze")],
        [InlineKeyboardButton(text="💼 Профильная консультация", callback_data="mode_prof")],
        [InlineKeyboardButton(text="📍 Сменить город", callback_data="change_city")]
    ])

def get_back_kb():
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🔙 В главное меню", callback_data="back_to_menu")
    ]])

def redirect_kb(m_type):
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="➡️ Перейти в нужный раздел", callback_data=m_type)
    ]])

# ============ СТАРТ И ИНТЕРФЕЙС ============
async def send_agreement(message_obj):
    welcome = (
        "⚖️ <b>Правовой AI-Консультант</b>\n\n"
        "Профессиональный сервис экспресс-анализа юридических ситуаций, "
        "проверки документов и подготовки правовых решений на базе искусственного интеллекта.\n\n"
        "🎙 <i>Принимаю текстовые и голосовые сообщения!</i>\n"
        "⚡️ <i>Анализ ситуаций за секунды • Работа 24/7</i>"
    )
    await message_obj.answer(welcome, parse_mode="HTML")
    await asyncio.sleep(2.5)
    try:
        await message_obj.answer_document(FSInputFile("agreement.pdf"), caption="📄 Пользовательское соглашение")
        await message_obj.answer_document(FSInputFile("faq.pdf"), caption="❓ Часто задаваемые вопросы (FAQ)")
    except Exception as e:
        logging.error(f"PDF Send Error: {e}")
    await message_obj.answer(
        "📋 <b>Условия использования сервиса</b>\n\nНажимая «Соглашаюсь», вы принимаете условия.",
        reply_markup=agree_kb(),
        parse_mode="HTML"
    )

@dp.message(CommandStart())
async def cmd_start(m: Message, state: FSMContext):
    u = get_db_user(m.from_user.id)
    if u["accepted"]:
        await m.answer("👋 <b>С возвращением!</b>", parse_mode="HTML")
        user_history[m.from_user.id] = {'h': [], 'rc': 0}
        await state.set_state(BotStates.main_menu)
        return await m.answer(
            "📋 <b>ГЛАВНОЕ МЕНЮ</b>\n\nВыберите нужный раздел (доступен текст и голос 🎙):",
            reply_markup=main_kb(),
            parse_mode="HTML"
        )
    await send_agreement(m)

@dp.callback_query(F.data == "decline")
async def process_decline(c: CallbackQuery):
    await c.message.edit_text(
        "😔 Без принятия условий доступ к сервису ограничен. Вы можете передумать в любой момент:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="📄 Вернуться к соглашению", callback_data="return_agree")
        ]]),
        parse_mode="HTML"
    )
    await c.answer()

@dp.callback_query(F.data == "return_agree")
async def ret_agree(c: CallbackQuery):
    await c.message.delete()
    await send_agreement(c.message)
    await c.answer()

@dp.callback_query(F.data == "accept")
async def process_accept(c: CallbackQuery, state: FSMContext):
    set_db_accept(c.from_user.id)
    await c.message.edit_text("✅ <b>Условия использования приняты.</b>", parse_mode="HTML")
    await c.message.answer(
        "🏙 <b>Укажите ваш город</b> (для учета региональных законов):",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="🚫 Не указывать (Пропустить)", callback_data="skip_city")
        ]]),
        parse_mode="HTML"
    )
    await state.set_state(BotStates.waiting_for_city)
    await state.update_data(prompt_msg_id=c.message.message_id + 1)
    await c.answer()

@dp.callback_query(F.data == "skip_city")
async def skip_city(c: CallbackQuery, state: FSMContext):
    set_db_city(c.from_user.id, "Не указан")
    await c.message.delete()
    user_history[c.from_user.id] = {'h': [], 'rc': 0}
    await state.set_state(BotStates.main_menu)
    await c.message.answer(
        "📍 Город не указан.\n\n📋 <b>ГЛАВНОЕ МЕНЮ</b>\n\nВыберите нужный раздел (доступен текст и голос 🎙):",
        reply_markup=main_kb(),
        parse_mode="HTML"
    )
    await c.answer()

@dp.message(BotStates.waiting_for_city)
async def city_in(m: Message, state: FSMContext):
    city_val = m.text.strip()
    set_db_city(m.from_user.id, city_val)
    data = await state.get_data()
    if 'prompt_msg_id' in data:
        try:
            await bot.delete_message(m.chat.id, data['prompt_msg_id'])
        except Exception:
            pass
    try:
        await m.delete()
    except Exception:
        pass
    user_history[m.from_user.id] = {'h': [], 'rc': 0}
    await state.set_state(BotStates.main_menu)
    await m.answer(
        f"📍 Ваш город: <b>{city_val}</b>\n\n📋 <b>ГЛАВНОЕ МЕНЮ</b>\n\nВыберите нужный раздел (доступен текст и голос 🎙):",
        reply_markup=main_kb(),
        parse_mode="HTML"
    )

@dp.callback_query(F.data == "change_city")
async def ch_city(c: CallbackQuery, state: FSMContext):
    msg = await c.message.edit_text("🏙 Введите название нового города:")
    await state.set_state(BotStates.waiting_for_city)
    await state.update_data(prompt_msg_id=msg.message_id)
    await c.answer()

@dp.callback_query(F.data == "back_to_menu")
async def back_menu(c: CallbackQuery, state: FSMContext):
    user_history[c.from_user.id] = {'h': [], 'rc': 0}
    await state.set_state(BotStates.main_menu)

    msg_text = c.message.text or c.message.caption or ""
    if "РЕЖИМ:" in msg_text:
        await c.message.edit_text(
            "📋 <b>ГЛАВНОЕ МЕНЮ</b>\n\nВыберите нужный раздел (доступен текст и голос 🎙):",
            reply_markup=main_kb(),
            parse_mode="HTML"
        )
    else:
        try:
            await c.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
        await c.message.answer(
            "📋 <b>ГЛАВНОЕ МЕНЮ</b>\n\nВыберите нужный раздел (доступен текст и голос 🎙):",
            reply_markup=main_kb(),
            parse_mode="HTML"
        )
    await c.answer()

@dp.callback_query(F.data.startswith("mode_"))
async def modes(c: CallbackQuery, state: FSMContext):
    if c.data == "mode_prof":
        await c.message.edit_text(
            "💼 <b>РЕЖИМ: Профильная консультация</b>\n\n<i>_ [⚠️в разработке🛠️]</i>",
            reply_markup=get_back_kb(),
            parse_mode="HTML"
        )
        await state.set_state(BotStates.prof_consult_mode)
        await c.answer()
        return

    modes_map = {
        "mode_qa": BotStates.qa_mode,
        "mode_doc_gen": BotStates.doc_gen_mode,
        "mode_doc_analyze": BotStates.doc_analyze_mode
    }

    txt_map = {
        "mode_qa": "⚖️ <b>РЕЖИМ: Юридический вопрос</b>\n\nОпишите вашу ситуацию текстом или голосом 🎙.",
        "mode_doc_gen": (
            "📝 <b>РЕЖИМ: Создание документа</b>\n\n"
            "Пришлите <b>фото или PDF</b> образца, либо напишите название документа.\n"
            "Я могу задать вопросы для заполнения или прислать готовый бланк."
        ),
        "mode_doc_analyze": "🔍 <b>РЕЖИМ: Разбор документа</b>\n\nПришлите <b>фото, PDF</b> или текст документа, и я найду в нем риски и ошибки."
    }

    await state.set_state(modes_map[c.data])
    await c.message.edit_text(txt_map[c.data], reply_markup=get_back_kb(), parse_mode="HTML")
    await c.answer()

# ============ ДВУХУРОВНЕВАЯ ЛОГИКА СОЗДАНИЯ ДОКУМЕНТОВ ============

def keyword_forces_pdf(text: str) -> bool:
    return bool(FORCE_PDF_PATTERNS.search(text))

def response_is_refusal(text: str) -> bool:
    return bool(REFUSAL_PATTERNS.search(text))

async def classify_doc_intent(user_message: str) -> str:
    if keyword_forces_pdf(user_message):
        logging.info("Keyword-override: NEED_PDF")
        return "NEED_PDF"

    if re.search(r"(?i)(текстом|в\s+чат|без\s+pdf|без\s+пдф)", user_message):
        return "NEED_TEXT"

    try:
        res = await asyncio.to_thread(
            giga.chat,
            {
                "messages": [
                    {"role": "system", "content": PROMPT_DOC_INTENT_DETECTOR},
                    {"role": "user", "content": user_message}
                ]
            }
        )
        raw = res.choices[0].message.content.strip().upper()
        first_word = raw.split()[0] if raw.split() else ""
        valid = {"NEED_PDF", "NEED_CLARIFY", "NEED_TEXT", "REDIRECT_QA"}
        if first_word in valid:
            return first_word
        else:
            return "NEED_PDF"
    except Exception as e:
        logging.error(f"Intent detector error: {e}")
        return "NEED_PDF"

async def generate_document_text(user_request: str, history: list) -> str:
    messages = [{"role": "system", "content": PROMPT_DOC_GENERATOR}]
    if history:
        messages.extend(history[-4:])
    messages.append({"role": "user", "content": user_request})

    try:
        res = await asyncio.to_thread(giga.chat, {"messages": messages})
        text = res.choices[0].message.content.strip()
        text = text.replace('**', '').replace('*', '').replace('#', '')

        if response_is_refusal(text):
            logging.warning("Модель вернула отказ — запускаем принудительный шаблон")
            return ""
        return text
    except Exception as e:
        logging.error(f"Document generator error: {e}")
        return ""

async def get_clarify_questions(user_request: str) -> str:
    try:
        res = await asyncio.to_thread(
            giga.chat,
            {
                "messages": [
                    {"role": "system", "content": PROMPT_DOC_CLARIFY},
                    {"role": "user", "content": f"Запрос пользователя: {user_request}"}
                ]
            }
        )
        text = res.choices[0].message.content.strip()
        return text.replace('**', '').replace('*', '')
    except Exception as e:
        logging.error(f"Clarify questions error: {e}")
        return "Уточните, пожалуйста: кто является сторонами договора, каков предмет и основные условия?"

# ============ ЦЕНТРАЛЬНАЯ ЛОГИКА ОБРАБОТКИ ============
@dp.message(F.text | F.voice | F.photo | F.document)
async def handle(m: Message, state: FSMContext):
    user_id = m.from_user.id
    user = get_db_user(user_id)
    curr = await state.get_state()

    if not user["accepted"] or curr == BotStates.main_menu.state:
        return await m.answer("Пожалуйста, выберите раздел в меню 👇", reply_markup=main_kb())

    if curr == BotStates.prof_consult_mode.state:
        return await m.answer("<i>_ [⚠️в разработке🛠️]</i>", reply_markup=get_back_kb(), parse_mode="HTML")

    status = await m.answer("🔍 <i>Обрабатываю данные...</i>", parse_mode="HTML")

    try:
        caption = m.caption.strip() if m.caption else ""
        input_text = ""

        if m.text:
            input_text = m.text
        elif m.voice:
            await status.edit_text("🎙 <i>Распознаю голос...</i>", parse_mode="HTML")
            input_text = await transcribe_voice(m.voice.file_id)
            if input_text:
                await m.answer(f"🎙 <b>Вы сказали:</b>\n«<i>{input_text}</i>»", parse_mode="HTML")
        elif m.photo:
            await status.edit_text("📸 <i>Сканирую текст с фото...</i>", parse_mode="HTML")
            ocr_text = await extract_text_from_photo(m.photo[-1].file_id)
            if not ocr_text or len(ocr_text) < 3:
                return await status.edit_text(
                    "⚠️ <b>Не удалось четко распознать текст с фото.</b>",
                    reply_markup=get_back_kb(), parse_mode="HTML"
                )
            input_text = f"Текст с фото документа:\n{ocr_text}"
            if caption:
                input_text += f"\n\nУказание пользователя: {caption}"
        elif m.document:
            await status.edit_text("📄 <i>Читаю PDF-файл...</i>", parse_mode="HTML")
            pdf_text = await extract_text_from_pdf(m.document.file_id)
            if not pdf_text or len(pdf_text) < 3:
                return await status.edit_text(
                    "⚠️ <b>Не удалось извлечь текст из файла.</b>",
                    reply_markup=get_back_kb(), parse_mode="HTML"
                )
            input_text = f"Текст из PDF-документа:\n{pdf_text}"
            if caption:
                input_text += f"\n\nУказание пользователя: {caption}"

        if not input_text:
            return await status.edit_text(
                "⚠️ Не удалось разобрать данные. Попробуйте повторить передачу.",
                reply_markup=get_back_kb()
            )

        if user_id not in user_history:
            user_history[user_id] = {'h': [], 'rc': 0}

        # РЕЖИМ 1: ЮРИДИЧЕСКИЙ ВОПРОС
        if curr == BotStates.qa_mode.state:
            user_history[user_id]['h'].append({"role": "user", "content": input_text})
            if len(user_history[user_id]['h']) > 6:
                user_history[user_id]['h'] = user_history[user_id]['h'][-6:]

            sys_prompt = PROMPT_QA + f"\nГород пользователя: {user['city']}"
            res = await asyncio.to_thread(
                giga.chat,
                {"messages": [{"role": "system", "content": sys_prompt}] + user_history[user_id]['h']}
            )
            ans = res.choices[0].message.content
            ans = ans.replace('**', '').replace('*', '')

            if "REDIRECT_DOC" in ans or "REDIRECT_ANALYZE" in ans:
                user_history[user_id]['rc'] += 1
                target = "mode_doc_gen" if "DOC" in ans else "mode_doc_analyze"
                if user_history[user_id]['rc'] > 2:
                    return await status.edit_text(
                        "Вы задаете вопрос не по профилю раздела. Чтобы выбрать нужную функцию, нажмите «В главное меню».",
                        reply_markup=get_back_kb()
                    )
                return await status.edit_text(
                    "Данную функцию можно сделать, выбрав другой раздел. Нажмите кнопку ниже:",
                    reply_markup=redirect_kb(target)
                )

            user_history[user_id]['h'].append({"role": "assistant", "content": ans})
            footer = "\n\n<i>ℹ️ Информация носит справочный характер. Для консультации обратитесь к юристу: /lawyer</i>"
            await status.edit_text(ans + footer, reply_markup=get_back_kb(), parse_mode="HTML")

        # РЕЖИМ 2: СОЗДАНИЕ ДОКУМЕНТА
        elif curr == BotStates.doc_gen_mode.state:
            await status.edit_text("📋 <i>Анализирую запрос...</i>", parse_mode="HTML")

            intent = await classify_doc_intent(input_text)

            if intent == "REDIRECT_QA":
                user_history[user_id]['rc'] += 1
                if user_history[user_id]['rc'] > 2:
                    return await status.edit_text(
                        "Кажется, вы задаёте правовой вопрос. На него я отвечу в разделе «Задать вопрос» — выйдите в главное меню.",
                        reply_markup=get_back_kb()
                    )
                return await status.edit_text(
                    "Здесь я составляю документы. На этот правовой вопрос я могу ответить в другом разделе:",
                    reply_markup=redirect_kb("mode_qa")
                )

            if intent == "NEED_CLARIFY":
                await status.edit_text("💬 <i>Уточняю детали...</i>", parse_mode="HTML")
                clarify_text = await get_clarify_questions(input_text)
                user_history[user_id]['h'].append({"role": "user", "content": input_text})
                user_history[user_id]['h'].append({"role": "assistant", "content": clarify_text})
                if len(user_history[user_id]['h']) > 8:
                    user_history[user_id]['h'] = user_history[user_id]['h'][-8:]
                return await status.edit_text(
                    f"📝 {clarify_text}",
                    reply_markup=get_back_kb(),
                    parse_mode="HTML"
                )

            if intent == "NEED_TEXT":
                await status.edit_text("✍️ <i>Составляю документ...</i>", parse_mode="HTML")
                doc_text = await generate_document_text(input_text, user_history[user_id]['h'])
                if not doc_text:
                    doc_text = "⚠️ Не удалось составить документ. Попробуйте уточнить запрос."
                user_history[user_id]['h'].append({"role": "user", "content": input_text})
                user_history[user_id]['h'].append({"role": "assistant", "content": doc_text})
                if len(user_history[user_id]['h']) > 8:
                    user_history[user_id]['h'] = user_history[user_id]['h'][-8:]
                return await status.edit_text(doc_text, reply_markup=get_back_kb(), parse_mode="HTML")

            # NEED_PDF — гарантированная сборка и отправка PDF
            await status.edit_text("⚙️ <i>Составляю проект документа...</i>", parse_mode="HTML")
            doc_text = await generate_document_text(input_text, user_history[user_id]['h'])

            if not doc_text or len(doc_text) < 30:
                fallback_request = f"Составь полный образец документа: {input_text}\nИспользуй прочерки для незаполненных реквизитов."
                doc_text = await generate_document_text(fallback_request, [])

            if not doc_text or len(doc_text) < 30:
                await status.delete()
                return await m.answer(
                    "⚠️ Не удалось составить документ. Уточните, пожалуйста, наименование документа.",
                    reply_markup=get_back_kb(),
                    parse_mode="HTML"
                )

            await status.edit_text("📄 <i>Формирую PDF-файл...</i>", parse_mode="HTML")
            pdf_name = f"doc_{user_id}.pdf"
            
            # АСИНХРОННАЯ СБОРКА С СИСТЕМНЫМ ШРИФТОМ
            pdf_success = await asyncio.to_thread(generate_pdf_file, doc_text, pdf_name)

            await status.delete()

            if pdf_success and os.path.exists(pdf_name):
                try:
                    await m.answer_document(
                        FSInputFile(pdf_name),
                        caption="📄 <b>Ваш проект документа готов (формат PDF).</b>\n\n<i>Сформирован по стандартам РФ. При необходимости обратитесь к юристу для заверения.</i>",
                        reply_markup=get_back_kb(),
                        parse_mode="HTML"
                    )
                finally:
                    if os.path.exists(pdf_name):
                        os.remove(pdf_name)
            else:
                await m.answer(
                    f"📄 <b>Ваш документ готов:</b>\n\n{doc_text[:4000]}",
                    reply_markup=get_back_kb(),
                    parse_mode="HTML"
                )

            user_history[user_id]['h'].append({"role": "user", "content": input_text})
            user_history[user_id]['h'].append({"role": "assistant", "content": "[PDF Документ отправлен]"})
            if len(user_history[user_id]['h']) > 8:
                user_history[user_id]['h'] = user_history[user_id]['h'][-8:]

        # РЕЖИМ 3: РАЗБОР ДОКУМЕНТА
        elif curr == BotStates.doc_analyze_mode.state:
            sys_prompt = "Ты опытный юрист РФ. Проанализируй предоставленный текст документа. Найди все правовые риски, скрытые комиссии, ошибки и ущемления прав пользователя. Выдай понятный и подробный отчет со ссылками на законы."
            res = await asyncio.to_thread(giga.chat, {"messages": [{"role": "system", "content": sys_prompt}, {"role": "user", "content": input_text}]})
            ans = res.choices[0].message.content
            ans = ans.replace('**', '').replace('*', '')
            await status.edit_text(f"🔍 <b>Результат правового анализа:</b>\n\n{ans}", reply_markup=get_back_kb(), parse_mode="HTML")

    except Exception as e:
        logging.error(f"AI Error: {e}", exc_info=True)
        try:
            await status.edit_text("⚠️ Произошла ошибка. Попробуйте сформулировать запрос иначе.", reply_markup=get_back_kb())
        except Exception:
            pass

async def main():
    download_font()
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())