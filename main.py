from __future__ import annotations

import asyncio
import base64
import hmac
import hashlib
import html
import json
import logging
import os
import secrets
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any, Optional

import aiohttp
import psutil
from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.filters import BaseFilter, Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from aiogram.utils.formatting import Bold, CustomEmoji, Text
from aiohttp import web
from cryptography.fernet import Fernet, InvalidToken
from motor.motor_asyncio import AsyncIOMotorClient
from pymongo import ASCENDING, DESCENDING, ReturnDocument

import config

log = logging.getLogger("crypto_store")
ADMIN_SET = set(config.ADMIN_IDS)
PAGE_SIZE = 10
MAX_PRICE_CENTS = 100_000_000

# OxaPay constants
OXAPAY_API_KEY = config.OXAPAY_API_KEY
WEBHOOK_BASE = config.WEBHOOK_URL.rstrip("/")
OXAPAY_BASE_URL = "https://api.oxapay.com/v1"
OXAPAY_CALLBACK_URL = f"{WEBHOOK_BASE}/oxapay/callback"
OXAPAY_TO_CURRENCY = "USDT"
UNDERPAY_TOLERANCE = Decimal("0.02")

mongo: Any = None
db: Any = None
bot_ref: Optional[Bot] = None

# ── Helper Utilities ──

def now() -> datetime:
    return datetime.now(timezone.utc)

def esc(v: Any) -> str:
    return html.escape(str(v), quote=False)

def new_id(prefix: str, nbytes: int = 4) -> str:
    return f"{prefix}-{secrets.token_hex(nbytes).upper()}"

def money(cents: int) -> str:
    return f"${cents / 100:,.2f}"

def parse_money(text: str) -> Optional[int]:
    try:
        d = Decimal(text.strip().replace("$", "").replace(",", ""))
    except (InvalidOperation, AttributeError):
        return None
    if not d.is_finite() or d <= 0:
        return None
    cents = int((d * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    return cents if 0 < cents <= MAX_PRICE_CENTS else None

def to_int(s: str, default: int = 0) -> int:
    try:
        return int(s)
    except (TypeError, ValueError):
        return default

def get_emoji(emoji_attr: str, fallback_str: str) -> Text:
    emoji_id = getattr(config, emoji_attr, None)
    if emoji_id:
        return Text(CustomEmoji(fallback_str, custom_emoji_id=str(emoji_id)))
    return Text(fallback_str)

_fernet = Fernet(config.SETTINGS_ENCRYPTION_KEY.encode() if config.SETTINGS_ENCRYPTION_KEY else Fernet.generate_key())

def enc(s: str) -> str:
    return _fernet.encrypt(s.encode()).decode()

def dec(s: str) -> str:
    try:
        return _fernet.decrypt(s.encode()).decode()
    except (InvalidToken, ValueError):
        return ""


# ════════════════════════ OXAPAY API INTEGRATION ════════════════════════

async def _oxapay_request(method: str, path: str, payload: Optional[dict] = None) -> tuple[Optional[dict], str]:
    if not OXAPAY_API_KEY:
        return None, "OxaPay API key is not configured."
    headers = {"merchant_api_key": OXAPAY_API_KEY, "Content-Type": "application/json"}
    try:
        timeout = aiohttp.ClientTimeout(total=20)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            if method == "GET":
                ctx = session.get(f"{OXAPAY_BASE_URL}{path}", headers=headers)
            else:
                ctx = session.post(f"{OXAPAY_BASE_URL}{path}", json=payload, headers=headers)
            async with ctx as resp:
                http_status = resp.status
                body = await resp.json(content_type=None)
    except Exception as e:
        log.error("OxaPay request failed: %s", e)
        return None, str(e)

    data = body.get("data") if isinstance(body, dict) else None
    if http_status == 200 and isinstance(data, dict) and data:
        return data, ""
    err = (body.get("error") or {}) if isinstance(body, dict) else {}
    msg = (err.get("message") if isinstance(err, dict) else None) or (body.get("message") if isinstance(body, dict) else None) or f"HTTP {http_status}"
    return None, str(msg)

async def fetch_payment_info(track_id: str) -> tuple[Optional[dict], str]:
    return await _oxapay_request("GET", f"/payment/{track_id}")

async def create_oxapay_invoice(user_id: int, amount_usd: float, order_ref: str) -> tuple[Optional[dict], str]:
    payload = {
        "amount": round(float(amount_usd), 2),
        "currency": "USD",
        "lifetime": 60,
        "fee_paid_by_payer": 1,
        "under_paid_coverage": 2.5,
        "to_currency": OXAPAY_TO_CURRENCY,
        "auto_withdrawal": False,
        "mixed_payment": True,
        "callback_url": OXAPAY_CALLBACK_URL,
        "return_url": config.RETURN_URL or WEBHOOK_BASE or "https://t.me",
        "order_id": order_ref,
        "thanks_message": "Payment verified! Funds credited to store balance.",
        "description": f"Balance top-up for user {user_id}",
        "sandbox": config.OXAPAY_SANDBOX,
    }
    data, err = await _oxapay_request("POST", "/payment/invoice", payload)
    if data and data.get("payment_url"):
        return data, ""
    return None, err or "Failed to generate OxaPay invoice."


# ════════════════════════ TRANSFER PROVIDER ABSTRACTION ════════════════════════

async def execute_blockchain_transfer(network: str, asset: str, quantity: str, destination_address: str) -> tuple[bool, str, str]:
    provider = config.TRANSFER_PROVIDER.lower()
    if provider == "mock" or not config.TRANSFER_PROVIDER_BASE_URL:
        if config.OXAPAY_SANDBOX:
            await asyncio.sleep(1)
            return True, f"0xTESTHASH{secrets.token_hex(16).upper()}", ""
        return False, "", "Real transfer provider is not configured or disabled in production."

    headers = {
        "Authorization": f"Bearer {config.TRANSFER_PROVIDER_API_KEY}",
        "X-API-Secret": config.TRANSFER_PROVIDER_SECRET,
        "Content-Type": "application/json"
    }
    payload = {
        "network": network,
        "asset": asset,
        "quantity": quantity,
        "destination": destination_address
    }
    try:
        timeout = aiohttp.ClientTimeout(total=30)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(f"{config.TRANSFER_PROVIDER_BASE_URL}/transfer", json=payload, headers=headers) as resp:
                body = await resp.json(content_type=None)
                if resp.status == 200 and isinstance(body, dict) and body.get("success"):
                    tx_hash = body.get("tx_hash") or body.get("reference") or "0xCONFIRMED"
                    return True, str(tx_hash), ""
                return False, "", body.get("message") or f"Transfer API error HTTP {resp.status}"
    except Exception as e:
        log.error("Blockchain transfer execution error: %s", e)
        return False, "", str(e)


# ════════════════════════ DATABASE INITIALIZATION ════════════════════════

async def init_db():
    global mongo, db
    mongo = AsyncIOMotorClient(config.MONGO_URI, tz_aware=True, serverSelectionTimeoutMS=8000)
    db = mongo[config.DATABASE_NAME]
    await mongo.admin.command("ping")
    
    A, D = ASCENDING, DESCENDING
    await db.users.create_index("user_id", unique=True)
    await db.packs.create_index("pack_id", unique=True)
    await db.packs.create_index([("active", A), ("asset", A)])
    await db.orders.create_index("order_id", unique=True)
    await db.orders.create_index([("user_id", A), ("created_at", D)])
    await db.payments.create_index("track_id", unique=True)
    await db.transactions.create_index([("user_id", A), ("created_at", D)])
    await db.transfers.create_index("transfer_id", unique=True)
    await db.settings.create_index("key", unique=True)
    await db.admin_audit_logs.create_index([("created_at", D)])

    if await db.packs.count_documents({}) == 0:
        default_packs = [
            {
                "pack_id": "PACK-USDT-10K",
                "asset": "USDT",
                "network": "BSC",
                "quantity": "10000",
                "price_usd_cents": 1000000, # $10,000.00
                "inventory_available": 50,
                "provider": "default",
                "active": True,
                "title": "USDT Flash Pack 10K",
                "description": "10,000 USDT deliverable instantly on BNB Smart Chain.",
                "created_at": now()
            },
            {
                "pack_id": "PACK-BTC-01",
                "asset": "BTC",
                "network": "Bitcoin",
                "quantity": "0.1",
                "price_usd_cents": 6500000, # $65,000.00
                "inventory_available": 10,
                "provider": "default",
                "active": True,
                "title": "Bitcoin Flash Pack 0.1 BTC",
                "description": "0.1 BTC deliverable on Bitcoin Network.",
                "created_at": now()
            }
        ]
        await db.packs.insert_many(default_packs)


async def get_setting(key: str, default: Any = None) -> Any:
    doc = await db.settings.find_one({"key": key})
    return doc["value"] if doc and "value" in doc else default

async def set_setting(key: str, value: Any):
    await db.settings.update_one({"key": key}, {"$set": {"value": value, "updated_at": now()}}, upsert=True)

async def get_user_balance(user_id: int) -> int:
    u = await db.users.find_one({"user_id": user_id})
    return u.get("balance_usd_cents", 0) if u else 0


# ════════════════════════ KEYBOARDS & STYLING ════════════════════════

def btn(text: str, cb: Optional[str] = None, style: Optional[str] = None, url: Optional[str] = None):
    kw: dict = {"text": text}
    if url:
        kw["url"] = url
    else:
        kw["callback_data"] = cb
    if style:
        kw["style"] = style
    return InlineKeyboardButton(**kw)

def kb(rows: list) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=rows)

def back(cb: str = "home", text: str = "⬅️ Back to Menu"):
    return btn(text, cb)

def cancel_btn(cb: str = "home"):
    return btn("❌ Cancel", cb)

def main_menu(admin: bool, user_balance: int = 0) -> InlineKeyboardMarkup:
    rows = [
        [btn("🛍 Buy Crypto Packs", "store:list:0", style="success")],
        [btn("👛 My Wallet", "wallet_main", style="success"), btn("➕ Top Up Balance", "topup_prompt", style="success")],
        [btn("📦 My Orders", "orders:0", style="primary"), btn("💳 Transactions", "tx:0", style="primary")],
        [btn("🎧 Support", "support", style="success"), btn("📜 Terms & Conditions", "terms", style="primary")],
    ]
    if admin:
        rows.append([
            InlineKeyboardButton(text="🟢 Green Button", callback_data="btn_green", style="success"),
            InlineKeyboardButton(text="🔴 Red Button", callback_data="btn_red", style="danger")
        ])
        rows.append([
            InlineKeyboardButton(text="🔵 Blue Button", callback_data="btn_blue", style="primary")
        ])
        # Transparent button (without style parameter) + Admin Panel button
        rows.append([
            InlineKeyboardButton(text="⚪ Transparent Button", callback_data="btn_transparent"),
            btn("⚙️ Admin Panel", "adm:home", style="danger")
        ])
    return kb(rows)

async def show(ev, text: str, markup=None):
    if isinstance(ev, CallbackQuery):
        try:
            await ev.message.edit_text(text, reply_markup=markup, parse_mode=ParseMode.HTML)
        except TelegramBadRequest as e:
            if "not modified" not in str(e):
                await ev.message.answer(text, reply_markup=markup, parse_mode=ParseMode.HTML)
        try:
            await ev.answer()
        except TelegramAPIError:
            pass
    else:
        await ev.answer(text, reply_markup=markup, parse_mode=ParseMode.HTML)

async def alert(c: CallbackQuery, text: str):
    try:
        await c.answer(text, show_alert=True)
    except TelegramAPIError:
        pass


# ════════════════════════ MIDDLEWARES & FSM ════════════════════════

class UserMiddleware(BaseMiddleware):
    async def __call__(self, handler, event, data):
        u = data.get("event_from_user")
        if u is None or u.is_bot:
            return await handler(event, data)
        await db.users.update_one(
            {"user_id": u.id},
            {
                "$set": {"username": u.username, "first_name": u.first_name, "last_seen": now()},
                "$setOnInsert": {"created_at": now(), "balance_usd_cents": 0, "status": "active"}
            },
            upsert=True
        )
        return await handler(event, data)

class IsAdmin(BaseFilter):
    async def __call__(self, event) -> bool:
        u = getattr(event, "from_user", None)
        return bool(u and u.id in ADMIN_SET)

class TopUpStates(StatesGroup):
    amount = State()

class CheckoutStates(StatesGroup):
    wallet_address = State()
    confirm = State()

class AdminStates(StatesGroup):
    pack_title = State()
    pack_asset = State()
    pack_network = State()
    pack_qty = State()
    pack_price = State()
    pack_inventory = State()
    pack_desc = State()
    balance_user_id = State()
    balance_amount = State()
    balance_reason = State()


user_router = Router(name="user")
admin_router = Router(name="admin")
admin_router.message.filter(IsAdmin())
admin_router.callback_query.filter(IsAdmin())


# ════════════════════════ USER HANDLERS ════════════════════════

@user_router.message(CommandStart())
async def cmd_start(m: Message, state: FSMContext):
    await state.clear()
    user_bal = await get_user_balance(m.from_user.id)
    text = (
        f"🌟 <b>Welcome to {esc(config.STORE_NAME)}</b>\n\n"
        f"👛 Internal Store Balance: <b>{money(user_bal)}</b>\n\n"
        "Explore premium crypto packages, top up securely with OxaPay, and receive genuine on-chain assets."
    )
    await m.answer(text, parse_mode=ParseMode.HTML, reply_markup=main_menu(m.from_user.id in ADMIN_SET, user_bal))

@user_router.callback_query(F.data == "home")
@user_router.callback_query(F.data == "cancel")
async def cb_home(c: CallbackQuery, state: FSMContext):
    await state.clear()
    user_bal = await get_user_balance(c.from_user.id)
    text = (
        f"🌟 <b>Welcome to {esc(config.STORE_NAME)}</b>\n\n"
        f"👛 Internal Store Balance: <b>{money(user_bal)}</b>\n\n"
        "Select an option below:"
    )
    await show(c, text, main_menu(c.from_user.id in ADMIN_SET, user_bal))

@user_router.callback_query(F.data.in_({"btn_green", "btn_red", "btn_blue", "btn_transparent"}))
async def cb_colored_buttons(c: CallbackQuery):
    btn_names = {
        "btn_green": "Green Button (Success)",
        "btn_red": "Red Button (Danger)",
        "btn_blue": "Blue Button (Primary)",
        "btn_transparent": "Transparent Button (Default)"
    }
    await alert(c, f"You clicked the {btn_names.get(c.data, 'Button')}!")

@user_router.callback_query(F.data == "wallet_main")
async def cb_wallet_main(c: CallbackQuery):
    uid = c.from_user.id
    user = await db.users.find_one({"user_id": uid})
    bal = user.get("balance_usd_cents", 0) if user else 0

    pipeline = [
        {"$match": {"user_id": uid, "status": "completed"}},
        {"$group": {"_id": None, "total": {"$sum": "$expected_amount"}}}
    ]
    dep_res = await db.payments.aggregate(pipeline).to_list(1)
    total_dep = dep_res[0]["total"] if dep_res else 0.0

    spend_pipeline = [
        {"$match": {"user_id": uid, "status": "completed"}},
        {"$group": {"_id": None, "total": {"$sum": "$price_usd_cents"}}}
    ]
    spend_res = await db.orders.aggregate(spend_pipeline).to_list(1)
    total_spent = spend_res[0]["total"] if spend_res else 0

    recent_txs = await db.transactions.find({"user_id": uid}).sort("created_at", -1).limit(5).to_list(5)
    tx_lines = [f"• {tx['description']} ({money(tx['amount_usd_cents'])})" for tx in recent_txs]
    tx_str = "\n".join(tx_lines) if tx_lines else "No recent transactions."

    text = (
        f"👛 <b>My Wallet & Account Overview</b>\n\n"
        f"• Available Balance: <b>{money(bal)}</b>\n"
        f"• Total Deposited: <b>${total_dep:,.2f} USD</b>\n"
        f"• Total Spent: <b>{money(total_spent)}</b>\n\n"
        f"<b>Recent Transactions:</b>\n{tx_str}"
    )
    await show(c, text, kb([[btn("➕ Top Up Balance", "topup_prompt", style="success")], [back()]]))


# ── Top-Up Flow with OxaPay & Status Checking ──

@user_router.callback_query(F.data == "topup_prompt")
async def cb_topup_prompt(c: CallbackQuery, state: FSMContext):
    await state.clear()
    await state.set_state(TopUpStates.amount)
    text = (
        f"➕ <b>Top Up Balance via OxaPay</b>\n\n"
        f"Enter deposit amount in USD (Limits: ${config.MIN_TOPUP_USD:,.2f} - ${config.MAX_TOPUP_USD:,.2f}):"
    )
    await show(c, text, kb([[cancel_btn()]]))

@user_router.message(TopUpStates.amount, F.text)
async def msg_topup_amount(m: Message, state: FSMContext):
    cents = parse_money(m.text)
    if not cents or not (config.MIN_TOPUP_USD * 100 <= cents <= config.MAX_TOPUP_USD * 100):
        return await m.answer(
            f"❌ Invalid amount. Enter a value between ${config.MIN_TOPUP_USD:,.2f} and${config.MAX_TOPUP_USD:,.2f}:",
            reply_markup=kb([[cancel_btn()]])
        )
    await state.clear()
    usd_val = cents / 100.0
    uid = m.from_user.id
    order_ref = new_id("TOP")

    invoice, err = await create_oxapay_invoice(uid, usd_val, order_ref)
    if not invoice:
        return await m.answer(f"❌ Payment Provider Error: {err}", reply_markup=kb([[back()]]))

    track_id = str(invoice.get("track_id", ""))
    pay_url = invoice.get("payment_url", "")

    await db.payments.update_one(
        {"track_id": track_id},
        {"$set": {
            "track_id": track_id,
            "user_id": uid,
            "order_ref": order_ref,
            "expected_amount": usd_val,
            "expected_currency": "USD",
            "confirmed_amount": 0.0,
            "status": "pending",
            "webhook_verified": False,
            "created_at": now()
        }},
        upsert=True
    )

    text = (
        f"🧾 <b>OxaPay Invoice Created</b>\n\n"
        f"Amount: <b>${usd_val:,.2f} USD</b>\n"
        f"Reference: <code>{order_ref}</code>\n\n"
        "Click below to complete crypto payment, then tap <b>'I have paid / Check Status'</b> to verify:"
    )
    await m.answer(text, reply_markup=kb([
        [btn("💳 Pay via OxaPay", url=pay_url, style="success")],
        [btn("🔄 I have paid / Check Status", f"topup:check:{track_id}", style="primary")],
        [back()]
    ]), parse_mode=ParseMode.HTML)

@user_router.callback_query(F.data.startswith("topup:check:"))
async def cb_check_topup_status(c: CallbackQuery):
    track_id = c.data.split(":")[2]
    payment = await db.payments.find_one({"track_id": track_id})
    if not payment:
        return await alert(c, "Payment record not found.")

    if payment["status"] == "completed":
        return await alert(c, "✅ Payment already verified and credited to your balance!")

    data, err = await fetch_payment_info(track_id)
    if not data:
        return await alert(c, f"Could not fetch status: {err}")

    status = str(data.get("status", "")).lower()
    paid_amt = float(data.get("pay_amount") or data.get("amount") or 0)

    if status in ("paid", "completed", "complete"):
        await process_payment_notification_from_check(payment, paid_amt)
        user_bal = await get_user_balance(c.from_user.id)
        return await show(c, f"✅ <b>Payment Verified Successfully!</b>\n\nYour internal balance has been updated.\nCurrent Balance: <b>{money(user_bal)}</b>", main_menu(c.from_user.id in ADMIN_SET, user_bal))
    else:
        return await alert(c, f"⏳ Status: {status.title()}. Payment not yet fully confirmed on-chain. Please wait or complete payment.")

async def process_payment_notification_from_check(payment: dict, paid_amt: float):
    track_id = payment["track_id"]
    claimed = await db.payments.find_one_and_update(
        {"track_id": track_id, "status": "pending"},
        {"$set": {"status": "completed", "webhook_verified": True, "confirmed_at": now()}},
        return_document=ReturnDocument.BEFORE
    )
    if not claimed:
        return

    uid = payment["user_id"]
    amount_usd = payment["expected_amount"]
    cents = int(amount_usd * 100)

    await db.users.update_one({"user_id": uid}, {"$inc": {"balance_usd_cents": cents}})
    await db.transactions.insert_one({
        "transaction_id": new_id("TX"),
        "user_id": uid,
        "type": "deposit",
        "amount_usd_cents": cents,
        "reference_id": track_id,
        "status": "completed",
        "description": f"OxaPay Verified Deposit (${amount_usd:,.2f} USD)",
        "created_at": now()
    })


# ── Store & Purchase Flow ──

@user_router.callback_query(F.data.startswith("store:list:"))
async def cb_store_list(c: CallbackQuery):
    page = to_int(c.data.split(":")[2])
    total = await db.packs.count_documents({"active": True})
    pages = max(1, -(-total // PAGE_SIZE))
    page = min(max(page, 0), pages - 1)
    packs = await db.packs.find({"active": True}).skip(page * PAGE_SIZE).limit(PAGE_SIZE).to_list(PAGE_SIZE)

    if not packs:
        return await show(c, "🛍 <b>Crypto Pack Store</b>\n\nNo packages currently available.", kb([[back()]]))

    rows = [[btn(f"{p['title']} — {money(p['price_usd_cents'])}", f"pack:view:{p['pack_id']}", style="success")] for p in packs]
    
    pager_row = []
    if page > 0:
        pager_row.append(btn("◀ Prev", f"store:list:{page-1}"))
    pager_row.append(btn(f"Page {page+1}/{pages}", "noop"))
    if page < pages - 1:
        pager_row.append(btn("Next ▶️", f"store:list:{page+1}"))
    if pager_row:
        rows.append(pager_row)
    rows.append([back()])

    await show(c, "🛍 <b>Crypto Pack Store</b>\n\nSelect a verified package to inspect details and purchase:", kb(rows))

@user_router.callback_query(F.data.startswith("pack:view:"))
async def cb_pack_view(c: CallbackQuery):
    pack_id = c.data.split(":")[2]
    p = await db.packs.find_one({"pack_id": pack_id, "active": True})
    if not p:
        return await alert(c, "Package not found or inactive.")

    text = (
        f"📦 <b>{esc(p['title'])}</b>\n\n"
        f"• Asset: <b>{esc(p['asset'])}</b>\n"
        f"• Quantity: <b>{esc(p['quantity'])} {esc(p['asset'])}</b>\n"
        f"• Network: <b>{esc(p['network'])}</b>\n"
        f"• Price: <b>{money(p['price_usd_cents'])}</b>\n"
        f"• Inventory Available: <b>{p['inventory_available']} units</b>\n"
        f"• Estimated Processing: <b>Instant on-chain delivery</b>\n\n"
        f"<i>{esc(p['description'])}</i>\n\n"
        "✅ Delivery involves genuine, transferable on-chain assets."
    )
    rows = [
        [btn("🛒 Proceed to Purchase", f"pack:buy:{pack_id}", style="success")],
        [back("store:list:0", "⬅️ Back to Store")]
    ]
    await show(c, text, kb(rows))

@user_router.callback_query(F.data.startswith("pack:buy:"))
async def cb_pack_buy(c: CallbackQuery, state: FSMContext):
    pack_id = c.data.split(":")[2]
    p = await db.packs.find_one({"pack_id": pack_id, "active": True})
    if not p or p.get("inventory_available", 0) <= 0:
        return await alert(c, "Sorry, this package is out of stock.")

    uid = c.from_user.id
    user_bal = await get_user_balance(uid)
    price = p["price_usd_cents"]

    if user_bal < price:
        return await show(
            c,
            f"❌ <b>Insufficient Store Balance</b>\n\n"
            f"Required: <b>{money(price)}</b>\n"
            f"Your Balance: <b>{money(user_bal)}</b>\n\n"
            f"Please top up your balance first to purchase this package.",
            kb([[btn("➕ Top Up Balance", "topup_prompt", style="success")], [back(f"pack:view:{pack_id}", "⬅️ Back to Package")]])
        )

    await state.update_data(pack_id=pack_id)
    await state.set_state(CheckoutStates.wallet_address)

    text = (
        f"🎯 <b>Destination Wallet Address Collection</b>\n\n"
        f"Selected Package: <b>{esc(p['title'])}</b>\n"
        f"Network: <b>{esc(p['network'])}</b>\n"
        f"Asset: <b>{esc(p['asset'])}</b>\n\n"
        f"Please send the valid <b>{esc(p['network'])}</b> Trust Wallet or destination address where assets should be delivered.\n\n"
        "⚠️ <i>Ensure the address supports this network. Incorrect networks result in permanent loss. Never submit private keys or seed phrases.</i>"
    )
    await show(c, text, kb([[cancel_btn()]]))

@user_router.message(CheckoutStates.wallet_address, F.text)
async def msg_wallet_address(m: Message, state: FSMContext):
    addr = m.text.strip()
    if len(addr) < 15 or len(addr) > 120 or " " in addr:
        return await m.answer("❌ Invalid wallet address format. Please check and re-enter:", reply_markup=kb([[cancel_btn()]]))

    await state.update_data(destination_address=addr)
    data = await state.get_data()
    p = await db.packs.find_one({"pack_id": data["pack_id"]})

    await state.set_state(CheckoutStates.confirm)

    text = (
        f"🔒 <b>Confirm Order Checkout</b>\n\n"
        f"• Package: <b>{esc(p['title'])}</b>\n"
        f"• Quantity: <b>{esc(p['quantity'])} {esc(p['asset'])}</b>\n"
        f"• Network: <b>{esc(p['network'])}</b>\n"
        f"• Price: <b>{money(p['price_usd_cents'])}</b>\n"
        f"• Destination Address:\n<code>{esc(addr)}</code>\n\n"
        "Tap confirm below to authorize atomic deduction and initiate transfer."
    )
    await m.answer(text, parse_mode=ParseMode.HTML, reply_markup=kb([
        [btn("✅ Confirm & Send to Wallet", "checkout_execute", style="success")],
        [cancel_btn()]
    ]))

@user_router.callback_query(CheckoutStates.confirm, F.data == "checkout_execute")
async def cb_checkout_execute(c: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    await state.clear()
    uid = c.from_user.id
    pack_id = data["pack_id"]
    dest_addr = data["destination_address"]

    p = await db.packs.find_one({"pack_id": pack_id, "active": True})
    if not p or p.get("inventory_available", 0) <= 0:
        return await show(c, "❌ Package is no longer available.", kb([[back()]]))

    price = p["price_usd_cents"]

    user_upd = await db.users.find_one_and_update(
        {"user_id": uid, "balance_usd_cents": {"$gte": price}},
        {"$inc": {"balance_usd_cents": -price}},
        return_document=ReturnDocument.AFTER
    )
    if not user_upd:
        return await show(c, "❌ Transaction failed: Insufficient balance.", kb([[back()]]))

    pack_upd = await db.packs.find_one_and_update(
        {"pack_id": pack_id, "inventory_available": {"$gt": 0}},
        {"$inc": {"inventory_available": -1}},
        return_document=ReturnDocument.AFTER
    )
    if not pack_upd:
        await db.users.update_one({"user_id": uid}, {"$inc": {"balance_usd_cents": price}})
        return await show(c, "❌ Out of stock during checkout. Balance refunded.", kb([[back()]]))

    order_id = new_id("ORD")
    t = now()

    order_doc = {
        "order_id": order_id,
        "user_id": uid,
        "pack_id": pack_id,
        "asset": p["asset"],
        "network": p["network"],
        "quantity": p["quantity"],
        "price_usd_cents": price,
        "destination_address": dest_addr,
        "status": "processing",
        "transfer_tx_hash": None,
        "created_at": t,
        "updated_at": t
    }
    await db.orders.insert_one(order_doc)

    success, tx_hash, err = await execute_blockchain_transfer(p["network"], p["asset"], p["quantity"], dest_addr)

    if success:
        await db.orders.update_one(
            {"order_id": order_id},
            {"$set": {"status": "completed", "transfer_tx_hash": tx_hash, "updated_at": now()}}
        )
        await db.transactions.insert_one({
            "transaction_id": new_id("TX"),
            "user_id": uid,
            "type": "purchase",
            "amount_usd_cents": price,
            "reference_id": order_id,
            "status": "completed",
            "description": f"Purchased {p['title']}",
            "created_at": now()
        })
        text = (
            f"✅ <b>Order Successfully Delivered to Wallet!</b>\n\n"
            f"Order ID: <code>{order_id}</code>\n"
            f"Asset: <b>{esc(p['quantity'])} {esc(p['asset'])}</b>\n"
            f"Network: <b>{esc(p['network'])}</b>\n"
            f"Destination: <code>{esc(dest_addr)}</code>\n"
            f"Tx Hash: <code>{esc(tx_hash)}</code>"
        )
    else:
        await db.orders.update_one(
            {"order_id": order_id},
            {"$set": {"status": "pending_delivery_reconciliation", "updated_at": now()}}
        )
        text = (
            f"⚠️ <b>Order Placed, Transfer Pending Reconciliation</b>\n\n"
            f"Order ID: <code>{order_id}</code>\n"
            f"Payment verified, but automated transfer provider reported: {esc(err)}\n"
            "Our admin team has been alerted to review and push your on-chain assets manually."
        )
        for aid in ADMIN_SET:
            try:
                await bot_ref.send_message(
                    aid,
                    f"🚨 <b>Transfer Failure Alert</b>\nOrder: <code>{order_id}</code>\nUser: <code>{uid}</code>\nError: {esc(err)}",
                    parse_mode=ParseMode.HTML
                )
            except TelegramAPIError:
                pass

    await show(c, text, kb([[back()]]))

@user_router.callback_query(F.data.startswith("orders:"))
async def cb_orders(c: CallbackQuery):
    page = to_int(c.data.split(":")[1])
    uid = c.from_user.id
    total = await db.orders.count_documents({"user_id": uid})
    pages = max(1, -(-total // PAGE_SIZE))
    page = min(max(page, 0), pages - 1)
    orders = await db.orders.find({"user_id": uid}).sort("created_at", -1).skip(page * PAGE_SIZE).limit(PAGE_SIZE).to_list(PAGE_SIZE)

    if not total:
        return await show(c, "📦 <b>My Orders</b>\n\nNo orders found.", kb([[back()]]))

    rows = [[btn(f"{o['order_id']} • {money(o['price_usd_cents'])} • {o['status'].title()}", f"order:view:{o['order_id']}", style="primary")] for o in orders]
    rows.append([back()])
    await show(c, f"📦 <b>My Orders</b> ({total})", kb(rows))

@user_router.callback_query(F.data.startswith("order:view:"))
async def cb_order_view(c: CallbackQuery):
    order_id = c.data.split(":")[2]
    o = await db.orders.find_one({"order_id": order_id, "user_id": c.from_user.id})
    if not o:
        return await alert(c, "Order not found.")

    text = (
        f"📦 <b>Order Details: {o['order_id']}</b>\n\n"
        f"• Status: <b>{o['status'].title()}</b>\n"
        f"• Asset Quantity: <b>{esc(o['quantity'])} {esc(o['asset'])}</b>\n"
        f"• Network: <b>{esc(o['network'])}</b>\n"
        f"• Price Paid: <b>{money(o['price_usd_cents'])}</b>\n"
        f"• Destination: <code>{esc(o['destination_address'])}</code>\n"
        f"• Tx Hash: <code>{esc(o.get('transfer_tx_hash') or 'Pending')}</code>\n"
        f"• Date: {o['created_at'].strftime('%Y-%m-%d %H:%M UTC')}"
    )
    await show(c, text, kb([[back("orders:0", "⬅️ Back to Orders")]]))

@user_router.callback_query(F.data.startswith("tx:"))
async def cb_transactions(c: CallbackQuery):
    page = to_int(c.data.split(":")[1])
    uid = c.from_user.id
    total = await db.transactions.count_documents({"user_id": uid})
    txs = await db.transactions.find({"user_id": uid}).sort("created_at", -1).skip(page * PAGE_SIZE).limit(PAGE_SIZE).to_list(PAGE_SIZE)
    rows = [[btn(f"{t['description']} — {money(t['amount_usd_cents'])}", "noop", style="primary")] for t in txs]
    rows.append([back()])
    await show(c, f"💳 <b>Transaction History</b> ({total})", kb(rows))

@user_router.callback_query(F.data == "support")
async def cb_support(c: CallbackQuery):
    text = f"🎧 <b>Customer Support</b>\n\nReach out to our support team at @{esc(config.SUPPORT_USERNAME)} for prompt assistance."
    await show(c, text, kb([[btn("💬 Open Support Chat", url=f"https://t.me/{config.SUPPORT_USERNAME.lstrip('@')}", style="success")], [back()]]))

@user_router.callback_query(F.data == "terms")
async def cb_terms(c: CallbackQuery):
    text = f"📜 <b>Terms & Conditions</b>\n\n{esc(config.TERMS_TEXT)}"
    await show(c, text, kb([[back()]]))

@user_router.callback_query(F.data == "noop")
async def cb_noop(c: CallbackQuery):
    await c.answer()


# ════════════════════════ ADMIN PANEL & PACK CREATOR WIZARD ════════════════════════

def admin_menu() -> InlineKeyboardMarkup:
    return kb([
        [btn("➕ Add Crypto Pack", "adm:pack_add", style="success"), btn("📦 Manage Packs", "adm:packs:0", style="primary")],
        [btn("📋 Active Orders", "adm:orders:processing", style="success"), btn("✅ Completed Orders", "adm:orders:completed", style="primary")],
        [btn("⚖️ Adjust User Balance", "adm:bal_prompt", style="success"), btn("📊 Revenue & Stats", "adm:stats", style="primary")],
        [back("home", "🏠 User Main Menu")],
    ])

# Removed command triggers (`/admin` and `/addpack`) — Admin panel is now fully button-driven.

@admin_router.callback_query(F.data == "adm:home")
async def cb_admin_home(c: CallbackQuery, state: FSMContext):
    await state.clear()
    await show(c, "⚙️ <b>Admin Control Panel</b>", admin_menu())

# ── Pack Creator FSM Wizard ──

@admin_router.callback_query(F.data == "adm:pack_add")
async def cb_admin_pack_add(c: CallbackQuery, state: FSMContext):
    await state.clear()
    await state.set_state(AdminStates.pack_title)
    await show(c, "➕ <b>Add New Crypto Pack</b>\n\nEnter package title (e.g., <i>Bitcoin Mega Pack 0.5 BTC</i>):", kb([[cancel_btn("adm:home")]]))

@admin_router.message(AdminStates.pack_title, F.text)
async def msg_pack_title(m: Message, state: FSMContext):
    await state.update_data(pack_title=m.text.strip())
    await state.set_state(AdminStates.pack_asset)
    await m.answer("Enter cryptocurrency asset symbol (e.g., <code>BTC</code>, <code>ETH</code>, <code>USDT</code>, <code>SOL</code>, <code>LTC</code>):", parse_mode=ParseMode.HTML, reply_markup=kb([[cancel_btn("adm:home")]]))

@admin_router.message(AdminStates.pack_asset, F.text)
async def msg_pack_asset(m: Message, state: FSMContext):
    await state.update_data(pack_asset=m.text.strip().upper())
    await state.set_state(AdminStates.pack_network)
    await m.answer("Enter blockchain network (e.g., <code>Bitcoin</code>, <code>Ethereum</code>, <code>BSC</code>, <code>Polygon</code>, <code>Solana</code>):", parse_mode=ParseMode.HTML, reply_markup=kb([[cancel_btn("adm:home")]]))

@admin_router.message(AdminStates.pack_network, F.text)
async def msg_pack_network(m: Message, state: FSMContext):
    await state.update_data(pack_network=m.text.strip())
    await state.set_state(AdminStates.pack_qty)
    await m.answer("Enter asset quantity to deliver per purchase (e.g., <code>0.5</code> or <code>10000</code>):", parse_mode=ParseMode.HTML, reply_markup=kb([[cancel_btn("adm:home")]]))

@admin_router.message(AdminStates.pack_qty, F.text)
async def msg_pack_qty(m: Message, state: FSMContext):
    await state.update_data(pack_qty=m.text.strip())
    await state.set_state(AdminStates.pack_price)
    await m.answer("Enter price of this pack in USD dollars (e.g., <code>1500.00</code>):", parse_mode=ParseMode.HTML, reply_markup=kb([[cancel_btn("adm:home")]]))

@admin_router.message(AdminStates.pack_price, F.text)
async def msg_pack_price(m: Message, state: FSMContext):
    cents = parse_money(m.text)
    if not cents:
        return await m.answer("❌ Invalid price amount. Re-enter USD price:", reply_markup=kb([[cancel_btn("adm:home")]]))
    await state.update_data(price_cents=cents)
    await state.set_state(AdminStates.pack_inventory)
    await m.answer("Enter initial inventory stock count (e.g., <code>25</code>):", reply_markup=kb([[cancel_btn("adm:home")]]))

@admin_router.message(AdminStates.pack_inventory, F.text)
async def msg_pack_inventory(m: Message, state: FSMContext):
    inv = to_int(m.text.strip())
    if inv <= 0:
        return await m.answer("❌ Invalid stock count. Enter a positive number:", reply_markup=kb([[cancel_btn("adm:home")]]))
    await state.update_data(inventory=inv)
    await state.set_state(AdminStates.pack_desc)
    await m.answer("Enter marketing description or instructions for the buyer:", reply_markup=kb([[cancel_btn("adm:home")]]))

@admin_router.message(AdminStates.pack_desc, F.text)
async def msg_pack_desc(m: Message, state: FSMContext):
    desc = m.text.strip()
    data = await state.get_data()
    await state.clear()

    pack_id = f"PACK-{data['pack_asset']}-{secrets.token_hex(3).upper()}"
    pack_doc = {
        "pack_id": pack_id,
        "title": data["pack_title"],
        "asset": data["pack_asset"],
        "network": data["pack_network"],
        "quantity": data["pack_qty"],
        "price_usd_cents": data["price_cents"],
        "inventory_available": data["inventory"],
        "description": desc,
        "active": True,
        "created_at": now()
    }
    await db.packs.insert_one(pack_doc)

    await m.answer(
        f"✅ <b>Crypto Package Created Successfully!</b>\n\n"
        f"• ID: <code>{pack_id}</code>\n"
        f"• Title: <b>{esc(data['pack_title'])}</b>\n"
        f"• Asset: <b>{esc(data['pack_qty'])} {esc(data['pack_asset'])}</b> on {esc(data['pack_network'])}\n"
        f"• Price: <b>{money(data['price_cents'])}</b>\n"
        f"• Stock: <b>{data['inventory']} units</b>",
        parse_mode=ParseMode.HTML,
        reply_markup=kb([[btn("⚙️ Admin Menu", "adm:home", style="primary")]])
    )


# ── Admin Stats ──

@admin_router.callback_query(F.data == "adm:stats")
async def cb_admin_stats(c: CallbackQuery):
    users_count = await db.users.count_documents({})
    orders_count = await db.orders.count_documents({})
    pipeline = [{"$match": {"status": "completed"}}, {"$group": {"_id": None, "total": {"$sum": "$price_usd_cents"}}}]
    rev_res = await db.orders.aggregate(pipeline).to_list(1)
    rev_cents = rev_res[0]["total"] if rev_res else 0

    net_io = psutil.net_io_counters()
    text = (
        f"📊 <b>System Statistics & Metrics</b>\n\n"
        f"• Registered Users: <b>{users_count}</b>\n"
        f"• Total Orders: <b>{orders_count}</b>\n"
        f"• Total Revenue: <b>{money(rev_cents)}</b>\n"
        f"• Network I/O Sent: <b>{net_io.bytes_sent / 1024 / 1024:.2f} MB</b>\n"
        f"• Network I/O Recv: <b>{net_io.bytes_recv / 1024 / 1024:.2f} MB</b>"
    )
    await show(c, text, kb([[back("adm:home")]]))


# ── Balance Adjustment ──

@admin_router.callback_query(F.data == "adm:bal_prompt")
async def cb_bal_prompt(c: CallbackQuery, state: FSMContext):
    await state.set_state(AdminStates.balance_user_id)
    await show(c, "⚖️ Enter target Telegram User ID for balance adjustment:", kb([[cancel_btn("adm:home")]]))

@admin_router.message(AdminStates.balance_user_id, F.text)
async def msg_adm_bal_uid(m: Message, state: FSMContext):
    uid = to_int(m.text.strip())
    if uid <= 0:
        return await m.answer("❌ Invalid User ID. Enter a numeric ID:", reply_markup=kb([[cancel_btn("adm:home")]]))
    await state.update_data(target_user_id=uid)
    await state.set_state(AdminStates.balance_amount)
    await m.answer("Enter USD amount to add/subtract (e.g. <code>50</code> or <code>-10.50</code>):", parse_mode=ParseMode.HTML, reply_markup=kb([[cancel_btn("adm:home")]]))

@admin_router.message(AdminStates.balance_amount, F.text)
async def msg_adm_bal_amount(m: Message, state: FSMContext):
    cents = parse_money(m.text.strip().replace("+", ""))
    if not cents:
        return await m.answer("❌ Invalid amount format. Re-enter:", reply_markup=kb([[cancel_btn("adm:home")]]))
    if "-" in m.text:
        cents = -cents
    await state.update_data(amount_cents=cents)
    await state.set_state(AdminStates.balance_reason)
    await m.answer("Provide an audit reason for this balance adjustment:", reply_markup=kb([[cancel_btn("adm:home")]]))

@admin_router.message(AdminStates.balance_reason, F.text)
async def msg_adm_bal_reason(m: Message, state: FSMContext):
    reason = m.text.strip()
    data = await state.get_data()
    await state.clear()
    uid = data["target_user_id"]
    cents = data["amount_cents"]

    user_upd = await db.users.find_one_and_update(
        {"user_id": uid},
        {"$inc": {"balance_usd_cents": cents}},
        upsert=True,
        return_document=ReturnDocument.AFTER
    )
    new_bal = user_upd.get("balance_usd_cents", 0) if user_upd else cents

    await db.admin_audit_logs.insert_one({
        "admin_id": m.from_user.id,
        "action": "balance_adjustment",
        "target_user_id": uid,
        "amount_cents": cents,
        "reason": reason,
        "created_at": now()
    })

    await m.answer(f"✅ Balance adjusted successfully for user {uid}. New balance: {money(new_bal)}", reply_markup=kb([[back("adm:home")]]))
    try:
        await bot_ref.send_message(uid, f"⚖️ Your store balance was adjusted by admin.\nAmount: {money(cents)}\nReason: {esc(reason)}")
    except TelegramAPIError:
        pass


# ════════════════════════ OXAPAY WEBHOOK SERVER ════════════════════════

def verify_oxapay_hmac(body_bytes: bytes, hmac_header: Optional[str]) -> bool:
    if not hmac_header or not OXAPAY_API_KEY:
        return False
    calc = hmac.new(OXAPAY_API_KEY.encode("utf-8"), body_bytes, hashlib.sha512).hexdigest()
    return hmac.compare_digest(calc, hmac_header.strip())

async def process_payment_notification(data: dict):
    track_id = str(data.get("track_id") or data.get("trackId") or "")
    status = str(data.get("status", "")).strip().lower()
    if not track_id or status not in ("paid", "completed", "complete"):
        return

    payment = await db.payments.find_one({"track_id": track_id, "status": "pending"})
    if not payment:
        return

    claimed = await db.payments.find_one_and_update(
        {"track_id": track_id, "status": "pending"},
        {"$set": {"status": "completed", "webhook_verified": True, "confirmed_at": now()}},
        return_document=ReturnDocument.BEFORE
    )
    if not claimed:
        return

    uid = payment["user_id"]
    amount_usd = payment["expected_amount"]
    cents = int(amount_usd * 100)

    user_doc = await db.users.find_one_and_update(
        {"user_id": uid},
        {"$inc": {"balance_usd_cents": cents}},
        return_document=ReturnDocument.AFTER
    )
    new_bal = user_doc.get("balance_usd_cents", 0) if user_doc else cents

    await db.transactions.insert_one({
        "transaction_id": new_id("TX"),
        "user_id": uid,
        "type": "deposit",
        "amount_usd_cents": cents,
        "reference_id": track_id,
        "status": "completed",
        "description": f"OxaPay Deposit (${amount_usd:,.2f} USD)",
        "created_at": now()
    })

    try:
        await bot_ref.send_message(
            uid,
            f"✅ <b>Payment Confirmed & Wallet Credited!</b>\n\n"
            f"Added: <b>${amount_usd:,.2f} USD</b>\n"
            f"New Balance: <b>{money(new_bal)}</b>",
            parse_mode=ParseMode.HTML
        )
    except TelegramAPIError:
        pass

async def handle_oxapay_webhook(request: web.Request) -> web.Response:
    try:
        raw_body = await request.read()
        if not verify_oxapay_hmac(raw_body, request.headers.get("HMAC")):
            return web.json_response({"status": "unauthorized"}, status=401)
        data = json.loads(raw_body.decode("utf-8"))
        await process_payment_notification(data)
        return web.json_response({"status": "ok"})
    except Exception as e:
        log.exception("Webhook error: %s", e)
        return web.json_response({"status": "error"}, status=400)


# ════════════════════════ APPLICATION ENTRYPOINT ════════════════════════

async def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    await init_db()

    bot = Bot(config.BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML, link_preview_is_disabled=True))
    global bot_ref
    bot_ref = bot
    dp = Dispatcher(storage=MemoryStorage())

    dp.message.outer_middleware(UserMiddleware())
    dp.callback_query.outer_middleware(UserMiddleware())

    dp.include_router(admin_router)
    dp.include_router(user_router)

    app = web.Application()
    app.router.add_post("/oxapay/callback", handle_oxapay_webhook)
    app.router.add_get("/", lambda r: web.Response(text="Crypto Store Bot Running"))
    runner = web.AppRunner(app)
    await runner.setup()

    port = int(os.environ.get("PORT") or 8080)
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()

    try:
        await bot.delete_webhook(drop_pending_updates=True)
        log.info("Starting Telegram bot polling and OxaPay webhook server on port %s...", port)
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        await bot.session.close()
        mongo.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass
