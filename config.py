import os
from dotenv import load_dotenv
load_dotenv()

# ══════════════════════════════════════════════════════════════════════════════
# SAARE ON/OFF SWITCHES — EK HI JAGAH (owner ke liye)
# Har switch Render ke Environment me isi naam se badla ja sakta hai.
# Kuch set na karo toh neeche wala DEFAULT chalega — aur defaults hi sahi
# setting hain. File-file me dhundne ki zaroorat nahi: sab yahi hai.
# ══════════════════════════════════════════════════════════════════════════════
def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")

# Product Router — pin par sahi product ka link lagana (match na ho toh
# pin bina link ke jata hai, zabardasti link NAHI lagta)
PRODUCT_ROUTER_ENABLED = _env_bool("PRODUCT_ROUTER_ENABLED", True)   # pura router ON/OFF
ROUTER_LLM_ENABLED     = _env_bool("ROUTER_LLM_ENABLED", True)       # ambiguous pin par AI judge ON/OFF
PRODUCT_ROUTER_UTM     = _env_bool("PRODUCT_ROUTER_UTM", False)      # link ke peeche ?utm=... jodna (default OFF = saaf link)
PRODUCT_ROUTER_THRESHOLD = float(os.getenv("PRODUCT_ROUTER_THRESHOLD", "0.70"))  # judge ko kitna pakka hona chahiye (0.70 = 70%)
ROUTER_JUDGE_MODEL     = os.getenv("ROUTER_JUDGE_MODEL", "openai/gpt-oss-20b")   # judge wala model

# Image Mood Engine — har image ke prompt ko mood/story se enhance karna
IMAGE_MOOD_ENGINE = _env_bool("IMAGE_MOOD_ENGINE", True)             # ON = har baar naya mood/look; OFF = purana v4 style

GROQ_API_KEY          = os.getenv("GROQ_API_KEY")
CEREBRAS_API_KEY      = os.getenv("CEREBRAS_API_KEY")
OPENROUTER_API_KEY    = os.getenv("OPENROUTER_API_KEY")
IMGBB_API_KEY         = os.getenv("IMGBB_API_KEY")
GROQ_MODEL            = "qwen/qwen3.8-27b"
GROQ_VISION_MODEL     = "meta-llama/llama-4-scout-17b-16e-instruct"
CEREBRAS_VISION_MODEL = "qwen-3-235b-a22b-instruct-2507"
VISION_RETRY_DELAY    = 10   # seconds between fallback attempts
CEREBRAS_MODEL        = "llama3.1-8b"
CEREBRAS_CMO_MODEL    = "llama3.1-8b"
GEMINI_CMO_MODEL      = "gemini-3.8-flash"
GEMINI_CHAT_MODEL     = "gemini-3.8-flash"
OPENROUTER_IMAGE_MODEL = "black-forest-labs/flux.2-pro"
POLLINATIONS_MODEL    = "flux"
GOOGLE_CREDS_JSON     = os.getenv("GOOGLE_CREDS_JSON")
SPREADSHEET_ID        = os.getenv("SPREADSHEET_ID")
SHEET_NAME            = "Approved Deals"
RAPIDAPI_KEY          = os.getenv("RAPIDAPI_KEY")
RAPIDAPI_KEY2         = os.getenv("RAPIDAPI_KEY2")
TAVILY_API_KEY        = os.getenv("TAVILY_API_KEY")
AMAZON_STORE_ID       = "swiftmart0008-20"
MIN_GRAVITY           = 50
DAILY_POST_LIMIT      = 2   
LOW_STOCK_THRESHOLD   = 5
MAX_PRODUCTS_TO_FETCH = int(os.getenv("MAX_PRODUCTS_TO_FETCH", "20"))
CLOUDFLARE_ACCOUNT_ID = os.getenv("CLOUDFLARE_ACCOUNT_ID")
CLOUDFLARE_API_TOKEN  = os.getenv("CLOUDFLARE_API_TOKEN")
CLOUDFLARE_IMAGE_MODEL = "@cf/black-forest-labs/flux-2-klein-4b"
# ── Mastermind CEO System ─────────────────────────────────────────────────────
GEMINI_API_KEY          = os.getenv("GEMINI_API_KEY")
ANALYTICS_SHEET_ACC1    = "Analytics_Log"    # Account 1 — HomeDecor
ANALYTICS_SHEET_ACC2    = "Analytics_logs2"  # Account 2 — Tech
GEMINI_API_KEY_2        = os.getenv("GEMINI_API_KEY_2")
# Image generation model — update to gemini-2.5-flash-preview-image-generation
# when it becomes GA.  Tenacity fallback protects the pipeline if unavailable.
GEMINI_IMAGE_MODEL      = os.getenv(
    "GEMINI_IMAGE_MODEL",
    "gemini-3.8-flash",
)

# (Blog/Firebase config yaha se HATA diya gaya hai — blog pipeline band hai.
#  Do purane tools modules apni env khud padhte hain; pin pipeline me blog
#  ka koi role nahi bacha.)

# ── Pinteresto V4 — Product Router + Mastermind thinking ─────────────────────
# Router destinations (mastermind/product_router.py). Env-overridable so a
# product can be swapped without a code change.
PRODUCT_GARDEN_URL = os.getenv("PRODUCT_GARDEN_URL", "https://cozycanvasstudio.gumroad.com/l/nmkawd")
PRODUCT_DECOR_URL  = os.getenv("PRODUCT_DECOR_URL",  "https://cozycanvasstudio.gumroad.com/l/ictsf")
# Mastermind "thinking" fallback: Groq's reasoning model (free tier
# 30 RPM / 1,000 RPD per official rate-limit docs, Oct 2026). CMO chain:
# Gemini -> ye thinking model -> hardcoded fallback. (Qwen CMO me nahi
# hai — woh sirf task execution me hai.)
GROQ_THINKING_MODEL = os.getenv("GROQ_THINKING_MODEL", "openai/gpt-oss-120b")
# Image fallback (tools/image_creator.py) — Replicate FLUX schnell,
# $0.003/image; layer stays OFF until this token is set.
REPLICATE_API_TOKEN = os.getenv("REPLICATE_API_TOKEN")

PINTEREST_ACCOUNTS = [
    {
        "name":        "Account1_HomeDecor",
        "webhook_url": os.getenv("MAKE_WEBHOOK_URL"),
        "niche":       "home",
        "boards": {
            "home":     "909445787192886518",  
            "kitchen":  "909445787192891736",  
            "cozy":     "909445787192891741",  
            "gadgets":  "909445787192891742",  
            "organize": "909445787192891737",  
            "default":  "909445787192886518",
        }
    },
    {
        "name":        "Account2_Tech",
        "webhook_url": os.getenv("MAKE_WEBHOOK_URL_2"),
        "niche":       "tech",
        "boards": {
            "tech":      "1093952634426985800",  
            "budget":    "1093952634426985794",  
            "phone":     "1093952634426985799",  
            "smarthome": "1093952634426985795",  
            "wfh":       "1093952634426985796",  
            "default":   "1093952634426985800",
        }
    },
]
