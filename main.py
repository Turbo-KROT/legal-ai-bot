import asyncio
import logging
import os
import subprocess
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

from config import BOT_TOKEN, GIGACHAT_CREDENTIALS, ADMIN_ID

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

class UserForm(StatesGroup):
    waiting_for_city = State()
    waiting_for_question = State()
    waiting_for_doc_request = State()

SYSTEM_PROMPT = """Ты — высококвалифицированный юридический AI-консультант по праву РФ. 
Твоя задача — давать точные разборы и составлять проекты документов."""

# ============ ФУНКЦИЯ СОЗДАНИЯ PDF ============
def create_pdf(text, filename):
    pdf = FPDF()
    pdf.add_page()
    
    # Путь к шрифту DejaVu (стандарт для Linux)
    font_path = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
    
    if os.path.exists(font_path):
        pdf.add_font('DejaVu', '', font_path, uni=True)
        pdf.set_font('DejaVu', '', 12)
    else:
        pdf.set_font("Arial", size=12) # Если шрифта нет, будет кракозябра, но бот не упадет
        
    pdf.multi_cell(0, 10, text)
    pdf.output(filename)

# ============ РАСПОЗНАВАНИЕ ГОЛОСА ============
async def transcribe_voice_message(voice_message: Message) -> str:
    file_id = voice_message.voice.file_id
    file = await bot.get_file(file_id)
    ogg_path = f"voice_{file_id}.ogg"
    wav_path = f"voice_{file_id}.wav"
    try:
        await bot.download_file(file.file_path, ogg_path)
        subprocess.run(["ffmpeg", "-y", "-i", ogg_path, wav_path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
        recognizer = sr.Recognizer()
        with sr.AudioFile(wav_path) as source:
            audio_data = recognizer.record(source)
            text = recognizer.recognize_google(audio_data, language="ru-RU")
            return text
    except:
        return None
    finally:
        if os.path.exists(ogg_path): os.remove(ogg_path)
        if os.path.exists(wav_path): os.remove(wav_path)

def get_user(user_id: int):
    if user_id not in db:
        db[user_id] = {"accepted": False, "city": None, "history": []}
    return db[user_id]

# ============ КОМАНДА /template (Образцы) ============
@dp.message(Command("template"))
async def cmd_template(message: Message, state: FSMContext):
    await message.answer(
        "📝 <b>Конструктор документов</b>\n\n"
        "Напишите, какой документ вам нужен?\n"
        "Например: <i>«Договор аренды квартиры»</i> или <i>«Заявление на увольнение»</i>.\n\n"
        "Я составлю проект и пришлю его в формате PDF.",
        parse_mode="HTML"
    )
    await state.set_state(UserForm.waiting_for_doc_request)

# ============ ГЕНЕРАЦИЯ ДОКУМЕНТА ПО ЗАПРОСУ ============
@dp.message(UserForm.waiting_for_doc_request)
async def process_doc_request(message: Message, state: FSMContext):
    doc_name = message.text
    status_msg = await message.answer("🛠 <b>Составляю проект документа...</b>", parse_mode="HTML")
    
    try:
        prompt = f"Составь подробный юридически грамотный образец документа: {doc_name}. Используй официальный стиль, укажи места для подписей и дат."
        response = giga.chat(prompt)
        doc_text = response.choices[0].message.content
        
        pdf_filename = f"document_{message.from_user.id}.pdf"
        create_pdf(doc_text, pdf_filename)
        
        doc_file = FSInputFile(pdf_filename)
        await message.answer_document(doc_file, caption=f"📄 Готовый проект: {doc_name}")
        
        os.remove(pdf_filename) # Удаляем файл после отправки
        await status_msg.delete()
        await state.set_state(UserForm.waiting_for_question)
        
    except Exception as e:
        logging.error(f"Ошибка генерации PDF: {e}")
        await message.answer("⚠️ Не удалось создать файл. Попробуйте позже.")

# ============ ДАЛЕЕ СТАРЫЙ КОД (БЕЗ ИЗМЕНЕНИЙ В ЛОГИКЕ) ============

@dp.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    user_id = message.from_user.id
    user = get_user(user_id)
    if user["accepted"]:
        await message.answer(f"👋 С возвращением! Задайте вопрос или используйте /template для создания документов.")
        await state.set_state(UserForm.waiting_for_question)
        return
    await message.answer("⚖️ Правовой AI-Консультант. Принимаю текст и голос 🎙")
    await asyncio.sleep(2)
    agreement = FSInputFile("agreement.pdf")
    faq = FSInputFile("faq.pdf")
    await message.answer_document(agreement, caption="📄 Пользовательское соглашение")
    await message.answer_document(faq, caption="❓ Часто задаваемые вопросы (FAQ)")
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✅ Соглашаюсь", callback_data="accept")]])
    await message.answer("📋 Примите условия использования:", reply_markup=keyboard)

@dp.callback_query(F.data == "accept")
async def process_accept(callback: CallbackQuery, state: FSMContext):
    user = get_user(callback.from_user.id)
    user["accepted"] = True
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🚫 Пропустить", callback_data="skip_city")]])
    await callback.message.answer("🏙 Укажите ваш город:", reply_markup=keyboard)
    await state.set_state(UserForm.waiting_for_city)
    await callback.answer()

@dp.callback_query(F.data == "skip_city")
async def process_skip_city(callback: CallbackQuery, state: FSMContext):
    user = get_user(callback.from_user.id)
    user["city"] = None
    await callback.message.answer("Опишите проблему текстом или голосом 🎙:")
    await state.set_state(UserForm.waiting_for_question)

@dp.message(UserForm.waiting_for_city)
async def process_city_input(message: Message, state: FSMContext):
    user = get_user(message.from_user.id)
    user["city"] = message.text
    await message.answer("Город сохранен. Опишите ситуацию:")
    await state.set_state(UserForm.waiting_for_question)

@dp.message(UserForm.waiting_for_question, F.voice)
async def voice_q(message: Message, state: FSMContext):
    user = get_user(message.from_user.id)
    status_msg = await message.answer("🎙 Распознаю голос...")
    text = await transcribe_voice_message(message)
    if not text:
        await status_msg.edit_text("Не разобрал речь. Повторите четче.")
        return
    await status_msg.edit_text(f"🎙 Ваш запрос: {text}\n\n🔍 Анализирую...")
    await run_ai(message, text, user)

@dp.message(UserForm.waiting_for_question, F.text)
async def text_q(message: Message, state: FSMContext):
    user = get_user(message.from_user.id)
    if message.text.lower() in ["сменить город", "другой город"]:
        await message.answer("Введите город:")
        await state.set_state(UserForm.waiting_for_city)
        return
    msg = await message.answer("🔍 Анализирую...")
    await run_ai(message, message.text, user, msg)

async def run_ai(message, text, user, temp_msg=None):
    try:
        user["history"].append({"role": "user", "content": text})
        if len(user["history"]) > 6: user["history"] = user["history"][-6:]
        res = giga.chat({"messages": [{"role": "system", "content": SYSTEM_PROMPT}] + user["history"]})
        ans = res.choices[0].message.content
        user["history"].append({"role": "assistant", "content": ans})
        if temp_msg: await temp_msg.delete()
        await message.answer(ans + "\n\n<i>/lawyer - связь с юристом</i>", parse_mode="HTML")
    except Exception as e:
        logging.error(f"AI Error: {e}")
        await message.answer("Ошибка AI. Попробуйте позже.")

async def main():
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())