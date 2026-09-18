import asyncio
import logging
import os
import re
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
PROMPT_QA = """Ты — высококвалифицированный юридический AI-консультант РФ.
Отвечай СТРОГО по структуре (используй эмодзи):
📌 КРАТКИЙ ВЕРДИКТ:
📖 ПРАВОВОЕ ОБОСНОВАНИЕ:
💡 ПЛАН ДЕЙСТВИЙ:
⚠️ РИСКИ И СРОКИ:
Не пиши ничего лишнего."""

PROMPT_DOC_GEN = """Ты — опытный делопроизводитель РФ.
ПРАВИЛО 1: Если пользователь просит составить документ, но не дал нужных данных — НАПИШИ ТЕКСТОМ, какие данные нужны (ФИО, адрес, суммы и т.д.).
ПРАВИЛО 2: Если пользователь просит ПУСТОЙ образец ИЛИ уже дал все данные — СОСТАВЬ ТЕКСТ ДОКУМЕНТА.
ПРАВИЛО 3: Если ты выдаешь готовый текст документа, ОБЯЗАТЕЛЬНО начни ответ с кодового слова ДОКУМЕНТ_ГОТОВ (на первой строке). Если ты просто задаешь вопросы, НЕ пиши это слово.
ПРАВИЛО 4: Название документа пиши на первой строке."""


def get_user(user_id: int):
    if user_id not in db:
        db[user_id] = {"accepted": False, "city": None, "history": []}
    return db[user_id]

# ============ УМНАЯ ВЕРСТКА PDF ПО ГОСТУ ============
def create_pdf(text, filename):
    pdf = FPDF()
    pdf.add_page()
    font_path = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
    
    if os.path.exists(font_path):
        pdf.add_font('DejaVu', '', font_path, uni=True)
    else:
        pdf.set_font("Arial", size=11)
        
    lines = text.strip().split('\n')
    title_processed = False
    
    for line in lines:
        clean_line = line.strip()
        if not clean_line:
            pdf.ln(5) # Пустая строка
            continue
            
        if not title_processed:
            # Заголовок: Центр, размер 14
            if os.path.exists(font_path): pdf.set_font('DejaVu', '', 14)
            pdf.multi_cell(0, 8, clean_line, align='C')
            title_processed = True
            pdf.ln(5)
        else:
            # Основной текст: Выравнивание по ширине (J), отступ пробелами, размер 11
            if os.path.exists(font_path): pdf.set_font('DejaVu', '', 11)
            pdf.multi_cell(0, 6, "    " + clean_line, align='J')
            
    pdf.output(filename)

# ============ АСИНХРОННОЕ РАСПОЗНАВАНИЕ (ЧТОБЫ БОТ НЕ ПАДАЛ) ============
def recognize_audio(wav_path):
    recognizer = sr.Recognizer()
    with sr.AudioFile(wav_path) as source:
        audio_data = recognizer.record(source)
        return recognizer.recognize_google(audio_data, language="ru-RU")

async def transcribe_voice(file_id: str) -> str:
    file = await bot.get_file(file_id)
    ogg_path = f"voice_{file_id}.ogg"
    wav_path = f"voice_{file_id}.wav"
    try:
        await bot.download_file(file.file_path, ogg_path)
        # Асинхронный запуск ffmpeg
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-y", "-i", ogg_path, wav_path,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL
        )
        await proc.communicate()
        
        # Асинхронный запуск распознавания
        text = await asyncio.to_thread(recognize_audio, wav_path)
        return text
    except Exception as e:
        logging.error(f"Voice Error: {e}")
        return None
    finally:
        if os.path.exists(ogg_path): os.remove(ogg_path)
        if os.path.exists(wav_path): os.remove(wav_path)


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


@dp.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    user = get_user(message.from_user.id)
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
        await message.answer_document(FSInputFile("faq.pdf"), caption="❓ FAQ")
    except Exception:
        pass

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Соглашаюсь", callback_data="accept"), 
         InlineKeyboardButton(text="❌ Не соглашаюсь", callback_data="decline")]
    ])
    await message.answer("📋 <b>Условия использования сервиса</b>\n\nНажимая «Соглашаюсь», вы принимаете условия.", reply_markup=kb, parse_mode="HTML")

@dp.callback_query(F.data == "decline")
async def process_decline(callback: CallbackQuery):
    await callback.message.edit_text("😔 Без принятия условий доступ ограничен. Нажмите /start для повтора.")

@dp.callback_query(F.data == "accept")
async def process_accept(callback: CallbackQuery, state: FSMContext):
    user = get_user(callback.from_user.id)
    user["accepted"] = True
    await callback.message.edit_text("✅ <b>Условия использования приняты.</b>", parse_mode="HTML")
    
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🚫 Не указывать (Пропустить)", callback_data="skip_city")]])
    await callback.message.answer("🏙 <b>Укажите ваш город</b> (для учета региональных законов):", reply_markup=kb, parse_mode="HTML")
    await state.set_state(BotStates.waiting_for_city)

@dp.callback_query(F.data == "skip_city")
async def skip_city(callback: CallbackQuery, state: FSMContext):
    get_user(callback.from_user.id)["city"] = "Не указан"
    await callback.message.edit_text("📍 Город не указан.")
    await show_main_menu(callback.from_user.id, state)

@dp.message(BotStates.waiting_for_city)
async def city_input(message: Message, state: FSMContext):
    get_user(message.from_user.id)["city"] = message.text.strip()
    await message.answer(f"📍 Город сохранен: <b>{message.text}</b>", parse_mode="HTML")
    await show_main_menu(message.from_user.id, state)

@dp.callback_query(F.data == "change_city")
async def change_city_btn(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_text("🏙 Введите название нового города:")
    await state.set_state(BotStates.waiting_for_city)

async def show_main_menu(user_id: int, state: FSMContext):
    get_user(user_id)["history"] = [] # Очищаем историю при выходе в меню
    await bot.send_message(
        user_id, 
        "📋 <b>ГЛАВНОЕ МЕНЮ</b>\n\nВыберите нужный раздел (доступен текст и голос 🎙):", 
        reply_markup=get_main_menu_kb(), parse_mode="HTML"
    )
    await state.set_state(BotStates.main_menu)

@dp.callback_query(F.data == "back_to_menu")
async def back_to_menu_btn(callback: CallbackQuery, state: FSMContext):
    await callback.message.delete()
    await show_main_menu(callback.from_user.id, state)

@dp.callback_query(F.data == "mode_qa")
async def mode_qa(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_text("⚖️ <b>РЕЖИМ: Юридический вопрос</b>\n\nОпишите вашу ситуацию текстом или голосом 🎙. Я веду историю диалога.", reply_markup=get_back_kb(), parse_mode="HTML")
    await state.set_state(BotStates.qa_mode)

@dp.callback_query(F.data == "mode_doc_gen")
async def mode_doc_gen(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_text("📝 <b>РЕЖИМ: Создание документа</b>\n\nНапишите, какой документ нужен. Я могу задать уточняющие вопросы, чтобы составить его под вашу ситуацию.", reply_markup=get_back_kb(), parse_mode="HTML")
    await state.set_state(BotStates.doc_gen_mode)

@dp.callback_query(F.data == "mode_doc_analyze")
async def mode_doc_analyze(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_text("🔍 <b>РЕЖИМ: Разбор документа</b>\n\nПришлите текст документа, и я найду в нем риски и ошибки.", reply_markup=get_back_kb(), parse_mode="HTML")
    await state.set_state(BotStates.doc_analyze_mode)

@dp.message(F.text | F.voice)
async def handle_all_messages(message: Message, state: FSMContext):
    current_state = await state.get_state()
    user = get_user(message.from_user.id)
    
    if not user["accepted"]:
        await message.answer("⚠️ Пожалуйста, примите условия: /start")
        return

    if current_state not in [BotStates.qa_mode.state, BotStates.doc_gen_mode.state, BotStates.doc_analyze_mode.state]:
        await message.answer("Пожалуйста, выберите режим в меню 👇", reply_markup=get_main_menu_kb())
        return

    text_request = message.text
    if message.voice:
        status = await message.answer("🎙 <i>Распознаю голос...</i>", parse_mode="HTML")
        text_request = await transcribe_voice(message.voice.file_id)
        if not text_request:
            await status.edit_text("⚠️ Не удалось разобрать речь. Повторите четче.")
            return
        await status.edit_text(f"🎙 <b>Вы сказали:</b>\n«{text_request}»\n\n🔍 <i>Анализирую...</i>", parse_mode="HTML")
        msg_to_edit = status
    else:
        msg_to_edit = await message.answer("🔍 <i>Анализирую запрос...</i>", parse_mode="HTML")

    try:
        user["history"].append({"role": "user", "content": text_request})
        if len(user["history"]) > 6: user["history"] = user["history"][-6:]

        if current_state == BotStates.qa_mode.state:
            sys_prompt = PROMPT_QA + f"\nГород: {user['city']}"
            res = giga.chat({"messages": [{"role": "system", "content": sys_prompt}] + user["history"]})
            ans = res.choices[0].message.content
            user["history"].append({"role": "assistant", "content": ans})
            footer = "\n\n<i>ℹ️ Информация носит справочный характер. Связь с юристом: /lawyer</i>"
            await msg_to_edit.edit_text(ans + footer, reply_markup=get_back_kb(), parse_mode="HTML")

        elif current_state == BotStates.doc_gen_mode.state:
            sys_prompt = PROMPT_DOC_GEN
            res = giga.chat({"messages": [{"role": "system", "content": sys_prompt}] + user["history"]})
            ans = res.choices[0].message.content
            user["history"].append({"role": "assistant", "content": ans})
            
            if "ДОКУМЕНТ_ГОТОВ" in ans:
                clean_text = ans.replace("ДОКУМЕНТ_ГОТОВ", "").strip()
                pdf_name = f"doc_{message.from_user.id}.pdf"
                try:
                    create_pdf(clean_text, pdf_name)
                    await msg_to_edit.delete()
                    await message.answer_document(FSInputFile(pdf_name), caption="📄 <b>Ваш документ готов.</b>", reply_markup=get_back_kb(), parse_mode="HTML")
                    os.remove(pdf_name)
                except Exception as e:
                    logging.error(f"PDF Error: {e}")
                    await msg_to_edit.edit_text("⚠️ Ошибка верстки PDF. Попробуйте еще раз.", reply_markup=get_back_kb())
            else:
                await msg_to_edit.edit_text(ans, reply_markup=get_back_kb(), parse_mode="HTML")

        elif current_state == BotStates.doc_analyze_mode.state:
            sys_prompt = "Ты юрист. Найди правовые риски, скрытые комиссии и ущемления прав в тексте."
            res = giga.chat({"messages": [{"role": "system", "content": sys_prompt}, {"role": "user", "content": text_request}]})
            ans = res.choices[0].message.content
            await msg_to_edit.edit_text(f"🔍 <b>Результат анализа:</b>\n\n{ans}", reply_markup=get_back_kb(), parse_mode="HTML")

    except Exception as e:
        logging.error(f"AI Error: {e}")
        await msg_to_edit.edit_text("⚠️ Ошибка сервера. Попробуйте еще раз.", reply_markup=get_back_kb())

async def main():
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())