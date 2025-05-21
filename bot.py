import asyncio
import logging
import json
import sqlite3
import uuid
import sys
import re
import aiohttp
import html
from PIL import Image
import io
import pdfplumber
import pytesseract
import os
from datetime import datetime
import pytz

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart, Command, Filter
from aiogram.types import (
    Message, CallbackQuery, BotCommand,
    ReplyKeyboardMarkup, KeyboardButton,
    InlineKeyboardMarkup, InlineKeyboardButton
)
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.utils.keyboard import InlineKeyboardBuilder

# Import Database class
from database import Database

try:
    from config import (
        BOT_TOKEN, ADMIN_ID, CHANNEL_ID, CHANNEL_INVITE_LINK,
        REFERRAL_TARGET_COUNT, REFERRAL_BONUS_REQUESTS, INITIAL_REQUESTS, DB_FILE,
        WELCOME_TEXT, HELP_TEXT, START_WORK_TEXT, SUBSCRIPTION_PROMPT_PREFIX,
        SUBSCRIPTION_THANKS_TEXT, SUBSCRIPTION_NOT_YET_TEXT, NO_REQUESTS_TEXT,
        REFERRAL_INFO_TEXT,
        GOOGLE_AI_STUDIO_API_KEY, UNIVERSAL_AI_ENDPOINT_URL,
        SELECTED_MODEL_IO_NET_ID, AI_SYSTEM_PROMPT
    )
except ImportError:
    print("Ошибка: Файл config.py не найден или в нем отсутствуют необходимые переменные.")
    exit()

# Logging configuration
log_dir = "logs"
if not os.path.exists(log_dir):
    os.makedirs(log_dir)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(name)s - %(message)s",
    handlers=[
        logging.FileHandler("logs/bot.log", encoding='utf-8'),  # Use UTF-8 for log file
        logging.StreamHandler(sys.stdout)  # Use sys.stdout for console
    ]
)
logger = logging.getLogger(__name__)

# Tesseract path for Linux (adjust if needed)
pytesseract.pytesseract.tesseract_cmd = "/usr/bin/tesseract"
os.environ['TESSDATA_PREFIX'] = "/usr/share/tesseract-ocr/5/tessdata/"

# Bot initialization
default_props = DefaultBotProperties(parse_mode=ParseMode.HTML)
bot = Bot(token=BOT_TOKEN, default=default_props)
dp = Dispatcher()
router = Router()

# Initialize Database
db = Database(DB_FILE)

# Bot logic
class BroadcastStates(StatesGroup):
    waiting_for_message = State()
    waiting_for_media = State()
    waiting_for_button_text = State()
    waiting_for_button_url = State()

class AdminEditUser(StatesGroup):
    waiting_for_user_id_info = State()
    waiting_for_user_id_requests = State()
    waiting_for_requests_amount = State()
    waiting_for_referral_requests = State()
    waiting_for_bulk_referral_requests = State()

class AdminFilter(Filter):
    async def __call__(self, message_or_callback: Message | CallbackQuery) -> bool:
        return message_or_callback.from_user.id in ADMIN_ID

async def daily_balance_update():
    msk_tz = pytz.timezone('Europe/Moscow')
    last_update = None
    while True:
        now = datetime.now(msk_tz)
        current_date = now.date()
        if last_update != current_date and now.hour == 0 and now.minute == 0:
            try:
                user_ids = db.get_all_user_ids()
                for user_id in user_ids:
                    user = db.get_user(int(user_id))
                    if user:
                        current_requests = user.get('requests_left', 0)
                        start_requests = user.get('requests_at_start_of_day', 5)
                        if current_requests < start_requests:  # Check if requests were spent
                            new_balance = 5  # Replenish to 5
                            db.update_user(int(user_id), {
                                'requests_left': new_balance,
                                'requests_at_start_of_day': new_balance  # Reset start balance
                            })
                            if user.get('notifications_enabled', True):
                                try:
                                    await bot.send_message(
                                        int(user_id),
                                        f"Ежедневный бонус: баланс пополнен до {new_balance} запросов! 🎉"
                                    )
                                except Exception as e:
                                    logger.warning(f"Не удалось уведомить {user_id} о бонусе: {e}")
                        else:
                            # Reset start balance to current balance for the new day
                            db.update_user(int(user_id), {
                                'requests_at_start_of_day': current_requests
                            })
                            logger.info(f"Пользователь {user_id} не тратил запросы: {current_requests} запросов")
                logger.info(f"Ежедневное начисление выполнено для {len(user_ids)} пользователей")
                last_update = current_date
            except Exception as e:
                logger.error(f"Ошибка при ежедневном начислении: {e}")
        await asyncio.sleep(60)

async def get_channel_button_url() -> str | None:
    if CHANNEL_INVITE_LINK and isinstance(CHANNEL_ID, int) and CHANNEL_ID < 0:
        return CHANNEL_INVITE_LINK
    if isinstance(CHANNEL_ID, str) and not CHANNEL_ID.startswith("-100"):
        return f"https://t.me/{CHANNEL_ID.lstrip('@')}"
    if isinstance(CHANNEL_ID, int) and CHANNEL_INVITE_LINK:
        return CHANNEL_INVITE_LINK
    logger.warning(f"Не удалось сформировать URL для '{CHANNEL_ID}'.")
    return None

async def is_user_subscribed(user_id: int) -> bool:
    if not CHANNEL_ID:
        logger.warning("CHANNEL_ID не установлен.")
        return True
    try:
        member = await bot.get_chat_member(chat_id=CHANNEL_ID, user_id=user_id)
        return member.status.lower() in ['member', 'administrator', 'creator']
    except Exception as e:
        logger.error(f"Ошибка проверки подписки {user_id} на {CHANNEL_ID}: {e}")
        return False  # Return False to prompt user to subscribe

async def send_subscription_prompt(chat_id: int, custom_text: str | None = None):
    builder = InlineKeyboardBuilder()
    button_url = await get_channel_button_url()
    prompt_text = custom_text or SUBSCRIPTION_PROMPT_PREFIX
    if button_url:
        builder.button(text="➡️ Подписаться на канал", url=button_url)
    else:
        prompt_text += (f"\n\nКанал: {CHANNEL_ID}. Найдите вручную.")
    builder.button(text="✅ Я подписался", callback_data="check_subscription")
    builder.adjust(1)
    await bot.send_message(chat_id, prompt_text, reply_markup=builder.as_markup())

def get_main_keyboard(is_admin: bool = False):
    kb = [
        [KeyboardButton(text="🚀 Начать работу с ГДЗ AI")],
        [KeyboardButton(text="❓ ПОМОЩЬ"), KeyboardButton(text="👫💸 Пригласи друга")],
        [KeyboardButton(text="⚙️ Настройки")]
    ]
    if is_admin:
        kb.append([KeyboardButton(text="👑 Админ-панель")])
    return ReplyKeyboardMarkup(keyboard=kb, resize_keyboard=True)

async def extract_text_from_image(file_id: str) -> str:
    try:
        file = await bot.get_file(file_id)
        file_path = file.file_path
        file_bytes = await bot.download_file(file_path)
        img = Image.open(io.BytesIO(file_bytes.read()))
        logger.info(f"Размер изображения: {img.size}")
        text = pytesseract.image_to_string(img, lang='eng+rus')
        print(f"Extracted text: {text}")  # Add this line
        logger.info(f"Извлечен текст из изображения: {text[:100]}...")
        return text.strip() if text.strip() else "Не удалось извлечь текст из изображения."
    except Exception as e:
        logger.error(f"Ошибка OCR: {e}")
        return f"Ошибка извлечения текста из изображения: {e}"

async def extract_text_from_pdf(file_id: str) -> str:
    try:
        file = await bot.get_file(file_id)
        file_path = file.file_path
        file_bytes = await bot.download_file(file_path)
        with pdfplumber.open(io.BytesIO(file_bytes.read())) as pdf:
            text = ""
            for page in pdf.pages:
                page_text = page.extract_text()
                if page_text:
                    text += page_text + "\n"
            logger.info(f"Извлечен текст из PDF: {text[:100]}...")
            return text.strip() if text.strip() else "Не удалось извлечь текст из PDF."
    except Exception as e:
        logger.error(f"Ошибка обработки PDF: {e}")
        return f"Ошибка извлечения текста из PDF: {e}"

async def get_ai_response(user_prompt: str) -> str | None:
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {GOOGLE_AI_STUDIO_API_KEY}"}
    payload = {
        "model": SELECTED_MODEL_IO_NET_ID,
        "messages": [
            {"role": "system", "content": AI_SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt}
        ]
    }
    logger.info(f"Запрос к AI: {json.dumps(payload, ensure_ascii=False)[:200]}...")
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=180)) as session:
            async with session.post(UNIVERSAL_AI_ENDPOINT_URL, headers=headers, json=payload) as response:
                response_text = await response.text()
                logger.info(f"Ответ AI (статус {response.status}): {response_text[:200]}...")
                try:
                    response_data = json.loads(response_text)
                except json.JSONDecodeError:
                    logger.error(f"Не JSON от AI: {response_text}")
                    return f"🤖 Ошибка AI: не JSON (статус {response.status})."
                if response.status == 200:
                    content = response_data.get("choices", [{}])[0].get("message", {}).get("content", "")
                    clean_answer = content.replace("**", "").strip()
                    logger.info(f"AI Response: {clean_answer[:100]}...")
                    return clean_answer if clean_answer else "🤔 Ответ AI пуст."
                else:
                    error = response_data.get("error", {}).get("message", "Нет деталей.")
                    logger.error(f"Ошибка AI: Статус {response.status}, Детали: {error}")
                    return f"🤖 Ошибка AI ({response.status}): {error}."
    except Exception as e:
        logger.error(f"Ошибка AI API: {e}")
        return "🤖 Ошибка соединения с AI."

async def create_broadcast_buttons(button_text: str, button_url: str, broadcast_id: str) -> InlineKeyboardMarkup | None:
    if not button_text or not button_url:
        return None
    builder = InlineKeyboardBuilder()
    if re.match(r'https?://[^\s]+', button_url):
        builder.button(text=button_text, url=button_url, callback_data=f"broadcast_{broadcast_id}")
        builder.adjust(1)
        return builder.as_markup()
    logger.warning(f"Невалидный URL: {button_url}")
    return None

async def send_preview(message: Message, broadcast_text: str, broadcast_media: dict | None,
                      buttons: InlineKeyboardMarkup | None, broadcast_id: str):
    try:
        if not broadcast_text and not broadcast_media:
            await message.answer("Ошибка: необходимо указать текст или медиа для рассылки.")
            return
        if broadcast_media:
            if broadcast_media["type"] == "photo":
                await message.answer_photo(
                    broadcast_media["id"],
                    caption=broadcast_text or " ",
                    parse_mode=ParseMode.HTML,
                    reply_markup=buttons
                )
            elif broadcast_media["type"] == "video":
                await message.answer_video(
                    broadcast_media["id"],
                    caption=broadcast_text or " ",
                    parse_mode=ParseMode.HTML,
                    reply_markup=buttons
                )
            elif broadcast_media["type"] == "animation":
                await message.answer_animation(
                    broadcast_media["id"],
                    caption=broadcast_text or " ",
                    parse_mode=ParseMode.HTML,
                    reply_markup=buttons
                )
        else:
            await message.answer(broadcast_text, parse_mode=ParseMode.HTML, reply_markup=buttons)
        builder = InlineKeyboardBuilder()
        builder.button(text="✅ Отправить", callback_data=f"broadcast_send_{broadcast_id}")
        builder.button(text="❌ Отменить", callback_data="broadcast_cancel")
        builder.adjust(1)
        await message.answer("Предпросмотр выше. Отправить?", reply_markup=builder.as_markup())
    except Exception as e:
        logger.error(f"Ошибка предпросмотра: {e}")
        await message.answer("Ошибка предпросмотра. Попробуйте снова.")

@router.message(F.text == "👑 Админ-панель", AdminFilter())
async def admin_panel_button_handler(message: Message):
    await cmd_admin_panel(message)

@router.message(Command("admin"), AdminFilter())
async def cmd_admin_panel(message: Message):
    logger.info(f"{message.from_user.id} вошел в админ-панель.")
    kb = InlineKeyboardBuilder()
    kb.button(text="✉️ Рассылка", callback_data="admin:broadcast")
    kb.button(text="📊 Статистика", callback_data="admin:stats")
    kb.button(text="👤 Инфо о юзере", callback_data="admin:user_info_prompt")
    kb.button(text="➕ Выдать запросы", callback_data="admin:add_req_prompt")
    kb.button(text="📈 Настр. реф. бонус", callback_data="admin:set_referral_requests")
    kb.button(text="🎁 Настр. бонус за 5 реф.", callback_data="admin:set_bulk_referral_requests")
    kb.adjust(1)
    await message.answer("👑 Админ-панель:", reply_markup=kb.as_markup())

@router.callback_query(F.data == "admin:stats", AdminFilter())
async def cb_admin_stats(callback: CallbackQuery):
    try:
        with sqlite3.connect(DB_FILE) as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT COUNT(*) FROM users')
            total = cursor.fetchone()[0]
            cursor.execute('SELECT COUNT(*) FROM users WHERE subscribed_to_channel = 1')
            subscribed = cursor.fetchone()[0]
            cursor.execute('SELECT COUNT(*) FROM users WHERE requests_left > 0')
            active = cursor.fetchone()[0]
            broadcasts = db.get_broadcasts()
            broadcast_stats = "\n".join(
                f"Рассылка {bid}: {len(data['clicks'])} кликов, текст: {data['text'][:50]}..."
                for bid, data in broadcasts.items()
            )
            settings = db.get_referral_settings()
            stats_message = (
                f"📊 Статистика:\n"
                f"Всего пользователей: {total}\n"
                f"Подписаны: {subscribed}\n"
                f"Активны (запросы > 0): {active}\n"
                f"Реф. бонус: {settings['referral_requests']} запросов/реферал\n"
                f"Бонус за 5 реф.: {settings['bulk_referral_requests']} запросов\n"
                f"Рассылки:\n{broadcast_stats or 'Нет рассылок'}"
            )
            await callback.message.answer(stats_message)
    except sqlite3.Error as e:
        logger.error(f"Ошибка статистики: {e}")
        await callback.message.answer("Ошибка получения статистики.")
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except:
        pass
    await callback.answer()

@router.callback_query(F.data == "admin:broadcast", AdminFilter())
async def cb_admin_broadcast_start(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await callback.message.answer("Текст для рассылки (HTML). /cancel_action для отмены.")
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except:
        pass
    await state.set_state(BroadcastStates.waiting_for_message)

@router.message(Command("cancel_action"), AdminFilter())
async def cmd_cancel_admin_fsm_action(message: Message, state: FSMContext):
    current_state = await state.get_state()
    if current_state is None:
        await message.answer("Нет активной операции.")
        return
    logger.info(f"Отмена {current_state} админом {message.from_user.id}")
    await state.clear()
    await message.answer("Операция отменена.")

@router.message(BroadcastStates.waiting_for_message, AdminFilter(), F.text)
async def process_broadcast_text(message: Message, state: FSMContext):
    if message.text == "/cancel_action":
        await cmd_cancel_admin_fsm_action(message, state)
        return
    await state.update_data(broadcast_text=message.html_text)
    await message.answer("Отправьте фото, видео или GIF для рассылки (или /skip для пропуска).")
    await state.set_state(BroadcastStates.waiting_for_media)

@router.message(BroadcastStates.waiting_for_media, AdminFilter(), F.photo | F.video | F.animation | F.text)
async def process_broadcast_media(message: Message, state: FSMContext):
    if message.text == "/cancel_action":
        await cmd_cancel_admin_fsm_action(message, state)
        return
    elif message.text == "/skip":
        await state.update_data(broadcast_media=None)
    elif message.photo:
        media_type = "photo"
        media_id = message.photo[-1].file_id
        await state.update_data(broadcast_media={"type": media_type, "id": media_id})
    elif message.video:
        media_type = "video"
        media_id = message.video.file_id
        await state.update_data(broadcast_media={"type": media_type, "id": media_id})
    elif message.animation:
        media_type = "animation"
        media_id = message.animation.file_id
        await state.update_data(broadcast_media={"type": media_type, "id": media_id})
    else:
        await message.answer("Пожалуйста, отправьте фото, видео, GIF или /skip.")
        return
    await message.answer("Введите название кнопки (или /skip). /cancel_action для отмены.")
    await state.set_state(BroadcastStates.waiting_for_button_text)

@router.message(BroadcastStates.waiting_for_message, AdminFilter(), ~F.text)
async def invalid_input_waiting_for_message(message: Message):
    await message.answer("Пожалуйста, отправьте текст для рассылки.")

@router.message(BroadcastStates.waiting_for_button_text, AdminFilter(), F.text)
async def process_broadcast_button_text(message: Message, state: FSMContext):
    if message.text == "/cancel_action":
        await cmd_cancel_admin_fsm_action(message, state)
        return
    elif message.text == "/skip":
        await state.update_data(button_text=None, button_url=None)
        data = await state.get_data()
        broadcast_text = data.get("broadcast_text")
        broadcast_media = data.get("broadcast_media")
        broadcast_id = str(uuid.uuid4())[:8]
        await send_preview(message, broadcast_text, broadcast_media, None, broadcast_id)
        await state.set_state(BroadcastStates.waiting_for_button_url)
        return
    await state.update_data(button_text=message.text)
    await message.answer("Введите URL для кнопки (или /skip). /cancel_action для отмены.")
    await state.set_state(BroadcastStates.waiting_for_button_url)

@router.message(BroadcastStates.waiting_for_button_url, AdminFilter(), F.text)
async def process_broadcast_button_url(message: Message, state: FSMContext):
    if message.text == "/cancel_action":
        await cmd_cancel_admin_fsm_action(message, state)
        return
    data = await state.get_data()
    broadcast_text = data.get("broadcast_text")
    broadcast_media = data.get("broadcast_media")
    button_text = data.get("button_text")
    broadcast_id = str(uuid.uuid4())[:8]
    buttons = None
    if message.text != "/skip" and button_text:
        button_url = message.text
        buttons = await create_broadcast_buttons(button_text, button_url, broadcast_id)
        if not buttons:
            await message.answer("Невалидный URL. Отправьте корректный URL или /skip.")
            return
        await state.update_data(button_url=button_url)
    else:
        await state.update_data(button_url=None)
    await send_preview(message, broadcast_text, broadcast_media, buttons, broadcast_id)

@router.callback_query(F.data.startswith("broadcast_send_"))
async def confirm_broadcast(callback: CallbackQuery, state: FSMContext):
    broadcast_id = callback.data.split("_")[2]
    data = await state.get_data()
    broadcast_text = data.get("broadcast_text")
    broadcast_media = data.get("broadcast_media")
    button_text = data.get("button_text")
    button_url = data.get("button_url")
    if not broadcast_text and not broadcast_media:
        await callback.message.answer("Ошибка: нужен текст или медиа.")
        await state.clear()
        try:
            await callback.message.delete()
        except:
            pass
        return
    if not broadcast_text and broadcast_media:
        broadcast_text = " "
    buttons = await create_broadcast_buttons(button_text, button_url, broadcast_id) if button_text and button_url else None
    db.add_broadcast(broadcast_id, broadcast_text, broadcast_media)
    users_ids = db.get_all_user_ids()
    sent, failed, blocked = 0, 0, 0
    for user_id_str in users_ids:
        try:
            if broadcast_media:
                if broadcast_media["type"] == "photo":
                    await bot.send_photo(
                        int(user_id_str),
                        broadcast_media["id"],
                        caption=broadcast_text,
                        parse_mode=ParseMode.HTML,
                        reply_markup=buttons
                    )
                elif broadcast_media["type"] == "video":
                    await bot.send_video(
                        int(user_id_str),
                        broadcast_media["id"],
                        caption=broadcast_text,
                        parse_mode=ParseMode.HTML,
                        reply_markup=buttons
                    )
                elif broadcast_media["type"] == "animation":
                    await bot.send_animation(
                        int(user_id_str),
                        broadcast_media["id"],
                        caption=broadcast_text,
                        parse_mode=ParseMode.HTML,
                        reply_markup=buttons
                    )
            else:
                await bot.send_message(
                    int(user_id_str),
                    broadcast_text,
                    parse_mode=ParseMode.HTML,
                    reply_markup=buttons
                )
            sent += 1
        except Exception as e:
            err_str = str(e).lower()
            if "bot was blocked" in err_str or "user is deactivated" in err_str or "chat not found" in err_str:
                blocked += 1
            else:
                failed += 1
                logger.error(f"Broadcast error to {user_id_str}: {e}")
        await asyncio.sleep(0.05)
    await callback.message.answer(f"Рассылка: ✅ {sent} 🚫 {blocked} ❌ {failed}")
    await state.clear()
    try:
        await callback.message.delete()
    except:
        pass

@router.callback_query(F.data == "broadcast_cancel")
async def cancel_broadcast(callback: CallbackQuery, state: FSMContext):
    await callback.message.answer("Рассылка отменена.")
    await state.clear()
    try:
        await callback.message.delete()
    except:
        pass

@router.callback_query(F.data.startswith("broadcast_"))
async def track_broadcast_click(callback: CallbackQuery):
    broadcast_id = callback.data.split("_")[1]
    user_id = str(callback.from_user.id)
    db.add_broadcast_click(broadcast_id, user_id)
    await callback.answer()

@router.callback_query(F.data == "admin:user_info_prompt", AdminFilter())
async def cb_admin_user_info_prompt(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await callback.message.answer("User ID для инфо? /cancel_action отмена.")
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except:
        pass
    await state.set_state(AdminEditUser.waiting_for_user_id_info)

@router.message(AdminEditUser.waiting_for_user_id_info, AdminFilter(), F.text)
async def process_user_id_for_info(message: Message, state: FSMContext):
    if message.text == "/cancel_action":
        await cmd_cancel_admin_fsm_action(message, state)
        return
    if not message.text.isdigit():
        return await message.reply("User ID - число.")
    user_id = int(message.text)
    user = db.get_user(user_id)
    if user:
        info = f"Инфо о {user_id}:\nUsername: @{user.get('username', 'N/A')}\nЗапросы: {user.get('requests_left', 0)}\nПодписка: {'Да' if user.get('subscribed_to_channel') else 'Нет'}\nРеф.код: {user.get('referral_code', 'N/A')}\nПригласил: {user.get('invited_friends_count', 0)}\nПришел от: {user.get('referred_by') or 'N/A'}"
        await message.answer(info)
    else:
        await message.answer(f"Юзер {user_id} не найден.")
    await state.clear()

@router.callback_query(F.data == "admin:add_req_prompt", AdminFilter())
async def cb_admin_add_req_prompt(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await callback.message.answer("User ID для выдачи запросов? /cancel_action отмена.")
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except:
        pass
    await state.set_state(AdminEditUser.waiting_for_user_id_requests)

@router.message(AdminEditUser.waiting_for_user_id_requests, AdminFilter(), F.text)
async def process_user_id_for_requests(message: Message, state: FSMContext):
    if message.text == "/cancel_action":
        await cmd_cancel_admin_fsm_action(message, state)
        return
    if not message.text.isdigit():
        return await message.reply("User ID - число.")
    await state.update_data(target_user_id=int(message.text))
    await message.answer("Кол-во запросов (отрицательное для списания)?")
    await state.set_state(AdminEditUser.waiting_for_requests_amount)

@router.message(AdminEditUser.waiting_for_requests_amount, AdminFilter(), F.text)
async def process_requests_amount(message: Message, state: FSMContext):
    if message.text == "/cancel_action":
        await cmd_cancel_admin_fsm_action(message, state)
        return
    if not message.text.lstrip('-').isdigit():
        return await message.reply("Кол-во - число.")
    amount = int(message.text)
    data = await state.get_data()
    target_user_id = data.get("target_user_id")
    user = db.get_user(target_user_id)
    if user:
        db.update_user(target_user_id, {"requests_left": user.get("requests_left", 0) + amount})
        user = db.get_user(target_user_id)
        await message.answer(
            f"{target_user_id} {('добавлено' if amount >= 0 else 'списано')} {abs(amount)} запросов. Баланс: {user['requests_left']}")
        if user.get('notifications_enabled', True):
            try:
                await bot.send_message(
                    target_user_id,
                    f"Админ {('начислил' if amount >= 0 else 'списал')} {abs(amount)} запросов. Баланс: {user['requests_left']}"
                )
            except Exception as e:
                logger.warning(f"Не уведомил {target_user_id}: {e}")
    else:
        await message.answer(f"Юзер {target_user_id} не найден.")
    await state.clear()

@router.callback_query(F.data == "admin:set_referral_requests", AdminFilter())
async def cb_admin_set_referral_requests(callback: CallbackQuery, state: FSMContext):
    settings = db.get_referral_settings()
    await callback.answer()
    await callback.message.answer(
        f"Текущий бонус за реферала: {settings['referral_requests']} запросов.\n"
        "Введите новое количество (положительное число). /cancel_action для отмены."
    )
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except:
        pass
    await state.set_state(AdminEditUser.waiting_for_referral_requests)

@router.message(AdminEditUser.waiting_for_referral_requests, AdminFilter(), F.text)
async def process_referral_requests(message: Message, state: FSMContext):
    if message.text == "/cancel_action":
        await cmd_cancel_admin_fsm_action(message, state)
        return
    if not message.text.isdigit():
        return await message.reply("Введите положительное число.")
    amount = int(message.text)
    if amount <= 0:
        return await message.reply("Число должно быть больше 0.")
    db.update_referral_settings({'referral_requests': amount})
    await message.answer(f"Бонус за реферала: {amount} запросов.")
    await state.clear()

@router.callback_query(F.data == "admin:set_bulk_referral_requests", AdminFilter())
async def cb_admin_set_bulk_referral_requests(callback: CallbackQuery, state: FSMContext):
    settings = db.get_referral_settings()
    await callback.answer()
    await callback.message.answer(
        f"Текущий бонус за 5 рефералов: {settings['bulk_referral_requests']} запросов.\n"
        "Введите новое количество (положительное число). /cancel_action для отмены."
    )
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except:
        pass
    await state.set_state(AdminEditUser.waiting_for_bulk_referral_requests)

@router.message(AdminEditUser.waiting_for_bulk_referral_requests, AdminFilter(), F.text)
async def process_bulk_referral_requests(message: Message, state: FSMContext):
    if message.text == "/cancel_action":
        await cmd_cancel_admin_fsm_action(message, state)
        return
    if not message.text.isdigit():
        return await message.reply("Введите положительное число.")
    amount = int(message.text)
    if amount <= 0:
        return await message.reply("Число должно быть больше 0.")
    db.update_referral_settings({'bulk_referral_requests': amount})
    await message.answer(f"Бонус за 5 рефералов: {amount} запросов.")
    await state.clear()

@router.message(CommandStart())
async def handle_start(message: Message, state: FSMContext):
    await state.clear()
    user_tg = message.from_user
    user_id = user_tg.id
    username = user_tg.username or user_tg.first_name
    referred_by_id = None
    args = message.text.split()
    settings = db.get_referral_settings()
    referral_requests = settings['referral_requests']
    bulk_referral_requests = settings['bulk_referral_requests']
    if len(args) > 1 and args[0] == "/start":
        payload = args[1]
        referral_map = db.get_referral_map()
        if payload in referral_map:
            ref_id_str = referral_map[payload]
            if str(user_id) != ref_id_str:
                referred_by_id = int(ref_id_str)
            else:
                await message.answer("Своя реф. ссылка не засчитывается.")
    user = db.get_user(user_id)
    if not user:
        user = db.create_user(user_id, username, str(uuid.uuid4())[:8], referred_by_id, INITIAL_REQUESTS)
        if referred_by_id:
            referrer = db.get_user(referred_by_id)
            if referrer:
                new_invited_count = referrer.get("invited_friends_count", 0) + 1
                db.update_user(referred_by_id, {
                    "invited_friends_count": new_invited_count,
                    "requests_left": referrer.get("requests_left", 0) + referral_requests
                })
                referrer = db.get_user(referred_by_id)
                if referrer.get('notifications_enabled', True):
                    try:
                        await bot.send_message(
                            referred_by_id,
                            f"Новый реферал: @{username or user_id}! Приглашено: {new_invited_count}/5\n"
                            f"+{referral_requests} запросов. Баланс: {referrer['requests_left']}"
                        )
                    except:
                        pass
                if new_invited_count >= 5:
                    db.update_user(referred_by_id, {
                        "requests_left": referrer["requests_left"] + bulk_referral_requests,
                        "invited_friends_count": 0
                    })
                    referrer = db.get_user(referred_by_id)
                    if referrer.get('notifications_enabled', True):
                        try:
                            await bot.send_message(
                                referred_by_id,
                                f"🎉 Бонус за 5 рефералов! +{bulk_referral_requests} запросов. Баланс: {referrer['requests_left']}"
                            )
                        except:
                            pass
    else:
        db.update_user(user_id, {"username": username})
    is_admin = user_id in ADMIN_ID
    main_kb = get_main_keyboard(is_admin=is_admin)
    if CHANNEL_ID:
        subscribed = await is_user_subscribed(user_id)
        db.update_user(user_id, {"subscribed_to_channel": subscribed})
        if not subscribed:
            await send_subscription_prompt(user_id, WELCOME_TEXT + f"\n\n{SUBSCRIPTION_PROMPT_PREFIX}")
            return
    await message.answer(WELCOME_TEXT, reply_markup=main_kb)
    await message.answer(f"У вас {user['requests_left']} запросов.")

@router.callback_query(F.data == "check_subscription")
async def cb_check_subscription(callback: CallbackQuery):
    user_id = callback.from_user.id
    user = db.get_user(user_id)
    if not user:
        user = db.create_user(user_id, callback.from_user.username or callback.from_user.first_name, str(uuid.uuid4())[:8])
    await callback.answer("Проверяю...")
    is_admin = user_id in ADMIN_ID
    main_kb = get_main_keyboard(is_admin=is_admin)
    if CHANNEL_ID:
        subscribed = await is_user_subscribed(user_id)
        db.update_user(user_id, {"subscribed_to_channel": subscribed})
        if subscribed:
            try:
                await callback.message.delete()
            except:
                pass
            await callback.message.answer(SUBSCRIPTION_THANKS_TEXT)
            await callback.message.answer(WELCOME_TEXT, reply_markup=main_kb)
            await callback.message.answer(f"У вас {user['requests_left']} запросов.")
        else:
            try:
                await callback.message.delete()
            except:
                pass
            await send_subscription_prompt(user_id, SUBSCRIPTION_NOT_YET_TEXT)
    else:
        await callback.message.edit_text("Канал не настроен.", reply_markup=None)
        await callback.message.answer(WELCOME_TEXT, reply_markup=main_kb)

@router.message(F.text == "❓ ПОМОЩЬ")
async def msg_help_button(message: Message):
    await message.answer(HELP_TEXT)

@router.message(F.text == "🚀 Начать работу с ГДЗ AI")
async def msg_start_work_button(message: Message):
    user = db.get_user(message.from_user.id)
    if not user:
        return await message.answer("Пожалуйста, /start.")
    if CHANNEL_ID and not user.get("subscribed_to_channel", False):
        if not await is_user_subscribed(message.from_user.id):
            return await send_subscription_prompt(message.from_user.id)
    settings = db.get_referral_settings()
    if user['requests_left'] <= 0:
        bot_uname = (await bot.get_me()).username
        ref_link = f"https://t.me/{bot_uname}?start={user['referral_code']}"
        return await message.answer(
            NO_REQUESTS_TEXT.format(
                target_count=5,
                bonus_requests=settings['bulk_referral_requests'],
                referral_link=ref_link
            )
        )
    await message.answer(START_WORK_TEXT)

@router.message(F.text == "👫💸 Пригласи друга")
async def msg_referral_button(message: Message):
    user = db.get_user(message.from_user.id)
    if not user:
        return await message.answer("Пожалуйста, /start.")
    settings = db.get_referral_settings()
    bot_uname = (await bot.get_me()).username
    ref_link = f"https://t.me/{bot_uname}?start={user['referral_code']}"
    await message.answer(
        REFERRAL_INFO_TEXT.format(
            referral_link=ref_link,
            target_count=5,
            bonus_requests=settings['bulk_referral_requests'],
            invited_count=user.get('invited_friends_count', 0)
        )
    )

@router.message(F.text == "⚙️ Настройки")
async def msg_settings_button(message: Message):
    user = db.get_user(message.from_user.id)
    if not user:
        return await message.answer("Пожалуйста, /start.")
    builder = InlineKeyboardBuilder()
    builder.button(
        text=f"Уведомления: {'Вкл ✅' if user.get('notifications_enabled', True) else 'Выкл ❌'}",
        callback_data="toggle_notifications"
    )
    builder.button(text="Мой баланс", callback_data="check_balance")
    builder.adjust(1)
    await message.answer("⚙️ Настройки:", reply_markup=builder.as_markup())

@router.callback_query(F.data == "toggle_notifications")
async def cb_toggle_notifications(callback: CallbackQuery):
    user = db.get_user(callback.from_user.id)
    if not user:
        return await callback.answer("Юзер не найден.", show_alert=True)
    db.update_user(callback.from_user.id, {"notifications_enabled": not user.get('notifications_enabled', True)})
    user = db.get_user(callback.from_user.id)
    await callback.answer(f"Уведомления {'ВКЛ' if user['notifications_enabled'] else 'ВЫКЛ'}.")
    builder = InlineKeyboardBuilder()
    builder.button(
        text=f"Уведомления: {'Вкл ✅' if user.get('notifications_enabled', True) else 'Выкл ❌'}",
        callback_data="toggle_notifications"
    )
    builder.button(text="Мой баланс", callback_data="check_balance")
    builder.adjust(1)
    try:
        await callback.message.edit_reply_markup(reply_markup=builder.as_markup())
    except Exception as e:
        logger.error(f"Ошибка обновления клавиатуры: {e}")

@router.callback_query(F.data == "check_balance")
async def cb_check_balance(callback: CallbackQuery):
    user = db.get_user(callback.from_user.id)
    if not user:
        return await callback.answer("Юзер не найден.", show_alert=True)
    await callback.answer(f"У вас {user.get('requests_left', 0)} запросов.", show_alert=True)

@router.message(F.text.regexp(r'(какая ты модель|кто тебя создал|ты кто|что ты за ии)', flags=re.I))
async def who_are_you_handler(message: Message):
    await message.reply(
        "Я — продвинутая языковая модель Gemini 2.5 Pro, разработанная Google. Готов помочь! 😊")

@router.message(F.text | F.photo | F.document)
async def handle_user_task(message: Message):
    user_id = message.from_user.id
    user = db.get_user(user_id)
    if not user:
        return await message.answer("Пожалуйста, /start.")
    if CHANNEL_ID and not user.get("subscribed_to_channel", False):
        if not await is_user_subscribed(user_id):
            return await send_subscription_prompt(user_id)
        db.update_user(user_id, {"subscribed_to_channel": True})
    settings = db.get_referral_settings()
    if user["requests_left"] <= 0:
        bot_uname = (await bot.get_me()).username
        ref_link = f"https://t.me/{bot_uname}?start={user['referral_code']}"
        return await message.answer(
            NO_REQUESTS_TEXT.format(
                target_count=5,
                bonus_requests=settings['bulk_referral_requests'],
                referral_link=ref_link
            )
        )
    processing_msg = await message.answer("🧠 Думаю...")
    task_input = ""
    if message.text:
        if re.search(r'(какая ты модель|кто тебя создал|ты кто|что ты за ии)', message.text, flags=re.I):
            return
        if message.text.startswith('/'):
            logger.info(f"Команда '{message.text}' не для AI.")
            return
        if message.text in ["❓ ПОМОЩЬ", "🚀 Начать работу с ГДЗ AI", "👫💸 Пригласи друга", "⚙️ Настройки",
                            "👑 Админ-панель"]:
            return
        task_input = message.text
        logger.info(f"User {user_id} text: {task_input[:50]}")
    elif message.photo:
        task_input = await extract_text_from_image(message.photo[-1].file_id)
        logger.info(f"User {user_id} photo_id: {message.photo[-1].file_id}")
    elif message.document:
        if message.document.mime_type == "application/pdf":
            task_input = await extract_text_from_pdf(message.document.file_id)
            logger.info(f"User {user_id} document_id: {message.document.file_id}")
        else:
            await processing_msg.edit_text("Формат документа не поддерживается. Отправьте PDF.")
            return
    if not task_input:
        await processing_msg.edit_text("Нет данных для обработки.")
        return
    clean_answer = await get_ai_response(task_input)
    if clean_answer:
        escaped_answer = html.escape(clean_answer)
        try:
            await processing_msg.edit_text(escaped_answer)
        except Exception as e:
            logger.error(f"Не отправил ответ {user_id}: {e}")
            await processing_msg.edit_text("😕 Ошибка отображения ответа.")
    else:
        await processing_msg.edit_text("😕 Нет ответа от AI.")
    db.update_user(user_id, {"requests_left": user["requests_left"] - 1})
    user = db.get_user(user_id)
    await message.answer(f"(Запросов: {user['requests_left']})", disable_notification=True)

async def set_bot_commands_menu():
    commands = [
        BotCommand(command="start", description="🚀 Старт/Перезапуск"),
        BotCommand(command="help", description="❓ Помощь")
    ]
    try:
        await bot.set_my_commands(commands)
        logger.info("Команды бота установлены.")
    except Exception as e:
        logger.error(f"Ошибка установки команд: {e}")

async def main():
    if not BOT_TOKEN:
        logger.critical("Нет токена!")
        return
    dp.include_router(router)
    await set_bot_commands_menu()
    asyncio.create_task(daily_balance_update())
    logger.info("Бот запускается...")
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    except Exception as e:
        logger.critical(f"Ошибка запуска: {e}")
    finally:
        await bot.session.close()
        logger.info("Сессия закрыта.")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Бот остановлен.")
    except Exception as e:
        logger.critical(f"Непредвиденная ошибка: {e}")