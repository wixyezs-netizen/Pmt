# bot.py — PMT Premium Cheat Shop + MiniApp
# Один файл: бот + веб-сервер (aiohttp)
# Домен MiniApp: pmt.bothost.tech

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
from datetime import datetime
from urllib.parse import parse_qs, unquote, quote
from collections import OrderedDict
from typing import Optional, Union

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
    BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
    CRYPTOBOT_TOKEN = os.environ.get("CRYPTOBOT_TOKEN", "")
    YOOMONEY_ACCESS_TOKEN = os.environ.get("YOOMONEY_ACCESS_TOKEN", "")
    YOOMONEY_WALLET = os.environ.get("YOOMONEY_WALLET", "")
    SUPPORT_CHAT_USERNAME = os.environ.get("SUPPORT_CHAT_USERNAME", "PMThelp")
    DOWNLOAD_URL = os.environ.get("DOWNLOAD_URL", "https://go.linkify.ru/2GPF")
    WEBAPP_URL = os.environ.get("WEBAPP_URL", "https://pmt.bothost.tech")
    START_IMAGE_PATH = os.environ.get("START_IMAGE_PATH", "images/start_image.jpg")
    WEB_PORT = int(os.environ.get("PORT", os.environ.get("WEB_PORT", "8080")))

    ADMIN_IDS = set()
    ADMIN_ID = 0
    SUPPORT_CHAT_ID = 0
    BOT_USERNAME = ""

    MAX_PENDING_ORDERS = 1000
    ORDER_EXPIRY_SECONDS = 3600
    RATE_LIMIT_SECONDS = 2
    MAX_PAYMENT_CHECK_ATTEMPTS = 5
    PAYMENT_CHECK_INTERVAL = 5

    START_TEXT = (
        "PMT | STANDOFF 2 PREMIUM 💰\n\n"
        "🚀 Универсальное решение:\n"
        "📱 Android (APK, без Root)\n"
        "🍏 iOS\n\n"
        "🔥 Функционал:\n"
        "• Аимбот + WallHack + ESP\n"
        "• Анти-бан защита\n\n"
        "Лучшие цены | Быстрая поддержка 24/7\n\n"
        "Покупай чит, и разноси своих соперников ⚡️"
    )

    @classmethod
    def init(cls):
        if not cls.BOT_TOKEN:
            raise ValueError("BOT_TOKEN is required!")
        admin_ids_str = os.environ.get("ADMIN_ID", "")
        admin_ids_list = [int(x.strip()) for x in admin_ids_str.split(",") if x.strip().isdigit()]
        if not admin_ids_list:
            raise ValueError("ADMIN_ID is required!")
        cls.ADMIN_ID = admin_ids_list[0]
        cls.SUPPORT_CHAT_ID = admin_ids_list[1] if len(admin_ids_list) >= 2 else cls.ADMIN_ID
        cls.ADMIN_IDS = set(admin_ids_list)
        if not cls.CRYPTOBOT_TOKEN:
            logger.warning("CRYPTOBOT_TOKEN not set")
        if not cls.YOOMONEY_ACCESS_TOKEN:
            logger.warning("YOOMONEY_ACCESS_TOKEN not set")


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
        expired = [oid for oid, d in self._pending.items() if now - d.get("created_at", 0) > self._expiry_seconds]
        for oid in expired:
            del self._pending[oid]


class RateLimiter:
    def __init__(self, interval=2.0):
        self._last = {}
        self._interval = interval

    def check(self, user_id):
        now = time.time()
        if now - self._last.get(user_id, 0) < self._interval:
            return False
        self._last[user_id] = now
        return True


# ========== ПРОДУКТЫ ==========
PRODUCTS = {
    "apk_week": {
        "name": "📱 PMT Android", "period_text": "НЕДЕЛЮ", "price": 205,
        "price_stars": 250, "price_gold": 650, "price_nft": 500, "price_crypto_usdt": 3,
        "platform": "Android", "period": "НЕДЕЛЮ", "platform_code": "apk",
        "emoji": "📱", "duration": "7 дней"
    },
    "apk_month": {
        "name": "📱 PMT Android", "period_text": "МЕСЯЦ", "price": 450,
        "price_stars": 450, "price_gold": 1200, "price_nft": 1000, "price_crypto_usdt": 6,
        "platform": "Android", "period": "МЕСЯЦ", "platform_code": "apk",
        "emoji": "📱", "duration": "30 дней"
    },
    "apk_forever": {
        "name": "📱 PMT Android", "period_text": "НАВСЕГДА", "price": 890,
        "price_stars": 900, "price_gold": 2200, "price_nft": 1800, "price_crypto_usdt": 12,
        "platform": "Android", "period": "НАВСЕГДА", "platform_code": "apk",
        "emoji": "📱", "duration": "Навсегда"
    },
    "ios_week": {
        "name": "🍏 PMT iOS", "period_text": "НЕДЕЛЮ", "price": 359,
        "price_stars": 350, "price_gold": 700, "price_nft": 550, "price_crypto_usdt": 5,
        "platform": "iOS", "period": "НЕДЕЛЮ", "platform_code": "ios",
        "emoji": "🍏", "duration": "7 дней"
    },
    "ios_month": {
        "name": "🍏 PMT iOS", "period_text": "МЕСЯЦ", "price": 750,
        "price_stars": 750, "price_gold": 1400, "price_nft": 1200, "price_crypto_usdt": 10,
        "platform": "iOS", "period": "МЕСЯЦ", "platform_code": "ios",
        "emoji": "🍏", "duration": "30 дней"
    },
    "ios_forever": {
        "name": "🍏 PMT iOS", "period_text": "НАВСЕГДА", "price": 1400,
        "price_stars": 1400, "price_gold": 2500, "price_nft": 2200, "price_crypto_usdt": 18,
        "platform": "iOS", "period": "НАВСЕГДА", "platform_code": "ios",
        "emoji": "🍏", "duration": "Навсегда"
    }
}


# ========== ВСПОМОГАТЕЛЬНЫЕ ==========
def generate_order_id():
    raw = "{}_{}_{}".format(time.time(), random.randint(100000, 999999), os.urandom(4).hex())
    return hashlib.sha256(raw.encode()).hexdigest()[:12]


def generate_license_key(order_id, user_id):
    raw = "{}_{}_{}".format(order_id, user_id, os.urandom(8).hex())
    h = hashlib.sha256(raw.encode()).hexdigest()[:16].upper()
    return "PMT-{}-{}-{}-{}".format(h[:4], h[4:8], h[8:12], h[12:16])


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
    comment = "Заказ {}: {}".format(order_id, product_name)
    return (
        "https://yoomoney.ru/quickpay/confirm.xml"
        "?receiver={}&quickpay-form=shop&targets={}&sum={}&label={}&paymentType=AC"
    ).format(Config.YOOMONEY_WALLET, quote(comment, safe=''), amount, order_id)


def validate_init_data(init_data_str):
    try:
        parsed = dict(x.split('=', 1) for x in init_data_str.split('&') if '=' in x)
        hash_value = parsed.pop('hash', None)
        if not hash_value:
            return None
        items = sorted(parsed.items())
        data_check_string = '\n'.join('{}={}'.format(k, v) for k, v in items)
        secret = hmac.new(b'WebAppData', Config.BOT_TOKEN.encode(), hashlib.sha256).digest()
        computed = hmac.new(secret, data_check_string.encode(), hashlib.sha256).hexdigest()
        if computed != hash_value:
            return None
        user_str = parsed.get('user')
        if user_str:
            return json.loads(unquote(user_str))
        return {}
    except Exception as e:
        logger.error("initData validation error: %s", e)
        return None


# ========== ПЛАТЁЖНЫЕ СЕРВИСЫ ==========
class YooMoneyService:
    @staticmethod
    async def get_balance():
        if not Config.YOOMONEY_ACCESS_TOKEN:
            return None
        headers = {"Authorization": "Bearer {}".format(Config.YOOMONEY_ACCESS_TOKEN)}
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as s:
                async with s.get("https://yoomoney.ru/api/account-info", headers=headers) as r:
                    if r.status == 200:
                        return float((await r.json()).get('balance', 0))
        except Exception as e:
            logger.error("YooMoney balance: %s", e)
        return None

    @staticmethod
    async def check_payment(order_id, expected_amount, order_time):
        if not Config.YOOMONEY_ACCESS_TOKEN:
            return False
        headers = {"Authorization": "Bearer {}".format(Config.YOOMONEY_ACCESS_TOKEN)}
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as s:
                async with s.post("https://yoomoney.ru/api/operation-history", headers=headers,
                                  data={"type": "deposition", "records": 100}) as r:
                    if r.status != 200:
                        return False
                    ops = (await r.json()).get("operations", [])
                    for op in ops:
                        if op.get("label") == order_id and op.get("status") == "success" and abs(
                                float(op.get("amount", 0)) - expected_amount) <= 5:
                            return True
                    for op in ops:
                        if op.get("status") != "success":
                            continue
                        if abs(float(op.get("amount", 0)) - expected_amount) > 2:
                            continue
                        try:
                            ot = datetime.fromisoformat(op.get("datetime", "").replace("Z", "+00:00")).timestamp()
                            if abs(ot - order_time) <= 1800:
                                return True
                        except (ValueError, TypeError):
                            pass
        except Exception as e:
            logger.error("YooMoney check: %s", e)
        return False


class CryptoBotService:
    BASE = "https://pay.crypt.bot/api"

    @staticmethod
    async def create_invoice(amount_usdt, order_id, description):
        if not Config.CRYPTOBOT_TOKEN:
            return None
        headers = {"Crypto-Pay-API-Token": Config.CRYPTOBOT_TOKEN, "Content-Type": "application/json"}
        data = {
            "asset": "USDT", "amount": str(amount_usdt),
            "description": description[:256], "payload": order_id,
            "paid_btn_name": "callback",
            "paid_btn_url": "https://t.me/{}?start=paid_{}".format(Config.BOT_USERNAME or "pmt_bot", order_id)
        }
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as s:
                async with s.post(CryptoBotService.BASE + "/createInvoice", headers=headers, json=data) as r:
                    if r.status == 200:
                        res = await r.json()
                        if res.get("ok"):
                            inv = res["result"]
                            return {"invoice_id": inv.get("invoice_id"), "pay_url": inv.get("pay_url")}
        except Exception as e:
            logger.error("CryptoBot create: %s", e)
        return None

    @staticmethod
    async def check_invoice(invoice_id):
        if not Config.CRYPTOBOT_TOKEN:
            return False
        headers = {"Crypto-Pay-API-Token": Config.CRYPTOBOT_TOKEN, "Content-Type": "application/json"}
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as s:
                async with s.post(CryptoBotService.BASE + "/getInvoices", headers=headers,
                                  json={"invoice_ids": [invoice_id]}) as r:
                    if r.status == 200:
                        res = await r.json()
                        items = res.get("result", {}).get("items", [])
                        if items and items[0].get("status") == "paid":
                            return True
        except Exception as e:
            logger.error("CryptoBot check: %s", e)
        return False


# ============================================================
# =================== MINIAPP HTML ===========================
# ============================================================

MINIAPP_HTML = r"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no">
<title>PMT Premium</title>
<script src="https://telegram.org/js/telegram-web-app.js"></script>
<style>
*{margin:0;padding:0;box-sizing:border-box;-webkit-tap-highlight-color:transparent}
:root{
  --bg:#0b0b1e;--bg2:#10102a;--card:rgba(22,22,55,0.75);
  --primary:#a855f7;--primary-dim:rgba(168,85,247,0.25);
  --secondary:#22d3ee;--secondary-dim:rgba(34,211,238,0.2);
  --accent:#f43f5e;--success:#34d399;--gold:#fbbf24;
  --text:#f1f5f9;--text2:#94a3b8;--text3:#64748b;
  --border:rgba(168,85,247,0.15);--radius:16px;
  --shadow:0 8px 32px rgba(0,0,0,0.4);
}
html,body{height:100%;overflow:hidden;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;
  background:var(--bg);color:var(--text)}
canvas#particles{position:fixed;top:0;left:0;width:100%;height:100%;z-index:0;pointer-events:none}
#app{position:relative;z-index:1;height:100%;display:flex;flex-direction:column}

/* ===== SCREENS ===== */
.screen{display:none;flex-direction:column;height:100%;overflow-y:auto;
  padding:16px 16px 32px;animation:fadeSlide .35s ease}
.screen.active{display:flex}
@keyframes fadeSlide{from{opacity:0;transform:translateY(18px)}to{opacity:1;transform:translateY(0)}}

/* ===== HEADER ===== */
.header{text-align:center;padding:18px 0 10px}
.logo{font-size:36px;font-weight:900;background:linear-gradient(135deg,var(--primary),var(--secondary));
  -webkit-background-clip:text;-webkit-text-fill-color:transparent;letter-spacing:2px;
  text-shadow:none;filter:drop-shadow(0 0 20px var(--primary-dim))}
.logo-sub{font-size:13px;color:var(--text2);margin-top:2px;letter-spacing:3px;text-transform:uppercase}
.badge{display:inline-block;background:linear-gradient(135deg,var(--success),#059669);
  color:#fff;font-size:10px;font-weight:700;padding:3px 10px;border-radius:20px;margin-top:8px}

/* ===== HERO BLOCK ===== */
.hero{background:var(--card);backdrop-filter:blur(20px);-webkit-backdrop-filter:blur(20px);
  border:1px solid var(--border);border-radius:var(--radius);padding:20px;margin:14px 0;
  box-shadow:var(--shadow)}
.hero-title{font-size:15px;font-weight:700;margin-bottom:10px;color:var(--text)}
.hero-features{display:flex;flex-wrap:wrap;gap:6px}
.feat{background:var(--primary-dim);border:1px solid rgba(168,85,247,0.2);
  padding:5px 12px;border-radius:20px;font-size:12px;color:var(--text);white-space:nowrap}

/* ===== CARDS ===== */
.card{background:var(--card);backdrop-filter:blur(16px);-webkit-backdrop-filter:blur(16px);
  border:1px solid var(--border);border-radius:var(--radius);padding:18px;
  margin-bottom:12px;box-shadow:var(--shadow);cursor:pointer;
  transition:transform .2s,border-color .3s,box-shadow .3s;position:relative;overflow:hidden}
.card::before{content:'';position:absolute;top:0;left:0;right:0;height:2px;
  background:linear-gradient(90deg,transparent,var(--primary),var(--secondary),transparent);
  opacity:0;transition:opacity .3s}
.card:hover::before,.card:active::before{opacity:1}
.card:active{transform:scale(0.97)}
.card:hover{border-color:rgba(168,85,247,0.4);box-shadow:0 8px 40px rgba(168,85,247,0.15)}
.card-icon{font-size:32px;margin-bottom:8px}
.card-title{font-size:17px;font-weight:700;margin-bottom:4px}
.card-desc{font-size:13px;color:var(--text2)}
.card-badge{position:absolute;top:14px;right:14px;font-size:11px;font-weight:700;
  padding:3px 10px;border-radius:20px}
.badge-popular{background:linear-gradient(135deg,var(--accent),#e11d48);color:#fff}
.badge-best{background:linear-gradient(135deg,var(--gold),#f59e0b);color:#1a1a1a}

/* ===== PLAN CARDS ===== */
.plan{background:var(--card);backdrop-filter:blur(16px);border:1px solid var(--border);
  border-radius:var(--radius);padding:20px;margin-bottom:12px;
  box-shadow:var(--shadow);transition:all .25s;cursor:pointer;position:relative;overflow:hidden}
.plan:active{transform:scale(0.97)}
.plan.featured{border-color:rgba(168,85,247,0.5);
  box-shadow:0 0 30px rgba(168,85,247,0.15),var(--shadow)}
.plan-header{display:flex;align-items:center;justify-content:space-between;margin-bottom:12px}
.plan-name{font-size:18px;font-weight:800}
.plan-period{font-size:12px;color:var(--text2)}
.plan-prices{display:grid;grid-template-columns:1fr 1fr;gap:6px}
.plan-price{background:rgba(255,255,255,0.04);border-radius:10px;padding:8px 10px;text-align:center}
.plan-price .val{font-size:16px;font-weight:700;color:var(--text)}
.plan-price .label{font-size:10px;color:var(--text3);margin-top:1px}
.plan-price.main{background:var(--primary-dim);grid-column:span 2}
.plan-price.main .val{font-size:22px;color:var(--primary)}

/* ===== BUTTONS ===== */
.btn{display:flex;align-items:center;justify-content:center;gap:8px;width:100%;
  padding:15px 20px;border:none;border-radius:var(--radius);font-size:15px;font-weight:700;
  cursor:pointer;transition:all .2s;text-decoration:none;color:#fff;position:relative;overflow:hidden}
.btn:active{transform:scale(0.96)}
.btn-primary{background:linear-gradient(135deg,#8b5cf6,#6d28d9);
  box-shadow:0 4px 20px rgba(139,92,246,0.4)}
.btn-primary:hover{box-shadow:0 6px 30px rgba(139,92,246,0.6)}
.btn-secondary{background:var(--card);border:1px solid var(--border);color:var(--text)}
.btn-accent{background:linear-gradient(135deg,var(--accent),#be123c);
  box-shadow:0 4px 20px rgba(244,63,94,0.3)}
.btn-success{background:linear-gradient(135deg,var(--success),#059669);
  box-shadow:0 4px 20px rgba(52,211,153,0.3)}
.btn-outline{background:transparent;border:1px solid var(--border);color:var(--text)}

/* ===== PAYMENT METHODS ===== */
.pay-card{display:flex;align-items:center;gap:14px;background:var(--card);backdrop-filter:blur(16px);
  border:1px solid var(--border);border-radius:var(--radius);padding:16px;
  margin-bottom:10px;cursor:pointer;transition:all .2s}
.pay-card:active{transform:scale(0.97);border-color:var(--primary)}
.pay-icon{font-size:28px;min-width:36px;text-align:center}
.pay-info{flex:1}
.pay-name{font-size:15px;font-weight:600}
.pay-price{font-size:13px;color:var(--text2);margin-top:2px}
.pay-arrow{color:var(--text3);font-size:18px}

/* ===== CHECKOUT ===== */
.checkout-info{background:var(--card);backdrop-filter:blur(16px);border:1px solid var(--border);
  border-radius:var(--radius);padding:20px;margin:12px 0;text-align:center}
.checkout-amount{font-size:36px;font-weight:900;
  background:linear-gradient(135deg,var(--primary),var(--secondary));
  -webkit-background-clip:text;-webkit-text-fill-color:transparent;margin:10px 0}
.checkout-id{font-size:12px;color:var(--text3);font-family:monospace}
.steps{margin:16px 0}
.step{display:flex;align-items:center;gap:10px;padding:8px 0;font-size:13px;color:var(--text2)}
.step-num{width:24px;height:24px;border-radius:50%;background:var(--primary-dim);
  display:flex;align-items:center;justify-content:center;font-size:11px;font-weight:700;color:var(--primary)}

/* ===== SUCCESS ===== */
.success-icon{font-size:64px;text-align:center;margin:20px 0;
  animation:successPulse 2s ease infinite}
@keyframes successPulse{0%,100%{transform:scale(1)}50%{transform:scale(1.1)}}
.license-box{background:linear-gradient(135deg,rgba(168,85,247,0.15),rgba(34,211,238,0.1));
  border:2px solid var(--primary);border-radius:var(--radius);padding:18px;
  text-align:center;margin:12px 0;position:relative}
.license-key{font-size:18px;font-weight:800;font-family:'Courier New',monospace;letter-spacing:1px;
  color:var(--primary);word-break:break-all}
.license-label{font-size:12px;color:var(--text2);margin-bottom:6px}
.copy-hint{font-size:11px;color:var(--text3);margin-top:6px}

/* ===== ABOUT ===== */
.about-section{margin-bottom:16px}
.about-title{font-size:15px;font-weight:700;margin-bottom:8px;color:var(--primary)}
.about-list{list-style:none;padding:0}
.about-list li{padding:6px 0;font-size:13px;color:var(--text2);display:flex;align-items:center;gap:8px}

/* ===== LOADING ===== */
.loading-overlay{position:fixed;top:0;left:0;right:0;bottom:0;background:rgba(11,11,30,0.9);
  z-index:100;display:none;align-items:center;justify-content:center;flex-direction:column;gap:16px}
.loading-overlay.show{display:flex}
.spinner{width:44px;height:44px;border:3px solid var(--border);border-top-color:var(--primary);
  border-radius:50%;animation:spin .8s linear infinite}
@keyframes spin{to{transform:rotate(360deg)}}
.loading-text{font-size:14px;color:var(--text2)}

/* ===== CONFETTI ===== */
canvas#confetti{position:fixed;top:0;left:0;width:100%;height:100%;z-index:99;pointer-events:none}

/* ===== TOAST ===== */
.toast{position:fixed;top:20px;left:50%;transform:translateX(-50%) translateY(-100px);
  background:var(--card);border:1px solid var(--border);backdrop-filter:blur(20px);
  border-radius:12px;padding:12px 20px;font-size:13px;z-index:200;
  transition:transform .4s cubic-bezier(.4,0,.2,1);white-space:nowrap;max-width:90%}
.toast.show{transform:translateX(-50%) translateY(0)}
.toast.error{border-color:rgba(244,63,94,0.4)}
.toast.success{border-color:rgba(52,211,153,0.4)}

/* ===== SECTION TITLE ===== */
.section-title{font-size:20px;font-weight:800;margin:8px 0 16px;text-align:center}
.section-sub{font-size:13px;color:var(--text2);text-align:center;margin-bottom:16px}

/* ===== GAP ===== */
.gap{height:12px}
.gap-sm{height:8px}
.flex-grow{flex:1}
.text-center{text-align:center}
.mb-8{margin-bottom:8px}
.mb-12{margin-bottom:12px}
.mb-16{margin-bottom:16px}

/* ===== SCROLLBAR ===== */
::-webkit-scrollbar{width:4px}
::-webkit-scrollbar-track{background:transparent}
::-webkit-scrollbar-thumb{background:var(--primary-dim);border-radius:4px}
</style>
</head>
<body>

<canvas id="particles"></canvas>
<canvas id="confetti"></canvas>

<div class="loading-overlay" id="loadingOverlay">
  <div class="spinner"></div>
  <div class="loading-text" id="loadingText">Проверяем оплату...</div>
</div>

<div class="toast" id="toast"></div>

<div id="app">

  <!-- ===== HOME ===== -->
  <div class="screen active" id="screen-home">
    <div class="header">
      <div class="logo">⚡ PMT</div>
      <div class="logo-sub">Standoff 2 Premium</div>
      <div class="badge">🟢 ONLINE</div>
    </div>
    <div class="hero">
      <div class="hero-title">🔥 Функционал</div>
      <div class="hero-features">
        <span class="feat">🎯 AimBot</span>
        <span class="feat">👁️ WallHack</span>
        <span class="feat">📍 ESP</span>
        <span class="feat">🗺️ Радар</span>
        <span class="feat">🛡️ Анти-бан</span>
        <span class="feat">⚙️ Настройки</span>
      </div>
    </div>
    <div class="gap"></div>
    <button class="btn btn-primary mb-12" onclick="navigate('platform')">🛒 Купить чит</button>
    <button class="btn btn-secondary mb-12" onclick="navigate('about')">ℹ️ О программе</button>
    <a class="btn btn-outline" href="https://t.me/%%SUPPORT%%" target="_blank">💬 Поддержка</a>
    <div class="flex-grow"></div>
    <div class="text-center" style="font-size:11px;color:var(--text3);padding-top:12px">
      📱 Android • 🍏 iOS<br>Лучшие цены • Поддержка 24/7
    </div>
  </div>

  <!-- ===== PLATFORM ===== -->
  <div class="screen" id="screen-platform">
    <div class="section-title">💰 Выберите платформу</div>
    <div class="card" onclick="selectPlatform('apk')">
      <div class="card-icon">📱</div>
      <div class="card-title">Android</div>
      <div class="card-desc">APK файл • Без Root • Быстрая установка</div>
    </div>
    <div class="card" onclick="selectPlatform('ios')">
      <div class="card-icon">🍏</div>
      <div class="card-title">iOS</div>
      <div class="card-desc">IPA файл • Все устройства</div>
    </div>
  </div>

  <!-- ===== PLANS ===== -->
  <div class="screen" id="screen-plans">
    <div class="section-title">💎 Выберите тариф</div>
    <div id="plansContainer"></div>
  </div>

  <!-- ===== PAYMENT ===== -->
  <div class="screen" id="screen-payment">
    <div class="section-title">💳 Способ оплаты</div>
    <div class="section-sub" id="paymentProductName"></div>
    <div id="paymentMethods"></div>
  </div>

  <!-- ===== CHECKOUT ===== -->
  <div class="screen" id="screen-checkout">
    <div class="section-title" id="checkoutTitle">💳 Оплата</div>
    <div class="checkout-info">
      <div id="checkoutProduct" style="font-size:15px;font-weight:600"></div>
      <div class="checkout-amount" id="checkoutAmount"></div>
      <div class="checkout-id" id="checkoutOrderId"></div>
    </div>
    <div class="steps">
      <div class="step"><div class="step-num">1</div>Нажмите «Оплатить»</div>
      <div class="step"><div class="step-num">2</div>Завершите оплату</div>
      <div class="step"><div class="step-num">3</div>Нажмите «Проверить»</div>
    </div>
    <div class="gap"></div>
    <a class="btn btn-primary mb-12" id="checkoutPayBtn" href="#" target="_blank">💳 Оплатить</a>
    <button class="btn btn-success mb-12" id="checkoutCheckBtn" onclick="checkPayment()">✅ Проверить оплату</button>
    <button class="btn btn-outline" onclick="navigate('home')">❌ Отмена</button>
  </div>

  <!-- ===== MANUAL ===== -->
  <div class="screen" id="screen-manual">
    <div class="section-title" id="manualTitle">💰 Оплата</div>
    <div class="checkout-info">
      <div id="manualProduct" style="font-size:15px;font-weight:600"></div>
      <div class="checkout-amount" id="manualAmount"></div>
    </div>
    <div class="hero" style="margin:12px 0">
      <div class="hero-title">📝 Сообщение для оплаты:</div>
      <div id="manualMessage" style="font-size:12px;color:var(--text2);margin-top:6px;word-break:break-all;cursor:pointer"
           onclick="copyManualMsg()"></div>
    </div>
    <div class="steps">
      <div class="step"><div class="step-num">1</div>Напишите в поддержку</div>
      <div class="step"><div class="step-num">2</div>Ожидайте обработки (до 30 мин)</div>
    </div>
    <div class="gap"></div>
    <a class="btn btn-primary mb-12" id="manualSupportBtn" href="#" target="_blank">💬 Написать в поддержку</a>
    <button class="btn btn-outline" onclick="navigate('home')">❌ Отмена</button>
  </div>

  <!-- ===== SUCCESS ===== -->
  <div class="screen" id="screen-success">
    <div class="success-icon">🎉</div>
    <div class="section-title">Оплата подтверждена!</div>
    <div class="section-sub">Добро пожаловать в PMT!</div>
    <div class="license-box" onclick="copyLicense()">
      <div class="license-label">🔑 Ваш лицензионный ключ:</div>
      <div class="license-key" id="licenseKey"></div>
      <div class="copy-hint">Нажмите, чтобы скопировать</div>
    </div>
    <div class="steps">
      <div class="step"><div class="step-num">1</div>Скачайте файл</div>
      <div class="step"><div class="step-num">2</div>Установите приложение</div>
      <div class="step"><div class="step-num">3</div>Введите ключ</div>
      <div class="step"><div class="step-num">4</div>Наслаждайтесь игрой! 🎮</div>
    </div>
    <div class="gap"></div>
    <a class="btn btn-primary mb-12" href="%%DOWNLOAD%%" target="_blank">📥 Скачать PMT</a>
    <a class="btn btn-secondary mb-12" href="https://t.me/%%SUPPORT%%" target="_blank">💬 Поддержка</a>
    <button class="btn btn-outline" onclick="navigate('home')">🔄 Новая покупка</button>
  </div>

  <!-- ===== ABOUT ===== -->
  <div class="screen" id="screen-about">
    <div class="section-title">📋 О программе</div>
    <div class="hero">
      <div class="about-section">
        <div class="about-title">🎮 PMT — Standoff 2 Premium</div>
        <div style="font-size:13px;color:var(--text2)">Статус: 🟢 Активно обновляется</div>
      </div>
      <div class="about-section">
        <div class="about-title">🛠️ Функционал</div>
        <ul class="about-list">
          <li>🎯 Умный AimBot с настройкой FOV</li>
          <li>👁️ WallHack — видь сквозь стены</li>
          <li>📍 ESP — подсветка игроков</li>
          <li>🗺️ Мини-радар на экране</li>
          <li>⚙️ Гибкие настройки всего</li>
        </ul>
      </div>
      <div class="about-section">
        <div class="about-title">🛡️ Безопасность</div>
        <ul class="about-list">
          <li>🔒 Обход античитов</li>
          <li>🔄 Регулярные обновления</li>
          <li>📱 Android / iOS</li>
        </ul>
      </div>
    </div>
    <div class="gap"></div>
    <a class="btn btn-secondary" href="https://t.me/%%SUPPORT%%" target="_blank">💬 Поддержка: @%%SUPPORT%%</a>
  </div>

</div>

<script>
const tg = window.Telegram.WebApp;
tg.ready();
tg.expand();
try{tg.setHeaderColor('#0b0b1e');tg.setBackgroundColor('#0b0b1e')}catch(e){}

const BOT = '%%BOT%%';
const SUPPORT = '%%SUPPORT%%';
const PRODUCTS = %%PRODUCTS_JSON%%;

let state = {screen:'home',platform:null,productId:null,product:null,orderId:null,
  paymentMethod:null,paymentUrl:null,invoiceId:null,licenseKey:null};
let screenStack = ['home'];

// ===== NAVIGATION =====
function navigate(screen, noStack){
  document.querySelectorAll('.screen').forEach(s=>s.classList.remove('active'));
  const el = document.getElementById('screen-'+screen);
  if(el){el.classList.add('active');el.scrollTop=0}
  state.screen = screen;
  if(!noStack && screenStack[screenStack.length-1]!==screen) screenStack.push(screen);
  if(screen==='home'){screenStack=['home'];tg.BackButton.hide()}
  else tg.BackButton.show();
  try{tg.HapticFeedback.impactOccurred('light')}catch(e){}
}

tg.BackButton.onClick(()=>{
  if(screenStack.length>1){screenStack.pop();navigate(screenStack[screenStack.length-1],true)}
});

// ===== PLATFORM =====
function selectPlatform(p){
  state.platform=p;
  renderPlans(p);
  navigate('plans');
}

// ===== PLANS =====
function renderPlans(platform){
  const c=document.getElementById('plansContainer');
  c.innerHTML='';
  const plans = Object.entries(PRODUCTS).filter(([k,v])=>v.platform_code===platform);
  const badges={0:'',1:'badge-popular',2:'badge-best'};
  const badgeText={0:'',1:'🔥 Популярный',2:'💎 Лучшая цена'};
  plans.forEach(([pid,p],i)=>{
    const featured = i===1?' featured':'';
    const badge = i>0?`<div class="card-badge ${badges[i]}">${badgeText[i]}</div>`:'';
    const icons = ['⚡','🔥','💎'];
    c.innerHTML+=`
    <div class="plan${featured}" onclick="selectPlan('${pid}')">
      ${badge}
      <div class="plan-header">
        <div class="plan-name">${icons[i]} ${p.period_text}</div>
        <div class="plan-period">${p.duration}</div>
      </div>
      <div class="plan-prices">
        <div class="plan-price main"><div class="val">${p.price} ₽</div><div class="label">Картой</div></div>
        <div class="plan-price"><div class="val">${p.price_stars} ⭐</div><div class="label">Stars</div></div>
        <div class="plan-price"><div class="val">${p.price_crypto_usdt} USDT</div><div class="label">Крипта</div></div>
      </div>
    </div>`;
  });
}

// ===== SELECT PLAN =====
function selectPlan(pid){
  state.productId=pid;
  state.product=PRODUCTS[pid];
  const p=state.product;
  document.getElementById('paymentProductName').textContent=p.emoji+' '+p.name+' — '+p.duration;
  renderPaymentMethods(p);
  navigate('payment');
}

// ===== PAYMENT METHODS =====
function renderPaymentMethods(p){
  const c=document.getElementById('paymentMethods');
  const methods=[
    {id:'card',icon:'💳',name:'Картой',price:p.price+' ₽'},
    {id:'stars',icon:'⭐',name:'Telegram Stars',price:p.price_stars+' ⭐'},
    {id:'crypto',icon:'₿',name:'Криптовалюта',price:p.price_crypto_usdt+' USDT'},
    {id:'gold',icon:'🪙',name:'GOLD',price:p.price_gold+' 🪙'},
    {id:'nft',icon:'🎨',name:'NFT',price:p.price_nft+' 🖼️'}
  ];
  c.innerHTML='';
  methods.forEach(m=>{
    c.innerHTML+=`
    <div class="pay-card" onclick="selectPayment('${m.id}')">
      <div class="pay-icon">${m.icon}</div>
      <div class="pay-info"><div class="pay-name">${m.name}</div><div class="pay-price">${m.price}</div></div>
      <div class="pay-arrow">›</div>
    </div>`;
  });
}

// ===== SELECT PAYMENT =====
async function selectPayment(method){
  state.paymentMethod=method;
  const p=state.product;

  if(method==='stars'){
    const link='https://t.me/'+BOT+'?start=buy_stars_'+state.productId;
    try{tg.openTelegramLink(link)}catch(e){window.open(link,'_blank')}
    return;
  }
  if(method==='gold'||method==='nft'){
    showManualPayment(method);return;
  }

  showLoading('Создаём заказ...');
  try{
    const res=await apiCall('/api/create-order',{productId:state.productId,paymentMethod:method});
    hideLoading();
    if(!res.ok){showToast(res.error||'Ошибка','error');return}
    state.orderId=res.orderId;
    state.paymentUrl=res.paymentUrl||res.invoiceUrl;
    state.invoiceId=res.invoiceId||null;

    const titles={card:'💳 Оплата картой',crypto:'₿ Криптооплата'};
    document.getElementById('checkoutTitle').textContent=titles[method]||'Оплата';
    document.getElementById('checkoutProduct').textContent=p.emoji+' '+p.name+' — '+p.duration;
    const amounts={card:p.price+' ₽',crypto:p.price_crypto_usdt+' USDT'};
    document.getElementById('checkoutAmount').textContent=amounts[method];
    document.getElementById('checkoutOrderId').textContent='ID: '+res.orderId;
    document.getElementById('checkoutPayBtn').href=state.paymentUrl;
    document.getElementById('checkoutPayBtn').textContent=method==='crypto'?'₿ Оплатить криптой':'💳 Оплатить';
    navigate('checkout');
  }catch(e){hideLoading();showToast('Ошибка сети','error')}
}

// ===== MANUAL PAYMENT =====
function showManualPayment(method){
  const p=state.product;
  const cfg={gold:{icon:'🪙',name:'GOLD',price:p.price_gold},nft:{icon:'🎨',name:'NFT',price:p.price_nft}};
  const c=cfg[method];
  const msg='Привет! Хочу купить чит PMT. Подписка на '+p.period_text+' ('+p.platform+'). Готов за '+c.price+' '+c.name;
  const url='https://t.me/'+SUPPORT+'?text='+encodeURIComponent(msg);

  document.getElementById('manualTitle').textContent=c.icon+' Оплата '+c.name;
  document.getElementById('manualProduct').textContent=p.emoji+' '+p.name+' — '+p.duration;
  document.getElementById('manualAmount').textContent=c.price+' '+c.name;
  document.getElementById('manualMessage').textContent=msg;
  document.getElementById('manualSupportBtn').href=url;
  state._manualMsg=msg;
  navigate('manual');
}

function copyManualMsg(){
  try{navigator.clipboard.writeText(state._manualMsg);showToast('📋 Скопировано','success')}
  catch(e){showToast('Не удалось скопировать','error')}
}

// ===== CHECK PAYMENT =====
async function checkPayment(){
  if(!state.orderId)return;
  showLoading('Проверяем оплату...');
  try{
    let paid=false;
    for(let i=0;i<3;i++){
      const res=await apiCall('/api/check-payment',{orderId:state.orderId,paymentMethod:state.paymentMethod,invoiceId:state.invoiceId});
      if(res.paid){paid=true;state.licenseKey=res.licenseKey;break}
      if(i<2) await sleep(3000);
    }
    hideLoading();
    if(paid){
      document.getElementById('licenseKey').textContent=state.licenseKey||'';
      navigate('success');
      launchConfetti();
      try{tg.HapticFeedback.notificationOccurred('success')}catch(e){}
    }else{
      showToast('⏳ Платеж не найден. Попробуйте позже','error');
    }
  }catch(e){hideLoading();showToast('Ошибка проверки','error')}
}

// ===== COPY LICENSE =====
function copyLicense(){
  const key=document.getElementById('licenseKey').textContent;
  try{navigator.clipboard.writeText(key);showToast('🔑 Ключ скопирован!','success');
    try{tg.HapticFeedback.notificationOccurred('success')}catch(e){}}
  catch(e){showToast('Не удалось скопировать','error')}
}

// ===== API =====
async function apiCall(url,data){
  const r=await fetch(url,{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({...data,initData:tg.initData})});
  return r.json();
}
function sleep(ms){return new Promise(r=>setTimeout(r,ms))}

// ===== UI HELPERS =====
function showLoading(text){
  document.getElementById('loadingText').textContent=text||'Загрузка...';
  document.getElementById('loadingOverlay').classList.add('show');
}
function hideLoading(){document.getElementById('loadingOverlay').classList.remove('show')}

function showToast(text,type){
  const t=document.getElementById('toast');
  t.textContent=text;t.className='toast '+(type||'');
  t.classList.add('show');
  setTimeout(()=>t.classList.remove('show'),3000);
}

// ===== PARTICLES =====
(function(){
  const canvas=document.getElementById('particles');
  const ctx=canvas.getContext('2d');
  let W,H;
  function resize(){W=canvas.width=window.innerWidth;H=canvas.height=window.innerHeight}
  resize();window.addEventListener('resize',resize);
  const particles=[];
  const COUNT=70;
  for(let i=0;i<COUNT;i++){
    particles.push({
      x:Math.random()*W,y:Math.random()*H,
      r:Math.random()*1.8+0.3,
      dx:(Math.random()-0.5)*0.4,dy:(Math.random()-0.5)*0.4,
      o:Math.random()*0.35+0.05,
      c:Math.random()>0.5?'168,85,247':'34,211,238'
    });
  }
  function draw(){
    ctx.clearRect(0,0,W,H);
    for(let i=0;i<COUNT;i++){
      const p=particles[i];
      p.x+=p.dx;p.y+=p.dy;
      if(p.x<0)p.x=W;if(p.x>W)p.x=0;
      if(p.y<0)p.y=H;if(p.y>H)p.y=0;
      ctx.beginPath();ctx.arc(p.x,p.y,p.r,0,Math.PI*2);
      ctx.fillStyle='rgba('+p.c+','+p.o+')';ctx.fill();
      for(let j=i+1;j<COUNT;j++){
        const q=particles[j];
        const dist=Math.hypot(p.x-q.x,p.y-q.y);
        if(dist<120){
          ctx.beginPath();ctx.moveTo(p.x,p.y);ctx.lineTo(q.x,q.y);
          ctx.strokeStyle='rgba(168,85,247,'+(0.06*(1-dist/120))+')';
          ctx.lineWidth=0.5;ctx.stroke();
        }
      }
    }
    requestAnimationFrame(draw);
  }
  draw();
})();

// ===== CONFETTI =====
function launchConfetti(){
  const canvas=document.getElementById('confetti');
  const ctx=canvas.getContext('2d');
  canvas.width=window.innerWidth;canvas.height=window.innerHeight;
  const pieces=[];
  const colors=['#a855f7','#22d3ee','#f43f5e','#fbbf24','#34d399','#818cf8'];
  for(let i=0;i<120;i++){
    pieces.push({
      x:canvas.width/2,y:canvas.height/2,
      dx:(Math.random()-0.5)*12,dy:Math.random()*-14-4,
      r:Math.random()*6+3,rot:Math.random()*360,
      dr:Math.random()*8-4,g:0.25,
      c:colors[Math.floor(Math.random()*colors.length)],
      o:1,shape:Math.random()>0.5?'rect':'circle'
    });
  }
  let frame=0;
  function animate(){
    ctx.clearRect(0,0,canvas.width,canvas.height);
    let alive=false;
    pieces.forEach(p=>{
      p.x+=p.dx;p.y+=p.dy;p.dy+=p.g;p.rot+=p.dr;
      p.o-=0.008;if(p.o<=0)return;
      alive=true;ctx.save();ctx.translate(p.x,p.y);
      ctx.rotate(p.rot*Math.PI/180);ctx.globalAlpha=p.o;
      ctx.fillStyle=p.c;
      if(p.shape==='rect'){ctx.fillRect(-p.r/2,-p.r/2,p.r,p.r*0.6)}
      else{ctx.beginPath();ctx.arc(0,0,p.r/2,0,Math.PI*2);ctx.fill()}
      ctx.restore();
    });
    frame++;
    if(alive&&frame<200)requestAnimationFrame(animate);
    else ctx.clearRect(0,0,canvas.width,canvas.height);
  }
  animate();
}
</script>
</body>
</html>"""


# ============================================================
# =================== WEB SERVER =============================
# ============================================================

def get_products_json():
    return json.dumps({
        pid: {
            'name': p['name'], 'period_text': p['period_text'], 'price': p['price'],
            'price_stars': p['price_stars'], 'price_gold': p['price_gold'],
            'price_nft': p['price_nft'], 'price_crypto_usdt': p['price_crypto_usdt'],
            'platform': p['platform'], 'period': p['period'],
            'platform_code': p['platform_code'], 'emoji': p['emoji'], 'duration': p['duration']
        }
        for pid, p in PRODUCTS.items()
    }, ensure_ascii=False)


def render_html():
    html = MINIAPP_HTML
    html = html.replace('%%BOT%%', Config.BOT_USERNAME or 'pmt_bot')
    html = html.replace('%%SUPPORT%%', Config.SUPPORT_CHAT_USERNAME)
    html = html.replace('%%DOWNLOAD%%', Config.DOWNLOAD_URL)
    html = html.replace('%%PRODUCTS_JSON%%', get_products_json())
    return html


async def handle_index(request):
    return web.Response(text=render_html(), content_type='text/html', charset='utf-8')


async def handle_create_order(request):
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"ok": False, "error": "Bad request"}, status=400)

    init_data = body.get('initData', '')
    user = validate_init_data(init_data)
    if user is None:
        return web.json_response({"ok": False, "error": "Unauthorized"}, status=401)

    product_id = body.get('productId')
    payment_method = body.get('paymentMethod')
    product = find_product_by_id(product_id)
    if not product:
        return web.json_response({"ok": False, "error": "Product not found"})

    user_id = user.get('id', 0)
    user_name = "{} {}".format(user.get('first_name', ''), user.get('last_name', '')).strip() or "WebApp User"
    order_id = generate_order_id()

    if payment_method == 'card':
        if not Config.YOOMONEY_WALLET:
            return web.json_response({"ok": False, "error": "Оплата картой недоступна"})
        amount = product['price']
        payment_url = create_payment_link(amount, order_id, "{} ({})".format(product['name'], product['duration']))
        await orders.add_pending(order_id, {
            "user_id": user_id, "user_name": user_name, "product": product,
            "amount": amount, "currency": "₽", "payment_method": "Картой (WebApp)",
            "status": "pending", "created_at": time.time()
        })
        # Уведомляем админов
        asyncio.create_task(_notify_admins_web(user_id, user_name, product, "💳 Картой (WebApp)", "{} ₽".format(amount), order_id))
        return web.json_response({"ok": True, "orderId": order_id, "paymentUrl": payment_url})

    elif payment_method == 'crypto':
        if not Config.CRYPTOBOT_TOKEN:
            return web.json_response({"ok": False, "error": "Крипто недоступно"})
        amount_usdt = product['price_crypto_usdt']
        inv = await CryptoBotService.create_invoice(amount_usdt, order_id, "PMT {} ({})".format(product['name'], product['duration']))
        if not inv:
            return web.json_response({"ok": False, "error": "Ошибка создания инвойса"})
        await orders.add_pending(order_id, {
            "user_id": user_id, "user_name": user_name, "product": product,
            "amount": amount_usdt, "currency": "USDT", "payment_method": "CryptoBot (WebApp)",
            "status": "pending", "invoice_id": inv["invoice_id"], "created_at": time.time()
        })
        asyncio.create_task(_notify_admins_web(user_id, user_name, product, "₿ CryptoBot (WebApp)", "{} USDT".format(amount_usdt), order_id))
        return web.json_response({"ok": True, "orderId": order_id, "invoiceUrl": inv["pay_url"], "invoiceId": inv["invoice_id"]})

    return web.json_response({"ok": False, "error": "Unknown payment method"})


async def handle_check_payment(request):
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"ok": False, "error": "Bad request"}, status=400)

    init_data = body.get('initData', '')
    user = validate_init_data(init_data)
    if user is None:
        return web.json_response({"ok": False, "error": "Unauthorized"}, status=401)

    order_id = body.get('orderId')
    if not order_id:
        return web.json_response({"ok": False, "error": "No orderId"})

    # Уже подтверждён?
    confirmed = await orders.get_confirmed(order_id)
    if confirmed:
        return web.json_response({"ok": True, "paid": True, "licenseKey": confirmed.get('license_key', '')})

    order = await orders.get_pending(order_id)
    if not order:
        return web.json_response({"ok": True, "paid": False})

    payment_method = body.get('paymentMethod', order.get('payment_method', ''))
    paid = False

    if 'card' in payment_method.lower() or 'картой' in payment_method.lower():
        paid = await YooMoneyService.check_payment(order_id, order['amount'], order.get('created_at', time.time()))
    elif 'crypto' in payment_method.lower():
        invoice_id = body.get('invoiceId') or order.get('invoice_id')
        if invoice_id:
            paid = await CryptoBotService.check_invoice(invoice_id)

    if paid:
        success = await process_successful_payment(order_id, "WebApp Автопроверка")
        if success:
            confirmed = await orders.get_confirmed(order_id)
            return web.json_response({"ok": True, "paid": True, "licenseKey": confirmed.get('license_key', '') if confirmed else ''})
        else:
            conf2 = await orders.get_confirmed(order_id)
            if conf2:
                return web.json_response({"ok": True, "paid": True, "licenseKey": conf2.get('license_key', '')})

    return web.json_response({"ok": True, "paid": False})


async def _notify_admins_web(user_id, user_name, product, method, price, order_id):
    now_str = datetime.now().strftime('%d.%m.%Y %H:%M')
    msg = (
        "🔔 <b>НОВЫЙ ЗАКАЗ (WebApp)</b>\n\n"
        "👤 {}\n🆔 <code>{}</code>\n"
        "📦 {} ({})\n💰 {}\n💳 {}\n"
        "🆔 <code>{}</code>\n📅 {}"
    ).format(user_name, user_id, product['name'], product['duration'], price, method, order_id, now_str)
    for aid in Config.ADMIN_IDS:
        try:
            await bot.send_message(aid, msg, reply_markup=admin_confirm_keyboard(order_id))
        except Exception as e:
            logger.error("Admin notify error: %s", e)


# ============================================================
# =================== BOT ====================================
# ============================================================

Config.init()

bot = Bot(token=Config.BOT_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
storage = MemoryStorage()
dp = Dispatcher(storage=storage)
orders = OrderStorage(max_pending=Config.MAX_PENDING_ORDERS, expiry_seconds=Config.ORDER_EXPIRY_SECONDS)
rate_limiter = RateLimiter(interval=Config.RATE_LIMIT_SECONDS)

start_photo = None
try:
    if os.path.isfile(Config.START_IMAGE_PATH):
        start_photo = FSInputFile(Config.START_IMAGE_PATH)
except Exception:
    pass


# ========== СОСТОЯНИЯ ==========
class OrderState(StatesGroup):
    main_menu = State()
    choosing_platform = State()
    choosing_subscription = State()
    choosing_payment = State()


# ========== КЛАВИАТУРЫ ==========
def start_keyboard():
    kb = [
        [InlineKeyboardButton(text="🛒 Купить чит", callback_data="buy_cheat")],
        [InlineKeyboardButton(text="🌐 Открыть магазин", web_app=WebAppInfo(url=Config.WEBAPP_URL))],
        [InlineKeyboardButton(text="ℹ️ О программе", callback_data="about")],
        [InlineKeyboardButton(text="💬 Поддержка", url="https://t.me/{}".format(Config.SUPPORT_CHAT_USERNAME))]
    ]
    return InlineKeyboardMarkup(inline_keyboard=kb)


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
        [InlineKeyboardButton(text="💳 Картой", callback_data="pay_yoomoney_{}_{}".format(pc, p))],
        [InlineKeyboardButton(text="⭐ Telegram Stars", callback_data="pay_stars_{}_{}".format(pc, p))],
        [InlineKeyboardButton(text="₿ Криптобот", callback_data="pay_crypto_{}_{}".format(pc, p))],
        [InlineKeyboardButton(text="🪙 GOLD", callback_data="pay_gold_{}_{}".format(pc, p))],
        [InlineKeyboardButton(text="🎨 NFT", callback_data="pay_nft_{}_{}".format(pc, p))],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="back_to_subscription")]
    ])


def payment_keyboard(payment_url, order_id):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💳 Оплатить картой", url=payment_url)],
        [InlineKeyboardButton(text="✅ Проверить оплату", callback_data="checkym_{}".format(order_id))],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="restart")]
    ])


def crypto_payment_keyboard(invoice_url, order_id):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="₿ Оплатить криптой", url=invoice_url)],
        [InlineKeyboardButton(text="✅ Проверить платеж", callback_data="checkcr_{}".format(order_id))],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="restart")]
    ])


def support_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💬 Поддержка", url="https://t.me/{}".format(Config.SUPPORT_CHAT_USERNAME))],
        [InlineKeyboardButton(text="🔄 Новая покупка", callback_data="restart")]
    ])


def download_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📥 Скачать PMT", url=Config.DOWNLOAD_URL)],
        [InlineKeyboardButton(text="💬 Поддержка", url="https://t.me/{}".format(Config.SUPPORT_CHAT_USERNAME))],
        [InlineKeyboardButton(text="🔄 Новая покупка", callback_data="restart")]
    ])


def about_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="◀️ Назад", callback_data="back_to_start")]
    ])


def admin_confirm_keyboard(order_id):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Подтвердить", callback_data="admin_confirm_{}".format(order_id))],
        [InlineKeyboardButton(text="❌ Отклонить", callback_data="admin_reject_{}".format(order_id))]
    ])


def manual_payment_keyboard(support_url, sent_callback):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💬 Перейти к оплате", url=support_url)],
        [InlineKeyboardButton(text="✅ Я написал", callback_data=sent_callback)],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="restart")]
    ])


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

    success_text = (
        "🎉 <b>Оплата подтверждена!</b>\n\n"
        "✨ Добро пожаловать в PMT!\n\n"
        "📦 <b>Ваша покупка:</b>\n"
        "{emoji} {name}\n⏱️ Срок: {duration}\n🔍 Метод: {source}\n\n"
        "🔑 <b>Ваш лицензионный ключ:</b>\n<code>{key}</code>\n\n"
        "📥 <b>Скачивание:</b>\n👇 Нажмите кнопку ниже\n\n"
        "📫 Поддержка: @{support}"
    ).format(emoji=product['emoji'], name=product['name'], duration=product['duration'],
             source=source, key=license_key, support=Config.SUPPORT_CHAT_USERNAME)
    try:
        await bot.send_message(user_id, success_text, reply_markup=download_keyboard())
    except Exception as e:
        logger.error("Send to user %s: %s", user_id, e)

    now_str = datetime.now().strftime('%d.%m.%Y %H:%M')
    admin_text = (
        "💎 <b>НОВАЯ ПРОДАЖА ({source})</b>\n\n"
        "👤 {uname}\n🆔 {uid}\n📦 {pn} ({dur})\n"
        "💰 {amount} {cur}\n🔑 <code>{key}</code>\n📅 {now}"
    ).format(source=source, uname=order['user_name'], uid=user_id,
             pn=product['name'], dur=product['duration'],
             amount=order.get('amount', product['price']),
             cur=order.get('currency', '₽'), key=license_key, now=now_str)
    for aid in Config.ADMIN_IDS:
        try:
            await bot.send_message(aid, admin_text)
        except Exception:
            pass
    return True


async def send_admin_notification(user, product, payment_method, price, order_id):
    now_str = datetime.now().strftime('%d.%m.%Y %H:%M')
    msg = (
        "🔔 <b>НОВЫЙ ЗАКАЗ</b>\n\n"
        "👤 {fn}\n🆔 <code>{uid}</code>\n"
        "📦 {pn} ({dur})\n💰 {price}\n💳 {pm}\n"
        "🆔 <code>{oid}</code>\n📅 {now}"
    ).format(fn=user.full_name, uid=user.id, pn=product['name'],
             dur=product['duration'], price=price, pm=payment_method, oid=order_id, now=now_str)
    for aid in Config.ADMIN_IDS:
        try:
            await bot.send_message(aid, msg, reply_markup=admin_confirm_keyboard(order_id))
        except Exception:
            pass


async def edit_or_send_start(target, state: FSMContext):
    keyboard = start_keyboard()
    if isinstance(target, types.CallbackQuery):
        try:
            if start_photo:
                media = InputMediaPhoto(media=start_photo, caption=Config.START_TEXT, parse_mode="HTML")
                await target.message.edit_media(media, reply_markup=keyboard)
            else:
                await target.message.edit_text(Config.START_TEXT, reply_markup=keyboard)
        except Exception:
            try:
                await target.message.delete()
            except Exception:
                pass
            if start_photo:
                await target.message.answer_photo(photo=start_photo, caption=Config.START_TEXT, reply_markup=keyboard)
            else:
                await target.message.answer(Config.START_TEXT, reply_markup=keyboard)
    elif isinstance(target, types.Message):
        if start_photo:
            await target.answer_photo(photo=start_photo, caption=Config.START_TEXT, reply_markup=keyboard)
        else:
            await target.answer(Config.START_TEXT, reply_markup=keyboard)
    await state.set_state(OrderState.main_menu)


# ========== ОБРАБОТЧИКИ БОТА ==========
@dp.message(Command("start"))
async def cmd_start(message: types.Message, state: FSMContext):
    await state.clear()
    args = message.text.split()
    if len(args) > 1:
        deep_link = args[1]
        if deep_link.startswith("buy_stars_"):
            product_id = deep_link.replace("buy_stars_", "", 1)
            product = find_product_by_id(product_id)
            if product:
                order_id = generate_order_id()
                await orders.add_pending(order_id, {
                    "user_id": message.from_user.id, "user_name": message.from_user.full_name,
                    "product": product, "amount": product['price_stars'], "currency": "⭐",
                    "payment_method": "Telegram Stars", "status": "pending", "created_at": time.time()
                })
                await bot.send_invoice(
                    chat_id=message.from_user.id,
                    title="PMT — {}".format(product['name']),
                    description="Подписка на {} для {}".format(product['duration'], product['platform']),
                    payload="stars_{}".format(order_id), provider_token="", currency="XTR",
                    prices=[LabeledPrice(label="XTR", amount=product['price_stars'])],
                    start_parameter="pmt_payment"
                )
                return
    await edit_or_send_start(message, state)


@dp.callback_query(F.data == "buy_cheat")
async def buy_cheat(callback: types.CallbackQuery, state: FSMContext):
    text = "💰 <b>Выберите платформу:</b>\n\n📱 <b>Android</b> — APK файл\n🍏 <b>iOS</b> — IPA файл"
    try:
        await callback.message.edit_text(text, reply_markup=platform_keyboard())
    except Exception:
        await callback.message.answer(text, reply_markup=platform_keyboard())
    await state.set_state(OrderState.choosing_platform)
    await callback.answer()


@dp.callback_query(F.data == "about")
async def about_cheat(callback: types.CallbackQuery):
    text = (
        "📋 <b>Подробная информация</b>\n\n"
        "🎮 <b>Название:</b> PMT\n🔥 <b>Статус:</b> Активно обновляется\n\n"
        "🛠️ <b>Функционал:</b>\n• 🎯 Умный AimBot\n• 👁️ WallHack\n• 📍 ESP\n• 🗺️ Мини-радар\n• ⚙️ Гибкие настройки\n\n"
        "🛡️ <b>Безопасность:</b>\n• Обход античитов\n• Регулярные обновления\n\n"
        "💬 Поддержка: @{}"
    ).format(Config.SUPPORT_CHAT_USERNAME)
    await callback.message.edit_text(text, reply_markup=about_keyboard())
    await callback.answer()


@dp.callback_query(F.data.startswith("platform_"))
async def process_platform(callback: types.CallbackQuery, state: FSMContext):
    platform = callback.data.split("_")[1]
    if platform not in ("apk", "ios"):
        await callback.answer("❌", show_alert=True)
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
    product = find_product_by_id("{}_{}".format(parts[1], parts[2]))
    if not product:
        await callback.answer("❌ Не найден", show_alert=True)
        return
    await state.update_data(selected_product=product)
    text = (
        "🛒 <b>Оформление</b>\n\n"
        "{emoji} <b>{name}</b>\n⏱️ {duration}\n\n"
        "💎 <b>Стоимость:</b>\n"
        "💳 Картой: {price} ₽\n⭐ Stars: {stars} ⭐\n"
        "₿ Крипта: {crypto} USDT\n💰 GOLD: {gold} 🪙\n🎨 NFT: {nft} 🖼️\n\n"
        "🎯 <b>Способ оплаты:</b>"
    ).format(emoji=product['emoji'], name=product['name'], duration=product['duration'],
             price=product['price'], stars=product['price_stars'], crypto=product['price_crypto_usdt'],
             gold=product['price_gold'], nft=product['price_nft'])
    await callback.message.edit_text(text, reply_markup=payment_methods_keyboard(product))
    await state.set_state(OrderState.choosing_payment)
    await callback.answer()


# ===== Оплата картой =====
@dp.callback_query(F.data.startswith("pay_yoomoney_"))
async def process_yoomoney(callback: types.CallbackQuery):
    if not Config.YOOMONEY_WALLET:
        await callback.answer("❌ Недоступно", show_alert=True)
        return
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
    payment_url = create_payment_link(amount, order_id, "{} ({})".format(product['name'], product['duration']))
    await orders.add_pending(order_id, {
        "user_id": callback.from_user.id, "user_name": callback.from_user.full_name,
        "product": product, "amount": amount, "currency": "₽",
        "payment_method": "Картой", "status": "pending", "created_at": time.time()
    })
    text = (
        "💳 <b>Оплата картой</b>\n\n{emoji} {name}\n⏱️ {dur}\n"
        "💰 <b>{amount} ₽</b>\n🆔 <code>{oid}</code>\n\n"
        "1️⃣ Нажмите «Оплатить»\n2️⃣ Оплатите\n3️⃣ Нажмите «Проверить»"
    ).format(emoji=product['emoji'], name=product['name'], dur=product['duration'], amount=amount, oid=order_id)
    await callback.message.edit_text(text, reply_markup=payment_keyboard(payment_url, order_id))
    await send_admin_notification(callback.from_user, product, "💳 Картой", "{} ₽".format(amount), order_id)
    await callback.answer()


@dp.callback_query(F.data.startswith("checkym_"))
async def check_yoomoney(callback: types.CallbackQuery):
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
    msg = await callback.message.edit_text("🔄 <b>Проверка платежа...</b>\n⏳ Подождите 15-25 секунд...")
    found = False
    for _ in range(Config.MAX_PAYMENT_CHECK_ATTEMPTS):
        found = await YooMoneyService.check_payment(order_id, order["amount"], order.get("created_at", time.time()))
        if found:
            break
        await asyncio.sleep(Config.PAYMENT_CHECK_INTERVAL)
    if found:
        success = await process_successful_payment(order_id, "Автопроверка")
        if success:
            await msg.edit_text("✅ <b>Платеж найден!</b>\n📨 Проверьте сообщение ⬆️", reply_markup=support_keyboard())
        else:
            await msg.edit_text("✅ Уже обработан", reply_markup=support_keyboard())
    else:
        product = order['product']
        purl = create_payment_link(order["amount"], order_id, "{} ({})".format(product['name'], product['duration']))
        await msg.edit_text(
            "⏳ <b>Платеж не найден</b>\n\n💰 {amount} ₽\n🆔 <code>{oid}</code>\n\n⏰ Попробуйте через 1-2 мин".format(
                amount=order['amount'], oid=order_id),
            reply_markup=payment_keyboard(purl, order_id))


# ===== Оплата Stars =====
@dp.callback_query(F.data.startswith("pay_stars_"))
async def process_stars(callback: types.CallbackQuery):
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
    await orders.add_pending(order_id, {
        "user_id": callback.from_user.id, "user_name": callback.from_user.full_name,
        "product": product, "amount": product['price_stars'], "currency": "⭐",
        "payment_method": "Telegram Stars", "status": "pending", "created_at": time.time()
    })
    await bot.send_invoice(
        chat_id=callback.from_user.id,
        title="PMT — {}".format(product['name']),
        description="Подписка на {} для {}".format(product['duration'], product['platform']),
        payload="stars_{}".format(order_id), provider_token="", currency="XTR",
        prices=[LabeledPrice(label="XTR", amount=product['price_stars'])],
        start_parameter="pmt_payment"
    )
    await send_admin_notification(callback.from_user, product, "⭐ Stars", "{} ⭐".format(product['price_stars']), order_id)
    try:
        await callback.message.delete()
    except Exception:
        pass
    await callback.answer()


@dp.pre_checkout_query()
async def pre_checkout(pcq: PreCheckoutQuery):
    await bot.answer_pre_checkout_query(pcq.id, ok=True)


@dp.message(F.successful_payment)
async def successful_payment(message: types.Message):
    payload = message.successful_payment.invoice_payload
    if payload.startswith("stars_"):
        await process_successful_payment(payload.replace("stars_", "", 1), "Telegram Stars")


# ===== Оплата Крипто =====
@dp.callback_query(F.data.startswith("pay_crypto_"))
async def process_crypto(callback: types.CallbackQuery):
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
    amount = product["price_crypto_usdt"]
    inv = await CryptoBotService.create_invoice(amount, order_id, "PMT {} ({})".format(product['name'], product['duration']))
    if not inv:
        await callback.answer("❌ Ошибка инвойса", show_alert=True)
        return
    await orders.add_pending(order_id, {
        "user_id": callback.from_user.id, "user_name": callback.from_user.full_name,
        "product": product, "amount": amount, "currency": "USDT",
        "payment_method": "CryptoBot", "status": "pending",
        "invoice_id": inv["invoice_id"], "created_at": time.time()
    })
    text = (
        "₿ <b>Криптооплата</b>\n\n{emoji} {name}\n⏱️ {dur}\n"
        "💰 <b>{amount} USDT</b>\n🆔 <code>{oid}</code>\n\n"
        "1️⃣ Нажмите «Оплатить»\n2️⃣ Выберите валюту\n3️⃣ Нажмите «Проверить»"
    ).format(emoji=product['emoji'], name=product['name'], dur=product['duration'], amount=amount, oid=order_id)
    await callback.message.edit_text(text, reply_markup=crypto_payment_keyboard(inv["pay_url"], order_id))
    await send_admin_notification(callback.from_user, product, "₿ CryptoBot", "{} USDT".format(amount), order_id)
    await callback.answer()


@dp.callback_query(F.data.startswith("checkcr_"))
async def check_crypto(callback: types.CallbackQuery):
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
            await callback.message.edit_text("✅ <b>Криптоплатеж подтвержден!</b>\n📨 Ключ отправлен ⬆️", reply_markup=support_keyboard())
    else:
        await callback.answer("⏳ Не подтверждён. Попробуйте через минуту.", show_alert=True)


# ===== Оплата GOLD / NFT =====
@dp.callback_query(F.data.startswith("pay_gold_"))
async def process_gold(callback: types.CallbackQuery):
    await _manual_pay(callback, "gold")


@dp.callback_query(F.data.startswith("pay_nft_"))
async def process_nft(callback: types.CallbackQuery):
    await _manual_pay(callback, "nft")


async def _manual_pay(callback, method):
    parts = callback.data.split("_")
    if len(parts) < 4:
        return
    product = find_product(parts[2], parts[3])
    if not product:
        return
    if not rate_limiter.check(callback.from_user.id):
        await callback.answer("⏳", show_alert=True)
        return
    cfg = {
        "gold": {"name": "GOLD", "icon": "💰", "price_key": "price_gold", "emoji": "🪙"},
        "nft": {"name": "NFT", "icon": "🎨", "price_key": "price_nft", "emoji": "🖼️"}
    }[method]
    price = product[cfg["price_key"]]
    chat_msg = "Привет! Хочу купить чит PMT. Подписка на {} ({}). Готов за {} {}".format(
        product['period_text'], product['platform'], price, cfg['name'])
    support_url = "https://t.me/{}?text={}".format(Config.SUPPORT_CHAT_USERNAME, quote(chat_msg, safe=''))
    order_id = generate_order_id()
    await orders.add_pending(order_id, {
        "user_id": callback.from_user.id, "user_name": callback.from_user.full_name,
        "product": product, "amount": price, "currency": cfg["name"],
        "payment_method": cfg["name"], "status": "pending", "created_at": time.time()
    })
    text = (
        "{icon} <b>Оплата {mname}</b>\n\n"
        "{emoji} {pname}\n⏱️ {dur}\n💰 <b>{price} {mname}</b>\n\n"
        "📝 <b>Сообщение:</b>\n<code>{msg}</code>\n\n"
        "1️⃣ Напишите в поддержку\n2️⃣ Ожидайте обработки"
    ).format(icon=cfg['icon'], mname=cfg['name'], emoji=product['emoji'], pname=product['name'],
             dur=product['duration'], price=price, msg=chat_msg)
    await callback.message.edit_text(text, reply_markup=manual_payment_keyboard(support_url, "{}_sent".format(method)))
    await send_admin_notification(callback.from_user, product, "{} {}".format(cfg['icon'], cfg['name']),
                                  "{} {}".format(price, cfg['emoji']), order_id)
    await callback.answer()


@dp.callback_query(F.data.in_({"gold_sent", "nft_sent"}))
async def manual_sent(callback: types.CallbackQuery):
    mname = "GOLD" if callback.data == "gold_sent" else "NFT"
    icon = "💰" if callback.data == "gold_sent" else "🎨"
    text = (
        "✅ <b>Отлично!</b>\n\n{icon} Ваш {mname} заказ принят\n"
        "⏱️ Обработка: до 30 мин\n\n💬 Поддержка: @{support}"
    ).format(icon=icon, mname=mname, support=Config.SUPPORT_CHAT_USERNAME)
    await callback.message.edit_text(text, reply_markup=support_keyboard())
    await callback.answer()


# ===== Админ =====
@dp.callback_query(F.data.startswith("admin_confirm_"))
async def admin_confirm(callback: types.CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("❌", show_alert=True)
        return
    order_id = callback.data.replace("admin_confirm_", "", 1)
    success = await process_successful_payment(order_id, "👨‍💼 Админ")
    if success:
        await callback.message.edit_text("✅ <b>Подтверждён</b>\n🆔 {}\n👨‍💼 {}".format(order_id, callback.from_user.full_name))
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
        await callback.message.edit_text("❌ <b>Отклонён</b>\n🆔 {}".format(order_id))
        try:
            await bot.send_message(order['user_id'], "❌ <b>Заказ отклонён</b>\n💬 @{}".format(Config.SUPPORT_CHAT_USERNAME))
        except Exception:
            pass
    await callback.answer("❌")


@dp.message(Command("orders"))
async def cmd_orders(message: types.Message):
    if not is_admin(message.from_user.id):
        return
    stats = await orders.get_stats()
    text = "📊 <b>СТАТИСТИКА</b>\n\n⏳ Ожидают: {}\n".format(stats['pending'])
    for oid, order in await orders.get_recent_pending(5):
        t = datetime.fromtimestamp(order['created_at']).strftime('%H:%M')
        text += "• {} | {} | {}\n".format(t, order['user_name'], order['product']['name'])
    text += "\n✅ Подтверждено: {}\n".format(stats['confirmed'])
    balance = await YooMoneyService.get_balance()
    if balance is not None:
        text += "💰 Баланс: {} ₽".format(balance)
    await message.answer(text)


@dp.message(Command("help"))
async def cmd_help(message: types.Message):
    if not is_admin(message.from_user.id):
        return
    await message.answer("/orders — Статистика\n/help — Справка")


# ===== Навигация =====
@dp.callback_query(F.data == "restart")
async def restart(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await edit_or_send_start(callback, state)
    await callback.answer()


@dp.callback_query(F.data == "back_to_start")
async def back_start(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await edit_or_send_start(callback, state)
    await callback.answer()


@dp.callback_query(F.data == "back_to_subscription")
async def back_sub(callback: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    platform = data.get("platform", "apk")
    await callback.message.edit_text("💰 <b>Выберите тариф:</b>", reply_markup=subscription_keyboard(platform))
    await state.set_state(OrderState.choosing_subscription)
    await callback.answer()


# ============================================================
# =================== ЗАПУСК ==================================
# ============================================================

async def main():
    logger.info("=" * 50)
    logger.info("PMT PREMIUM CHEAT SHOP — BOT + MINIAPP")
    logger.info("=" * 50)

    try:
        me = await bot.get_me()
        Config.BOT_USERNAME = me.username
        logger.info("Bot: @%s", me.username)
    except Exception as e:
        logger.error("Failed to get bot info: %s", e)
        Config.BOT_USERNAME = "pmt_bot"

    logger.info("ADMIN_IDS: %s", Config.ADMIN_IDS)
    logger.info("WEBAPP: %s", Config.WEBAPP_URL)
    logger.info("WEB PORT: %s", Config.WEB_PORT)

    # ===== Web server =====
    app = web.Application()
    app.router.add_get('/', handle_index)
    app.router.add_post('/api/create-order', handle_create_order)
    app.router.add_post('/api/check-payment', handle_check_payment)

    # CORS middleware
    @web.middleware
    async def cors_middleware(request, handler):
        if request.method == 'OPTIONS':
            resp = web.Response(status=200)
        else:
            resp = await handler(request)
        resp.headers['Access-Control-Allow-Origin'] = '*'
        resp.headers['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS'
        resp.headers['Access-Control-Allow-Headers'] = 'Content-Type'
        return resp

    app.middlewares.append(cors_middleware)
    app.router.add_route('OPTIONS', '/api/create-order', lambda r: web.Response(status=200))
    app.router.add_route('OPTIONS', '/api/check-payment', lambda r: web.Response(status=200))

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', Config.WEB_PORT)
    await site.start()
    logger.info("Web server started on port %d", Config.WEB_PORT)

    # ===== Bot polling =====
    try:
        await dp.start_polling(bot)
    except KeyboardInterrupt:
        logger.info("Shutting down...")
    except Exception as e:
        logger.error("Fatal: %s", e)
        import traceback
        traceback.print_exc()
    finally:
        await runner.cleanup()
        await bot.session.close()
        logger.info("Stopped")


if __name__ == "__main__":
    asyncio.run(main())
