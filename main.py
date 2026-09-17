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

# База данных пользователей
db = {}

class UserForm(StatesGroup):
    waiting_for_city = State()
    waiting_for_question = State()

SYSTEM_PROMPT = """Ты — высококвалифицированный юридический AI-консультант по праву РФ.
Твоя задача — вести грамотный, профессиональный диалог с пользователем, анализировать его ситуацию и давать точные юридические разборы со ссылками на законы РФ (ГК, УК, ТК, КоАП, СК, ЖК и др.).

Формат ответа:
📌 КРАТКИЙ ВЕРДИКТ
📖 ПРАВОВОЕ ОБОСНОВАНИЕ (статьи законов)
💡 ПОШАГОВЫЙ ПЛАН ДЕЙСТВИЙ
⚠️ РИСКИ И СРОКИ

Правила:
- Учитывай контекст предыдущих сообщений в диалоге.
- Пиши строго, профессионально, но понятно.
- Не выдумывай статьи. Если не уверен — рекомендуй записаться к живому юристу через /lawyer.
- Ответ до 1500 символов."""

def get_user(user_id: int):
    if user_id not in db:
        db[user_id] = {"accepted": False, "city": None, "history": []}
    return db[user_id]

CITY_CHANGE_KEYWORDS = ["сменить город", "поменять город", "другой город", "неправильный город", "изменить город", "смена города"]

# ============ ФУНКЦИЯ РАСПОЗНАВАНИЯ ГОЛОСА ============
async def transcribe_voice_message(voice_message: Message) -> str:
    """Скачивает голосовое сообщение, конвертирует и распознает в текст"""
    file_id = voice_message.voice.file_id
    file = await bot.get_file(file_id)
    
    ogg_path = f"voice_{file_id}.ogg"
    wav_path = f"voice_{file_id}.wav"
    
    try:
        # Скачиваем .ogg
        await bot.download_file(file.file_path, ogg_path)
        
        # Конвертируем .ogg в .wav через ffmpeg
        subprocess.run(
            ["ffmpeg", "-y", "-i", ogg_path, wav_path],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=True
        )
        
        # Распознаем через Google Speech Recognition (бесплатно, язык русский)
        recognizer = sr.Recognizer()
        with sr.AudioFile(wav_path) as source:
            audio_data = recognizer.record(source)
            text = recognizer.recognize_google(audio_data, language="ru-RU")
            return text
            
    except sr.UnknownValueError:
        return None  # Не удалось разобрать речь
    except Exception as e:
        logging.error(f"Ошибка распознавания речи: {e}")
        return None
    finally:
        # Гарантированное удаление временных аудиофайлов (Zero-Retention)
        if os.path.exists(ogg_path):
            os.remove(ogg_path)
        if os.path.exists(wav_path):
            os.remove(wav_path)


# ============ ОБРАБОТЧИК /start ============
@dp.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    user_id = message.from_user.id
    user = get_user(user_id)
    
    if user["accepted"]:
        city_info = f" (Ваш город: <b>{user['city']}</b>)" if user['city'] else ""
        await message.answer(
            f"👋 <b>С возвращением!</b>{city_info}\n\n"
            f"Задайте ваш вопрос текстом или <b>запишите голосовое сообщение</b> 🎙\n\n"
            f"💡 <i>Чтобы сменить город, напишите «Сменить город».</i>",
            parse_mode="HTML"
        )
        await state.set_state(UserForm.waiting_for_question)
        return

    welcome_text = (
        "⚖️ <b>Правовой AI-Консультант</b>\n\n"
        "Профессиональный сервис экспресс-анализа юридических ситуаций, "
        "проверки документов и подготовки правовых решений на базе искусственного интеллекта.\n\n"
        "🎙 <b>Принимаю текстовые и голосовые сообщения!</b>\n"
        "⚡️ <i>Анализ ситуаций за секунды • Работа 24/7</i>"
    )
    await message.answer(welcome_text, parse_mode="HTML")
    
    await asyncio.sleep(2.5)
    
    try:
        agreement = FSInputFile("agreement.pdf")
        faq = FSInputFile("faq.pdf")
        await message.answer_document(agreement, caption="📄 Пользовательское соглашение")
        await message.answer_document(faq, caption="❓ Часто задаваемые вопросы (FAQ)")
    except Exception as e:
        logging.error(f"Ошибка отправки PDF: {e}")

    consent_text = (
        "📋 <b>Условия использования сервиса</b>\n\n"
        "Перед началом работы ознакомьтесь с Соглашением и FAQ выше.\n"
        "Нажимая «Соглашаюсь», вы подтверждаете acceptance условий."
    )
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Соглашаюсь", callback_data="accept")],
        [InlineKeyboardButton(text="❌ Не соглашаюсь", callback_data="decline")]
    ])
    
    await message.answer(consent_text, reply_markup=keyboard, parse_mode="HTML")


@dp.callback_query(F.data == "accept")
async def process_accept(callback: CallbackQuery, state: FSMContext):
    user = get_user(callback.from_user.id)
    user["accepted"] = True
    
    await callback.message.edit_text("✅ <b>Условия использования приняты.</b>", parse_mode="HTML")
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🚫 Не указывать (Пропустить)", callback_data="skip_city")]
    ])
    
    await callback.message.answer(
        "🏙 <b>Укажите ваш город или населенный пункт:</b>\n\n"
        "Это необходимо для учета регионального законодательства и подсудности судов.",
        reply_markup=keyboard,
        parse_mode="HTML"
    )
    await state.set_state(UserForm.waiting_for_city)
    await callback.answer()


@dp.callback_query(F.data == "decline")
async def process_decline(callback: CallbackQuery):
    await callback.message.edit_text("😔 Без принятия условий доступ к сервису ограничен. Напишите /start для повтора.")
    await callback.answer()


@dp.callback_query(F.data == "skip_city")
async def process_skip_city(callback: CallbackQuery, state: FSMContext):
    user = get_user(callback.from_user.id)
    user["city"] = None
    
    await callback.message.edit_text("📍 <b>Город не указан.</b>", parse_mode="HTML")
    await callback.message.answer("Опишите вашу проблему текстом или отправьте голосовое сообщение 🎙:")
    await state.set_state(UserForm.waiting_for_question)
    await callback.answer()


@dp.message(UserForm.waiting_for_city)
async def process_city_input(message: Message, state: FSMContext):
    text = message.text.strip() if message.text else ""
    
    if text.lower() in CITY_CHANGE_KEYWORDS:
        await message.answer("Введите название вашего города:")
        return

    user = get_user(message.from_user.id)
    user["city"] = text
    
    await message.answer(
        f"📍 Населенный пункт сохранен: <b>{text}</b>\n\n"
        f"Опишите вашу ситуацию текстом или <b>отправьте голосовое сообщение 🎙</b>:",
        parse_mode="HTML"
    )
    await state.set_state(UserForm.waiting_for_question)


# ============ ОБРАБОТКА ГОЛОСОВЫХ СООБЩЕНИЙ ============
@dp.message(UserForm.waiting_for_question, F.voice)
@dp.message(F.voice)
async def process_voice_question(message: Message, state: FSMContext):
    user_id = message.from_user.id
    user = get_user(user_id)

    if not user["accepted"]:
        await message.answer("⚠️ Пожалуйста, сначала примите условия использования: напишите /start")
        return

    status_msg = await message.answer("🎙 <i>Распознаю голосовое сообщение...</i>", parse_mode="HTML")
    
    # Распознаем речь
    recognized_text = await transcribe_voice_message(message)
    
    if not recognized_text:
        await status_msg.edit_text(
            "🎙 ⚠️ <b>Не удалось четко разобрать речь.</b>\n\n"
            "Пожалуйста, повторите запись чуть громче и медленнее или напишите вопрос текстом.",
            parse_mode="HTML"
        )
        return

    # Показываем человеку распознанный текст
    await status_msg.edit_text(
        f"🎙 <b>Распознанный запрос:</b>\n«<i>{recognized_text}</i>»\n\n"
        f"🔍 <i>Анализирую правовую ситуацию...</i>",
        parse_mode="HTML"
    )
    
    # Передаем распознанный текст в обработчик ИИ
    await run_ai_analysis(message, recognized_text, user)


# ============ ОБРАБОТКА ТЕКСТОВЫХ ВОПРОСОВ ============
@dp.message(UserForm.waiting_for_question, F.text)
@dp.message(F.text)
async def process_text_question(message: Message, state: FSMContext):
    user_id = message.from_user.id
    text = message.text.strip()
    user = get_user(user_id)

    if not user["accepted"]:
        await message.answer("⚠️ Пожалуйста, сначала примите условия использования: напишите /start")
        return

    if text.lower() in CITY_CHANGE_KEYWORDS:
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🚫 Сбросить город", callback_data="skip_city")]
        ])
        await message.answer("🏙 Введите новое название города или нажмите кнопку:", reply_markup=keyboard)
        await state.set_state(UserForm.waiting_for_city)
        return

    thinking_msg = await message.answer("🔍 <i>Анализирую правовую ситуацию и законодательство...</i>", parse_mode="HTML")
    await run_ai_analysis(message, text, user, thinking_msg)


# ============ ОБЩАЯ ФУНКЦИЯ ВЫЗОВА ИИ ============
async def run_ai_analysis(message: Message, question_text: str, user: dict, temp_msg: Message = None):
    try:
        user["history"].append({"role": "user", "content": question_text})
        
        if len(user["history"]) > 6:
            user["history"] = user["history"][-6:]

        city_context = f"\nРегион/Город пользователя: {user['city']}" if user['city'] else ""
        system_content = SYSTEM_PROMPT + city_context

        messages_payload = [{"role": "system", "content": system_content}] + user["history"]

        response = giga.chat({"messages": messages_payload})
        answer = response.choices[0].message.content

        user["history"].append({"role": "assistant", "content": answer})

        if temp_msg:
            await temp_msg.delete()

        footer = "\n\n<i>ℹ️ Информация носит справочный характер. Связь с юристом: /lawyer</i>"
        await message.answer(answer + footer, parse_mode="HTML")

    except Exception as e:
        logging.error(f"Ошибка GigaChat: {e}")
        if temp_msg:
            await temp_msg.delete()
        await message.answer("⚠️ Произошла ошибка при обращении к AI. Попробуйте переформулировать вопрос.")


async def main():
    logging.info("Бот запущен с поддержкой голосовых сообщений.")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())