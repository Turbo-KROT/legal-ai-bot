import asyncio
import logging
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

# Настройка логирования (чтобы видеть, что происходит)
logging.basicConfig(level=logging.INFO)

# Создаём бота и диспетчер
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

# Создаём клиент GigaChat
giga = GigaChat(
    credentials=GIGACHAT_CREDENTIALS,
    verify_ssl_certs=False,
    model="GigaChat"
)

# Простое хранилище пользователей (в памяти)
# Формат: {user_id: {"accepted": True/False, "city": "Москва"}}
users_data = {}


# ============ СОСТОЯНИЯ ДЛЯ АНКЕТЫ ============
class UserForm(StatesGroup):
    waiting_for_city = State()
    waiting_for_question = State()


# ============ СИСТЕМНЫЙ ПРОМПТ ДЛЯ AI ============
SYSTEM_PROMPT = """Ты — опытный юридический консультант с 20-летним стажем работы 
в российском праве. Ты специализируешься на всех отраслях права РФ: 
гражданском, уголовном, административном, трудовом, семейном, жилищном, 
налоговом, земельном.

Твои задачи:
1. Отвечать на юридические вопросы пользователей, ссылаясь на конкретные 
   статьи российских кодексов и федеральных законов.
2. Давать чёткие практические рекомендации: что делать, куда обращаться, 
   какие документы нужны.
3. Учитывать регион (город) пользователя, если это влияет на подсудность 
   или региональное законодательство.

Формат ответа:
📌 КРАТКИЙ ОТВЕТ (2-3 предложения по сути)
📖 ОБОСНОВАНИЕ (ссылки на статьи законов)
💡 РЕКОМЕНДАЦИИ (пошаговый план действий)
⚠️ ВАЖНО (риски, сроки, подводные камни)

Правила:
- Отвечай простым языком, без сложного юридического жаргона.
- Ссылайся ТОЛЬКО на реально существующие статьи. Если не уверен — 
  честно скажи: «Ваша ситуация требует индивидуального разбора 
  специалистом. Рекомендую бесплатную консультацию юриста-партнёра. 
  Напишите /lawyer».
- НИКОГДА не говори «я не могу» или «я не знаю». Вместо этого 
  предлагай обратиться к юристу.
- Уголовные дела, сложные споры о наследстве и разделе имущества — 
  всегда рекомендуй консультацию юриста через /lawyer.
- Ответ должен быть не длиннее 1500 символов."""


# ============ ОБРАБОТЧИК /start И "Привет" ============
@dp.message(CommandStart())
@dp.message(F.text.lower().in_(["привет", "здравствуйте", "здравствуй", "hi", "hello"]))
async def cmd_start(message: Message):
    user_id = message.from_user.id
    
    # Приветственное сообщение
    welcome_text = (
        "👋 <b>Приветствую! Я — правовой AI-консультант.</b>\n\n"
        "Помогу вам разобраться в юридических вопросах:\n\n"
        "🔹 Отвечу на вопросы по любой отрасли права РФ\n"
        "🔹 Проанализирую ваш документ (фото или PDF)\n"
        "🔹 Помогу составить шаблон документа\n"
        "🔹 Дам пошаговый план действий в вашей ситуации\n"
        "🔹 При необходимости — соединю с живым юристом\n\n"
        "Работаю 24/7, отвечаю за секунды."
    )
    await message.answer(welcome_text, parse_mode="HTML")
    
    # Отправляем PDF с соглашением и FAQ
    try:
        agreement = FSInputFile("agreement.pdf")
        faq = FSInputFile("faq.pdf")
        await message.answer_document(agreement, caption="📄 Пользовательское соглашение")
        await message.answer_document(faq, caption="❓ Часто задаваемые вопросы (FAQ)")
    except Exception as e:
        logging.error(f"Ошибка отправки PDF: {e}")
        await message.answer("⚠️ Не удалось загрузить документы. Обратитесь к администратору.")
    
    # Сообщение с кнопками согласия
    consent_text = (
        "📋 <b>Перед началом работы</b>\n\n"
        "Пожалуйста, ознакомьтесь с Пользовательским соглашением и FAQ выше.\n\n"
        "Нажимая «Соглашаюсь», вы подтверждаете, что:\n"
        "• Бот является информационным помощником, а не юристом\n"
        "• Ответы могут содержать неточности\n"
        "• Документы, создаваемые ботом, не являются официальными\n"
        "• Ваши данные не хранятся и не передаются без вашего согласия"
    )
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Соглашаюсь", callback_data="accept")],
        [InlineKeyboardButton(text="❌ Не соглашаюсь", callback_data="decline")]
    ])
    
    await message.answer(consent_text, reply_markup=keyboard, parse_mode="HTML")


# ============ ОБРАБОТЧИК КНОПКИ "СОГЛАШАЮСЬ" ============
@dp.callback_query(F.data == "accept")
async def process_accept(callback: CallbackQuery, state: FSMContext):
    user_id = callback.from_user.id
    users_data[user_id] = {"accepted": True, "city": None}
    
    await callback.message.edit_text(
        "✅ <b>Спасибо! Вы приняли условия использования.</b>",
        parse_mode="HTML"
    )
    
    await callback.message.answer(
        "🏙 <b>Из какого вы города?</b>\n\n"
        "Это нужно, чтобы учитывать региональные особенности и подсудность.",
        parse_mode="HTML"
    )
    
    # Переводим пользователя в состояние ожидания города
    await state.set_state(UserForm.waiting_for_city)
    await callback.answer()


# ============ ОБРАБОТЧИК КНОПКИ "НЕ СОГЛАШАЮСЬ" ============
@dp.callback_query(F.data == "decline")
async def process_decline(callback: CallbackQuery):
    await callback.message.edit_text(
        "😔 К сожалению, без принятия условий использование сервиса невозможно.\n\n"
        "Если передумаете — напишите /start"
    )
    await callback.answer()


# ============ ОБРАБОТЧИК ВВОДА ГОРОДА ============
@dp.message(UserForm.waiting_for_city)
async def process_city(message: Message, state: FSMContext):
    user_id = message.from_user.id
    city = message.text.strip()
    
    # Сохраняем город
    if user_id not in users_data:
        users_data[user_id] = {"accepted": True, "city": city}
    else:
        users_data[user_id]["city"] = city
    
    await message.answer(
        f"📍 Отлично, ваш город: <b>{city}</b>\n\n"
        f"❓ <b>Опишите ваш вопрос или ситуацию.</b>\n\n"
        f"Например:\n"
        f"• «Работодатель не выплатил зарплату 2 месяца»\n"
        f"• «Соседи затопили квартиру, что делать?»\n"
        f"• «Как расторгнуть договор с фитнес-клубом?»",
        parse_mode="HTML"
    )
    
    await state.set_state(UserForm.waiting_for_question)


# ============ ОБРАБОТЧИК ВОПРОСА ПОЛЬЗОВАТЕЛЯ ============
@dp.message(UserForm.waiting_for_question)
async def process_question(message: Message, state: FSMContext):
    user_id = message.from_user.id
    question = message.text.strip()
    city = users_data.get(user_id, {}).get("city", "не указан")
    
    # Показываем, что бот думает
    thinking_msg = await message.answer("🤔 Анализирую вашу ситуацию...")
    
    try:
        # Формируем полный запрос
        full_prompt = (
            f"Город пользователя: {city}\n\n"
            f"Вопрос: {question}"
        )
        
        # Отправляем в GigaChat
        response = giga.chat({
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": full_prompt}
            ],
            "temperature": 0.3,
            "max_tokens": 1500
        })
        
        answer = response.choices[0].message.content
        
        # Удаляем сообщение "думаю..."
        await thinking_msg.delete()
        
        # Отправляем ответ + короткий футер
        footer = "\n\n<i>ℹ️ Информация носит справочный характер. Для точного решения: /lawyer</i>"
        await message.answer(answer + footer, parse_mode="HTML")
        
        # Предлагаем задать следующий вопрос
        await message.answer(
            "💬 Задайте следующий вопрос или напишите /start для сброса."
        )
        
    except Exception as e:
        logging.error(f"Ошибка GigaChat: {e}")
        await thinking_msg.delete()
        await message.answer(
            "⚠️ Произошла техническая ошибка. Попробуйте ещё раз через минуту.\n\n"
            "Если ошибка повторяется — напишите /start"
        )
    
    # Остаёмся в том же состоянии, чтобы можно было задавать ещё вопросы
    # (не сбрасываем state)


# ============ ЗАПУСК БОТА ============
async def main():
    logging.info("Бот запускается...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())