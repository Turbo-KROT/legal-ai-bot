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

# ============ БАЗА ДАННЫХ ============
def init_db():
    conn = sqlite3.connect("users.db")
    c = conn.cursor()
    c.execute("CREATE TABLE IF NOT EXISTS users (user_id INTEGER PRIMARY KEY, accepted INTEGER DEFAULT 0, city TEXT DEFAULT NULL)")
    conn.commit()
    conn.close()

init_db()

def get_db_user(user_id: int):
    conn = sqlite3.connect("users.db")
    c = conn.cursor()
    c.execute("SELECT accepted, city FROM users WHERE user_id = ?", (user_id,))
    row = c.fetchone()
    conn.close()
    if not row:
        conn = sqlite3.connect("users.db")
        c2 = conn.cursor()
        c2.execute("INSERT INTO users (user_id, accepted, city) VALUES (?, 0, NULL)", (user_id,))
        conn.commit()
        conn.close()
        return {"accepted": False, "city": None}
    return {"accepted": bool(row[0]), "city": row[1]}

def update_db(user_id: int, field: str, value):
    conn = sqlite3.connect("users.db")
    conn.execute(f"UPDATE users SET {field} = ? WHERE user_id = ?", (value, user_id))
    conn.commit()
    conn.close()

user_history = {}

@dp.errors()
async def global_error_handler(event: ErrorEvent):
    logging.critical(f"Ошибка подавлена: {event.exception}")

class BotStates(StatesGroup):
    waiting_for_city = State()
    main_menu = State()
    qa_mode = State()
    doc_gen_mode = State()
    doc_analyze_mode = State()
    prof_consult_mode = State()

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

PROMPT_DOC_GEN = """Ты — юридический делопроизводитель РФ. Твоя задача — подготавливать тексты юридических документов.
ВНИМАНИЕ! Если пользователь задает общий правовой вопрос, ответь ОДНИМ СЛОВОМ: REDIRECT_QA

ПРАВИЛА:
Ты обязан начинать ответ с тега [ЧАТ] или [ФАЙЛ].
1. Если просят образец, шаблон, бланк или просят заполнить по данным: сгенерируй текст документа. Начни ответ с тега [ФАЙЛ]. Далее со следующей строки пиши текст документа (без "Вот ваш документ"). Если данных нет - ставь прочерки (___). Если клиент прислал текст образца и свои данные - аккуратно вставь данные в его текст.
2. Если данных не хватает и нужно их запросить, начни ответ с тега [ЧАТ]. Далее напиши, какие данные нужны (ФИО, адреса и т.д.)."""

# ============ ВЕРСТКА PDF (ГОСТ) ============
def download_font():
    font_file = "DejaVuSans.ttf"
    if not os.path.exists(font_file):
        try:
            urllib.request.urlretrieve("https://github.com/matomo-org/travis-scripts/raw/master/fonts/DejaVuSans.ttf", font_file)
        except:
            pass
    return font_file if os.path.exists(font_file) else None

def generate_pdf_gost(text: str, filename: str) -> bool:
    try:
        clean_text = text.replace("[ФАЙЛ]", "").replace("[ЧАТ]", "").replace("===DOCUMENT_START===", "").strip()
        clean_text = re.sub(r'\*\*(.*?)\*\*', r'\1', clean_text)
        clean_text = re.sub(r'\*(.*?)\*', r'\1', clean_text)
        clean_text = re.sub(r'#(.*?)\n', r'\1\n', clean_text)
        
        pdf = FPDF()
        pdf.add_page()
        pdf.set_margins(20, 20, 10)
        pdf.set_auto_page_break(auto=True, margin=20)
        
        font_candidates = [
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "DejaVuSans.ttf",
            "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf"
        ]
        font_path = next((f for f in font_candidates if os.path.exists(f)), download_font())
        
        font_added = False
        if font_path:
            try:
                pdf.add_font('DejaVu', '', font_path)
                pdf.set_font('DejaVu', '', 11)
                font_added = True
            except Exception:
                try:
                    pdf.add_font('DejaVu', '', font_path, uni=True)
                    pdf.set_font('DejaVu', '', 11)
                    font_added = True
                except Exception as fe:
                    logging.error(f"Add font failed: {fe}")

        if not font_added:
            pdf.set_font("Arial", size=11)
            
        lines = clean_text.split('\n')
        title_done = False
        
        for line in lines:
            c_line = line.strip()
            if not c_line:
                pdf.ln(4)
                continue
                
            if not font_added:
                c_line = c_line.encode('ascii', 'ignore').decode('ascii')

            if not title_done and len(c_line) < 120:
                if font_added: pdf.set_font('DejaVu', '', 13)
                pdf.multi_cell(0, 7, c_line, align='C')
                if font_added: pdf.set_font('DejaVu', '', 11)
                pdf.ln(4)
                title_done = True
            else:
                pdf.multi_cell(0, 6, "      " + c_line, align='J')
                
        pdf.output(filename)
        return True
    except Exception as e:
        logging.error(f"PDF Build Error: {e}", exc_info=True)
        return False

# ============ МЕДИА ОБРАБОТКА ============
def recognize_audio(wav_path):
    r = sr.Recognizer()
    with sr.AudioFile(wav_path) as source:
        return r.recognize_google(r.record(source), language="ru-RU")

async def process_media(message: Message):
    if message.text:
        return message.text
    if message.voice:
        f_id = message.voice.file_id
        ogg, wav = f"v_{f_id}.ogg", f"v_{f_id}.wav"
        await bot.download_file((await bot.get_file(f_id)).file_path, ogg)
        proc = await asyncio.create_subprocess_exec("ffmpeg", "-y", "-i", ogg, "-ar", "16000", "-ac", "1", wav, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        await proc.communicate()
        res = await asyncio.to_thread(recognize_audio, wav)
        for p in [ogg, wav]:
            if os.path.exists(p):
                os.remove(p)
        return res
    if message.photo:
        p = f"p_{message.photo[-1].file_id}.jpg"
        await bot.download_file((await bot.get_file(message.photo[-1].file_id)).file_path, p)
        res = await asyncio.to_thread(pytesseract.image_to_string, Image.open(p).convert('L'), lang='rus+eng')
        os.remove(p)
        return res.strip()
    if message.document:
        d = f"d_{message.document.file_id}.pdf"
        await bot.download_file((await bot.get_file(message.document.file_id)).file_path, d)
        res = "".join([page.get_text() for page in fitz.open(d)])
        os.remove(d)
        return res.strip()
    return ""

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
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 В главное меню", callback_data="back_to_menu")]])

def redirect_kb(m_type):
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="➡️ Перейти в нужный раздел", callback_data=m_type)]])

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
    await message_obj.answer("📋 <b>Условия использования сервиса</b>\n\nНажимая «Соглашаюсь», вы принимаете условия.", reply_markup=agree_kb(), parse_mode="HTML")

@dp.message(CommandStart())
async def cmd_start(m: Message, state: FSMContext):
    u = get_db_user(m.from_user.id)
    if u["accepted"]:
        await m.answer("👋 <b>С возвращением!</b>", parse_mode="HTML")
        user_history[m.from_user.id] = {'h': [], 'rc': 0}
        await state.set_state(BotStates.main_menu)
        return await m.answer("📋 <b>ГЛАВНОЕ МЕНЮ</b>\n\nВыберите нужный раздел (доступен текст и голос 🎙):", reply_markup=main_kb(), parse_mode="HTML")
    await send_agreement(m)

@dp.callback_query(F.data == "decline")
async def decline(c: CallbackQuery):
    await c.message.edit_text(
        "😔 Без принятия условий доступ к сервису ограничен. Вы можете передумать в любой момент:", 
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="📄 Вернуться к соглашению", callback_data="return_agree")]]),
        parse_mode="HTML"
    )
    await c.answer()

@dp.callback_query(F.data == "return_agree")
async def ret_agree(c: CallbackQuery):
    await c.message.delete()
    await send_agreement(c.message)
    await c.answer()

@dp.callback_query(F.data == "accept")
async def accept(c: CallbackQuery, state: FSMContext):
    update_db(c.from_user.id, "accepted", 1)
    await c.message.edit_text("✅ <b>Условия использования приняты.</b>", parse_mode="HTML")
    await c.message.answer(
        "🏙 <b>Укажите ваш город</b> (для учета региональных законов):", 
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🚫 Не указывать (Пропустить)", callback_data="skip_city")]]), 
        parse_mode="HTML"
    )
    await state.set_state(BotStates.waiting_for_city)
    await state.update_data(prompt_msg_id=c.message.message_id + 1)
    await c.answer()

@dp.callback_query(F.data == "skip_city")
async def skip_city(c: CallbackQuery, state: FSMContext):
    update_db(c.from_user.id, "city", "Не указан")
    await c.message.delete()
    user_history[c.from_user.id] = {'h': [], 'rc': 0}
    await state.set_state(BotStates.main_menu)
    await c.message.answer("📍 Город не указан.\n\n📋 <b>ГЛАВНОЕ МЕНЮ</b>\n\nВыберите нужный раздел (доступен текст и голос 🎙):", reply_markup=main_kb(), parse_mode="HTML")
    await c.answer()

@dp.message(BotStates.waiting_for_city)
async def city_in(m: Message, state: FSMContext):
    update_db(m.from_user.id, "city", m.text.strip())
    data = await state.get_data()
    if 'prompt_msg_id' in data:
        try:
            await bot.delete_message(m.chat.id, data['prompt_msg_id'])
        except Exception:
            pass
    await m.delete()
    user_history[m.from_user.id] = {'h': [], 'rc': 0}
    await state.set_state(BotStates.main_menu)
    await m.answer(f"📍 Ваш город: <b>{m.text.strip()}</b>\n\n📋 <b>ГЛАВНОЕ МЕНЮ</b>\n\nВыберите нужный раздел (доступен текст и голос 🎙):", reply_markup=main_kb(), parse_mode="HTML")

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
    await c.message.edit_text("📋 <b>ГЛАВНОЕ МЕНЮ</b>\n\nВыберите нужный раздел (доступен текст и голос 🎙):", reply_markup=main_kb(), parse_mode="HTML")
    await c.answer()

@dp.callback_query(F.data.startswith("mode_"))
async def modes(c: CallbackQuery, state: FSMContext):
    if c.data == "mode_prof":
        await c.message.edit_text("💼 <b>РЕЖИМ: Профильная консультация</b>\n\n<i>_ [⚠️в разработке🛠️]</i>", reply_markup=get_back_kb(), parse_mode="HTML")
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
        "mode_doc_gen": "📝 <b>РЕЖИМ: Создание документа</b>\n\nПришлите <b>фото или PDF</b> образца, либо напишите название документа. Я могу задать вопросы для заполнения или прислать готовый бланк.",
        "mode_doc_analyze": "🔍 <b>РЕЖИМ: Разбор документа</b>\n\nПришлите <b>фото, PDF</b> или текст документа, и я найду в нем риски и ошибки."
    }
    
    await state.set_state(modes_map[c.data])
    await c.message.edit_text(txt_map[c.data], reply_markup=get_back_kb(), parse_mode="HTML")
    await c.answer()

# ============ ЦЕНТРАЛЬНАЯ ЛОГИКА ОБРАБОТКИ ============
@dp.message(F.text | F.voice | F.photo | F.document)
async def handle(m: Message, state: FSMContext):
    u = get_db_user(m.from_user.id)
    curr = await state.get_state()
    
    if not u["accepted"] or curr == BotStates.main_menu.state: 
        return await m.answer("Пожалуйста, выберите раздел в меню 👇", reply_markup=main_kb())

    if curr == BotStates.prof_consult_mode.state:
        return await m.answer("<i>_ [⚠️в разработке🛠️]</i>", reply_markup=get_back_kb(), parse_mode="HTML")

    status = await m.answer("🔍 <i>Обрабатываю данные...</i>", parse_mode="HTML")
    inp = await process_media(m)
    
    if m.voice and inp: 
        await status.edit_text(f"🎙 <b>Вы сказали:</b>\n«<i>{inp}</i>»\n\n🔍 <i>Анализирую...</i>", parse_mode="HTML")
    
    if not inp: 
        return await status.edit_text("⚠️ Не удалось получить данные. Попробуйте еще раз.", reply_markup=get_back_kb())
        
    if m.caption: 
        inp += f"\nДополнение от пользователя: {m.caption}"

    uid = m.from_user.id
    if uid not in user_history: 
        user_history[uid] = {'h': [], 'rc': 0}
    hist = user_history[uid]['h']

    try:
        # 1. РЕЖИМ ВОПРОСА
        if curr == BotStates.qa_mode.state:
            hist.append({"role": "user", "content": inp})
            res = await asyncio.to_thread(giga.chat, {"messages": [{"role": "system", "content": PROMPT_QA + f"\nГород пользователя: {u['city']}"}] + hist[-6:]})
            ans = res.choices[0].message.content
            
            if "REDIRECT_DOC" in ans or "REDIRECT_ANALYZE" in ans:
                user_history[uid]['rc'] += 1
                target = "mode_doc_gen" if "DOC" in ans else "mode_doc_analyze"
                if user_history[uid]['rc'] > 2:
                    return await status.edit_text("Вы задаете вопрос не по профилю раздела. Чтобы выбрать нужную функцию, нажмите «В главное меню».", reply_markup=get_back_kb())
                return await status.edit_text("Данную функцию можно сделать, выбрав другой раздел. Нажмите кнопку ниже:", reply_markup=redirect_kb(target))
                
            hist.append({"role": "assistant", "content": ans})
            footer = "\n\n<i>ℹ️ Информация носит справочный характер. Для консультации обратитесь к юристу: /lawyer</i>"
            await status.edit_text(ans + footer, reply_markup=get_back_kb(), parse_mode="HTML")

        # 2. РЕЖИМ СОЗДАНИЯ ДОКУМЕНТА
        elif curr == BotStates.doc_gen_mode.state:
            clean_prompt_text = re.sub(r'(?i)\b(в\s+формате\s+)?(pdf|пдф|файл(ом)?|документ(ом)?|word|ворд)\b', '', inp).strip()
            if len(clean_prompt_text) < 3: clean_prompt_text = inp

            hist.append({"role": "user", "content": clean_prompt_text})
            res = await asyncio.to_thread(giga.chat, {"messages": [{"role": "system", "content": PROMPT_DOC_GEN}] + hist[-6:]})
            ans = res.choices[0].message.content.strip()

            if "REDIRECT_QA" in ans:
                user_history[uid]['rc'] += 1
                if user_history[uid]['rc'] > 2:
                    return await status.edit_text("Кажется, вы запутались. Для ответов на юридические вопросы выйдите в главное меню.", reply_markup=get_back_kb())
                return await status.edit_text("Здесь я составляю документы. На этот правовой вопрос я могу ответить в разделе «Задать вопрос»:", reply_markup=redirect_kb("mode_qa"))

            is_explicit_text = "текстом" in inp.lower() or "в чат" in inp.lower()

            if "[ФАЙЛ]" in ans or len(ans) > 150:
                if not is_explicit_text:
                    f_name = f"doc_{uid}.pdf"
                    pdf_success = await asyncio.to_thread(generate_pdf_gost, ans, f_name)
                    
                    await status.delete()
                    if pdf_success and os.path.exists(f_name):
                        await m.answer_document(FSInputFile(f_name), caption="📄 <b>Ваш проект документа готов (формат PDF).</b>", reply_markup=get_back_kb(), parse_mode="HTML")
                        os.remove(f_name)
                    else:
                        await m.answer(f"📄 <b>Ваш документ готов:</b>\n\n{ans.replace('[ФАЙЛ]', '').replace('[ЧАТ]', '').strip()}", reply_markup=get_back_kb(), parse_mode="HTML")
                else:
                    clean_ans = ans.replace("[ЧАТ]", "").replace("[ФАЙЛ]", "").strip()
                    hist.append({"role": "assistant", "content": clean_ans})
                    await status.edit_text(clean_ans, reply_markup=get_back_kb(), parse_mode="HTML")
            else:
                clean_ans = ans.replace("[ЧАТ]", "").replace("[ФАЙЛ]", "").strip()
                hist.append({"role": "assistant", "content": clean_ans})
                await status.edit_text(clean_ans, reply_markup=get_back_kb(), parse_mode="HTML")

        # 3. РЕЖИМ РАЗБОРА ДОКУМЕНТА
        elif curr_state == BotStates.doc_analyze_mode.state:
            res = await asyncio.to_thread(giga.chat, {"messages": [{"role": "system", "content": "Ты опытный юрист РФ. Проанализируй предоставленный текст документа. Найди все правовые риски, скрытые комиссии, ошибки и ущемления прав пользователя. Выдай понятный и подробный отчет со ссылками на законы."}] + [{"role": "user", "content": inp}]})
            await status.edit_text(f"🔍 <b>Результат правового анализа:</b>\n\n{res.choices[0].message.content}", reply_markup=get_back_kb(), parse_mode="HTML")

    except Exception as e:
        logging.error(f"Error: {e}")
        try:
            await status.edit_text("⚠️ Произошла ошибка. Попробуйте сформулировать запрос иначе.", reply_markup=get_back_kb())
        except Exception:
            pass

async def main():
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())