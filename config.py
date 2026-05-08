# ── Shared config between telegram_bot.py and bale_bot.py ──

# Telegram
API_ID          = 35592666
API_HASH        = "ed6be4855f2b5a2974f2632d05ae4d2d"
BOT_TOKEN       = "8654428900:AAEo9rIchnsjbxYzPuaWCa84Zk9_nc9MPtY"
ADMIN_IDS       = [7055859698]
TG_MAIN_ADMIN   = 7055859698   # only this admin can change channels / toggle publish
TARGET_CHANNEL  = "@mosbateiranchannel"
TG_FOOTER      = "\n\n🇮🇷 +مثبت ایران\n@mosbateiranchannel"

# Bale
BALE_BOT_TOKEN      = "944232679:wSNlQZtETWB64BvhG1k2O4aQKWrTSxS8Vbo"
BALE_ADMIN_IDS      = [1932923897, 298868, 467450740, 1521176985, 979164226, 1726449723, 815216755, 569245532]
BALE_MAIN_ADMIN     = 1932923897   # only this admin can change channels / toggle publish
BALE_TARGET_CHANNEL = "@mosbateiran"
BALE_FOOTER         = "\n\n🇮🇷 *+مثبت ایران*\n📄 [@mosbateiran](ble.ir/join/4kUtvRC9wD)"
BALE_MAX_VIDEO_BYTES = 20 * 1024 * 1024  # 20 MB

# Source channels
SOURCE_CHANNELS = [
    "@AjaNews",
    "@irna_1313",
    "@isna94",
    "@Tasnimnews",
    "@farsna",
    "@iribnews",
    "@mehrnews",
    "@euronewspe",
    "@bbcpersian",
    "@farhikhteganonline",
    "@akharinkhabar",
    "@khabarfouri"
]

PERSIAN_CHANNELS = {
    "@irna_1313", "@isna94", "@Tasnimnews",
    "@farsna", "@iribnews", "@mehrnews",
    "@euronewspe", "@bbcpersian", "@farhikhteganonline",
    "@akharinkhabar", "@khabarfouri"
}

SOURCE_NAMES = {
    "@AjaNews":       "الجزیره",
    "@irna_1313":     "ایرنا",
    "@isna94":        "ایسنا",
    "@Tasnimnews":    "تسنیم",
    "@farsna":        "فارس",
    "@iribnews":      "خبرگزاری صداوسیما",
    "@mehrnews":      "مهر",
    "@euronewspe":    "یورونیوز فارسی",
    "@bbcpersian":    "بی‌بی‌سی فارسی",
    "@farhikhteganonline": "فرهیختگان",
    "@akharinkhabar": "آخرین خبر",
    "@khabarfouri":   "خبر فوری"
}
