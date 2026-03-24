# app.py — PMT Premium Cheat Shop: Bot + MiniApp (single file)
import logging
import asyncio
import aiohttp
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

from flask import Flask, request, jsonify, Response
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

    async def get_confirmed(self, order_id):
        async with self._lock:
            return self._confirmed.get(order_id)

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

def validate_webapp_data(init_data_raw):
    try:
        parsed = dict(parse_qs(init_data_raw))
        parsed = {k: v[0] for k, v in parsed.items()}
        check_hash = parsed.pop('hash', None)
        if not check_hash:
            return None
        data_check = "\n".join(f"{k}={v}" for k, v in sorted(parsed.items()))
        secret_key = hmac.new(b"WebAppData", Config.BOT_TOKEN.encode(), hashlib.sha256).digest()
        computed = hmac.new(secret_key, data_check.encode(), hashlib.sha256).hexdigest()
        if computed == check_hash:
            user_data = json.loads(parsed.get('user', '{}'))
            return user_data
        return None
    except Exception as e:
        logger.error("Validate webapp data error: %s", e)
        return None

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

bot_loop = None

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
2️⃣ Установите приложение
3️⃣ Введите ключ
4️⃣ Наслаждайтесь игрой! 🎮

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
    admin_text = f"""💎 <b>НОВАЯ ПРОДАЖА ({source})</b>

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

Покупай чит, и разноси своих соперников ⚡️"""
    await message.answer(text, reply_markup=start_keyboard())
    await state.set_state(OrderState.main_menu)

@dp.callback_query(F.data == "buy_cheat")
async def buy_cheat(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.edit_text("💰 <b>Выберите платформу:</b>\n\n📱 <b>Android</b> — APK файл\n🍏 <b>iOS</b> — IPA файл", reply_markup=platform_keyboard())
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
    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀️ Назад", callback_data="back_to_start")]]))
    await callback.answer()

@dp.callback_query(F.data.startswith("platform_"))
async def process_platform(callback: types.CallbackQuery, state: FSMContext):
    platform = callback.data.split("_")[1]
    if platform not in ("apk", "ios"):
        await callback.answer("❌ Ошибка", show_alert=True)
        return
    await state.update_data(platform=platform)
    await callback.message.edit_text("💰 <b>Выберите тариф:</b>", reply_markup=subscription_keyboard(platform))
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
    await callback.message.edit_text(text, reply_markup=payment_methods_keyboard(product))
    await state.set_state(OrderState.choosing_payment)
    await callback.answer()

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
    await callback.message.edit_text(text, reply_markup=kb)
    await send_admin_notification(callback.from_user, product, "💳 Картой", f"{amount} ₽", order_id)
    await callback.answer()

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
    checking_msg = await callback.message.edit_text("🔄 <b>Проверка платежа...</b>\n⏳ Подождите 15-25 секунд...")
    payment_found = False
    for _ in range(Config.MAX_PAYMENT_CHECK_ATTEMPTS):
        payment_found = await YooMoneyService.check_payment(order_id, order["amount"], order.get("created_at", time.time()))
        if payment_found:
            break
        await asyncio.sleep(Config.PAYMENT_CHECK_INTERVAL)
    if payment_found:
        success = await process_successful_payment(order_id, "Автопроверка")
        if success:
            await checking_msg.edit_text("✅ <b>Платеж найден!</b>\n📨 Проверьте сообщение ⬆️")
        else:
            await checking_msg.edit_text("✅ Уже обработан")
    else:
        product = order['product']
        payment_url = create_payment_link(order["amount"], order_id, f"{product['name']} ({product['duration']})")
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="💳 Оплатить картой", url=payment_url)],
            [InlineKeyboardButton(text="✅ Проверить оплату", callback_data=f"checkym_{order_id}")],
            [InlineKeyboardButton(text="❌ Отмена", callback_data="restart")]
        ])
        await checking_msg.edit_text(f"⏳ <b>Платеж не найден</b>\n\n💰 {order['amount']} ₽\n🆔 <code>{order_id}</code>\n\n⏰ Попробуйте через 1-2 мин", reply_markup=kb)

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
        [InlineKeyboardButton(text="✅ Проверить платеж", callback_data=f"checkcr_{order_id}")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="restart")]
    ])
    await callback.message.edit_text(text, reply_markup=kb)
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
            await callback.message.edit_text("✅ <b>Криптоплатеж подтвержден!</b>\n📨 Ключ отправлен ⬆️")
    else:
        await callback.answer("⏳ Не подтвержден. Попробуйте через минуту.", show_alert=True)

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
        [InlineKeyboardButton(text=f"✅ Я написал", callback_data=f"{method}_sent")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="restart")]
    ])
    await callback.message.edit_text(text, reply_markup=kb)
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
    await callback.message.edit_text(text, reply_markup=kb)
    await callback.answer()

@dp.callback_query(F.data.startswith("admin_confirm_"))
async def admin_confirm(callback: types.CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("❌", show_alert=True)
        return
    order_id = callback.data.replace("admin_confirm_", "", 1)
    success = await process_successful_payment(order_id, "👨‍💼 Админ")
    if success:
        await callback.message.edit_text(f"✅ <b>Подтверждён</b>\n🆔 {order_id}\n👨‍💼 {callback.from_user.full_name}")
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
        await callback.message.edit_text(f"❌ <b>Отклонён</b>\n🆔 {order_id}")
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

Покупай чит, и разноси своих соперников ⚡️"""
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
    await callback.message.edit_text("💰 <b>Выберите тариф:</b>", reply_markup=subscription_keyboard(platform))
    await state.set_state(OrderState.choosing_subscription)
    await callback.answer()

# Обработка данных из MiniApp
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
                await orders.add_pending(order_id, {"user_id": user.id, "user_name": user.full_name, "product": product, "amount": amount, "currency": "₽", "payment_method": "Картой", "status": "pending", "created_at": time.time()})
                kb = InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="💳 Оплатить картой", url=payment_url)],
                    [InlineKeyboardButton(text="✅ Проверить оплату", callback_data=f"checkym_{order_id}")],
                    [InlineKeyboardButton(text="❌ Отмена", callback_data="restart")]
                ])
                await message.answer(f"💳 <b>Оплата картой</b>\n\n{product['emoji']} {product['name']}\n⏱️ {product['duration']}\n💰 <b>{amount} ₽</b>\n🆔 <code>{order_id}</code>", reply_markup=kb)
                await send_admin_notification(user, product, "💳 Картой", f"{amount} ₽", order_id)

            elif payment_method == "stars":
                await orders.add_pending(order_id, {"user_id": user.id, "user_name": user.full_name, "product": product, "amount": product['price_stars'], "currency": "⭐", "payment_method": "Telegram Stars", "status": "pending", "created_at": time.time()})
                await bot.send_invoice(chat_id=user.id, title=f"PMT — {product['name']}", description=f"Подписка на {product['duration']} для {product['platform']}", payload=f"stars_{order_id}", provider_token="", currency="XTR", prices=[LabeledPrice(label="XTR", amount=product['price_stars'])], start_parameter="pmt_payment")
                await send_admin_notification(user, product, "⭐ Stars", f"{product['price_stars']} ⭐", order_id)

            elif payment_method == "crypto":
                amount_usdt = product["price_crypto_usdt"]
                invoice_data = await CryptoBotService.create_invoice(amount_usdt, order_id, f"PMT {product['name']} ({product['duration']})")
                if not invoice_data:
                    await message.answer("❌ Ошибка создания инвойса")
                    return
                await orders.add_pending(order_id, {"user_id": user.id, "user_name": user.full_name, "product": product, "amount": amount_usdt, "currency": "USDT", "payment_method": "CryptoBot", "status": "pending", "invoice_id": invoice_data["invoice_id"], "created_at": time.time()})
                kb = InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="₿ Оплатить криптой", url=invoice_data["pay_url"])],
                    [InlineKeyboardButton(text="✅ Проверить платеж", callback_data=f"checkcr_{order_id}")],
                    [InlineKeyboardButton(text="❌ Отмена", callback_data="restart")]
                ])
                await message.answer(f"₿ <b>Криптооплата</b>\n\n{product['emoji']} {product['name']}\n⏱️ {product['duration']}\n💰 <b>{amount_usdt} USDT</b>\n🆔 <code>{order_id}</code>", reply_markup=kb)
                await send_admin_notification(user, product, "₿ CryptoBot", f"{amount_usdt} USDT", order_id)

            elif payment_method in ("gold", "nft"):
                cfg = {"gold": {"name": "GOLD", "icon": "💰", "price_key": "price_gold"}, "nft": {"name": "NFT", "icon": "🎨", "price_key": "price_nft"}}[payment_method]
                price = product[cfg["price_key"]]
                chat_message = f"Привет! Хочу купить чит PMT на Standoff 2. Подписка на {product['period_text']} ({product['platform']}). Готов купить за {price} {cfg['name']}"
                support_url = f"https://t.me/{Config.SUPPORT_CHAT_USERNAME}?text={quote(chat_message, safe='')}"
                await orders.add_pending(order_id, {"user_id": user.id, "user_name": user.full_name, "product": product, "amount": price, "currency": cfg["name"], "payment_method": cfg["name"], "status": "pending", "created_at": time.time()})
                kb = InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="💬 Перейти к оплате", url=support_url)],
                    [InlineKeyboardButton(text="❌ Отмена", callback_data="restart")]
                ])
                await message.answer(f"{cfg['icon']} <b>Оплата {cfg['name']}</b>\n\n{product['emoji']} {product['name']}\n⏱️ {product['duration']}\n💰 <b>{price} {cfg['name']}</b>", reply_markup=kb)
                await send_admin_notification(user, product, f"{cfg['icon']} {cfg['name']}", f"{price} {cfg['name']}", order_id)
    except Exception as e:
        logger.error("WebApp data error: %s", e)
        await message.answer("❌ Ошибка обработки заказа")


# =============================================
# ============ FLASK MINIAPP =================
# =============================================
flask_app = Flask(__name__)

MINIAPP_HTML = '''<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
<title>PMT Premium Shop</title>
<script src="https://telegram.org/js/telegram-web-app.js"></script>
<style>
* {
    margin: 0;
    padding: 0;
    box-sizing: border-box;
    -webkit-tap-highlight-color: transparent;
}

:root {
    --bg-primary: #0a0a1a;
    --bg-secondary: #111128;
    --bg-card: #161640;
    --bg-card-hover: #1c1c50;
    --accent-primary: #6c5ce7;
    --accent-secondary: #a29bfe;
    --accent-glow: rgba(108, 92, 231, 0.3);
    --accent-gold: #ffd700;
    --accent-green: #00e676;
    --accent-red: #ff5252;
    --accent-cyan: #00e5ff;
    --text-primary: #ffffff;
    --text-secondary: #b8b8d4;
    --text-muted: #6b6b8d;
    --border-color: rgba(108, 92, 231, 0.2);
    --gradient-primary: linear-gradient(135deg, #6c5ce7 0%, #a29bfe 100%);
    --gradient-gold: linear-gradient(135deg, #f5af19 0%, #f12711 100%);
    --gradient-bg: linear-gradient(180deg, #0a0a1a 0%, #0d0d2b 50%, #111128 100%);
}

@font-face {
    font-family: 'Inter';
    src: url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700;800;900&display=swap');
}

body {
    font-family: 'Inter', -apple-system, BlinkMacSystemFont, sans-serif;
    background: var(--gradient-bg);
    color: var(--text-primary);
    min-height: 100vh;
    overflow-x: hidden;
    position: relative;
}

/* ===== ANIMATED BACKGROUND ===== */
.bg-animation {
    position: fixed;
    top: 0; left: 0;
    width: 100%; height: 100%;
    z-index: 0;
    overflow: hidden;
    pointer-events: none;
}

.bg-animation .orb {
    position: absolute;
    border-radius: 50%;
    filter: blur(80px);
    opacity: 0.15;
    animation: floatOrb 20s ease-in-out infinite;
}

.bg-animation .orb:nth-child(1) {
    width: 400px; height: 400px;
    background: #6c5ce7;
    top: -100px; left: -100px;
    animation-delay: 0s;
    animation-duration: 25s;
}

.bg-animation .orb:nth-child(2) {
    width: 300px; height: 300px;
    background: #00e5ff;
    top: 50%; right: -80px;
    animation-delay: -5s;
    animation-duration: 20s;
}

.bg-animation .orb:nth-child(3) {
    width: 350px; height: 350px;
    background: #f12711;
    bottom: -100px; left: 30%;
    animation-delay: -10s;
    animation-duration: 30s;
}

.bg-animation .orb:nth-child(4) {
    width: 250px; height: 250px;
    background: #ffd700;
    top: 30%; left: 50%;
    animation-delay: -7s;
    animation-duration: 22s;
}

@keyframes floatOrb {
    0%, 100% { transform: translate(0, 0) scale(1); }
    25% { transform: translate(80px, -60px) scale(1.1); }
    50% { transform: translate(-40px, 80px) scale(0.9); }
    75% { transform: translate(60px, 40px) scale(1.05); }
}

/* Grid pattern overlay */
.bg-grid {
    position: fixed;
    top: 0; left: 0;
    width: 100%; height: 100%;
    z-index: 0;
    pointer-events: none;
    background-image:
        linear-gradient(rgba(108, 92, 231, 0.03) 1px, transparent 1px),
        linear-gradient(90deg, rgba(108, 92, 231, 0.03) 1px, transparent 1px);
    background-size: 40px 40px;
}

/* Particles */
.particles {
    position: fixed;
    top: 0; left: 0;
    width: 100%; height: 100%;
    z-index: 0;
    pointer-events: none;
}

.particle {
    position: absolute;
    width: 3px; height: 3px;
    background: var(--accent-secondary);
    border-radius: 50%;
    opacity: 0;
    animation: particleFloat 8s ease-in-out infinite;
}

@keyframes particleFloat {
    0% { opacity: 0; transform: translateY(100vh) scale(0); }
    10% { opacity: 0.6; }
    90% { opacity: 0.6; }
    100% { opacity: 0; transform: translateY(-20px) scale(1); }
}

/* ===== MAIN CONTAINER ===== */
.app-container {
    position: relative;
    z-index: 1;
    max-width: 480px;
    margin: 0 auto;
    padding: 16px;
    padding-bottom: 100px;
}

/* ===== HEADER ===== */
.header {
    text-align: center;
    padding: 32px 20px 24px;
    position: relative;
}

.logo-container {
    position: relative;
    display: inline-block;
    margin-bottom: 16px;
}

.logo-glow {
    position: absolute;
    top: 50%; left: 50%;
    transform: translate(-50%, -50%);
    width: 120px; height: 120px;
    background: var(--accent-primary);
    border-radius: 50%;
    filter: blur(40px);
    opacity: 0.4;
    animation: logoGlow 3s ease-in-out infinite alternate;
}

@keyframes logoGlow {
    0% { opacity: 0.3; transform: translate(-50%, -50%) scale(0.8); }
    100% { opacity: 0.6; transform: translate(-50%, -50%) scale(1.2); }
}

.logo-icon {
    position: relative;
    width: 80px; height: 80px;
    background: var(--gradient-primary);
    border-radius: 24px;
    display: flex;
    align-items: center;
    justify-content: center;
    font-size: 40px;
    box-shadow: 0 8px 32px rgba(108, 92, 231, 0.4);
    animation: logoFloat 4s ease-in-out infinite;
}

@keyframes logoFloat {
    0%, 100% { transform: translateY(0); }
    50% { transform: translateY(-8px); }
}

.header h1 {
    font-size: 28px;
    font-weight: 800;
    background: linear-gradient(135deg, #fff 0%, #a29bfe 100%);
    -webkit-background-clip: text;
    -webkit-text-fill-color: transparent;
    background-clip: text;
    letter-spacing: -0.5px;
}

.header .subtitle {
    font-size: 14px;
    color: var(--text-secondary);
    margin-top: 6px;
    font-weight: 400;
}

.header .badge {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    background: rgba(0, 230, 118, 0.1);
    border: 1px solid rgba(0, 230, 118, 0.3);
    color: var(--accent-green);
    padding: 6px 16px;
    border-radius: 20px;
    font-size: 12px;
    font-weight: 600;
    margin-top: 12px;
}

.badge .pulse-dot {
    width: 8px; height: 8px;
    background: var(--accent-green);
    border-radius: 50%;
    animation: pulseDot 2s ease-in-out infinite;
}

@keyframes pulseDot {
    0%, 100% { opacity: 1; transform: scale(1); }
    50% { opacity: 0.5; transform: scale(0.7); }
}

/* ===== FEATURES BAR ===== */
.features-bar {
    display: flex;
    gap: 8px;
    overflow-x: auto;
    padding: 4px 0 16px;
    scrollbar-width: none;
    -ms-overflow-style: none;
}

.features-bar::-webkit-scrollbar { display: none; }

.feature-chip {
    flex-shrink: 0;
    display: flex;
    align-items: center;
    gap: 6px;
    background: var(--bg-card);
    border: 1px solid var(--border-color);
    padding: 8px 14px;
    border-radius: 12px;
    font-size: 12px;
    color: var(--text-secondary);
    font-weight: 500;
    white-space: nowrap;
    transition: all 0.3s;
}

.feature-chip:hover {
    border-color: var(--accent-primary);
    background: var(--bg-card-hover);
}

.feature-chip .icon { font-size: 14px; }

/* ===== SECTION TITLE ===== */
.section-title {
    display: flex;
    align-items: center;
    gap: 10px;
    margin: 24px 0 16px;
    padding: 0 4px;
}

.section-title .line {
    flex: 1;
    height: 1px;
    background: linear-gradient(90deg, var(--border-color) 0%, transparent 100%);
}

.section-title .line:last-child {
    background: linear-gradient(90deg, transparent 0%, var(--border-color) 100%);
}

.section-title span {
    font-size: 13px;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 2px;
    color: var(--text-muted);
}

/* ===== PLATFORM TABS ===== */
.platform-tabs {
    display: flex;
    gap: 8px;
    margin-bottom: 20px;
}

.platform-tab {
    flex: 1;
    padding: 14px 16px;
    background: var(--bg-card);
    border: 2px solid var(--border-color);
    border-radius: 16px;
    text-align: center;
    cursor: pointer;
    transition: all 0.3s cubic-bezier(0.4, 0, 0.2, 1);
    position: relative;
    overflow: hidden;
}

.platform-tab::before {
    content: '';
    position: absolute;
    top: 0; left: 0;
    width: 100%; height: 100%;
    background: var(--gradient-primary);
    opacity: 0;
    transition: opacity 0.3s;
}

.platform-tab.active {
    border-color: var(--accent-primary);
    box-shadow: 0 4px 20px var(--accent-glow);
}

.platform-tab.active::before { opacity: 0.1; }

.platform-tab .tab-icon {
    font-size: 28px;
    display: block;
    margin-bottom: 6px;
    position: relative;
    z-index: 1;
}

.platform-tab .tab-label {
    font-size: 13px;
    font-weight: 700;
    position: relative;
    z-index: 1;
    color: var(--text-secondary);
}

.platform-tab.active .tab-label { color: var(--text-primary); }

/* ===== PRODUCT CARDS ===== */
.products-grid {
    display: flex;
    flex-direction: column;
    gap: 12px;
}

.product-card {
    background: var(--bg-card);
    border: 1px solid var(--border-color);
    border-radius: 20px;
    padding: 20px;
    position: relative;
    overflow: hidden;
    cursor: pointer;
    transition: all 0.3s cubic-bezier(0.4, 0, 0.2, 1);
    transform: translateY(0);
}

.product-card:hover {
    transform: translateY(-2px);
    border-color: var(--accent-primary);
    box-shadow: 0 8px 30px var(--accent-glow);
}

.product-card.selected {
    border-color: var(--accent-primary);
    box-shadow: 0 4px 24px var(--accent-glow);
    background: var(--bg-card-hover);
}

.product-card .card-glow {
    position: absolute;
    top: -50%; right: -50%;
    width: 200px; height: 200px;
    background: var(--accent-primary);
    border-radius: 50%;
    filter: blur(80px);
    opacity: 0;
    transition: opacity 0.5s;
    pointer-events: none;
}

.product-card.selected .card-glow,
.product-card:hover .card-glow { opacity: 0.08; }

.product-card .popular-badge {
    position: absolute;
    top: 12px; right: 12px;
    background: var(--gradient-gold);
    color: #000;
    padding: 4px 10px;
    border-radius: 8px;
    font-size: 10px;
    font-weight: 800;
    text-transform: uppercase;
    letter-spacing: 1px;
}

.product-card .card-header {
    display: flex;
    align-items: center;
    gap: 14px;
    margin-bottom: 14px;
}

.card-header .period-icon {
    width: 48px; height: 48px;
    border-radius: 14px;
    display: flex;
    align-items: center;
    justify-content: center;
    font-size: 24px;
    flex-shrink: 0;
}

.card-header .period-icon.week { background: rgba(0, 229, 255, 0.15); }
.card-header .period-icon.month { background: rgba(108, 92, 231, 0.15); }
.card-header .period-icon.forever { background: rgba(255, 215, 0, 0.15); }

.card-header .card-info h3 {
    font-size: 16px;
    font-weight: 700;
    color: var(--text-primary);
}

.card-header .card-info .duration {
    font-size: 12px;
    color: var(--text-secondary);
    margin-top: 2px;
}

.card-prices {
    display: grid;
    grid-template-columns: repeat(2, 1fr);
    gap: 8px;
    margin-top: 12px;
}

.price-tag {
    display: flex;
    align-items: center;
    gap: 6px;
    background: rgba(255,255,255,0.03);
    padding: 8px 10px;
    border-radius: 10px;
    font-size: 13px;
    font-weight: 600;
}

.price-tag .price-icon { font-size: 14px; }
.price-tag.main-price {
    grid-column: 1 / -1;
    background: rgba(108, 92, 231, 0.1);
    border: 1px solid rgba(108, 92, 231, 0.2);
    font-size: 18px;
    font-weight: 800;
    justify-content: center;
    padding: 12px;
    color: var(--accent-secondary);
}

/* ===== PAYMENT SECTION ===== */
.payment-section {
    display: none;
    animation: slideUp 0.4s cubic-bezier(0.4, 0, 0.2, 1);
}

.payment-section.visible { display: block; }

@keyframes slideUp {
    from { opacity: 0; transform: translateY(20px); }
    to { opacity: 1; transform: translateY(0); }
}

.payment-methods {
    display: flex;
    flex-direction: column;
    gap: 8px;
}

.payment-btn {
    display: flex;
    align-items: center;
    gap: 14px;
    background: var(--bg-card);
    border: 1px solid var(--border-color);
    border-radius: 16px;
    padding: 16px 18px;
    cursor: pointer;
    transition: all 0.3s;
    color: var(--text-primary);
    width: 100%;
    font-family: inherit;
    font-size: 15px;
    font-weight: 600;
}

.payment-btn:hover,
.payment-btn:active {
    border-color: var(--accent-primary);
    background: var(--bg-card-hover);
    transform: scale(0.98);
}

.payment-btn .pay-icon {
    width: 44px; height: 44px;
    border-radius: 12px;
    display: flex;
    align-items: center;
    justify-content: center;
    font-size: 22px;
    flex-shrink: 0;
}

.payment-btn .pay-icon.card { background: rgba(108, 92, 231, 0.15); }
.payment-btn .pay-icon.stars { background: rgba(255, 193, 7, 0.15); }
.payment-btn .pay-icon.crypto { background: rgba(0, 229, 255, 0.15); }
.payment-btn .pay-icon.gold { background: rgba(255, 215, 0, 0.15); }
.payment-btn .pay-icon.nft { background: rgba(233, 30, 99, 0.15); }

.payment-btn .pay-info {
    flex: 1;
    text-align: left;
}

.payment-btn .pay-info .pay-name { font-weight: 600; }
.payment-btn .pay-info .pay-price {
    font-size: 12px;
    color: var(--text-secondary);
    margin-top: 2px;
}

.payment-btn .pay-arrow {
    color: var(--text-muted);
    font-size: 18px;
}

/* ===== SELECTED PRODUCT SUMMARY ===== */
.selected-summary {
    display: none;
    background: linear-gradient(135deg, rgba(108, 92, 231, 0.1) 0%, rgba(162, 155, 254, 0.05) 100%);
    border: 1px solid rgba(108, 92, 231, 0.3);
    border-radius: 16px;
    padding: 16px;
    margin-bottom: 16px;
    animation: slideUp 0.3s ease;
}

.selected-summary.visible { display: flex; align-items: center; gap: 12px; }

.selected-summary .sum-icon {
    font-size: 32px;
}

.selected-summary .sum-info h4 {
    font-size: 15px;
    font-weight: 700;
}

.selected-summary .sum-info p {
    font-size: 12px;
    color: var(--text-secondary);
    margin-top: 2px;
}

/* ===== FOOTER INFO ===== */
.footer-info {
    text-align: center;
    padding: 24px 0;
    color: var(--text-muted);
    font-size: 12px;
}

.footer-info a {
    color: var(--accent-secondary);
    text-decoration: none;
}

/* ===== SUPPORT FLOAT BTN ===== */
.support-float {
    position: fixed;
    bottom: 20px;
    right: 20px;
    width: 56px; height: 56px;
    background: var(--gradient-primary);
    border-radius: 50%;
    display: flex;
    align-items: center;
    justify-content: center;
    font-size: 24px;
    box-shadow: 0 4px 20px var(--accent-glow);
    z-index: 100;
    cursor: pointer;
    transition: transform 0.3s;
    text-decoration: none;
}

.support-float:hover { transform: scale(1.1); }

/* ===== TOAST ===== */
.toast {
    position: fixed;
    bottom: 90px;
    left: 50%;
    transform: translateX(-50%) translateY(20px);
    background: var(--bg-card);
    border: 1px solid var(--accent-primary);
    padding: 12px 24px;
    border-radius: 12px;
    font-size: 13px;
    font-weight: 600;
    opacity: 0;
    transition: all 0.3s;
    z-index: 200;
    pointer-events: none;
    white-space: nowrap;
}

.toast.show {
    opacity: 1;
    transform: translateX(-50%) translateY(0);
}

/* ===== SCROLLBAR ===== */
::-webkit-scrollbar { width: 4px; }
::-webkit-scrollbar-track { background: transparent; }
::-webkit-scrollbar-thumb { background: var(--accent-primary); border-radius: 4px; }

/* ===== LOADING ===== */
.loading-overlay {
    position: fixed;
    top: 0; left: 0;
    width: 100%; height: 100%;
    background: var(--bg-primary);
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    z-index: 9999;
    transition: opacity 0.5s;
}

.loading-overlay.hidden {
    opacity: 0;
    pointer-events: none;
}

.loading-spinner {
    width: 48px; height: 48px;
    border: 3px solid var(--border-color);
    border-top-color: var(--accent-primary);
    border-radius: 50%;
    animation: spin 0.8s linear infinite;
}

@keyframes spin { to { transform: rotate(360deg); } }

.loading-text {
    margin-top: 16px;
    font-size: 14px;
    color: var(--text-secondary);
    font-weight: 500;
}

/* Responsive */
@media (max-width: 360px) {
    .card-prices { grid-template-columns: 1fr; }
    .header h1 { font-size: 24px; }
}
</style>
</head>
<body>

<!-- Loading -->
<div class="loading-overlay" id="loadingOverlay">
    <div class="loading-spinner"></div>
    <div class="loading-text">Загрузка PMT Shop...</div>
</div>

<!-- Background -->
<div class="bg-animation">
    <div class="orb"></div>
    <div class="orb"></div>
    <div class="orb"></div>
    <div class="orb"></div>
</div>
<div class="bg-grid"></div>
<div class="particles" id="particles"></div>

<!-- Main App -->
<div class="app-container">

    <!-- Header -->
    <div class="header">
        <div class="logo-container">
            <div class="logo-glow"></div>
            <div class="logo-icon">🎮</div>
        </div>
        <h1>PMT PREMIUM</h1>
        <div class="subtitle">Standoff 2 Cheat Shop</div>
        <div class="badge">
            <span class="pulse-dot"></span>
            ONLINE • UNDETECTED
        </div>
    </div>

    <!-- Features -->
    <div class="features-bar">
        <div class="feature-chip"><span class="icon">🎯</span> AimBot</div>
        <div class="feature-chip"><span class="icon">👁️</span> WallHack</div>
        <div class="feature-chip"><span class="icon">📍</span> ESP</div>
        <div class="feature-chip"><span class="icon">🗺️</span> Radar</div>
        <div class="feature-chip"><span class="icon">🛡️</span> Anti-Ban</div>
        <div class="feature-chip"><span class="icon">⚡</span> No Root</div>
    </div>

    <!-- Platform Selection -->
    <div class="section-title">
        <div class="line"></div>
        <span>Платформа</span>
        <div class="line"></div>
    </div>

    <div class="platform-tabs">
        <div class="platform-tab active" data-platform="apk" onclick="selectPlatform('apk')">
            <span class="tab-icon">📱</span>
            <span class="tab-label">Android</span>
        </div>
        <div class="platform-tab" data-platform="ios" onclick="selectPlatform('ios')">
            <span class="tab-icon">🍎</span>
            <span class="tab-label">iOS</span>
        </div>
    </div>

    <!-- Products -->
    <div class="section-title">
        <div class="line"></div>
        <span>Тарифы</span>
        <div class="line"></div>
    </div>

    <div class="products-grid" id="productsGrid"></div>

    <!-- Selected Summary -->
    <div class="selected-summary" id="selectedSummary">
        <div class="sum-icon" id="sumIcon"></div>
        <div class="sum-info">
            <h4 id="sumTitle"></h4>
            <p id="sumDesc"></p>
        </div>
    </div>

    <!-- Payment Methods -->
    <div class="payment-section" id="paymentSection">
        <div class="section-title">
            <div class="line"></div>
            <span>Оплата</span>
            <div class="line"></div>
        </div>
        <div class="payment-methods" id="paymentMethods"></div>
    </div>

    <!-- Footer -->
    <div class="footer-info">
        <p>PMT Premium © 2024</p>
        <p style="margin-top:4px">Поддержка: <a href="https://t.me/''' + Config.SUPPORT_CHAT_USERNAME + '''">@''' + Config.SUPPORT_CHAT_USERNAME + '''</a></p>
    </div>
</div>

<!-- Support Button -->
<a class="support-float" href="https://t.me/''' + Config.SUPPORT_CHAT_USERNAME + '''" target="_blank">💬</a>

<!-- Toast -->
<div class="toast" id="toast"></div>

<script>
// ===== INIT =====
const tg = window.Telegram.WebApp;
tg.ready();
tg.expand();
tg.setHeaderColor('#0a0a1a');
tg.setBackgroundColor('#0a0a1a');

// Products data
const PRODUCTS = ''' + json.dumps(PRODUCTS, ensure_ascii=False) + ''';

let selectedPlatform = 'apk';
let selectedProductId = null;

// ===== PARTICLES =====
function createParticles() {
    const container = document.getElementById('particles');
    for (let i = 0; i < 30; i++) {
        const p = document.createElement('div');
        p.className = 'particle';
        p.style.left = Math.random() * 100 + '%';
        p.style.animationDelay = Math.random() * 8 + 's';
        p.style.animationDuration = (6 + Math.random() * 6) + 's';
        p.style.width = p.style.height = (2 + Math.random() * 3) + 'px';
        container.appendChild(p);
    }
}

// ===== PLATFORM =====
function selectPlatform(platform) {
    selectedPlatform = platform;
    selectedProductId = null;
    document.querySelectorAll('.platform-tab').forEach(tab => {
        tab.classList.toggle('active', tab.dataset.platform === platform);
    });
    renderProducts();
    hidePayment();
    haptic('light');
}

// ===== RENDER PRODUCTS =====
function renderProducts() {
    const grid = document.getElementById('productsGrid');
    grid.innerHTML = '';

    const platformProducts = Object.entries(PRODUCTS).filter(([id, p]) => p.platform_code === selectedPlatform);

    platformProducts.forEach(([id, product], index) => {
        const isPopular = id.includes('month');
        const periodClass = id.includes('week') ? 'week' : id.includes('month') ? 'month' : 'forever';
        const periodEmoji = id.includes('week') ? '⚡' : id.includes('month') ? '🔥' : '💎';

        const card = document.createElement('div');
        card.className = 'product-card';
        card.dataset.productId = id;
        card.style.animationDelay = (index * 0.1) + 's';
        card.onclick = () => selectProduct(id);

        card.innerHTML = `
            <div class="card-glow"></div>
            ${isPopular ? '<div class="popular-badge">ПОПУЛЯРНЫЙ</div>' : ''}
            <div class="card-header">
                <div class="period-icon ${periodClass}">${periodEmoji}</div>
                <div class="card-info">
                    <h3>${product.period_text}</h3>
                    <div class="duration">${product.duration}</div>
                </div>
            </div>
            <div class="card-prices">
                <div class="price-tag main-price">
                    <span class="price-icon">💳</span> ${product.price} ₽
                </div>
                <div class="price-tag">
                    <span class="price-icon">⭐</span> ${product.price_stars} Stars
                </div>
                <div class="price-tag">
                    <span class="price-icon">₿</span> ${product.price_crypto_usdt} USDT
                </div>
                <div class="price-tag">
                    <span class="price-icon">🪙</span> ${product.price_gold} GOLD
                </div>
                <div class="price-tag">
                    <span class="price-icon">🎨</span> ${product.price_nft} NFT
                </div>
            </div>
        `;
        grid.appendChild(card);
    });
}

// ===== SELECT PRODUCT =====
function selectProduct(productId) {
    selectedProductId = productId;
    const product = PRODUCTS[productId];

    // Highlight card
    document.querySelectorAll('.product-card').forEach(card => {
        card.classList.toggle('selected', card.dataset.productId === productId);
    });

    // Summary
    const summary = document.getElementById('selectedSummary');
    document.getElementById('sumIcon').textContent = product.emoji;
    document.getElementById('sumTitle').textContent = `${product.name} — ${product.period_text}`;
    document.getElementById('sumDesc').textContent = `${product.duration} • ${product.price} ₽`;
    summary.classList.add('visible');

    // Show payments
    showPayment(product);
    haptic('medium');

    // Scroll to payment
    setTimeout(() => {
        document.getElementById('paymentSection').scrollIntoView({ behavior: 'smooth', block: 'start' });
    }, 200);
}

// ===== PAYMENT =====
function showPayment(product) {
    const section = document.getElementById('paymentSection');
    const methods = document.getElementById('paymentMethods');
    methods.innerHTML = '';

    const payments = [
        { method: 'card', icon: '💳', iconClass: 'card', name: 'Банковская карта', price: `${product.price} ₽` },
        { method: 'stars', icon: '⭐', iconClass: 'stars', name: 'Telegram Stars', price: `${product.price_stars} Stars` },
        { method: 'crypto', icon: '₿', iconClass: 'crypto', name: 'Криптовалюта', price: `${product.price_crypto_usdt} USDT` },
        { method: 'gold', icon: '🪙', iconClass: 'gold', name: 'Standoff GOLD', price: `${product.price_gold} GOLD` },
        { method: 'nft', icon: '🎨', iconClass: 'nft', name: 'NFT Standoff', price: `${product.price_nft} NFT` }
    ];

    payments.forEach((pay, i) => {
        const btn = document.createElement('button');
        btn.className = 'payment-btn';
        btn.style.animationDelay = (i * 0.05) + 's';
        btn.onclick = () => processPayment(pay.method);
        btn.innerHTML = `
            <div class="pay-icon ${pay.iconClass}">${pay.icon}</div>
            <div class="pay-info">
                <div class="pay-name">${pay.name}</div>
                <div class="pay-price">${pay.price}</div>
            </div>
            <div class="pay-arrow">›</div>
        `;
        methods.appendChild(btn);
    });

    section.classList.add('visible');
}

function hidePayment() {
    document.getElementById('paymentSection').classList.remove('visible');
    document.getElementById('selectedSummary').classList.remove('visible');
}

// ===== PROCESS PAYMENT =====
function processPayment(method) {
    if (!selectedProductId) {
        showToast('Выберите тариф!');
        return;
    }

    haptic('heavy');
    showToast('⏳ Создаём заказ...');

    const data = {
        action: 'buy',
        product_id: selectedProductId,
        payment_method: method
    };

    try {
        tg.sendData(JSON.stringify(data));
    } catch(e) {
        showToast('❌ Ошибка отправки');
        console.error(e);
    }
}

// ===== UTILS =====
function showToast(text) {
    const toast = document.getElementById('toast');
    toast.textContent = text;
    toast.classList.add('show');
    setTimeout(() => toast.classList.remove('show'), 2500);
}

function haptic(type) {
    try {
        if (type === 'light') tg.HapticFeedback.impactOccurred('light');
        else if (type === 'medium') tg.HapticFeedback.impactOccurred('medium');
        else if (type === 'heavy') tg.HapticFeedback.impactOccurred('heavy');
    } catch(e) {}
}

// ===== INIT =====
window.addEventListener('load', () => {
    createParticles();
    renderProducts();

    // Hide loading
    setTimeout(() => {
        document.getElementById('loadingOverlay').classList.add('hidden');
    }, 800);
});
</script>
</body>
</html>'''

@flask_app.route('/')
def miniapp_index():
    return Response(MINIAPP_HTML, mimetype='text/html')

@flask_app.route('/api/products')
def api_products():
    return jsonify(PRODUCTS)

@flask_app.route('/api/health')
def api_health():
    return jsonify({"status": "ok", "time": time.time()})


# ========== ЗАПУСК ==========
def run_flask():
    port = int(os.environ.get("PORT", 8080))
    flask_app.run(host='0.0.0.0', port=port, debug=False)


async def run_bot():
    global bot_loop
    bot_loop = asyncio.get_event_loop()
    logger.info("=" * 50)
    logger.info("PMT PREMIUM CHEAT SHOP — Bot + MiniApp")
    logger.info("=" * 50)
    logger.info("ADMIN_IDS: %s", Config.ADMIN_IDS)
    logger.info("WEBAPP: %s", Config.WEBAPP_URL)
    try:
        me = await bot.get_me()
        logger.info("Bot: @%s", me.username)
        for key, product in PRODUCTS.items():
            logger.info("%s %s (%s) - %s RUB / %s Stars", product['emoji'], product['name'], product['duration'], product['price'], product['price_stars'])
        await dp.start_polling(bot)
    except Exception as e:
        logger.error("Fatal: %s", e)
        import traceback
        traceback.print_exc()
    finally:
        await bot.session.close()


def main():
    # Flask в отдельном потоке
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()
    logger.info("Flask MiniApp started on port %s", os.environ.get("PORT", 8080))

    # Bot в основном потоке
    asyncio.run(run_bot())


if __name__ == "__main__":
    main()
