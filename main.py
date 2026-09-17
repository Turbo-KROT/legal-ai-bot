import asyncio
import logging
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

# База данных пользователей в памяти
# db = { user_id: {"accepted": True, "city": "Москва", "history": []} }
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
- Не выдумывай статьи. Если не уверен — рекомендуй записаться к живиму юристу через /lawyer.
- Ответ до 1500 символов."""

# ============ ВПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ============
def get_user(user_id: int):
    if user_id not in db:
        db[user_id] = {"accepted": False, "city": None, "history": []}
    return db[user_id]

# Ключевые слова для смены города
CITY_CHANGE_KEYWORDS = ["сменить город", "поменять город", "другой город", "неправильный город", "изменить город", "смена города"]


# ============ ОБРАБОТЧИК /start ============
@dp.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    user_id = message.from_user.id
    user = get_user(user_id)
    
    # Если пользователь УЖЕ принимал соглашение
    if user["accepted"]:
        city_info = f" (Ваш город: <b>{user['city']}</b>)" if user['city'] else ""
        await message.answer(
            f"👋 <b>С возвращением!</b>{city_info}\n\n"
            f"Я готов продолжить работу. Задайте ваш вопрос или опишите ситуацию.\n\n"
            f"💡 <i>Чтобы сменить город, напишите «Сменить город».</i>",
            parse_mode="HTML"
        )
        await state.set_state(UserForm.waiting_for_question)
        return

    # Если НОВЫЙ пользователь
    welcome_text = (
        "⚖️ <b>Правовой AI-Консультант</b>\n\n"
        "Профессиональный сервис экспресс-анализа юридических ситуаций, "
        "проверки документов и подготовки правовых решений на базе искусственного интеллекта.\n\n"
        "⚡️ <i>Анализ ситуаций за 5 секунд • Работа 24/7</i>"
    )
    await message.answer(welcome_text, parse_mode="HTML")
    
    # Пауза 2.5 секунды перед документами
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


# ============ ПРИНЯТИЕ СОГЛАШЕНИЯ ============
@dp.callback_query(F.data == "accept")
async def process_accept(callback: CallbackQuery, state: FSMContext):
    user = get_user(callback.from_user.id)
    user["accepted"] = True
    
    await callback.message.edit_text("✅ <b>Условия использования приняты.</b>", parse_mode="HTML")
    
    # Запрос города с кнопкой [Пропустить]
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


# ============ КНОПКА [ПРОПУСТИТЬ ГОРОД] ============
@dp.callback_query(F.data == "skip_city")
async def process_skip_city(callback: CallbackQuery, state: FSMContext):
    user = get_user(callback.from_user.id)
    user["city"] = None
    
    await callback.message.edit_text("📍 <b>Город не указан.</b>", parse_mode="HTML")
    await callback.message.answer("Опишите вашу проблему или задайте юридический вопрос:")
    await state.set_state(UserForm.waiting_for_question)
    await callback.answer()


# ============ ВВОД ГОРОДА ============
@dp.message(UserForm.waiting_for_city)
async def process_city_input(message: Message, state: FSMContext):
    text = message.text.strip()
    
    # Если случайно ввел ключевое слово смены города
    if text.lower() in CITY_CHANGE_KEYWORDS:
        await message.answer("Введите название вашего города:")
        return

    user = get_user(message.from_user.id)
    user["city"] = text
    
    await message.answer(
        f"📍 Населенный пункт сохранен: <b>{text}</b>\n\n"
        f"Опишите вашу правовую ситуацию или задайте вопрос:",
        parse_mode="HTML"
    )
    await state.set_state(UserForm.waiting_for_question)


# ============ ОБРАБОТКА ВОПРОСОВ И ИСТОРИИ ДИАЛОГА ============
@dp.message(UserForm.waiting_for_question)
@dp.message(F.text)
async def process_question(message: Message, state: FSMContext):
    user_id = message.from_user.id
    text = message.text.strip()
    user = get_user(user_id)

    # Проверка на запрос смены города
    if text.lower() in CITY_CHANGE_KEYWORDS:
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🚫 Сбросить город", callback_data="skip_city")]
        ])
        await message.answer("🏙 Введите новое название города или нажмите кнопку:", reply_markup=keyboard)
        await state.set_state(UserForm.waiting_for_city)
        return

    thinking_msg = await message.answer("🔍 <i>Анализирую правовую ситуацию и законодательство...</i>", parse_mode="HTML")

    try:
        # Сохраняем вопрос пользователя в историю
        user["history"].append({"role": "user", "content": text})
        
        # Храним только последние 6 сообщений (чтобы не перегружать память)
        if len(user["history"]) > 6:
            user["history"] = user["history"][-6:]

        # Формируем контекст
        city_context = f"\nРегион/Город пользователя: {user['city']}" if user['city'] else ""
        system_content = SYSTEM_PROMPT + city_context

        # Собираем запрос с историей
        messages_payload = [{"role": "system", "content": system_content}] + user["history"]

        # Вызов GigaChat
        response = giga.chat({"messages": messages_payload})
        answer = response.choices[0].message.content

        # Сохраняем ответ бота в историю
        user["history"].append({"role": "assistant", "content": answer})

        await thinking_msg.delete()

        footer = "\n\n<i>ℹ️ Информация носит справочный характер. Связь с юристом: /lawyer</i>"
        await message.answer(answer + footer, parse_mode="HTML")

    except Exception as e:
        logging.error(f"Ошибка GigaChat: {e}")
        await thinking_msg.delete()
        await message.answer("⚠️ Произошла ошибка при обращении к AI. Попробуйте переформулировать вопрос.")


async def main():
    logging.info("Бот запущен.")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())