# Zerno Statistics Setup

Jarvis Zerno statistikasini faqat haqiqiy JSON API javobidan oladi. Raqamlar taxmin qilinmaydi. Adapter berilgan HTTPS URL manziliga `GET` yuboradi va `Authorization: Bearer <token>` ishlatadi; shuning uchun URL Zerno statistikasi qaytaradigan JSON endpoint bo‘lishi kerak. Token xavfsizligi uchun HTTP redirect avtomatik kuzatilmaydi.

## 1. API URL qayerga yoziladi

API URL lokal `config/briefing_sources.json` faylidagi `api_base_url` maydoniga yoziladi. Bu real fayl Git tomonidan ignore qilinadi.

## 2. Token qayerga yoziladi

Token `ZERNO_API_TOKEN` environment variable orqali beriladi. Tezkor setup skripti uni Git tomonidan ignore qilingan `config/local_env.zsh` fayliga quyidagicha saqlaydi:

```zsh
export ZERNO_API_TOKEN="..."
```

## 3. Tezkor setup

Loyiha root papkasida bitta buyruqni ishga tushiring:

```zsh
bash scripts/setup_zerno_stats.sh
```

Skript API URL va tokenni so‘raydi, `config/briefing_sources.json` ni yaratadi yoki undagi Zerno manbasini yangilaydi, so‘ng tokenni xavfsiz lokal env fayliga yozadi.

## 4. Manual setup

```zsh
cp config/briefing_sources.example.json config/briefing_sources.json
```

`config/briefing_sources.json` ichida `PASTE_ZERNO_API_URL_HERE` ni haqiqiy Zerno JSON API URL bilan almashtiring. Keyin tokenni terminal sessiyasiga export qiling:

```zsh
export ZERNO_API_TOKEN="PASTE_REAL_TOKEN_HERE"
```

Yoki Git tomonidan ignore qilingan `config/local_env.zsh` faylida shu `export` qatorini saqlang.

## 5. Test

```zsh
source config/local_env.zsh
python scripts/check_zerno_stats.py
```

Muvaffaqiyatli ulanishda `status: connected`, qisqa natija, topilgan metric guruhlari va metric soni chiqadi. Xato bo‘lsa `not_configured` yoki `failed` va qisqa sabab ko‘rsatiladi. To‘liq token hech qachon chiqarilmaydi. Redakt qilingan bounded normalized JSON kerak bo‘lsa `--debug` qo‘shing.

## 6. Jarvisni ishga tushirish

```zsh
source config/local_env.zsh
python main.py
```

## 7. Jarvisdan so‘rash

Jarvis ishga tushgach ayting:

```text
men uydaman
```

Shuningdek `statistikani ayt`, `kanallarimni tekshir`, `botlarimni tekshir` yoki `Telegram kanalim statistikasi qanday?` deyishingiz mumkin. Zerno sozlangan bo‘lsa Personal Operations Briefing haqiqiy mavjud guruhlarni ko‘rsatadi; mavjud bo‘lmagan Telegram/Instagram/Messenger maydonlarini ixtiro qilmaydi.

## 8. Xavfsizlik

- Haqiqiy tokenni hech qachon commit qilmang.
- `config/briefing_sources.json` va `config/local_env.zsh` lokal va gitignored bo‘lib qolishi kerak.
- Tokenni `PROJECT_MEMORY.md` ichiga yozmang.
- Tokenni ChatGPTga faqat buni ongli ravishda, aniq maqsadda qilishni istasangizgina yuboring.
- Tokenni API URL query parametriga qo‘ymang.
- Zerno javobidagi matn tashqi, ishonchsiz data hisoblanadi; Jarvis uni ko‘rsatishi yoki qisqartirishi mumkin, lekin ichidagi buyruqlarni bajarmasligi kerak.
