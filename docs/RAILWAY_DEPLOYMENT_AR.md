# تشغيل المتجر على Railway للمبتدئ

لا تحتاج إلى VPS ولا إلى شراء نطاق في البداية. Railway يشغّل التطبيق باستمرار ويعطيه
عنوان HTTPS عامًا. إغلاق تيليجرام أو الهاتف لا يوقف البوت؛ الذي يجب أن يبقى عاملًا هو
خادم Railway.

## الترتيب الآمن

1. أنشئ البوت من `@BotFather` واحفظ الرمز سريًا.
2. نزّل ملف OpenAPI من المورد وطابق حقول الطلب قبل تفعيل المورد.
3. ارفع المشروع إلى مستودع GitHub خاص، ثم أنشئ خدمة Railway من المستودع.
4. أضف PostgreSQL وRedis من لوحة Railway.
5. أضف متغيرات البيئة من `.env.example` داخل Railway، ولا ترفع ملف `.env` إلى GitHub.
6. استخدم النطاق الذي تولده Railway في `PUBLIC_BASE_URL`.
7. شغّل الترحيلات: `alembic upgrade head`.
8. ابدأ بالدفع اليدوي لجيب والكريمي مع اعتماد الأدمن، واختبر بمبالغ صغيرة.
9. فعّل المورد لمنتج تجريبي رخيص فقط، ثم راقب الطلب والرصيد والتسليم.
10. فعّل Binance Pay لاحقًا بعد الحصول على حساب تاجر ومفاتيح Merchant API الرسمية.

## القيم المهمة

```env
APP_ENV=production
ADMIN_IDS=8884716304
BOT_TOKEN=ضعه_داخل_Railway_فقط
PUBLIC_BASE_URL=https://العنوان-الذي-تعطيه-railway
SUPPLIER_ENABLED=false
SUPPLIER_BASE_URL=https://ventetelegrambotrailway-production.up.railway.app
SUPPLIER_API_KEY=ضعه_داخل_Railway_فقط
```

لا تجعل `SUPPLIER_ENABLED=true` قبل نجاح اختبار قراءة الحساب والمنتجات والتسعير، ثم
طلب واحد منخفض القيمة. لا ترسل رمز BotFather أو مفتاح المورد في المحادثات أو الصور.
