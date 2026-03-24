# bot.py — PMT Premium Cheat Shop: Bot + MiniApp (single file, no Flask)
import logging
import asyncio
import aiohttp
from aiohttp import web
import hashlib
import hmac
import time
import random
import json
import os
import threading
from datetime import datetime, timedelta
from urllib.parse import parse_qs, unquote, quote
from collections import OrderedDict
from typing import Optional, Dict, Any, Union

from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.types import (
    InlineKeyboardMarkup, InlineKeyboardButton,
    LabeledPrice, PreCheckoutQuery, FSInputFile,
    InputMediaPhoto, WebAppInfo
)
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.client.default import DefaultBotProperties

# ========== ЛОГИРОВАНИЕ ==========
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# ========== КОНФИГУРАЦИЯ ==========
class Config:
    BOT_TOKEN = os.environ.get("BOT_TOKEN", "8562090085:AAFjA1rD2ff9RDvDWOZnOWJbcRBA34lJWMk")
    CRYPTOBOT_TOKEN = os.environ.get("CRYPTOBOT_TOKEN", "493276:AAtS7R1zYy0gaPw8eax1EgiWo0tdnd6dQ9c")
    YOOMONEY_ACCESS_TOKEN = os.environ.get("YOOMONEY_ACCESS_TOKEN", "4100118889570559.3288B2E716CEEB922A26BD6BEAC58648FBFB680CCF64E4E1447D714D6FB5EA5F01F1478FAC686BEF394C8A186C98982DE563C1ABCDF9F2F61D971B61DA3C7E486CA818F98B9E0069F1C0891E090DD56A11319D626A40F0AE8302A8339DED9EB7969617F191D93275F64C4127A3ECB7AED33FCDE91CA68690EB7534C67E6C219E")
    YOOMONEY_WALLET = os.environ.get("YOOMONEY_WALLET", "4100118889570559")
    SUPPORT_CHAT_USERNAME = os.environ.get("SUPPORT_CHAT_USERNAME", "PMThelp")
    DOWNLOAD_URL = os.environ.get("DOWNLOAD_URL", "https://go.linkify.ru/2GPF")
    WEBAPP_DOMAIN = os.environ.get("WEBAPP_DOMAIN", "pmt.bothost.tech")
    WEBAPP_URL = f"https://{WEBAPP_DOMAIN}"
    WEBAPP_PORT = int(os.environ.get("PORT", os.environ.get("WEBAPP_PORT", "8080")))
    ADMIN_IDS = set()
    ADMIN_ID = 0
    SUPPORT_CHAT_ID = 0
    MAX_PENDING_ORDERS = 1000
    ORDER_EXPIRY_SECONDS = 3600
    RATE_LIMIT_SECONDS = 2
    MAX_PAYMENT_CHECK_ATTEMPTS = 5
    PAYMENT_CHECK_INTERVAL = 5
    START_IMAGE_PATH = os.environ.get("START_IMAGE_PATH", "images/start_image.jpg")

    @classmethod
    def init(cls):
        if not cls.BOT_TOKEN:
            raise ValueError("BOT_TOKEN required!")
        admin_ids_str = os.environ.get("ADMIN_ID", "")
        admin_ids_list = [int(x.strip()) for x in admin_ids_str.split(",") if x.strip().isdigit()]
        if not admin_ids_list:
            raise ValueError("ADMIN_ID required!")
        cls.ADMIN_ID = admin_ids_list[0]
        cls.SUPPORT_CHAT_ID = admin_ids_list[1] if len(admin_ids_list) >= 2 else int(os.environ.get("SUPPORT_CHAT_ID", str(cls.ADMIN_ID)))
        cls.ADMIN_IDS = set(admin_ids_list)

Config.init()

# ========== ХРАНИЛИЩЕ ==========
class OrderStorage:
    def __init__(self, max_pending=1000, expiry_seconds=3600):
        self._pending = OrderedDict()
        self._confirmed = {}
        self._lock = asyncio.Lock()
        self._max_pending = max_pending
        self._expiry_seconds = expiry_seconds

    async def add_pending(self, order_id, order_data):
        async with self._lock:
            await self._cleanup_expired()
            if len(self._pending) >= self._max_pending:
                self._pending.popitem(last=False)
            self._pending[order_id] = order_data

    async def get_pending(self, order_id):
        async with self._lock:
            return self._pending.get(order_id)

    async def confirm(self, order_id, extra_data):
        async with self._lock:
            if order_id in self._confirmed:
                return False
            order = self._pending.pop(order_id, None)
            if order is None:
                return False
            self._confirmed[order_id] = {**order, **extra_data}
            return True

    async def is_confirmed(self, order_id):
        async with self._lock:
            return order_id in self._confirmed

    async def remove_pending(self, order_id):
        async with self._lock:
            return self._pending.pop(order_id, None)

    async def get_stats(self):
        async with self._lock:
            return {"pending": len(self._pending), "confirmed": len(self._confirmed)}

    async def get_recent_pending(self, limit=5):
        async with self._lock:
            return list(self._pending.items())[-limit:]

    async def _cleanup_expired(self):
        now = time.time()
        expired = [oid for oid, data in self._pending.items() if now - data.get("created_at", 0) > self._expiry_seconds]
        for oid in expired:
            del self._pending[oid]

class RateLimiter:
    def __init__(self, interval=2.0):
        self._last_action = {}
        self._interval = interval
    def check(self, user_id):
        now = time.time()
        last = self._last_action.get(user_id, 0)
        if now - last < self._interval:
            return False
        self._last_action[user_id] = now
        return True

# ========== ПРОДУКТЫ ==========
PRODUCTS = {
    "apk_week": {"name": "📱 PMT Android", "period_text": "НЕДЕЛЮ", "price": 205, "price_stars": 250, "price_gold": 650, "price_nft": 500, "price_crypto_usdt": 3, "platform": "Android", "period": "НЕДЕЛЮ", "platform_code": "apk", "emoji": "📱", "duration": "7 дней"},
    "apk_month": {"name": "📱 PMT Android", "period_text": "МЕСЯЦ", "price": 450, "price_stars": 450, "price_gold": 1200, "price_nft": 1000, "price_crypto_usdt": 6, "platform": "Android", "period": "МЕСЯЦ", "platform_code": "apk", "emoji": "📱", "duration": "30 дней"},
    "apk_forever": {"name": "📱 PMT Android", "period_text": "НАВСЕГДА", "price": 890, "price_stars": 900, "price_gold": 2200, "price_nft": 1800, "price_crypto_usdt": 12, "platform": "Android", "period": "НАВСЕГДА", "platform_code": "apk", "emoji": "📱", "duration": "Навсегда"},
    "ios_week": {"name": "🍎 PMT iOS", "period_text": "НЕДЕЛЮ", "price": 359, "price_stars": 350, "price_gold": 700, "price_nft": 550, "price_crypto_usdt": 5, "platform": "iOS", "period": "НЕДЕЛЮ", "platform_code": "ios", "emoji": "🍎", "duration": "7 дней"},
    "ios_month": {"name": "🍎 PMT iOS", "period_text": "МЕСЯЦ", "price": 750, "price_stars": 750, "price_gold": 1400, "price_nft": 1200, "price_crypto_usdt": 10, "platform": "iOS", "period": "МЕСЯЦ", "platform_code": "ios", "emoji": "🍎", "duration": "30 дней"},
    "ios_forever": {"name": "🍎 PMT iOS", "period_text": "НАВСЕГДА", "price": 1400, "price_stars": 1400, "price_gold": 2500, "price_nft": 2200, "price_crypto_usdt": 18, "platform": "iOS", "period": "НАВСЕГДА", "platform_code": "ios", "emoji": "🍎", "duration": "Навсегда"},
}

PRODUCTS_JSON = json.dumps(PRODUCTS, ensure_ascii=False)

# ========== ВСПОМОГАТЕЛЬНЫЕ ==========
def generate_order_id():
    raw = f"{time.time()}_{random.randint(100000,999999)}_{os.urandom(4).hex()}"
    return hashlib.sha256(raw.encode()).hexdigest()[:12]

def generate_license_key(order_id, user_id):
    raw = f"{order_id}_{user_id}_{os.urandom(8).hex()}"
    h = hashlib.sha256(raw.encode()).hexdigest()[:16].upper()
    return f"PMT-{h[:4]}-{h[4:8]}-{h[8:12]}-{h[12:16]}"

def is_admin(user_id):
    return user_id in Config.ADMIN_IDS

def find_product(platform_code, period):
    for p in PRODUCTS.values():
        if p['platform_code'] == platform_code and p['period'] == period:
            return p
    return None

def find_product_by_id(product_id):
    return PRODUCTS.get(product_id)

def create_payment_link(amount, order_id, product_name):
    comment = f"Заказ {order_id}: {product_name}"
    return f"https://yoomoney.ru/quickpay/confirm.xml?receiver={Config.YOOMONEY_WALLET}&quickpay-form=shop&targets={quote(comment,safe='')}&sum={amount}&label={order_id}&successURL={quote('https://t.me/pmt_bot?start=success',safe='')}&paymentType=AC"

# ========== ПЛАТЁЖНЫЕ СЕРВИСЫ ==========
class YooMoneyService:
    @staticmethod
    async def get_balance():
        if not Config.YOOMONEY_ACCESS_TOKEN:
            return None
        headers = {"Authorization": f"Bearer {Config.YOOMONEY_ACCESS_TOKEN}"}
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as session:
                async with session.get("https://yoomoney.ru/api/account-info", headers=headers) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        return float(data.get('balance', 0))
        except Exception as e:
            logger.error("YooMoney balance: %s", e)
        return None

    @staticmethod
    async def check_payment(order_id, expected_amount, order_time):
        if not Config.YOOMONEY_ACCESS_TOKEN:
            return False
        headers = {"Authorization": f"Bearer {Config.YOOMONEY_ACCESS_TOKEN}"}
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as session:
                async with session.post("https://yoomoney.ru/api/operation-history", headers=headers, data={"type": "deposition", "records": 100}) as resp:
                    if resp.status != 200:
                        return False
                    result = await resp.json()
                    for op in result.get("operations", []):
                        if op.get("label") == order_id and op.get("status") == "success" and abs(float(op.get("amount", 0)) - expected_amount) <= 5:
                            return True
                        if op.get("status") == "success" and abs(float(op.get("amount", 0)) - expected_amount) <= 2:
                            try:
                                op_time = datetime.fromisoformat(op.get("datetime", "").replace("Z", "+00:00")).timestamp()
                                if abs(op_time - order_time) <= 1800:
                                    return True
                            except:
                                pass
        except Exception as e:
            logger.error("YooMoney check: %s", e)
        return False

class CryptoBotService:
    BASE_URL = "https://pay.crypt.bot/api"
    @staticmethod
    async def create_invoice(amount_usdt, order_id, description):
        if not Config.CRYPTOBOT_TOKEN:
            return None
        headers = {"Crypto-Pay-API-Token": Config.CRYPTOBOT_TOKEN, "Content-Type": "application/json"}
        data = {"asset": "USDT", "amount": str(amount_usdt), "description": description[:256], "payload": order_id, "paid_btn_name": "callback", "paid_btn_url": f"https://t.me/pmt_bot?start=paid_{order_id}"}
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as session:
                async with session.post(f"{CryptoBotService.BASE_URL}/createInvoice", headers=headers, json=data) as resp:
                    if resp.status == 200:
                        result = await resp.json()
                        if result.get("ok"):
                            inv = result["result"]
                            return {"invoice_id": inv.get("invoice_id"), "pay_url": inv.get("pay_url"), "amount": inv.get("amount")}
        except Exception as e:
            logger.error("CryptoBot create: %s", e)
        return None

    @staticmethod
    async def check_invoice(invoice_id):
        if not Config.CRYPTOBOT_TOKEN:
            return False
        headers = {"Crypto-Pay-API-Token": Config.CRYPTOBOT_TOKEN, "Content-Type": "application/json"}
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as session:
                async with session.post(f"{CryptoBotService.BASE_URL}/getInvoices", headers=headers, json={"invoice_ids": [invoice_id]}) as resp:
                    if resp.status == 200:
                        result = await resp.json()
                        if result.get("ok"):
                            items = result.get("result", {}).get("items", [])
                            if items:
                                return items[0].get("status") == "paid"
        except Exception as e:
            logger.error("CryptoBot check: %s", e)
        return False

# ========== ИНИЦИАЛИЗАЦИЯ BOT ==========
bot = Bot(token=Config.BOT_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
storage = MemoryStorage()
dp = Dispatcher(storage=storage)
orders = OrderStorage(max_pending=Config.MAX_PENDING_ORDERS, expiry_seconds=Config.ORDER_EXPIRY_SECONDS)
rate_limiter = RateLimiter(interval=Config.RATE_LIMIT_SECONDS)

# ========== СОСТОЯНИЯ ==========
class OrderState(StatesGroup):
    main_menu = State()
    choosing_platform = State()
    choosing_subscription = State()
    choosing_payment = State()

# ========== БИЗНЕС-ЛОГИКА ==========
async def process_successful_payment(order_id, source="API"):
    order = await orders.get_pending(order_id)
    if not order:
        return False
    product = order["product"]
    user_id = order["user_id"]
    license_key = generate_license_key(order_id, user_id)
    confirmed = await orders.confirm(order_id, {'confirmed_at': time.time(), 'confirmed_by': source, 'license_key': license_key})
    if not confirmed:
        return False
    success_text = f"""🎉 <b>Оплата подтверждена!</b>

✨ Добро пожаловать в PMT!

📦 <b>Ваша покупка:</b>
{product['emoji']} {product['name']}
⏱️ Срок: {product['duration']}
🔍 Метод: {source}

🔑 <b>Ваш лицензионный ключ:</b>
<code>{license_key}</code>

📥 <b>Скачивание:</b>
👇 Нажмите кнопку ниже

💫 <b>Активация:</b>
1️⃣ Скачайте файл
2️⃣ Установите
3️⃣ Введите ключ
4️⃣ Наслаждайтесь! 🎮

💬 Поддержка: @{Config.SUPPORT_CHAT_USERNAME}"""

    download_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📥 Скачать PMT", url=Config.DOWNLOAD_URL)],
        [InlineKeyboardButton(text="💬 Поддержка", url=f"https://t.me/{Config.SUPPORT_CHAT_USERNAME}")],
    ])
    try:
        await bot.send_message(user_id, success_text, reply_markup=download_kb)
    except Exception as e:
        logger.error("Send to user %s: %s", user_id, e)

    now_str = datetime.now().strftime('%d.%m.%Y %H:%M')
    admin_text = f"""💎 <b>ПРОДАЖА ({source})</b>

👤 {order['user_name']}
🆔 {user_id}
📦 {product['name']} ({product['duration']})
💰 {order.get('amount', product['price'])} {order.get('currency', '₽')}
🔑 <code>{license_key}</code>
📅 {now_str}"""
    for aid in Config.ADMIN_IDS:
        try:
            await bot.send_message(aid, admin_text)
        except:
            pass
    return True

async def send_admin_notification(user, product, payment_method, price, order_id):
    now_str = datetime.now().strftime('%d.%m.%Y %H:%M')
    message = f"""🔔 <b>НОВЫЙ ЗАКАЗ</b>

👤 {user.full_name}
🆔 <code>{user.id}</code>
📦 {product['name']} ({product['duration']})
💰 {price}
💳 {payment_method}
🆔 <code>{order_id}</code>
📅 {now_str}"""
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Подтвердить", callback_data=f"admin_confirm_{order_id}")],
        [InlineKeyboardButton(text="❌ Отклонить", callback_data=f"admin_reject_{order_id}")]
    ])
    for aid in Config.ADMIN_IDS:
        try:
            await bot.send_message(aid, message, reply_markup=kb)
        except:
            pass

# ========== КЛАВИАТУРЫ ==========
def start_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🛒 Открыть магазин", web_app=WebAppInfo(url=Config.WEBAPP_URL))],
        [InlineKeyboardButton(text="🛒 Купить чит", callback_data="buy_cheat")],
        [InlineKeyboardButton(text="ℹ️ О программе", callback_data="about")],
        [InlineKeyboardButton(text="💬 Поддержка", url=f"https://t.me/{Config.SUPPORT_CHAT_USERNAME}")]
    ])

def platform_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📱 Android", callback_data="platform_apk")],
        [InlineKeyboardButton(text="🍏 iOS", callback_data="platform_ios")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="back_to_start")]
    ])

def subscription_keyboard(platform):
    prices = {
        "apk": [("⚡ НЕДЕЛЯ — 205₽", "sub_apk_week"), ("🔥 МЕСЯЦ — 450₽", "sub_apk_month"), ("💎 НАВСЕГДА — 890₽", "sub_apk_forever")],
        "ios": [("⚡ НЕДЕЛЯ — 359₽", "sub_ios_week"), ("🔥 МЕСЯЦ — 750₽", "sub_ios_month"), ("💎 НАВСЕГДА — 1400₽", "sub_ios_forever")]
    }
    buttons = [[InlineKeyboardButton(text=t, callback_data=cb)] for t, cb in prices.get(platform, [])]
    buttons.append([InlineKeyboardButton(text="◀️ Назад", callback_data="buy_cheat")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def payment_methods_keyboard(product):
    pc, p = product['platform_code'], product['period']
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💳 Картой", callback_data=f"pay_yoomoney_{pc}_{p}")],
        [InlineKeyboardButton(text="⭐ Telegram Stars", callback_data=f"pay_stars_{pc}_{p}")],
        [InlineKeyboardButton(text="₿ Криптобот", callback_data=f"pay_crypto_{pc}_{p}")],
        [InlineKeyboardButton(text="🪙 GOLD", callback_data=f"pay_gold_{pc}_{p}")],
        [InlineKeyboardButton(text="🎨 NFT", callback_data=f"pay_nft_{pc}_{p}")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="back_to_subscription")]
    ])

# ========== ОБРАБОТЧИКИ БОТА ==========
@dp.message(Command("start"))
async def cmd_start(message: types.Message, state: FSMContext):
    await state.clear()
    text = """<b>PMT | STANDOFF 2 PREMIUM 💰</b>

🚀 Универсальное решение:
📱 Android (APK, без Root)
💻 PC
🍏 iOS

🔥 Функционал:
• Аимбот + WallHack + ESP
• Анти-бан защита

Лучшие цены | Быстрая поддержка 24/7

Покупай чит, и разноси соперников ⚡️"""
    await message.answer(text, reply_markup=start_keyboard())
    await state.set_state(OrderState.main_menu)

@dp.callback_query(F.data == "buy_cheat")
async def buy_cheat(callback: types.CallbackQuery, state: FSMContext):
    try:
        await callback.message.edit_text("💰 <b>Выберите платформу:</b>\n\n📱 <b>Android</b> — APK\n🍏 <b>iOS</b> — IPA", reply_markup=platform_keyboard())
    except:
        await callback.message.answer("💰 <b>Выберите платформу:</b>\n\n📱 <b>Android</b> — APK\n🍏 <b>iOS</b> — IPA", reply_markup=platform_keyboard())
    await state.set_state(OrderState.choosing_platform)
    await callback.answer()

@dp.callback_query(F.data == "about")
async def about_cheat(callback: types.CallbackQuery):
    text = f"""📋 <b>Подробная информация</b>

🎮 <b>Название:</b> PMT
🔥 <b>Статус:</b> Активно обновляется

🛠️ <b>Функционал:</b>
• 🎯 Умный AimBot
• 👁️ WallHack
• 📍 ESP
• 🗺️ Мини-радар
• ⚙️ Гибкие настройки

🛡️ <b>Безопасность:</b>
• Обход античитов
• Регулярные обновления

💬 Поддержка: @{Config.SUPPORT_CHAT_USERNAME}"""
    try:
        await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀️ Назад", callback_data="back_to_start")]]))
    except:
        pass
    await callback.answer()

@dp.callback_query(F.data.startswith("platform_"))
async def process_platform(callback: types.CallbackQuery, state: FSMContext):
    platform = callback.data.split("_")[1]
    if platform not in ("apk", "ios"):
        await callback.answer("❌", show_alert=True)
        return
    await state.update_data(platform=platform)
    try:
        await callback.message.edit_text("💰 <b>Выберите тариф:</b>", reply_markup=subscription_keyboard(platform))
    except:
        pass
    await state.set_state(OrderState.choosing_subscription)
    await callback.answer()

@dp.callback_query(F.data.startswith("sub_"))
async def process_subscription(callback: types.CallbackQuery, state: FSMContext):
    parts = callback.data.split("_")
    if len(parts) < 3:
        return
    product = find_product_by_id(f"{parts[1]}_{parts[2]}")
    if not product:
        await callback.answer("❌ Не найден", show_alert=True)
        return
    await state.update_data(selected_product=product)
    text = f"""🛒 <b>Оформление</b>

{product['emoji']} <b>{product['name']}</b>
⏱️ {product['duration']}

💎 <b>Стоимость:</b>
💳 Картой: {product['price']} ₽
⭐ Stars: {product['price_stars']} ⭐
₿ Крипта: {product['price_crypto_usdt']} USDT
💰 GOLD: {product['price_gold']} 🪙
🎨 NFT: {product['price_nft']} 🖼️

🎯 <b>Способ оплаты:</b>"""
    try:
        await callback.message.edit_text(text, reply_markup=payment_methods_keyboard(product))
    except:
        pass
    await state.set_state(OrderState.choosing_payment)
    await callback.answer()

# ========== ОПЛАТА КАРТОЙ ==========
@dp.callback_query(F.data.startswith("pay_yoomoney_"))
async def process_yoomoney_payment(callback: types.CallbackQuery):
    parts = callback.data.split("_")
    if len(parts) < 4:
        return
    product = find_product(parts[2], parts[3])
    if not product:
        return
    if not rate_limiter.check(callback.from_user.id):
        await callback.answer("⏳ Подождите...", show_alert=True)
        return
    order_id = generate_order_id()
    amount = product["price"]
    payment_url = create_payment_link(amount, order_id, f"{product['name']} ({product['duration']})")
    await orders.add_pending(order_id, {"user_id": callback.from_user.id, "user_name": callback.from_user.full_name, "product": product, "amount": amount, "currency": "₽", "payment_method": "Картой", "status": "pending", "created_at": time.time()})
    text = f"""💳 <b>Оплата картой</b>

{product['emoji']} {product['name']}
⏱️ {product['duration']}
💰 <b>{amount} ₽</b>
🆔 <code>{order_id}</code>

1️⃣ Нажмите «Оплатить»
2️⃣ Оплатите
3️⃣ Нажмите «Проверить»"""
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💳 Оплатить картой", url=payment_url)],
        [InlineKeyboardButton(text="✅ Проверить оплату", callback_data=f"checkym_{order_id}")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="restart")]
    ])
    try:
        await callback.message.edit_text(text, reply_markup=kb)
    except:
        pass
    await send_admin_notification(callback.from_user, product, "💳 Картой", f"{amount} ₽", order_id)
    await callback.answer()

# ========== ПРОВЕРКА ЮMONEY ==========
@dp.callback_query(F.data.startswith("checkym_"))
async def check_yoomoney_callback(callback: types.CallbackQuery):
    order_id = callback.data.replace("checkym_", "", 1)
    order = await orders.get_pending(order_id)
    if not order:
        if await orders.is_confirmed(order_id):
            await callback.answer("✅ Уже подтверждён!", show_alert=True)
        else:
            await callback.answer("❌ Не найден", show_alert=True)
        return
    if not rate_limiter.check(callback.from_user.id):
        await callback.answer("⏳ Подождите...", show_alert=True)
        return
    await callback.answer("🔍 Проверяем...")
    try:
        checking_msg = await callback.message.edit_text("🔄 <b>Проверка платежа...</b>\n⏳ Подождите 15-25 секунд...")
    except:
        checking_msg = callback.message
    payment_found = False
    for _ in range(Config.MAX_PAYMENT_CHECK_ATTEMPTS):
        payment_found = await YooMoneyService.check_payment(order_id, order["amount"], order.get("created_at", time.time()))
        if payment_found:
            break
        await asyncio.sleep(Config.PAYMENT_CHECK_INTERVAL)
    if payment_found:
        success = await process_successful_payment(order_id, "Автопроверка")
        if success:
            try:
                await checking_msg.edit_text("✅ <b>Платеж найден!</b>\n📨 Проверьте сообщение ⬆️")
            except:
                pass
        else:
            try:
                await checking_msg.edit_text("✅ Уже обработан")
            except:
                pass
    else:
        product = order['product']
        payment_url = create_payment_link(order["amount"], order_id, f"{product['name']} ({product['duration']})")
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="💳 Оплатить картой", url=payment_url)],
            [InlineKeyboardButton(text="✅ Проверить оплату", callback_data=f"checkym_{order_id}")],
            [InlineKeyboardButton(text="❌ Отмена", callback_data="restart")]
        ])
        try:
            await checking_msg.edit_text(f"⏳ <b>Платеж не найден</b>\n\n💰 {order['amount']} ₽\n🆔 <code>{order_id}</code>\n\n⏰ Попробуйте через 1-2 мин", reply_markup=kb)
        except:
            pass

# ========== ОПЛАТА STARS ==========
@dp.callback_query(F.data.startswith("pay_stars_"))
async def process_stars_payment(callback: types.CallbackQuery):
    parts = callback.data.split("_")
    if len(parts) < 4:
        return
    product = find_product(parts[2], parts[3])
    if not product:
        return
    if not rate_limiter.check(callback.from_user.id):
        await callback.answer("⏳", show_alert=True)
        return
    order_id = generate_order_id()
    await orders.add_pending(order_id, {"user_id": callback.from_user.id, "user_name": callback.from_user.full_name, "product": product, "amount": product['price_stars'], "currency": "⭐", "payment_method": "Telegram Stars", "status": "pending", "created_at": time.time()})
    await bot.send_invoice(chat_id=callback.from_user.id, title=f"PMT — {product['name']}", description=f"Подписка на {product['duration']} для {product['platform']}", payload=f"stars_{order_id}", provider_token="", currency="XTR", prices=[LabeledPrice(label="XTR", amount=product['price_stars'])], start_parameter="pmt_payment")
    await send_admin_notification(callback.from_user, product, "⭐ Stars", f"{product['price_stars']} ⭐", order_id)
    try:
        await callback.message.delete()
    except:
        pass
    await callback.answer()

@dp.pre_checkout_query()
async def pre_checkout_query_handler(pcq: PreCheckoutQuery):
    await bot.answer_pre_checkout_query(pcq.id, ok=True)

@dp.message(F.successful_payment)
async def successful_payment(message: types.Message):
    payload = message.successful_payment.invoice_payload
    if payload.startswith("stars_"):
        await process_successful_payment(payload.replace("stars_", "", 1), "Telegram Stars")

# ========== ОПЛАТА КРИПТО ==========
@dp.callback_query(F.data.startswith("pay_crypto_"))
async def process_crypto_payment(callback: types.CallbackQuery):
    if not Config.CRYPTOBOT_TOKEN:
        await callback.answer("❌ Недоступно", show_alert=True)
        return
    parts = callback.data.split("_")
    if len(parts) < 4:
        return
    product = find_product(parts[2], parts[3])
    if not product:
        return
    if not rate_limiter.check(callback.from_user.id):
        await callback.answer("⏳", show_alert=True)
        return
    order_id = generate_order_id()
    amount_usdt = product["price_crypto_usdt"]
    invoice_data = await CryptoBotService.create_invoice(amount_usdt, order_id, f"PMT {product['name']} ({product['duration']})")
    if not invoice_data:
        await callback.answer("❌ Ошибка инвойса", show_alert=True)
        return
    await orders.add_pending(order_id, {"user_id": callback.from_user.id, "user_name": callback.from_user.full_name, "product": product, "amount": amount_usdt, "currency": "USDT", "payment_method": "CryptoBot", "status": "pending", "invoice_id": invoice_data["invoice_id"], "created_at": time.time()})
    text = f"""₿ <b>Криптооплата</b>

{product['emoji']} {product['name']}
⏱️ {product['duration']}
💰 <b>{amount_usdt} USDT</b>
🆔 <code>{order_id}</code>

1️⃣ Нажмите «Оплатить»
2️⃣ Выберите валюту
3️⃣ Нажмите «Проверить»"""
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="₿ Оплатить криптой", url=invoice_data["pay_url"])],
        [InlineKeyboardButton(text="✅ Проверить", callback_data=f"checkcr_{order_id}")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="restart")]
    ])
    try:
        await callback.message.edit_text(text, reply_markup=kb)
    except:
        pass
    await send_admin_notification(callback.from_user, product, "₿ CryptoBot", f"{amount_usdt} USDT", order_id)
    await callback.answer()

@dp.callback_query(F.data.startswith("checkcr_"))
async def check_crypto_callback(callback: types.CallbackQuery):
    order_id = callback.data.replace("checkcr_", "", 1)
    order = await orders.get_pending(order_id)
    if not order:
        if await orders.is_confirmed(order_id):
            await callback.answer("✅ Уже оплачено!", show_alert=True)
        else:
            await callback.answer("❌ Не найден", show_alert=True)
        return
    if not rate_limiter.check(callback.from_user.id):
        await callback.answer("⏳", show_alert=True)
        return
    await callback.answer("🔍 Проверяем...")
    invoice_id = order.get("invoice_id")
    if not invoice_id:
        return
    if await CryptoBotService.check_invoice(invoice_id):
        success = await process_successful_payment(order_id, "CryptoBot")
        if success:
            try:
                await callback.message.edit_text("✅ <b>Криптоплатеж подтвержден!</b>\n📨 Ключ отправлен ⬆️")
            except:
                pass
    else:
        await callback.answer("⏳ Не подтвержден. Попробуйте через минуту.", show_alert=True)

# ========== GOLD / NFT ==========
@dp.callback_query(F.data.startswith("pay_gold_"))
async def process_gold_payment(callback: types.CallbackQuery):
    await _process_manual_payment(callback, "gold")

@dp.callback_query(F.data.startswith("pay_nft_"))
async def process_nft_payment(callback: types.CallbackQuery):
    await _process_manual_payment(callback, "nft")

async def _process_manual_payment(callback, method):
    parts = callback.data.split("_")
    if len(parts) < 4:
        return
    product = find_product(parts[2], parts[3])
    if not product:
        return
    if not rate_limiter.check(callback.from_user.id):
        await callback.answer("⏳", show_alert=True)
        return
    cfg = {"gold": {"name": "GOLD", "icon": "💰", "price_key": "price_gold"}, "nft": {"name": "NFT", "icon": "🎨", "price_key": "price_nft"}}[method]
    price = product[cfg["price_key"]]
    chat_message = f"Привет! Хочу купить чит PMT на Standoff 2. Подписка на {product['period_text']} ({product['platform']}). Готов купить за {price} {cfg['name']}"
    support_url = f"https://t.me/{Config.SUPPORT_CHAT_USERNAME}?text={quote(chat_message, safe='')}"
    order_id = generate_order_id()
    await orders.add_pending(order_id, {"user_id": callback.from_user.id, "user_name": callback.from_user.full_name, "product": product, "amount": price, "currency": cfg["name"], "payment_method": cfg["name"], "status": "pending", "created_at": time.time()})
    text = f"""{cfg['icon']} <b>Оплата {cfg['name']}</b>

{product['emoji']} {product['name']}
⏱️ {product['duration']}
💰 <b>{price} {cfg['name']}</b>

1️⃣ Напишите в поддержку
2️⃣ Ожидайте обработки"""
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💬 Перейти к оплате", url=support_url)],
        [InlineKeyboardButton(text="✅ Я написал", callback_data=f"{method}_sent")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="restart")]
    ])
    try:
        await callback.message.edit_text(text, reply_markup=kb)
    except:
        pass
    await send_admin_notification(callback.from_user, product, f"{cfg['icon']} {cfg['name']}", f"{price} {cfg['name']}", order_id)
    await callback.answer()

@dp.callback_query(F.data.in_({"gold_sent", "nft_sent"}))
async def manual_payment_sent(callback: types.CallbackQuery):
    mname = "GOLD" if callback.data == "gold_sent" else "NFT"
    icon = "💰" if callback.data == "gold_sent" else "🎨"
    text = f"""✅ <b>Отлично!</b>

{icon} Ваш {mname} заказ принят
⏱️ Обработка: до 30 мин

💬 Поддержка: @{Config.SUPPORT_CHAT_USERNAME}"""
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💬 Поддержка", url=f"https://t.me/{Config.SUPPORT_CHAT_USERNAME}")],
        [InlineKeyboardButton(text="🔄 Новая покупка", callback_data="restart")]
    ])
    try:
        await callback.message.edit_text(text, reply_markup=kb)
    except:
        pass
    await callback.answer()

# ========== АДМИН ==========
@dp.callback_query(F.data.startswith("admin_confirm_"))
async def admin_confirm(callback: types.CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("❌", show_alert=True)
        return
    order_id = callback.data.replace("admin_confirm_", "", 1)
    success = await process_successful_payment(order_id, "👨‍💼 Админ")
    if success:
        try:
            await callback.message.edit_text(f"✅ <b>Подтверждён</b>\n🆔 {order_id}\n👨‍💼 {callback.from_user.full_name}")
        except:
            pass
        await callback.answer("✅")
    else:
        await callback.answer("❌ Не найден", show_alert=True)

@dp.callback_query(F.data.startswith("admin_reject_"))
async def admin_reject(callback: types.CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("❌", show_alert=True)
        return
    order_id = callback.data.replace("admin_reject_", "", 1)
    order = await orders.remove_pending(order_id)
    if order:
        try:
            await callback.message.edit_text(f"❌ <b>Отклонён</b>\n🆔 {order_id}")
        except:
            pass
        try:
            await bot.send_message(order['user_id'], f"❌ <b>Заказ отклонён</b>\n💬 @{Config.SUPPORT_CHAT_USERNAME}")
        except:
            pass
    await callback.answer("❌")

@dp.message(Command("orders"))
async def cmd_orders(message: types.Message):
    if not is_admin(message.from_user.id):
        return
    stats = await orders.get_stats()
    text = f"📊 <b>СТАТИСТИКА</b>\n\n⏳ Ожидают: {stats['pending']}\n"
    for oid, order in await orders.get_recent_pending(5):
        t = datetime.fromtimestamp(order['created_at']).strftime('%H:%M')
        text += f"• {t} | {order['user_name']} | {order['product']['name']}\n"
    text += f"\n✅ Подтверждено: {stats['confirmed']}\n"
    balance = await YooMoneyService.get_balance()
    if balance is not None:
        text += f"💰 Баланс: {balance} ₽"
    await message.answer(text)

# ========== НАВИГАЦИЯ ==========
@dp.callback_query(F.data == "restart")
async def restart_order(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    text = """<b>PMT | STANDOFF 2 PREMIUM 💰</b>

🚀 Универсальное решение:
📱 Android (APK, без Root)
💻 PC
🍏 iOS

🔥 Функционал:
• Аимбот + WallHack + ESP
• Анти-бан защита

Лучшие цены | Быстрая поддержка 24/7

Покупай чит, и разноси соперников ⚡️"""
    try:
        await callback.message.edit_text(text, reply_markup=start_keyboard())
    except:
        await callback.message.answer(text, reply_markup=start_keyboard())
    await callback.answer()

@dp.callback_query(F.data == "back_to_start")
async def back_to_start(callback: types.CallbackQuery, state: FSMContext):
    await restart_order(callback, state)

@dp.callback_query(F.data == "back_to_subscription")
async def back_to_subscription(callback: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    platform = data.get("platform", "apk")
    try:
        await callback.message.edit_text("💰 <b>Выберите тариф:</b>", reply_markup=subscription_keyboard(platform))
    except:
        pass
    await state.set_state(OrderState.choosing_subscription)
    await callback.answer()

# ========== WEBAPP DATA ==========
@dp.message(F.web_app_data)
async def handle_webapp_data(message: types.Message):
    try:
        data = json.loads(message.web_app_data.data)
        action = data.get("action")
        if action == "buy":
            product_id = data.get("product_id")
            payment_method = data.get("payment_method")
            product = find_product_by_id(product_id)
            if not product:
                await message.answer("❌ Продукт не найден")
                return
            order_id = generate_order_id()
            user = message.from_user

            if payment_method == "card":
                amount = product["price"]
                payment_url = create_payment_link(amount, order_id, f"{product['name']} ({product['duration']})")
                await orders.add_pending(order_id, {"user_id": user.id, "user_name": user.full_name, "product": product, "amount": amount, "currency": "₽", "payment_method": "Картой (MiniApp)", "status": "pending", "created_at": time.time()})
                kb = InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="💳 Оплатить картой", url=payment_url)],
                    [InlineKeyboardButton(text="✅ Проверить оплату", callback_data=f"checkym_{order_id}")],
                    [InlineKeyboardButton(text="❌ Отмена", callback_data="restart")]
                ])
                await message.answer(f"💳 <b>Оплата картой</b>\n\n{product['emoji']} {product['name']}\n⏱️ {product['duration']}\n💰 <b>{amount} ₽</b>\n🆔 <code>{order_id}</code>", reply_markup=kb)
                await send_admin_notification(user, product, "💳 Картой (MiniApp)", f"{amount} ₽", order_id)

            elif payment_method == "stars":
                await orders.add_pending(order_id, {"user_id": user.id, "user_name": user.full_name, "product": product, "amount": product['price_stars'], "currency": "⭐", "payment_method": "Stars (MiniApp)", "status": "pending", "created_at": time.time()})
                await bot.send_invoice(chat_id=user.id, title=f"PMT — {product['name']}", description=f"Подписка на {product['duration']} для {product['platform']}", payload=f"stars_{order_id}", provider_token="", currency="XTR", prices=[LabeledPrice(label="XTR", amount=product['price_stars'])], start_parameter="pmt_payment")
                await send_admin_notification(user, product, "⭐ Stars (MiniApp)", f"{product['price_stars']} ⭐", order_id)

            elif payment_method == "crypto":
                amount_usdt = product["price_crypto_usdt"]
                invoice_data = await CryptoBotService.create_invoice(amount_usdt, order_id, f"PMT {product['name']} ({product['duration']})")
                if not invoice_data:
                    await message.answer("❌ Ошибка создания инвойса")
                    return
                await orders.add_pending(order_id, {"user_id": user.id, "user_name": user.full_name, "product": product, "amount": amount_usdt, "currency": "USDT", "payment_method": "CryptoBot (MiniApp)", "status": "pending", "invoice_id": invoice_data["invoice_id"], "created_at": time.time()})
                kb = InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="₿ Оплатить криптой", url=invoice_data["pay_url"])],
                    [InlineKeyboardButton(text="✅ Проверить", callback_data=f"checkcr_{order_id}")],
                    [InlineKeyboardButton(text="❌ Отмена", callback_data="restart")]
                ])
                await message.answer(f"₿ <b>Криптооплата</b>\n\n{product['emoji']} {product['name']}\n⏱️ {product['duration']}\n💰 <b>{amount_usdt} USDT</b>\n🆔 <code>{order_id}</code>", reply_markup=kb)
                await send_admin_notification(user, product, "₿ CryptoBot (MiniApp)", f"{amount_usdt} USDT", order_id)

            elif payment_method in ("gold", "nft"):
                cfg = {"gold": {"name": "GOLD", "icon": "💰", "price_key": "price_gold"}, "nft": {"name": "NFT", "icon": "🎨", "price_key": "price_nft"}}[payment_method]
                price = product[cfg["price_key"]]
                chat_msg = f"Привет! Хочу купить чит PMT. {product['period_text']} ({product['platform']}). За {price} {cfg['name']}"
                support_url = f"https://t.me/{Config.SUPPORT_CHAT_USERNAME}?text={quote(chat_msg, safe='')}"
                await orders.add_pending(order_id, {"user_id": user.id, "user_name": user.full_name, "product": product, "amount": price, "currency": cfg["name"], "payment_method": f"{cfg['name']} (MiniApp)", "status": "pending", "created_at": time.time()})
                kb = InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="💬 Перейти к оплате", url=support_url)],
                    [InlineKeyboardButton(text="❌ Отмена", callback_data="restart")]
                ])
                await message.answer(f"{cfg['icon']} <b>Оплата {cfg['name']}</b>\n\n{product['emoji']} {product['name']}\n⏱️ {product['duration']}\n💰 <b>{price} {cfg['name']}</b>", reply_markup=kb)
                await send_admin_notification(user, product, f"{cfg['icon']} {cfg['name']} (MiniApp)", f"{price} {cfg['name']}", order_id)
    except Exception as e:
        logger.error("WebApp data error: %s", e)
        await message.answer("❌ Ошибка обработки заказа")


# =============================================
# ========== MINIAPP HTML (aiohttp) ===========
# =============================================

MINIAPP_HTML = """<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0,maximum-scale=1.0,user-scalable=no">
<title>PMT Premium Shop</title>
<script src="https://telegram.org/js/telegram-web-app.js"></script>
<style>
*{margin:0;padding:0;box-sizing:border-box;-webkit-tap-highlight-color:transparent}
:root{
--bg1:#0a0a1a;--bg2:#111128;--bg3:#161640;--bg4:#1c1c50;
--ac1:#6c5ce7;--ac2:#a29bfe;--acg:rgba(108,92,231,0.3);
--gold:#ffd700;--green:#00e676;--red:#ff5252;--cyan:#00e5ff;
--t1:#fff;--t2:#b8b8d4;--t3:#6b6b8d;
--bc:rgba(108,92,231,0.2);
--gp:linear-gradient(135deg,#6c5ce7 0%,#a29bfe 100%);
--gg:linear-gradient(135deg,#f5af19 0%,#f12711 100%)
}
body{
font-family:Inter,-apple-system,BlinkMacSystemFont,sans-serif;
background:linear-gradient(180deg,#0a0a1a 0%,#0d0d2b 50%,#111128 100%);
color:var(--t1);min-height:100vh;overflow-x:hidden;position:relative
}
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700;800;900&display=swap');

/* BG ANIMATION */
.bg-anim{position:fixed;top:0;left:0;width:100%;height:100%;z-index:0;overflow:hidden;pointer-events:none}
.orb{position:absolute;border-radius:50%;filter:blur(80px);opacity:.15;animation:fo 20s ease-in-out infinite}
.orb:nth-child(1){width:400px;height:400px;background:#6c5ce7;top:-100px;left:-100px;animation-duration:25s}
.orb:nth-child(2){width:300px;height:300px;background:#00e5ff;top:50%;right:-80px;animation-delay:-5s;animation-duration:20s}
.orb:nth-child(3){width:350px;height:350px;background:#f12711;bottom:-100px;left:30%;animation-delay:-10s;animation-duration:30s}
.orb:nth-child(4){width:250px;height:250px;background:#ffd700;top:30%;left:50%;animation-delay:-7s;animation-duration:22s}
@keyframes fo{0%,100%{transform:translate(0,0) scale(1)}25%{transform:translate(80px,-60px) scale(1.1)}50%{transform:translate(-40px,80px) scale(.9)}75%{transform:translate(60px,40px) scale(1.05)}}

.bg-grid{position:fixed;top:0;left:0;width:100%;height:100%;z-index:0;pointer-events:none;
background-image:linear-gradient(rgba(108,92,231,.03) 1px,transparent 1px),linear-gradient(90deg,rgba(108,92,231,.03) 1px,transparent 1px);
background-size:40px 40px}

.particles{position:fixed;top:0;left:0;width:100%;height:100%;z-index:0;pointer-events:none}
.particle{position:absolute;width:3px;height:3px;background:var(--ac2);border-radius:50%;opacity:0;animation:pf 8s ease-in-out infinite}
@keyframes pf{0%{opacity:0;transform:translateY(100vh) scale(0)}10%{opacity:.6}90%{opacity:.6}100%{opacity:0;transform:translateY(-20px) scale(1)}}

/* CONTAINER */
.app{position:relative;z-index:1;max-width:480px;margin:0 auto;padding:16px;padding-bottom:100px}

/* HEADER */
.hdr{text-align:center;padding:32px 20px 24px;position:relative}
.logo-c{position:relative;display:inline-block;margin-bottom:16px}
.logo-glow{position:absolute;top:50%;left:50%;transform:translate(-50%,-50%);width:120px;height:120px;background:var(--ac1);border-radius:50%;filter:blur(40px);opacity:.4;animation:lg 3s ease-in-out infinite alternate}
@keyframes lg{0%{opacity:.3;transform:translate(-50%,-50%) scale(.8)}100%{opacity:.6;transform:translate(-50%,-50%) scale(1.2)}}
.logo-i{position:relative;width:80px;height:80px;background:var(--gp);border-radius:24px;display:flex;align-items:center;justify-content:center;font-size:40px;box-shadow:0 8px 32px rgba(108,92,231,.4);animation:lf 4s ease-in-out infinite}
@keyframes lf{0%,100%{transform:translateY(0)}50%{transform:translateY(-8px)}}
.hdr h1{font-size:28px;font-weight:800;background:linear-gradient(135deg,#fff 0%,#a29bfe 100%);-webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text;letter-spacing:-.5px}
.hdr .sub{font-size:14px;color:var(--t2);margin-top:6px}
.hdr .badge{display:inline-flex;align-items:center;gap:6px;background:rgba(0,230,118,.1);border:1px solid rgba(0,230,118,.3);color:var(--green);padding:6px 16px;border-radius:20px;font-size:12px;font-weight:600;margin-top:12px}
.pulse-dot{width:8px;height:8px;background:var(--green);border-radius:50%;animation:pd 2s ease-in-out infinite}
@keyframes pd{0%,100%{opacity:1;transform:scale(1)}50%{opacity:.5;transform:scale(.7)}}

/* FEATURES */
.feat{display:flex;gap:8px;overflow-x:auto;padding:4px 0 16px;scrollbar-width:none;-ms-overflow-style:none}
.feat::-webkit-scrollbar{display:none}
.fchip{flex-shrink:0;display:flex;align-items:center;gap:6px;background:var(--bg3);border:1px solid var(--bc);padding:8px 14px;border-radius:12px;font-size:12px;color:var(--t2);font-weight:500;white-space:nowrap;transition:.3s}
.fchip:hover{border-color:var(--ac1);background:var(--bg4)}

/* SECTION TITLE */
.stitle{display:flex;align-items:center;gap:10px;margin:24px 0 16px;padding:0 4px}
.stitle .ln{flex:1;height:1px;background:linear-gradient(90deg,var(--bc) 0%,transparent 100%)}
.stitle .ln:last-child{background:linear-gradient(90deg,transparent 0%,var(--bc) 100%)}
.stitle span{font-size:13px;font-weight:700;text-transform:uppercase;letter-spacing:2px;color:var(--t3)}

/* PLATFORM TABS */
.ptabs{display:flex;gap:8px;margin-bottom:20px}
.ptab{flex:1;padding:14px 16px;background:var(--bg3);border:2px solid var(--bc);border-radius:16px;text-align:center;cursor:pointer;transition:.3s cubic-bezier(.4,0,.2,1);position:relative;overflow:hidden}
.ptab::before{content:'';position:absolute;top:0;left:0;width:100%;height:100%;background:var(--gp);opacity:0;transition:opacity .3s}
.ptab.active{border-color:var(--ac1);box-shadow:0 4px 20px var(--acg)}
.ptab.active::before{opacity:.1}
.ptab .ti{font-size:28px;display:block;margin-bottom:6px;position:relative;z-index:1}
.ptab .tl{font-size:13px;font-weight:700;position:relative;z-index:1;color:var(--t2)}
.ptab.active .tl{color:var(--t1)}

/* PRODUCT CARDS */
.pgrid{display:flex;flex-direction:column;gap:12px}
.pcard{background:var(--bg3);border:1px solid var(--bc);border-radius:20px;padding:20px;position:relative;overflow:hidden;cursor:pointer;transition:.3s cubic-bezier(.4,0,.2,1)}
.pcard:hover{transform:translateY(-2px);border-color:var(--ac1);box-shadow:0 8px 30px var(--acg)}
.pcard.sel{border-color:var(--ac1);box-shadow:0 4px 24px var(--acg);background:var(--bg4)}
.pcard .cglow{position:absolute;top:-50%;right:-50%;width:200px;height:200px;background:var(--ac1);border-radius:50%;filter:blur(80px);opacity:0;transition:.5s;pointer-events:none}
.pcard.sel .cglow,.pcard:hover .cglow{opacity:.08}
.pcard .popb{position:absolute;top:12px;right:12px;background:var(--gg);color:#000;padding:4px 10px;border-radius:8px;font-size:10px;font-weight:800;text-transform:uppercase;letter-spacing:1px}
.pcard .chdr{display:flex;align-items:center;gap:14px;margin-bottom:14px}
.chdr .pi{width:48px;height:48px;border-radius:14px;display:flex;align-items:center;justify-content:center;font-size:24px;flex-shrink:0}
.chdr .pi.week{background:rgba(0,229,255,.15)}
.chdr .pi.month{background:rgba(108,92,231,.15)}
.chdr .pi.forever{background:rgba(255,215,0,.15)}
.chdr .ci h3{font-size:16px;font-weight:700}
.chdr .ci .dur{font-size:12px;color:var(--t2);margin-top:2px}
.cprices{display:grid;grid-template-columns:repeat(2,1fr);gap:8px;margin-top:12px}
.ptag{display:flex;align-items:center;gap:6px;background:rgba(255,255,255,.03);padding:8px 10px;border-radius:10px;font-size:13px;font-weight:600}
.ptag.main{grid-column:1/-1;background:rgba(108,92,231,.1);border:1px solid rgba(108,92,231,.2);font-size:18px;font-weight:800;justify-content:center;padding:12px;color:var(--ac2)}

/* PAYMENT */
.paysec{display:none;animation:su .4s cubic-bezier(.4,0,.2,1)}
.paysec.vis{display:block}
@keyframes su{from{opacity:0;transform:translateY(20px)}to{opacity:1;transform:translateY(0)}}
.paymethods{display:flex;flex-direction:column;gap:8px}
.paybtn{display:flex;align-items:center;gap:14px;background:var(--bg3);border:1px solid var(--bc);border-radius:16px;padding:16px 18px;cursor:pointer;transition:.3s;color:var(--t1);width:100%;font-family:inherit;font-size:15px;font-weight:600}
.paybtn:hover,.paybtn:active{border-color:var(--ac1);background:var(--bg4);transform:scale(.98)}
.paybtn .picon{width:44px;height:44px;border-radius:12px;display:flex;align-items:center;justify-content:center;font-size:22px;flex-shrink:0}
.paybtn .picon.card{background:rgba(108,92,231,.15)}
.paybtn .picon.stars{background:rgba(255,193,7,.15)}
.paybtn .picon.crypto{background:rgba(0,229,255,.15)}
.paybtn .picon.gold{background:rgba(255,215,0,.15)}
.paybtn .picon.nft{background:rgba(233,30,99,.15)}
.paybtn .pinfo{flex:1;text-align:left}
.paybtn .pinfo .pn{font-weight:600}
.paybtn .pinfo .pp{font-size:12px;color:var(--t2);margin-top:2px}
.paybtn .parr{color:var(--t3);font-size:18px}

/* SUMMARY */
.selsum{display:none;background:linear-gradient(135deg,rgba(108,92,231,.1) 0%,rgba(162,155,254,.05) 100%);border:1px solid rgba(108,92,231,.3);border-radius:16px;padding:16px;margin-bottom:16px;animation:su .3s ease}
.selsum.vis{display:flex;align-items:center;gap:12px}
.selsum .si{font-size:32px}
.selsum .sinfo h4{font-size:15px;font-weight:700}
.selsum .sinfo p{font-size:12px;color:var(--t2);margin-top:2px}

/* FOOTER */
.footer{text-align:center;padding:24px 0;color:var(--t3);font-size:12px}
.footer a{color:var(--ac2);text-decoration:none}

/* SUPPORT FLOAT */
.sfloat{position:fixed;bottom:20px;right:20px;width:56px;height:56px;background:var(--gp);border-radius:50%;display:flex;align-items:center;justify-content:center;font-size:24px;box-shadow:0 4px 20px var(--acg);z-index:100;cursor:pointer;transition:.3s;text-decoration:none}
.sfloat:hover{transform:scale(1.1)}

/* TOAST */
.toast{position:fixed;bottom:90px;left:50%;transform:translateX(-50%) translateY(20px);background:var(--bg3);border:1px solid var(--ac1);padding:12px 24px;border-radius:12px;font-size:13px;font-weight:600;opacity:0;transition:.3s;z-index:200;pointer-events:none;white-space:nowrap}
.toast.show{opacity:1;transform:translateX(-50%) translateY(0)}

/* LOADING */
.lov{position:fixed;top:0;left:0;width:100%;height:100%;background:var(--bg1);display:flex;flex-direction:column;align-items:center;justify-content:center;z-index:9999;transition:opacity .5s}
.lov.hid{opacity:0;pointer-events:none}
.lspin{width:48px;height:48px;border:3px solid var(--bc);border-top-color:var(--ac1);border-radius:50%;animation:spin .8s linear infinite}
@keyframes spin{to{transform:rotate(360deg)}}
.ltxt{margin-top:16px;font-size:14px;color:var(--t2);font-weight:500}

::-webkit-scrollbar{width:4px}
::-webkit-scrollbar-track{background:transparent}
::-webkit-scrollbar-thumb{background:var(--ac1);border-radius:4px}
@media(max-width:360px){.cprices{grid-template-columns:1fr}.hdr h1{font-size:24px}}
</style>
</head>
<body>

<div class="lov" id="lov">
<div class="lspin"></div>
<div class="ltxt">Загрузка PMT Shop...</div>
</div>

<div class="bg-anim"><div class="orb"></div><div class="orb"></div><div class="orb"></div><div class="orb"></div></div>
<div class="bg-grid"></div>
<div class="particles" id="particles"></div>

<div class="app">

<div class="hdr">
<div class="logo-c"><div class="logo-glow"></div><div class="logo-i">🎮</div></div>
<h1>PMT PREMIUM</h1>
<div class="sub">Standoff 2 Cheat Shop</div>
<div class="badge"><span class="pulse-dot"></span>ONLINE • UNDETECTED</div>
</div>

<div class="feat">
<div class="fchip"><span>🎯</span> AimBot</div>
<div class="fchip"><span>👁️</span> WallHack</div>
<div class="fchip"><span>📍</span> ESP</div>
<div class="fchip"><span>🗺️</span> Radar</div>
<div class="fchip"><span>🛡️</span> Anti-Ban</div>
<div class="fchip"><span>⚡</span> No Root</div>
</div>

<div class="stitle"><div class="ln"></div><span>Платформа</span><div class="ln"></div></div>

<div class="ptabs">
<div class="ptab active" data-platform="apk" onclick="selPlat('apk')"><span class="ti">📱</span><span class="tl">Android</span></div>
<div class="ptab" data-platform="ios" onclick="selPlat('ios')"><span class="ti">🍎</span><span class="tl">iOS</span></div>
</div>

<div class="stitle"><div class="ln"></div><span>Тарифы</span><div class="ln"></div></div>
<div class="pgrid" id="pgrid"></div>

<div class="selsum" id="selsum"><div class="si" id="si"></div><div class="sinfo"><h4 id="st"></h4><p id="sd"></p></div></div>

<div class="paysec" id="paysec">
<div class="stitle"><div class="ln"></div><span>Оплата</span><div class="ln"></div></div>
<div class="paymethods" id="paymethods"></div>
</div>

<div class="footer">
<p>PMT Premium © 2024</p>
<p style="margin-top:4px">Поддержка: <a href="https://t.me/""" + Config.SUPPORT_CHAT_USERNAME + """">@""" + Config.SUPPORT_CHAT_USERNAME + """</a></p>
</div>

</div>

<a class="sfloat" href="https://t.me/""" + Config.SUPPORT_CHAT_USERNAME + """" target="_blank">💬</a>
<div class="toast" id="toast"></div>

<script>
const tg=window.Telegram.WebApp;
tg.ready();tg.expand();
try{tg.setHeaderColor('#0a0a1a');tg.setBackgroundColor('#0a0a1a')}catch(e){}

const P=""" + PRODUCTS_JSON + """;
let curPlat='apk',curProd=null;

function mkParts(){
const c=document.getElementById('particles');
for(let i=0;i<30;i++){
const p=document.createElement('div');p.className='particle';
p.style.left=Math.random()*100+'%';
p.style.animationDelay=Math.random()*8+'s';
p.style.animationDuration=(6+Math.random()*6)+'s';
p.style.width=p.style.height=(2+Math.random()*3)+'px';
c.appendChild(p)}}

function selPlat(pl){
curPlat=pl;curProd=null;
document.querySelectorAll('.ptab').forEach(t=>t.classList.toggle('active',t.dataset.platform===pl));
render();hidePay();hap('light')}

function render(){
const g=document.getElementById('pgrid');g.innerHTML='';
const prods=Object.entries(P).filter(([id,p])=>p.platform_code===curPlat);
prods.forEach(([id,product],i)=>{
const pop=id.includes('month');
const pc=id.includes('week')?'week':id.includes('month')?'month':'forever';
const pe=id.includes('week')?'⚡':id.includes('month')?'🔥':'💎';
const c=document.createElement('div');c.className='pcard';c.dataset.pid=id;
c.onclick=()=>selProd(id);
c.innerHTML=`
<div class="cglow"></div>
${pop?'<div class="popb">ХИТ</div>':''}
<div class="chdr">
<div class="pi ${pc}">${pe}</div>
<div class="ci"><h3>${product.period_text}</h3><div class="dur">${product.duration}</div></div>
</div>
<div class="cprices">
<div class="ptag main">💳 ${product.price} ₽</div>
<div class="ptag">⭐ ${product.price_stars} Stars</div>
<div class="ptag">₿ ${product.price_crypto_usdt} USDT</div>
<div class="ptag">🪙 ${product.price_gold} GOLD</div>
<div class="ptag">🎨 ${product.price_nft} NFT</div>
</div>`;
g.appendChild(c)})}

function selProd(id){
curProd=id;const p=P[id];
document.querySelectorAll('.pcard').forEach(c=>c.classList.toggle('sel',c.dataset.pid===id));
const s=document.getElementById('selsum');
document.getElementById('si').textContent=p.emoji;
document.getElementById('st').textContent=p.name+' — '+p.period_text;
document.getElementById('sd').textContent=p.duration+' • '+p.price+' ₽';
s.classList.add('vis');
showPay(p);hap('medium');
setTimeout(()=>document.getElementById('paysec').scrollIntoView({behavior:'smooth',block:'start'}),200)}

function showPay(p){
const sec=document.getElementById('paysec'),m=document.getElementById('paymethods');m.innerHTML='';
const pays=[
{m:'card',i:'💳',c:'card',n:'Банковская карта',p:p.price+' ₽'},
{m:'stars',i:'⭐',c:'stars',n:'Telegram Stars',p:p.price_stars+' Stars'},
{m:'crypto',i:'₿',c:'crypto',n:'Криптовалюта',p:p.price_crypto_usdt+' USDT'},
{m:'gold',i:'🪙',c:'gold',n:'Standoff GOLD',p:p.price_gold+' GOLD'},
{m:'nft',i:'🎨',c:'nft',n:'NFT Standoff',p:p.price_nft+' NFT'}
];
pays.forEach(pay=>{
const b=document.createElement('button');b.className='paybtn';
b.onclick=()=>doPay(pay.m);
b.innerHTML=`
<div class="picon ${pay.c}">${pay.i}</div>
<div class="pinfo"><div class="pn">${pay.n}</div><div class="pp">${pay.p}</div></div>
<div class="parr">›</div>`;
m.appendChild(b)});
sec.classList.add('vis')}

function hidePay(){
document.getElementById('paysec').classList.remove('vis');
document.getElementById('selsum').classList.remove('vis')}

function doPay(method){
if(!curProd){toast('Выберите тариф!');return}
hap('heavy');toast('⏳ Создаём заказ...');
const d={action:'buy',product_id:curProd,payment_method:method};
try{tg.sendData(JSON.stringify(d))}catch(e){toast('❌ Ошибка');console.error(e)}}

function toast(t){
const el=document.getElementById('toast');el.textContent=t;el.classList.add('show');
setTimeout(()=>el.classList.remove('show'),2500)}

function hap(t){try{
if(t==='light')tg.HapticFeedback.impactOccurred('light');
else if(t==='medium')tg.HapticFeedback.impactOccurred('medium');
else if(t==='heavy')tg.HapticFeedback.impactOccurred('heavy');
}catch(e){}}

window.addEventListener('load',()=>{mkParts();render();setTimeout(()=>document.getElementById('lov').classList.add('hid'),600)});
</script>
</body>
</html>"""


# =============================================
# ========== AIOHTTP WEB SERVER ===============
# =============================================

async def handle_index(request):
    return web.Response(text=MINIAPP_HTML, content_type='text/html', charset='utf-8')

async def handle_api_products(request):
    return web.json_response(PRODUCTS)

async def handle_api_health(request):
    return web.json_response({"status": "ok", "time": time.time()})

async def start_web_server():
    app = web.Application()
    app.router.add_get('/', handle_index)
    app.router.add_get('/api/products', handle_api_products)
    app.router.add_get('/api/health', handle_api_health)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', Config.WEBAPP_PORT)
    await site.start()
    logger.info("🌐 MiniApp web server started on port %s", Config.WEBAPP_PORT)
    return runner


# ========== ЗАПУСК ==========
async def main():
    logger.info("=" * 50)
    logger.info("PMT PREMIUM CHEAT SHOP — Bot + MiniApp")
    logger.info("=" * 50)
    logger.info("ADMIN_IDS: %s", Config.ADMIN_IDS)
    logger.info("WEBAPP: %s", Config.WEBAPP_URL)
    logger.info("PORT: %s", Config.WEBAPP_PORT)

    # Запускаем веб-сервер
    web_runner = await start_web_server()

    try:
        me = await bot.get_me()
        logger.info("Bot: @%s", me.username)
        for key, product in PRODUCTS.items():
            logger.info("%s %s (%s) - %s RUB / %s Stars", product['emoji'], product['name'], product['duration'], product['price'], product['price_stars'])
        logger.info("🚀 Bot starting polling...")
        await dp.start_polling(bot)
    except KeyboardInterrupt:
        logger.info("Shutting down...")
    except Exception as e:
        logger.error("Fatal: %s", e)
        import traceback
        traceback.print_exc()
    finally:
        await web_runner.cleanup()
        await bot.session.close()
        logger.info("Bot stopped")


if __name__ == "__main__":
    asyncio.run(main())
