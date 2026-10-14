import os
import sys

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_IDS_RAW = os.getenv("ADMIN_IDS", "")
ADMIN_IDS = [int(x.strip()) for x in ADMIN_IDS_RAW.split(",") if x.strip().isdigit()]

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
DATABASE_NAME = os.getenv("DATABASE_NAME", "crypto_store_db")

OXAPAY_API_KEY = os.getenv("OXAPAY_API_KEY", "")
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "")
RETURN_URL = os.getenv("RETURN_URL", "")
SUPPORT_USERNAME = os.getenv("SUPPORT_USERNAME", "support")
STORE_NAME = os.getenv("STORE_NAME", "Premium Crypto Store")
SETTINGS_ENCRYPTION_KEY = os.getenv("SETTINGS_ENCRYPTION_KEY", "")

# Owner Trust Wallet configuration for automated post-verification routing
OWNER_WALLET_ADDRESS = os.getenv("OWNER_WALLET_ADDRESS", "0xYourOwnerTrustWalletAddressHere")

TRUST_WALLET_NETWORK = os.getenv("TRUST_WALLET_NETWORK", "BSC")
TRANSFER_PROVIDER = os.getenv("TRANSFER_PROVIDER", "mock")
TRANSFER_PROVIDER_API_KEY = os.getenv("TRANSFER_PROVIDER_API_KEY", "")
TRANSFER_PROVIDER_SECRET = os.getenv("TRANSFER_PROVIDER_SECRET", "")
TRANSFER_PROVIDER_BASE_URL = os.getenv("TRANSFER_PROVIDER_BASE_URL", "")

MIN_TOPUP_USD = float(os.getenv("MIN_TOPUP_USD", "5.0"))
MAX_TOPUP_USD = float(os.getenv("MAX_TOPUP_USD", "10000.0"))
OXAPAY_SANDBOX = os.getenv("OXAPAY_SANDBOX", "false").lower() in ("1", "true", "yes")
TERMS_TEXT = os.getenv(
    "TERMS_TEXT",
    "All sales are final. Genuine on-chain asset delivery is processed securely on supported networks. "
    "Please verify your destination address carefully before confirming transfers."
)

# Custom Telegram Premium Emoji IDs
STORE_EMOJI_ID = os.getenv("STORE_EMOJI_ID", "5451937962629544243")
BOX_EMOJI_ID = os.getenv("BOX_EMOJI_ID", "5355193051193059834")
TERMS_EMOJI_ID = os.getenv("TERMS_EMOJI_ID", "5258500400918587241")
TRANSACTION_EMOJI_ID = os.getenv("TRANSACTION_EMOJI_ID", "5258500400918587241")
USDT_EMOJI_ID = os.getenv("USDT_EMOJI_ID", "6035288280562404083")
BTC_EMOJI_ID = os.getenv("BTC_EMOJI_ID", "5465465383035083768")
ETH_EMOJI_ID = os.getenv("ETH_EMOJI_ID", "5830292326202741807")
LTC_EMOJI_ID = os.getenv("LTC_EMOJI_ID", "5116097208281727613")
SOL_EMOJI_ID = os.getenv("SOL_EMOJI_ID", "5449658099499547173")
WALLET_EMOJI_ID = os.getenv("WALLET_EMOJI_ID", "5256186332669035163")
SUPPORT_EMOJI_ID = os.getenv("SUPPORT_EMOJI_ID", "5395804191769763641")
GIFTS_EMOJI_ID = os.getenv("GIFTS_EMOJI_ID", "536832417067120228")

# Startup Safety Checks
if not BOT_TOKEN:
    print("CRITICAL ERROR: BOT_TOKEN environment variable is missing.", file=sys.stderr)
    sys.exit(1)
if not MONGO_URI:
    print("CRITICAL ERROR: MONGO_URI environment variable is missing.", file=sys.stderr)
    sys.exit(1)
