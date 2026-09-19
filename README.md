# AZX Keys Store Bot

## تشغيل محلي

```bash
export BOT_TOKEN='توكن_البوت_من_BotFather'
export OWNER_ID='آيدي_المالك_الرقمي'
export BOT_USERNAME='@يوزر_البوت'
python3 keys_store_bot.py
```

## GitHub Actions (التشغيل التلقائي)

1. ارفع الملفات على ريبو GitHub
2. روح **Settings → Secrets and variables → Actions**
3. أضف الـ secrets التالية:

| Secret | القيمة |
|--------|--------|
| `BOT_TOKEN` | توكن البوت من BotFather |
| `OWNER_ID` | آيدي حسابك الرقمي على تيليجرام |
| `BOT_USERNAME` | يوزر البوت مثل `@Volte_4_bot` |

4. اعمل push — البوت هيشتغل أوتوماتيك

## ملاحظات
- قاعدة البيانات `keys_store_bot.db` بتتعمل تلقائي
- البوت بيستخدم stdlib بس، مفيش pip install
- التوكن القديم كان مكشوف — لازم تلغيه من BotFather وتعمل واحد جديد

## تنبيه أمني
لا ترفع التوكن داخل الكود أو على GitHub أبدًا — استخدم Secrets دايمًا.
