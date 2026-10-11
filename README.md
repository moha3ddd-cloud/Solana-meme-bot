# Solana Meme Coin Trading System

> "أنا مش عايز Bot بيطارد نسبة ربح يومية. أنا عايز Trading System قابل للاختبار، وإدارة المخاطر فيه أهم من الاستراتيجية نفسها."

## الحالة الحالية

✅ **Phase 0 — Risk Engine (خلصان)**
- `core/risk_engine.py` — position sizing بناءً على Stop Loss، Daily Loss Limit، Drawdown Scaling، Kill Switch، Emergency Mode
- `config/risk_config.yaml` — كل الأرقام قابلة للتعديل من غير كود
- `tests/test_risk_engine.py` — 7 اختبارات، كلها عدّت

الـRisk Engine ده مستقل تمامًا عن أي استراتيجية دخول/خروج — أي جزء تاني في
النظام (scanner, execution, strategy) لازم يمر عليه الأول قبل ما يفتح أي صفقة.

✅ **Phase 1 — Scanner + Scoring System (خلصان)**
- `core/scanner.py` — مرحلتين: Hard Filters (استبعاد فوري) ثم Scoring (0-100)
- `config/scanner_config.yaml` — كل الحدود والأوزان قابلة للتعديل
- `tests/test_scanner.py` — 10 اختبارات، كلها عدّت

**Hard Filters (استبعاد فوري، ملهاش Score أصلًا):**
liquidity ضعيفة، عملة أصغر من عمر معين، تركيز holders عالي، liquidity
مش locked/burned، مش sellable (honeypot)، mint/freeze authority لسه شغالة،
buy/sell tax عالي، أو أي critical flag (rug wallet, wash trading pattern...).

**Scoring (بعد ما تعدي الفلاتر):** liquidity depth, volume activity
(مع كشف wash trading لو الحجم أعلى من السيولة بشكل غير طبيعي), holder
distribution, buy/sell pressure, holder growth rate, wallet reputation,
age maturity. البوت مايدخلش إلا لو الـScore عدى `minimum_entry_score`
(70 حاليًا) في الـconfig.

✅ **Phase 2 — Strategy & Signal Engine (خلصان)**
- `core/strategy.py` — `SignalEngine` (توقيت الدخول) + `PositionManager` (إدارة الخروج)
- `config/strategy_config.yaml` — أوزان إشارات التوقيت وحدودها
- `tests/test_strategy.py` — 9 اختبارات، كلها عدّت

**Entry (SignalEngine):** منفصل تمامًا عن الـScanner —
الـScanner بيحكم على "أمان" العملة، والـSignalEngine بيحكم على
"هل دلوقتي لحظة كويسة للدخول؟" (momentum, volume acceleration,
buy pressure في آخر 5 دقايق, breakout confirmation بحجم تداول).
لازم `min_positive_signals_required` إشارات مستقلة إيجابية،
مش بس متوسط مرجح عالي من عامل واحد قوي.

**Exit (PositionManager):** بياخد قواعد الـexit من ملف مستقل
(`config/exit_rules_conservative.yaml`). بيطبق: Stop Loss، Take
Profit تدريجي (يقفل جزء من المركز عند كل target ويحرك الستوب
لـbreakeven)، وTrailing Stop للجزء المتبقي بعد أول target — والستوب
مايرجعش لتحت أبدًا.

**الدخول الفعلي في صفقة محتاج موافقة 3 أنظمة مستقلة:**
`RiskEngine.can_open_new_trade()` + `ScannerEngine.scan().eligible_for_entry`
+ `SignalEngine.evaluate_entry().approved`

✅ **Phase 2b — مسار سكالب موازي (خلصان)**
- `config/strategy_scalp_config.yaml` + `config/exit_rules_scalp.yaml` — نفس الكلاسات بالظبط (`SignalEngine`, `PositionManager`)، config مختلف بس
- `core/paper_trading.py` اتعدل يدعم `profiles: dict[str, StrategyProfile]` — أكتر من مسار في نفس الوقت
- `tests/test_paper_trading.py` — 13 اختبار (فيهم اختبار بيأكد إن السكالب فعلًا بيقفل عند نسب ربح أقرب من المحافظ)

**الفرق بين المسارين:** السكالب مش "فلترة أسهل" — العكس.
`minimum_signal_score` أعلى (72 مقابل 65) و`min_positive_signals_required`
أعلى (3 مقابل 2)، لأن مساحة الخطأ في صفقة قريبة الخروج أصغر بكتير،
فلازم تأكيد أقوى قبل الدخول. الفرق الحقيقي في الـ**exit rules**: أهداف
ربح قريبة (8%/15%/25% مقابل 30%/60%/100%) وtrailing stop بيتفعل بدري
وبمسافة أضيق.

**مشتركين في:** نفس `RiskEngine` (رأس المال، Daily Loss Limit،
Drawdown Scaling) ونفس `ScannerEngine` (معيار الأمان واحد لكل
المسارات — الفرق في التوقيت والخروج بس، مش في قبول عملات أخطر).
يعني خسارة من مسار بتأثر على قدرة المسار التاني يفتح صفقات جديدة
في نفس اليوم — الحماية دي مقصودة، مش مشتركين لكن منفصلين تمامًا.

**⚠️ نقطة صراحة مهمة:** مفيش "نسبة نجاح مضمونة" في أي من المسارين.
الـconfig بيتحكم في صرامة الدخول وسرعة الخروج بس — الـWin Rate
الفعلي نتيجة، مش إعداد، وهيطلع بس من backtesting حقيقي بعد ما
`DataLogger` يجمع بيانات كفاية (Phase 4c).

✅ **Phase 2c — Auto-Routing بين المسارين (خلصان)**
- `PaperTradingEngine.evaluate_candidate_auto()` — العملة تتفحص ضد المسارين مع بعض تلقائيًا
- `tests/test_paper_trading.py` — 6 اختبارات إضافية (21 إجمالي)، كلها عدّت

**قبل كده** كان لازم تحدد "conservative" ولا "scalp" يدويًا في كل
استدعاء — مفيش قرار مين ياخد أي عملة. **دلوقتي:** أي عملة عدّت
الـScanner (مرة واحدة، مشترك) بتتفحص ضد `SignalEngine` بتاع المسارين
الاتنين. أي مسار وافق، يدخل قائمة المرشحين؛ لو أكتر من واحد وافق،
الأعلى `signal_score` هو اللي بيفتح الصفقة فعليًا.

**ليه القرار ده مش "قاعدة مصطنعة" زي (عملة صغيرة = سكالب تلقائيًا):**
معندناش data تأكد قاعدة تصنيف مسبقة زي كده أصلًا — كان هيبقى تخمين
إضافي فوق تخمينات موجودة. خلينا الفرق الفعلي في العتبات (72 مقابل 65)
هو اللي بيقرر عمليًا، مش قاعدة يدوية. `profiles_considered` في
`CandidateDecision` بيسجل كل المسارات اللي وافقت، مش بس الفايز —
شفافية كاملة لو عايز تراجع القرار بعدين.

✅ **Phase 3a — DexScreener Data Layer (خلصان)**
- `core/data/dexscreener_client.py` — HTTP client مقصور على Solana، rate limiting مدمج (300 req/min)
- `core/data/mappers.py` — تحويل رد DexScreener لـ`TimingSignals` و dict جزئي لـ`TokenMetrics`
- `core/data/data_logger.py` — تسجيل snapshots دورية في SQLite + تصدير OHLC candles
- `tests/test_data_layer.py` — 9 اختبارات، كلها عدّت

**حدود مهمة (موثقة في الكود نفسه):**
- DexScreener مفهوش m15 حقيقي (بس m5, h1, h6, h24) — الـ`price_change_15m_pct`
  و `volume_15m_usd` تقريب (weighted blend)، مش رقم حقيقي.
- `breakout_confirmation` (كسر أعلى قمة) مايتحسبش من طلب واحد — لازم
  تاريخ أسعار متراكم. حاليًا القيم الافتراضية آمنة (False/1.0) لحد ما
  الـData Logger يجمع بيانات كفاية.
- **DexScreener لوحده مايكفيش للـScanner كامل.** الحقول دي لسه محتاجة
  مصدر on-chain تاني (Solana RPC / RugCheck / Helius): holder
  distribution, mint/freeze authority, honeypot test, buy/sell tax.
  `core/data/mappers.py` فيه `ONCHAIN_REQUIRED_FIELDS` كمرجع واضح لما
  نبني `onchain_client.py`.
- الكود اتكتب صح حسب توثيق DexScreener الرسمي بس **متجربش على شبكة
  حقيقية** — بيئة التطوير دي معندهاش اتصال إنترنت خارجي. جرّبه فعليًا
  قبل ما تعتمد عليه.

✅ **Phase 3b — On-chain Data via RugCheck (خلصان)**
- `core/data/rugcheck_client.py` — HTTP client لـ`api.rugcheck.xyz` (مجاني، من غير API key، 10 req/min بدون مفتاح / 60 بيه)
- `core/data/onchain_mappers.py` — تحويل تقرير RugCheck لحقول on-chain + دمجها مع حقول DexScreener في `TokenMetrics` كامل
- `tests/test_onchain_layer.py` — 11 اختبار (فيهم اختبارين تكامليين بيأكدوا إن الناتج فعلًا بيشتغل مع `ScannerEngine`)، كلها عدّت

**ليه RugCheck مش Solana RPC من الصفر؟** RugCheck أصلًا بيعمل الشغل
الصعب (parsing SPL accounts, تتبع Raydium/Orca pool state, حساب
holder distribution) وهو معيار معروف في مجتمع Solana. بناء البديل من
الصفر تكرار مجهود من غير فايدة واضحة دلوقتي.

**بيغطي:** mint/freeze authority, holder distribution (باستبعاد
عنوان الـLP نفسه من الحساب عشان مانعاقبش عملة على إن سيولتها "محفظة
كبيرة"), liquidity locked/burned, وrisk flags بتتحول لـcritical_flags.

**لسه مش متغطي (وموثق بوضوح في الكود):**
- `is_sellable` الحقيقي (simulate-sell فعلي) — حاليًا **fail-safe**:
  `False` افتراضيًا إلا لو RugCheck مايشوفش أي علامة honeypot. يعني
  العملة بترفض لحد ما نتأكد، مش العكس.
- `buy_tax_pct` / `sell_tax_pct` الدقيقين — افتراضي 0.0 لحد ما نعمل
  simulate حقيقي عبر Jupiter في الـExecution Engine (Phase 4)
- `holder_count_1h_ago` / `unique_buyers_last_hour` — تقديرات محافظة
  لحد ما الـData Logger يجمع تاريخ كفاية

✅ **Phase 4a — Execution Engine عبر Jupiter (خلصان، لحد حد التوقيع)**
- `core/execution/jupiter_client.py` — client لـ`api.jup.ag` (Jupiter، أشهر aggregator على Solana، ملحوظة: بيتطلب API key مجاني من `portal.jup.ag` حتى للـFree tier في 2026)
- `core/execution/execution_engine.py` — pre-trade safety checks (slippage/price impact/liquidity من `risk_config.yaml`) + `check_sellability()` الحقيقي
- `tests/test_execution_engine.py` — 12 اختبار، كلها عدّت

**حل الـplaceholder اللي سبناه في Phase 3b:** `check_sellability()`
بيعمل quote حقيقي من التوكن رجوع لـSOL — لو مفيش route أو الـprice
impact جنوني وقت البيع، ده مؤشر honeypot قوي. الفحص ده بيحصل **قبل**
حتى ما نحاول نجهز صفقة شراء، عشان مانضيعش وقت على عملة already مقفولة.

**الحد الفاصل اللي اتفقنا عليه محترم بالكامل:** أقصى حاجة الكلاس ده
بيوصلها هي `unsigned_swap_transaction` (base64، **غير موقّعة**). مفيش
أي كود هنا بيلمس private keys أو بيوقع أو بيبعت transaction للشبكة.
التوقيع والإرسال الفعلي = طبقة Wallet/Signer منفصلة، لسه هنتكلم فيها.

**ترتيب الفحوصات في `prepare_buy()` مقصود:** sellability check الأول
(أهم حاجة وأرخص)، بعدين quote الشراء، بعدين pre-trade safety checks،
وأخيرًا بس لو كل حاجة عدت — تجهيز الـtransaction. أي فشل في أي مرحلة
يوقف على طول من غير ما يكمل للمرحلة اللي بعدها.

✅ **Phase 5 — Paper Trading Engine (خلصان)**
- `core/paper_trading.py` — `PaperTradingEngine` بيدمج كل حاجة بنيناها في تدفق واحد كامل
- `tests/test_paper_trading.py` — 11 اختبار تكاملي، كلها عدّت

**ده أول مكان في المشروع بيربط كل حاجة مع بعض فعليًا:** أي عملة مرشحة
لازم تعدي 3 أنظمة موافقة مستقلة بالترتيب ده بالظبط —
`RiskEngine.can_open_new_trade()` -> `ScannerEngine.scan()` ->
`SignalEngine.evaluate_entry()` -> `RiskEngine.calculate_position_size()`.
أي فشل في أي مرحلة يوقف على طول.

بعد الدخول، محفظة افتراضية (`cash_usd` + `open_positions`) بتتبع كل
حركة عبر `PositionManager` (نفس منطق الـTake Profit التدريجي
والـTrailing Stop)، وكل نتيجة (ربح/خسارة) بترجع تلقائيًا لـ`RiskEngine`
عشان الـDaily Loss Limit والـDrawdown Scaling يفضلوا شغالين صح.

`trade_log` بيسجل سبب كل دخول (`scanner_score` + `signal_score`) وكل
خروج — بالظبط المتطلب اللي طلبته للـDashboard ("سبب دخول كل صفقة،
سبب خروجها"). `portfolio_snapshot()` بيرجع Win Rate, Profit Factor,
Balance — جاهز يتوصل مباشرة بـDashboard لما نوصلها.

**مقصود إنها تيجي قبل الـWallet/Signer وقبل تجميع البيانات التاريخية:**
كده نقدر نتأكد إن كل الأنظمة شغالة صح مع بعضها *قبل* ما نحتاج نفكر
في فلوس حقيقية أو private keys أصلًا.

✅ **Phase 6a — Dashboard (خلصان، read-only)**
- `core/dashboard_export.py` — بيحول حالة الـ`PaperTradingEngine` لـJSON جاهز
- `dashboard/index.html` — واجهة مستقلة (مفيش backend server) بتقرأ الـJSON وتعرضه
- `dashboard/snapshot.json` — مثال حقيقي (winner + loser + مركز مفتوح) عشان تجرب الداشبورد على طول
- `tests/test_dashboard_export.py` — 5 اختبارات، كلها عدّت

**العناصر:** Estimated Equity, Daily PnL, Win Rate, Profit Factor,
Trades Today, حالة الـHalt/Kill Switch (banner واضح لو البوت متوقف)،
الصفقات المفتوحة مع سبب الدخول، Trade Log كامل (كل حركة وسببها)،
وعرض الـRisk Config الحالي.

**العنصر المميز — Drawdown Altimeter:** مقياس عمودي بيوضح بصريًا
مكانك دلوقتي بالنسبة لمستويات تقليل المخاطرة الفعلية المعرّفة في
`risk_config.yaml` (`drawdown_scaling.tiers`) — مش رقم مجرد، ده تمثيل
حي لنفس القرار اللي `RiskEngine._drawdown_risk_multiplier()` بياخده.

**حدود مقصودة:** الداشبورد read-only بالكامل. تعديل الـconfig (Risk %,
Daily Loss Limit...) لسه من ملفات YAML مباشرة، مش من الواجهة — عمل
تعديل حي محتاج backend server بيكتب على الملفات، ومقدرش أوعدك بحاجة
كده تشتغل فعليًا من غير ما نبنيها صح.

✅ **Phase 4b — Wallet / Signer Layer (خلصان)**
- `core/wallet/keystore.py` — تشفير الـprivate key محليًا بباسورد (PBKDF2 + Fernet)
- `core/wallet/signer.py` — `EnvKeypairSigner` و `EncryptedKeystoreSigner`، بيوقعوا ويبعتوا للشبكة عبر Solana RPC
- `scripts/manage_wallet.py` — CLI لإنشاء/استيراد wallet (بالباسورد مخفي عبر `getpass`)
- `.gitignore` — يمنع الـkeystore والباسورد من الوصول لـgit خالص
- `tests/test_keystore.py` (8) + `tests/test_signer.py` (6) — كلها عدّت

**⚠️ يتطلب `pip install solders base58`** — المكتبتين دول مش متاحين
في بيئة التطوير الحالية (sandbox مقفول)، فالكود اتكتب صح حسب توثيق
solders الرسمي بس **متجربش فعليًا هنا**. الاختبارات غطّت كل حاجة
ممكن تتجرب من غيرهم (الـkeystore بتشفير حقيقي، وplumbing الـsigner
زي قراءة env vars ومعالجة الأخطاء وبناء RPC requests) — التوقيع
الفعلي نفسه لازم تتأكد منه على جهازك.

**خيارين للتخزين:**
1. `EnvKeypairSigner` — أبسط (متغير بيئة)، أقل أمانًا (أي حاجة عندها
   وصول لمتغيرات البروسيس ممكن تشوفه)
2. `EncryptedKeystoreSigner` — الملف نفسه مشفّر على القرص، عديم الفايدة
   من غير الباسورد حتى لو اتسرق. **الموصى بيه.**

**الحد الفاصل محترم بالكامل:** `sign_and_send()` هي أول وآخر نقطة في
المشروع كله بتلمس private key فعلي. كل حاجة قبلها (Scanner, Strategy,
ExecutionEngine) بتوصل لـunsigned transaction وتوقف. فيه كمان
`dry_run=True` (افتراضي) بيوقّع محليًا من غير ما يبعت أي حاجة للشبكة —
مفيد جدًا تتأكد إن كل حاجة شغالة قبل أي إرسال حقيقي.

**مبادئ أمان تانية في الكود:** رسائل الخطأ مصممة عشان متسربش أي جزء
من الـkey أو الباسورد حتى لو حصل استثناء غير متوقع؛ `.gitignore`
بيمنع `data/wallet_keystore*.json` و`.env` من `git add` بالغلط؛
CLI بياخد الباسورد عبر `getpass` (مش command-line argument، عشان
مايفضلش في shell history).

✅ **Phase 1b — Smart Money + Social Presence (خلصان)**
- `core/data/wallet_reputation.py` — `WalletReputationTracker` (SQLite)
- `core/data/social_signals.py` — `SocialSignalProvider` (`NullSocialSignalProvider` + `DexScreenerSocialPresenceProvider`)
- `tests/test_wallet_reputation.py` (8) + `tests/test_social_signals.py` (6) + إضافات لـ`test_scanner.py` و`test_paper_trading.py` — كلها عدّت

**⚠️ صراحة عن حدود الإضافتين دول (مهم تقراها):**

**Smart Money — مش Nansen:** ده مش قاعدة بيانات خارجية بتتبع محافظ
معروفة عبر آلاف المستخدمين (الخدمات دي مكلفة جدًا ومحتاجة بنية تحتية
خارج نطاق المشروع ده). اللي بنيناه فعليًا: نظام بيتعلم من **صفقاتنا
إحنا بنفسنا** بمرور الوقت. كل صفقة تتقفل (`PaperTradingEngine`)، بتسجل
عناوين أكبر الحاملين وقت الدخول مع النتيجة (ربح/خسارة) في
`WalletReputationTracker`. الفايدة **صفر لحد ما نجمع بيانات كفاية** —
مش أداة تدّيك edge من اليوم الأول، أداة بتتحسن مع الاستخدام المستمر.
الثقة في أي محفظة بتزيد تدريجيًا مع عدد الصفقات اللي شفناها فيها (أقل
من 5 صفقات = تأثير ضعيف، بيتشبع بعد كده).

**Social Presence — مش قياس فيروسية:** قياس انتشار حقيقي على
X/Twitter محتاج API مدفوع (X API v2 أو LunarCrush) مش موجود في
المشروع. اللي موجود: فحص وجود روابط social موثقة في بيانات DexScreener
بس — مؤشر ضعيف جدًا على "المشروع كلّف نفسه يعبي بيانات أساسية"، مش
قياس اهتمام حقيقي. عشان كده وزنه في الـScanner صغير جدًا (0.05) —
البنية (`SocialSignalProvider`) مصممة عشان تستبدل provider حقيقي
لاحقًا من غير ما تلمس باقي الكود.

**اتضاف للـScanner:** `wallet_reputation` (الوزن 0.10) دلوقتي بيدمج
مصدرين — الخصم القديم من warning flags + Smart Money score، **بس
لو فعلًا عندنا تاريخ حقيقي عن واحدة من المحافظ دي**. من غير تاريخ
(الحالة الشائعة في البداية)، السلوك زي القديم بالظبط — مفيش "خصم
لمجرد إن مفيش بيانات". الأوزان اتعاد توزيعها: `volume_activity`
0.15→0.13، `age_maturity` 0.10→0.07، `social_presence` جديد 0.05.

**⚠️ تصحيح حصل أثناء البناء (شفافية):** أول نسخة من الدمج كانت
بترجع "50 محايد" لما مفيش بيانات smart money، وده كان بيسحب score
عملة سليمة تمامًا من ~75 لـ~71 — تحيّز غلط بيعاقب "مفيش بيانات"
كأنها "فيه خطر". اتصلح عبر `has_reputation_data()` — الإشارة دلوقتي
مالهاش أي تأثير خالص إلا لو فيه تاريخ حقيقي مسجل. أثر متبقي بسيط
(~2 نقطة) من إعادة توزيع الأوزان لصالح `social_presence` (0.05 وزن
جديد) لسه موجود ومقصود — ده فرق منطقي (بُعد جديد اتضاف)، مش bug.

**⚠️ قرار متعمد — مبنيناش سرعة دخول لحظية (block-zero sniping):**
سألتني هل ده نقص، والإجابة: مش نقص تقني، ده تعارض فلسفي مباشر مع أول
جملة اتفقنا عليها في المشروع كله ("إدارة المخاطر أهم من الاستراتيجية
نفسها"). الـsniping الحقيقي محتاج بنية تحتية مختلفة تمامًا (bundlers،
قرب من الـvalidators، دخول في الـblock صفر) وملف مخاطرة مختلف بالكامل
— ده لعبة تانية غير اللي بنيناها. النظام الحالي بيستنى 30 دقيقة على
الأقل ويتأكد من استقرار نسبي قبل الدخول، يعني بطبيعته هيفوّت أي حركة
سريعة جدًا — ده اختيار واعي، مش عيب. لو عايز مسار "sniper" فعلًا،
ممكن نبنيه كـprofile ثالث منفصل، بس لازم تكون واعي إنه ملف مخاطرة
مختلف جوهريًا عن الفلسفة المحافظة اللي المشروع كله مبني عليها.

✅ **Phase 0b — Emergency Mode الفعلي (خلصان)**
- `RiskEngine.emergency_trigger_reason()` — يفرّق بين "امنع صفقات جديدة" (`can_open_new_trade`) و"اقفل كل المفتوح فورًا"
- `PaperTradingEngine.run_emergency_check()` — قفل فعلي لكل المراكز المفتوحة، من غير المرور على Take Profit/Trailing التدريجي
- `tests/test_risk_engine.py` (+5) و `tests/test_paper_trading.py` (+7) — كلها عدّت

**3 حالات بتفعّل الطوارئ:** Kill Switch يدوي، Drawdown وصل لحد الطوارئ
(`emergency_drawdown_pct`)، أو أخطاء تنفيذ متتالية وصلت للحد الأقصى.
**الفرق المهم عن `can_open_new_trade`:** ده بيمنع صفقات جديدة بس —
الطوارئ بتقفل الموجود فعليًا. لو سعر مركز معين مش متاح وقت القفل،
بيتقفل بسعر الدخول (PnL صفر) بدل ما يتسيب مفتوح أو نخترع رقم — وده
مسجل صراحة في سبب الإغلاق.

**⚠️ لسه محتاج "المستدعي":** الدالة دي جاهزة ومختبرة، بس محدش بينادّيها
لسه بشكل دوري — ده هيبقى جزء من الـMain Loop (Phase التالية).

✅ **Phase 4c — Discovery Layer (خلصان)**
- `core/data/dexscreener_client.py` — endpoints جديدة: `get_latest_token_profiles()` و `get_latest_token_boosts()`
- `core/discovery.py` — `DiscoveryLayer` (فلترة رخيصة قبل RugCheck) + `SeenTokensStore` (cooldown، SQLite)
- `config/discovery_config.yaml` — عتبات الفلترة الرخيصة، cooldown، حد أقصى للمرشحين لكل دورة
- `tests/test_discovery.py` — 11 اختبار (fake client، من غير شبكة حقيقية)، كلها عدّت

**الفلسفة:** الـDiscovery مش أمان ولا توقيت — دي فلترة رخيصة جدًا
(DexScreener بس) قبل حتى ما نفكر نستهلك rate limit الـRugCheck
المحدود (10-60 طلب/دقيقة) على عملة واضح إنها مش تستاهل. أي عملة
عدّت هنا لسه لازم تعدي Scanner + SignalEngine الكاملين بعد كده.

**المصادر المستخدمة (ومصدر صراحة عن حدودها):**
- `token-profiles/latest` — مشاريع سجّلت بيانات meta حديثًا
- `token-boosts/latest` — عملات "معزّزة" على DexScreener (دفعت فلوس
  لظهور أعلى — إشارة ضعيفة عمدًا، مش إنجاز)
- Watchlist يدوي — عناوين تحددها إنت بنفسك بغض النظر عن المصادر فوق

**⚠️ حد حقيقي، مش تقصير:** DexScreener مفهوش "بث حي" لكل عملة جديدة
على Solana في الـAPI المجاني. يعني الـDiscovery ده **مكمّل مش شامل** —
عملة جديدة تمامًا لسه معندهاش profile مسجل ومحدش عزّزها ممكن تفوتك.
لو حبيت تغطية أشمل لاحقًا، الحل الحقيقي الوحيد هو مراقبة أحداث إنشاء
pools على Raydium/pump.fun مباشرة عبر Solana RPC — ده أعقد وأغلى،
ومش جزء من المشروع دلوقتي.

**مصمم عشان يفشل بأمان:** لو مصدر واحد فشل (مثلًا token-profiles
down)، الباقي بيكمل عادي. كل عملة بتتسجل في `SeenTokensStore` بغض
النظر عن نتيجة الفلتر — عشان منعيدش نفس الطلب كل دورة من غير داعي.

✅ **Phase 4c-2 — GeckoTerminal Integration (خلصان)**
- `core/data/geckoterminal_client.py` — client مجاني بدون key (30 طلب/دقيقة، أضيق مصدر في المشروع): `get_trending_pools()`, `get_new_pools()`, `get_pool()`, `get_token_pools()`, `get_pool_ohlcv()`
- `core/data/geckoterminal_mappers.py` — `ohlcv_response_to_candles()` (JSON:API → نفس `OHLCCandle` بتاع `DataLogger`)، `pools_response_to_candidates()`
- اندمج في `DiscoveryLayer` كمصدر إضافي اختياري (`geckoterminal_client` param + `use_geckoterminal_new_pools`/`use_geckoterminal_trending` في الـconfig — **الاتنين `false` افتراضيًا** عمدًا بسبب ضيق الـrate limit)
- `tests/test_geckoterminal.py` (11) + امتداد `tests/test_discovery.py` (+2) — كله عدّى

**السبب اللي الإضافة دي مهمة:** pump.fun فقد الـAPI الرسمي بتاعه (DNS outage يونيو 2026، بقى موجّه لمنصة تانية) — أي "API مجاني لـpump.fun" شايفه دلوقتي طرف تالت مش رسمي، **ومينفعش توصل محفظة بأي خدمة توقيع منه**. GeckoTerminal (ملك CoinGecko، شركة موثوقة) بيحل فجوتين حقيقيتين كانت موجودة:
1. **Backtesting:** DexScreener مفهوش historical OHLC خالص. GeckoTerminal عنده شموع حقيقية (دقيقة/ساعة/يوم، لحد 1000 شمعة) — **معندناش داعي نستنى أسابيع DataLogger قبل أول backtest حقيقي.**
2. **Discovery:** `new_pools`/`trending_pools` بديل أقوى من `token-profiles`/`token-boosts` بتاعة DexScreener.

**TradingView اتفحص واتستبعد صراحة** — مبيغطيش عملات meme صغيرة على pump.fun/Raydium خالص، مش مصدر مناسب هنا.

✅ **Phase 4d — Main Loop / Orchestration (خلصان)**
- `core/orchestrator.py` — `Orchestrator.run_cycle()`: تحديث المفتوح -> Emergency Check -> Discovery -> بناء TokenMetrics/TimingSignals حقيقية لكل مرشح (DexScreener+RugCheck) -> `evaluate_candidate_auto()`
- `config/orchestrator_config.yaml` — `default_initial_stop_loss_pct` (10% افتراضي، نفس المبدأ اللي استخدمناه في كل التجارب اليدوية)
- `scripts/run_bot.py` — **ده السكريبت اللي هيشتغل فعليًا على الـVPS** — بيربط كل العملاء الحقيقيين (DexScreener, RugCheck, Discovery, Engine) في loop مستمر، بـlogging لملف `data/bot.log` و snapshot دوري للداشبورد
- `tests/test_orchestrator.py` — 8 اختبارات (الـEngine الكامل حقيقي بإعدادات الإنتاج، المصادر الخارجية fake)، كلها عدّت

**ترتيب كل دورة مهم ومقصود:**
1. تحديث المراكز المفتوحة أولًا (أهم من اكتشاف جديد)
2. `run_emergency_check()` بأحدث الأسعار — لو اتفعّلت، الدورة بتوقف هنا فورًا (مفيش اكتشاف أو فتح صفقات جديدة في نفس الدورة اللي قفلنا فيها كل حاجة لسبب خطير)
3. Discovery -> تقييم كل مرشح -> فتح لو عدّى

**ربط أخطاء RPC/DEX الحقيقية (كان من ضمن الناقص):** أي فشل فعلي في
جلب سعر، RugCheck، أو Discovery بينادي
`risk_engine.record_execution_error()` — يعني لو الشبكة أو مصدر
بيانات معين وقع لفترة، الـkill switch التلقائي هيتفعّل زي المفروض
بالظبط (بعد `max_consecutive_errors` من الـconfig)، مش هيفضل يحاول
لانهائي.

**مصمم عشان يفشل بأمان:** استثناء في دورة واحدة (مصدر بيانات واقع،
مثلًا) بيتسجل كخطأ وبيكمل للدورة اللي بعدها — السكريبت نفسه مابيوقفش
إلا بـCtrl+C يدوي (وقتها بيحفظ آخر snapshot قبل ما يقفل).

✅ **Phase 4e — Backtesting Engine (خلصان، أسرع بكتير من الخطة الأصلية بفضل GeckoTerminal)**
- `core/backtest.py` — `BacktestEngine`: بيشغّل شموع OHLCV تاريخية (من GeckoTerminal أو DataLogger) عبر **نفس كود PaperTradingEngine بالظبط**، مش محاكاة منفصلة
- `tests/test_backtest.py` — 7 اختبارات (سيناريوهات صعود/كراش اصطناعية بس واقعية الشكل)، كلها عدّت
- **تأكيد مهم:** أسماء الحقول في `geckoterminal_mappers.py` (`reserve_in_usd`, `pool_created_at`, `volume_usd.m5/h1/h24`) اتقارنت بتوثيق CoinGecko الرسمي وأمثلة JSON حقيقية — مطابقة تمامًا، مش افتراض

**⚠️ 4 حدود جوهرية لازم تفهمها قبل ما تثق في أي نتيجة Backtest — موثقة بالتفصيل في docstring الملف:**
1. **الـScanner بيستخدم لقطة ثابتة واحدة من حالة العملة الحالية** على طول الفترة التاريخية كلها — معندناش RugCheck تاريخي. يعني النتيجة بتقيّم **منطق التوقيت والخروج** أساسًا، مش الـScanner التاريخي بالكامل
2. **عدد صفقات الشراء/البيع مش متاح من شموع OHLCV** — بنستخدم عدد الشموع الصاعدة/الهابطة كـproxy، مش عدد حقيقي
3. فحص الستوب بأسوأ سعر (low) والأهداف بأحسن سعر (high) داخل كل شمعة — أدق من الاكتفاء بالإغلاق، لكن لسه تقريب
4. `price_change_15m` و`breakout_confirmation` فعليًا **أدق من Live mode** هنا — عندنا تاريخ حقيقي متراكم، مفيش تقريب الـ70/30

**لسه معلّق:** تشغيل فعلي على بيانات GeckoTerminal حقيقية — بيئة التطوير دي عندها قيد `robots.txt` بيمنع أداة البحث من نداء `api.geckoterminal.com` مباشرة (قيد خاص بالأداة، مش بالـAPI نفسه — الكود الحقيقي اللي بيستخدم مكتبة `requests` مش هيتأثر). التشغيل الفعلي الأول هيكون على الـVPS.

✅ **Phase 4g — طريقة تشغيل مجانية بالكامل (GitHub Actions) — بديل VPS**
- `core/risk_engine.py` + `core/paper_trading.py` — `export_state()`/`import_state()`: حفظ واسترجاع كامل لحالة البوت (كاش، صفقات مفتوحة، سجل، حالة RiskEngine) كـJSON — عشان كل تشغيلة (process منفصل) تكمل من اللي فات، مش تبدأ من الصفر
- `scripts/run_bot_cycle.py` — نسخة "دورة واحدة وقفل" من `run_bot.py` (بدل الـloop المستمر)، بتحمّل الحالة من `data/engine_state.json`، تشغّل دورة، تحفظ الحالة تاني
- `.github/workflows/run_bot.yml` — workflow بيشغّل السكريبت ده كل 15 دقيقة تلقائيًا، ويعمل commit للحالة الجديدة في نفس الـrepo
- `tests/test_paper_trading.py` (+4 اختبارات state roundtrip، شاملة اختبار حقيقي بـJSON roundtrip) — كلها عدّت

**ليه الطريقة دي مجانية فعليًا، مش "مجاني بحدود":** GitHub Actions **بلا سقف دقايق خالص** على الـrepos العامة (public) — مش تجربة، مش 2000 دقيقة زي الـrepos الخاصة. مفيش بطاقة بنكية مطلوبة أصلًا، بس حساب GitHub مجاني عادي.

**⚠️ حدود حقيقية لازم تعرفها قبل ما تعتمد عليها:**
1. **الـrepo لازم يكون Public** — يعني أي حد يقدر يشوف قرارات البوت وسجل صفقاته (مش حاجة حساسة، كله Paper Trading، بس لازم تكون مرتاح لفكرة إن الداتا ظاهرة للكل)
2. **ممنوع تحط أي مفتاح محفظة أو سر في الكود أبدًا** — `.gitignore` بيمنع ملفات الـkeystore والـ.env من الالتزام (commit) أصلًا، بس التحذير ده أهم بكتير على repo عام من repo خاص
3. **التوقيت مش دقيق 100%** — GitHub بيقول "ممكن يتأخر وقت الضغط الكبير"، فـ"كل 15 دقيقة" يعني تقريبًا، مش بالثانية
4. **لو الـrepo فضل من غير أي نشاط 60 يوم، الـscheduled workflow بيتعطّل تلقائيًا** — أي commit (حتى من البوت نفسه) بيصفّر العداد، فده نادرًا ما يحصل طول ما البوت شغال فعليًا
5. **GeckoTerminal معطّل افتراضيًا أصلًا** (زي ما اتوضح قبل كده) — مناسب أكتر هنا كمان لأن rate limit أضيق مصدر وممكن يتضارب مع تكرار التشغيل كل 15 دقيقة

## المراحل الجاية (بالترتيب)

### Phase 4f — تشغيل Backtest فعلي على بيانات GeckoTerminal حقيقية
- أول تشغيل حقيقي لـ`BacktestEngine` على تاريخ أسعار حقيقي، أول ما يبقى عندنا وصول شبكة فعلي (VPS)

### Phase 6b — Dashboard تفاعلي (لو حبينا)
- تعديل Risk %, Daily Loss Limit وباقي الإعدادات من الواجهة نفسها (محتاج backend)

### Phase 7 — Live (فلوس صغيرة جدًا الأول)
- بعد التأكد من نتائج Backtest + Paper Trading
- زيادة رأس المال تدريجيًا فقط بعد نتائج مستقرة

## مبادئ ثابتة (متتغيرش مهما كان)

1. حجم الصفقة = دالة في مكان الـStop Loss، مش رقم دولار ثابت
2. الخسارة اليومية أو الـdrawdown بيقللوا المخاطرة، مايزودوهاش
3. مفيش "تعويض خسارة" بصفقات أكبر
4. فلوس حقيقية = آخر خطوة، مش أول خطوة

## تشغيل الاختبارات

```bash
pip install -r requirements.txt --break-system-packages
python3 tests/test_risk_engine.py
python3 tests/test_scanner.py
python3 tests/test_strategy.py
python3 tests/test_data_layer.py
python3 tests/test_onchain_layer.py
python3 tests/test_execution_engine.py
python3 tests/test_paper_trading.py
python3 tests/test_dashboard_export.py
python3 tests/test_keystore.py
python3 tests/test_signer.py
python3 tests/test_wallet_reputation.py
python3 tests/test_social_signals.py
python3 tests/test_discovery.py
python3 tests/test_geckoterminal.py
python3 tests/test_orchestrator.py
python3 tests/test_backtest.py
python3 tests/test_check_connections.py
```

## فتح الداشبورد

افتح `dashboard/index.html` في أي متصفح — هيحمّل `dashboard/snapshot.json`
تلقائيًا لو حطيته جنبه (فيه مثال جاهز بالفعل)، أو ارفعه يدويًا من الزرار.
مفيش أي backend مطلوب.

## تشغيل مجاني بالكامل (من غير VPS) — GitHub Actions

1. اعمل حساب GitHub مجاني (لو معندكش) على github.com
2. ارفع المشروع ده كـ**repository عام (Public)** — مهم يكون Public عشان Actions يبقى مجاني بلا حدود
3. من تبويب **Settings → Actions → General** في الـrepo، تأكد إن "Allow all actions" مفعّل
4. من تبويب **Settings → Actions → General → Workflow permissions**، اختار **"Read and write permissions"** (عشان الـworkflow يقدر يعمل commit للحالة الجديدة)
5. كده خلاص — الـworkflow (`.github/workflows/run_bot.yml`) هيبدأ يشتغل لوحده كل 15 دقيقة
6. تقدر تشغّله يدويًا فورًا من تبويب **Actions** في الـrepo، تختار "Run Bot Cycle"، وتدوس **"Run workflow"** — مش لازم تستنى أول 15 دقيقة

بعد كذا تشغيلة، هتلاقي `dashboard/snapshot.json` اتحدّث في الـrepo —
نزّله وافتحه في `dashboard/index.html` على جهازك أو موبايلك عشان تشوف
الأداء.

## فحص الاتصال الحي بالمصادر (DexScreener / RugCheck / GeckoTerminal)

كل الـclients والـmappers اتختبرت على بيانات عينة، لكن عمرها ما اتجرّبت على
ردود حقيقية (الشبكة مقفولة في بيئة التطوير). السكريبت `scripts/check_connections.py`
بيجرّب الاتصال فعليًا ويفرّق بين 3 أنواع فشل: **الشبكة** (مفيش اتصال/IP محجوب)،
**العنوان** (endpoint غلط)، **الشكل** (الرد جه بحقول مختلفة عن المتوقع — وبيعرض
المفاتيح الفعلية عشان الـmapper يتصلّح).

من GitHub: تبويب **Actions ← Check Connections ← Run workflow**، وبعدين افتح
التشغيلة وشوف **Summary**. التقرير كمان بيتحفظ في `data/connection_report.json`.
exit code = 1 لو أي فحص أساسي فشل (GeckoTerminal اختياري). اختبارات السكريبت:
`python3 tests/test_check_connections.py`
