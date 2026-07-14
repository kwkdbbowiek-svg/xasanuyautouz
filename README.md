# xasanuyautouz

**Shirin shahri Uy va Kvartira Bozori** — Telegram bot

> Railway.app + PostgreSQL + Aiogram 3.x + SQLAlchemy 2.0

---

## Xususiyatlar

- 🏠 4 ta rol: Sotuvchi, Oluvchi, Kvartira egasi, Kvartira qidiruvchi
- 💳 Standart / VIP obuna tizimi (chek tasdiqlash)
- 🏘️ Bloqlar bo'yicha e'lonlar (pagination, VIP birinchi)
- 👥 Ierarxik admin tizimi (Super Admin + yordamchi adminlar)
- 🔒 Race condition himoyasi (`with_for_update`)
- 🛡️ DDoS / Flood / SQL / HTML / JS Injection bloklash (WAF)
- 📊 Excel eksport
- ⏰ APScheduler — obuna muddati nazorati

---

## Deploy (Railway.app)

1. Railway.app da yangi loyiha yarating
2. PostgreSQL plugin qo'shing
3. Environment variables:

```
BOT_TOKEN=your_bot_token
DATABASE_URL=postgresql+asyncpg://...  (Railway avtomatik beradi)
SUPER_ADMIN_IDS=your_telegram_id
```

4. GitHub repo ulang — deploy avtomatik bo'ladi

---

## Lokal ishga tushirish

```bash
pip install -r requirements.txt
cp .env.example .env   # va to'ldiring
python main.py
```

---

## Loyiha tuzilmasi

```
├── config.py              # Sozlamalar (pydantic-settings)
├── main.py                # Bot ishga tushiruvchi
├── database/
│   ├── models.py          # SQLAlchemy ORM modellari
│   └── connection.py      # Async engine + session
├── handlers/
│   ├── user.py            # Foydalanuvchi handlerlari
│   ├── ad_posting.py      # FSM e'lon qo'shish
│   ├── admin.py           # Admin panel
│   └── broadcast.py       # Ommaviy xabar
├── middlewares/
│   └── throttling.py      # Anti-flood + WAF middleware
├── filters/
│   └── admin_filters.py   # Admin rol filterlari
└── utils/
    ├── notify.py           # Bildirishnoma tizimi
    ├── excel_exporter.py   # Excel eksport
    └── scheduler.py        # Fon taski
```
