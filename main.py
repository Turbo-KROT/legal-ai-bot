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
        conn.execute("INSERT INTO users (user_id, accepted, city) VALUES (?, 0, NULL)", (user_id,))
        conn.commit(); conn.close()
        return {"accepted": False, "city": None}
    return {"accepted": bool(row[0]), "city": row[1]}

def update_db(user_id: int, field: str, value):
    conn = sqlite3.connect("users.db")
    conn.execute(f"UPDATE users SET {field} = ? WHERE user_id = ?", (value, user_id))
    conn.commit(); conn.close()

user_history = {} # {'history': [], 'redirects': 0}

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
PROMPT_QA = """Ты — высококвалифицированный юридический AI-консультант РФ.
ВНИМАНИЕ! Если пользователь просит СОСТАВИТЬ документ или бланк, ответь ОДНИМ СЛОВОМ: REDIRECT_DOC
Если просит проверить/проанализировать присланный документ, ответь ОДНИМ СЛОВОМ: REDIRECT_ANALYZE

Если это обычный вопрос, отвечай СТРОГО по структуре:
📌 КРАТКИЙ ВЕРДИКТ: 
📖 ПРАВОВОЕ ОБОСНОВАНИЕ: 
💡 ПЛАН ДЕЙСТВИЙ: 
⚠️ РИСКИ И СРОКИ: """

PROMPT_DOC_GEN = """Ты — юрист-делопроизводитель РФ. Твоя задача — ТОЛЬКО составление документов.
ВНИМАНИЕ! Если пользователь задает общий правовой вопрос, ответь ОДНИМ СЛОВОМ: REDIRECT_QA

ПРАВИЛА:
Ты обязан начинать ответ с тега [ЧАТ] или [ФАЙЛ].
1. Если просят образец, шаблон, бланк или просят заполнить по данным: сгенерируй текст документа. Начни ответ с тега [ФАЙЛ]. Далее со следующей строки пиши текст документа (без "Вот ваш документ"). Если данных нет - ставь прочерки (___). Если клиент прислал текст образца и свои данные - аккуратно вставь данные в его текст.
2. Если данных не хватает и нужно их запросить, начни ответ с тега [ЧАТ]. Далее напиши, какие данные нужны (ФИО, адреса и т.д.)."""

# ============ ВЕРСТКА PDF ============
def download_font():
    font_file = "DejaVuSans.ttf"
    if not os.path.exists(font_file):
        try: urllib.request.urlretrieve("https://github.com/matomo-org/travis-scripts/raw/master/fonts/DejaVuSans.ttf", font_file)
        except: pass
    return font_file if os.path.exists(font_file) else None

def generate_pdf_gost(text: str, filename: str):
    try:
        # Жесткая очистка
        text = re.sub(r'\*\*(.*?)\*\*', r'\1', text).replace('*', '').replace('#', '')
        text = re.sub(r'\[ФАЙЛ\]', '', text, flags=re.IGNORECASE).strip()
        text = re.sub(r'[^\w\s.,!?:;()\-—"«»№%§/]+', '', text, flags=re.UNICODE)
        
        pdf = FPDF()
        pdf.add_page()
        pdf.set_margins(20, 20, 10)
        
        font = download_font()
        if font:
            pdf.add_font('DejaVu', '', font)
            pdf.set_font('DejaVu', '', 12)
        else: pdf.set_font("Arial", size=12)
        
        lines = text.split('\n')
        title_done = False
        for line in lines:
            l = line.strip()
            if not l: pdf.ln(5); continue
            if not title_done and len(l) < 100:
                if font: pdf.set_font('DejaVu', '', 14)
                pdf.multi_cell(0, 10, l.upper(), align='C')
                if font: pdf.set_font('DejaVu', '', 12)
                pdf.ln(5); title_done = True
            else:
                pdf.multi_cell(0, 6, "      " + l, align='J')
        pdf.output(filename)
        return True
    except Exception as e:
        logging.error(f"PDF Error: {e}")
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
        res = await asyncio.to_thread(pytesseract.image_to_string, Image.open(p).convert('L'), lang='rus+eng')
        os.remove(p); return res.strip()
    if message.document:
        d = f"d_{message.document.file_id}.pdf"
        await bot.download_file((await bot.get_file(message.document.file_id)).file_path, d)
        res = "".join([page.get_text() for page in fitz.open(d)])
        os.remove(d); return res.strip()
    return ""

# ============ ИНТЕРФЕЙС ============
def agree_kb():
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✅ Соглашаюсь", callback_data="accept"), InlineKeyboardButton(text="❌ Не соглашаюсь", callback_data="decline")]])

def main_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⚖️ Задать юридический вопрос", callback_data="mode_qa")],
        [InlineKeyboardButton(text="📝 Создание документа", callback_data="mode_doc_gen")],
        [InlineKeyboardButton(text="🔍 Разбор документа", callback_data="mode_doc_analyze")],
        [InlineKeyboardButton(text="💼 Профильная консультация", callback_data="mode_prof")],
        [InlineKeyboardButton(text="📍 Сменить город", callback_data="change_city")]
    ])

def back_kb(): return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 В главное меню", callback_data="back_to_menu")]])
def redirect_kb(m_type): return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="➡️ Перейти в нужный раздел", callback_data=m_type)]])

# ============ ОБРАБОТЧИКИ ============
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
        user_history[m.from_user.id] = {'h': [], 'rc': 0}
        await state.set_state(BotStates.main_menu)
        return await m.answer("📋 <b>ГЛАВНОЕ МЕНЮ</b>\n\nВыберите нужный раздел (доступен текст и голос 🎙):", reply_markup=main_kb(), parse_mode="HTML")
    await send_agreement(m)

@dp.callback_query(F.data == "decline")
async def decline(c: CallbackQuery):
    await c.message.edit_text("😔 Без принятия условий доступ ограничен. Вы можете передумать:", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="📄 Вернуться к соглашению", callback_data="return_agree")]]))

@dp.callback_query(F.data == "return_agree")
async def ret_agree(c: CallbackQuery):
    await c.message.delete()
    await send_agreement(c.message)

@dp.callback_query(F.data == "accept")
async def accept(c: CallbackQuery, state: FSMContext):
    update_db(c.from_user.id, "accepted", 1)
    await c.message.edit_text("✅ <b>Условия использования приняты.</b>", parse_mode="HTML")
    await c.message.answer("🏙 <b>Укажите ваш город</b> (для учета региональных законов):", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🚫 Пропустить", callback_data="skip_city")]]), parse_mode="HTML")
    await state.set_state(BotStates.waiting_for_city)
    await state.update_data(prompt_msg_id=c.message.message_id + 1)

@dp.callback_query(F.data == "skip_city")
async def skip_city(c: CallbackQuery, state: FSMContext):
    update_db(c.from_user.id, "city", "Не указан")
    await c.message.delete()
    user_history[c.from_user.id] = {'h': [], 'rc': 0}
    await state.set_state(BotStates.main_menu)
    await c.message.answer("📍 Город не указан.\n\n📋 <b>ГЛАВНОЕ МЕНЮ</b>\n\nВыберите нужный раздел (доступен текст и голос 🎙):", reply_markup=main_kb(), parse_mode="HTML")

@dp.message(BotStates.waiting_for_city)
async def city_in(m: Message, state: FSMContext):
    update_db(m.from_user.id, "city", m.text.strip())
    data = await state.get_data()
    if 'prompt_msg_id' in data:
        try: await bot.delete_message(m.chat.id, data['prompt_msg_id'])
        except: pass
    await m.delete() # Удаляем сообщение юзера
    user_history[m.from_user.id] = {'h': [], 'rc': 0}
    await state.set_state(BotStates.main_menu)
    await m.answer(f"📍 Ваш город: <b>{m.text}</b>\n\n📋 <b>ГЛАВНОЕ МЕНЮ</b>\n\nВыберите нужный раздел (доступен текст и голос 🎙):", reply_markup=main_kb(), parse_mode="HTML")

@dp.callback_query(F.data == "change_city")
async def ch_city(c: CallbackQuery, state: FSMContext):
    msg = await c.message.edit_text("🏙 Введите название нового города:")
    await state.set_state(BotStates.waiting_for_city)
    await state.update_data(prompt_msg_id=msg.message_id)

@dp.callback_query(F.data == "back_to_menu")
async def back_menu(c: CallbackQuery, state: FSMContext):
    user_history[c.from_user.id] = {'h': [], 'rc': 0}
    await state.set_state(BotStates.main_menu)
    await c.message.edit_text("📋 <b>ГЛАВНОЕ МЕНЮ</b>\n\nВыберите нужный раздел (доступен текст и голос 🎙):", reply_markup=main_kb(), parse_mode="HTML")

@dp.callback_query(F.data.startswith("mode_"))
async def modes(c: CallbackQuery, state: FSMContext):
    modes_map = {"mode_qa": BotStates.qa_mode, "mode_doc_gen": BotStates.doc_gen_mode, "mode_doc_analyze": BotStates.doc_analyze_mode, "mode_prof": BotStates.prof_consult_mode}
    txt_map = {"mode_qa": "⚖️ Юридический вопрос", "mode_doc_gen": "📝 Создание документа", "mode_doc_analyze": "🔍 Разбор документа", "mode_prof": "💼 Профильная консультация"}
    await state.set_state(modes_map[c.data])
    await c.message.edit_text(f"<b>РЕЖИМ: {txt_map[c.data]}</b>", reply_markup=back_kb(), parse_mode="HTML")

# ============ ЦЕНТРАЛЬНАЯ ЛОГИКА ============
@dp.message(F.text | F.voice | F.photo | F.document)
async def handle(m: Message, state: FSMContext):
    u = get_db_user(m.from_user.id)
    curr = await state.get_state()
    if not u["accepted"] or curr == BotStates.main_menu.state: 
        return await m.answer("Пожалуйста, выберите раздел в меню 👇", reply_markup=main_kb())

    # Профильный раздел - заглушка
    if curr == BotStates.prof_consult_mode.state:
        return await m.answer("<i>_ [⚠️в разработке🛠️]</i>", reply_markup=back_kb(), parse_mode="HTML")

    status = await m.answer("🔍 <i>Обрабатываю...</i>", parse_mode="HTML")
    inp = await process_media(m)
    
    if m.voice and inp: await status.edit_text(f"🎙 <b>Вы сказали:</b>\n<i>{inp}</i>\n\n🔍 <i>Анализирую...</i>", parse_mode="HTML")
    if not inp: return await status.edit_text("⚠️ Не удалось получить текст.", reply_markup=back_kb())
    if m.caption: inp += f"\nДополнение: {m.caption}"

    uid = m.from_user.id
    if uid not in user_history: user_history[uid] = {'h': [], 'rc': 0}
    hist = user_history[uid]['h']

    try:
        if curr == BotStates.qa_mode.state:
            hist.append({"role": "user", "content": inp})
            res = await asyncio.to_thread(giga.chat, {"messages": [{"role": "system", "content": PROMPT_QA + f"\nГород: {u['city']}"}] + hist[-6:]})
            ans = res.choices[0].message.content
            
            # Логика перенаправления
            if "REDIRECT_DOC" in ans or "REDIRECT_ANALYZE" in ans:
                user_history[uid]['rc'] += 1
                target = "mode_doc_gen" if "DOC" in ans else "mode_doc_analyze"
                if user_history[uid]['rc'] > 2:
                    return await status.edit_text("Вы задаете вопрос не по профилю раздела. Для навигации нажмите «В главное меню» и выберите нужный раздел.", reply_markup=back_kb())
                return await status.edit_text("Данную функцию можно сделать, выбрав другой раздел. Нажмите кнопку ниже:", reply_markup=redirect_kb(target))
                
            hist.append({"role": "assistant", "content": ans})
            footer = "\n\n<i>ℹ️ Информация носит справочный характер. Для консультации обратитесь к юристу: /lawyer</i>"
            await status.edit_text(ans + footer, reply_markup=back_kb(), parse_mode="HTML")

        elif curr == BotStates.doc_gen_mode.state:
            hist.append({"role": "user", "content": inp})
            res = await asyncio.to_thread(giga.chat, {"messages": [{"role": "system", "content": PROMPT_DOC_GEN}] + hist[-6:]})
            ans = res.choices[0].message.content.strip()

            if "REDIRECT_QA" in ans:
                user_history[uid]['rc'] += 1
                if user_history[uid]['rc'] > 2:
                    return await status.edit_text("Кажется, вы запутались. Для ответов на вопросы выйдите в главное меню.", reply_markup=back_kb())
                return await status.edit_text("Здесь я составляю документы. На этот правовой вопрос я могу ответить в разделе «Задать вопрос»:", reply_markup=redirect_kb("mode_qa"))

            # Строгий тег PDF
            if "[ФАЙЛ]" in ans:
                f_name = f"doc_{uid}.pdf"
                if await asyncio.to_thread(generate_pdf_gost, ans, f_name):
                    await status.delete()
                    await m.answer_document(FSInputFile(f_name), caption="📄 <b>Ваш документ готов.</b>", reply_markup=back_kb(), parse_mode="HTML")
                    os.remove(f_name)
                else: await status.edit_text("⚠️ Ошибка создания PDF.", reply_markup=back_kb())
            elif "[ЧАТ]" in ans:
                clean_ans = ans.replace("[ЧАТ]", "").strip()
                hist.append({"role": "assistant", "content": clean_ans})
                await status.edit_text(clean_ans, reply_markup=back_kb(), parse_mode="HTML")
            else:
                # Если нейросеть затупила и не поставила тег, но написала длинный документ
                if len(ans) > 200:
                    f_name = f"doc_{uid}.pdf"
                    if await asyncio.to_thread(generate_pdf_gost, ans, f_name):
                        await status.delete()
                        await m.answer_document(FSInputFile(f_name), caption="📄 <b>Ваш документ готов.</b>", reply_markup=back_kb(), parse_mode="HTML")
                        os.remove(f_name)
                else:
                    await status.edit_text(ans, reply_markup=back_kb(), parse_mode="HTML")

        elif curr == BotStates.doc_analyze_mode.state:
            res = await asyncio.to_thread(giga.chat, {"messages": [{"role": "system", "content": "Ты юрист. Найди риски в тексте."}, {"role": "user", "content": inp}]})
            await status.edit_text(f"🔍 <b>Анализ:</b>\n\n{res.choices[0].message.content}", reply_markup=back_kb(), parse_mode="HTML")

    except Exception as e:
        logging.error(e)
        try: await status.edit_text("⚠️ Ошибка. Попробуйте переформулировать.", reply_markup=back_kb())
        except: pass

async def main():
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__": asyncio.run(main())