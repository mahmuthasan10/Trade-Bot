# 🤖 MASTER TRADING BOT v3.0

**Tam Metrikli Mimari Dokümantasyonu**

```
┌─────────────┐    ┌─────────────┐    ┌─────────────┐
│ UNIVERSAL   │    │ DAY TRADING │    │   FIRSAT    │
│   (Swing)   │    │  (Scalping) │    │ (Kovalayan) │
│ Haftalık+4S │    │   15m + 5m  │    │   15m + 5m  │
└──────┬──────┘    └──────┬──────┘    └──────┬──────┘
       │                  │                  │
       └──────────────────┼──────────────────┘
                          │
              ┌───────────┴───────────┐
              │   MASTER RISK ENGINE  │
              │  3 Katmanlı Hiyerarşi │
              │  Net PnL Hard Kill    │
              │  Recovery + Spread    │
              └───────────────────────┘
```

> **3 Strateji** · **5 Zaman Dilimi** (Haftalık > 4S > 15m > 5m) · **3 Risk Katmanı** (Portföy + Strateji + Hisse) · **Adaptif Karar Motoru** (ATR + Hacim + ADX + Spread)

---

# 📋 1. Sistem Kartı

Bu tablo tüm dokümantasyonun **merkezi referansıdır.** Detaylara dalmadan önce büyük resmi burada gör.

### Zaman & Kapsam

| | Universal (Swing) | Day Trading | Fırsat |
|---|---|---|---|
| **Ana TF** | Haftalık + Günlük | 15m yön · 5m tetik | 15m yön · 5m tetik |
| **Giriş TF** | 4 Saatlik | 5m mum kapanışı | 5m mum kapanışı |
| **Piyasalar** | BTC · Kripto · ABD · FX · Emtia | BTC · Kripto · ABD | BTC · Kripto · ABD |
| **Aktif Seans** | 7/24 kripto / piyasa saatleri | UTC 07–22 | UTC 07–22 |
| **Gece Pozisyonu** | ✅ Evet (swing doğası) | ❌ 22:00 UTC kapan | ❌ 22:00 UTC kapan |

### Hedef & Risk

| | Universal | Day Trading | Fırsat |
|---|---|---|---|
| **Günlük Hedef** | Aylık %30–60 | %1.0 taban | %1.0 taban · tavan yok |
| **Risk / İşlem** | %1.0–1.5 | %0.20–0.30 | %0.20–0.36 (mod bazlı) |
| **Min R:R** | 1:2.5 | 1:1.8 | 1:1.8 |
| **Max Açık Poz.** | 5–8 | 5 | Normal: 5 · Momentum: 3 |
| **Komisyon Etkisi** | Düşük (az işlem) | Kritik (max 7/gün) | Kritik (max 7/gün) |

### Stop & TP

| | Universal | Day Trading | Fırsat |
|---|---|---|---|
| **Stop** | ATR(14,Günlük) × 2.5 | Adaptif: ATR(14,5m) × 1.2–1.5 | ATR(14,5m) × 0.8–1.2 |
| **TP1** | +%5–7 → %40 çıkış | +1R → %50 çıkış | +1R → %50 çıkış |
| **TP2** | +%12–15 → %40 çıkış | +2R → %35 çıkış | +2R → %35 çıkış |
| **TP3** | +%25–40 trailing | +3R trailing ×1.2 | Mod bazlı: ×0.6–1.2 |

### Koruma Mekanizmaları

| | Universal | Day Trading | Fırsat |
|---|---|---|---|
| **Günlük Kayıp** | -%5 | -%1.5 | -%7 |
| **Ardışık Kayıp** | 3 stop → dur | 3 stop → gün kapanır | Adaptif karar motoru |
| **Hisse Limit** | %3.0 | %1.5 → kill | %1.5 → adaptif |
| **Min Sinyal** | >65/100 | 4 puan (0–12) | 4–6 puan (mod bazlı) |
| **Time-in-Trade** | — (swing) | ±0.3 ATR · 30dk | ±0.3 ATR · 30dk |
| **Spread Gate** | Normal kontrol | >2× ort. = red | >2× ort. = red |

---

# 📈 2. Universal Bot — Swing Stratejisi

> **Haftalık + Günlük + 4S** · BTC + Kripto + ABD + FX + Emtia · Yıllık %30–60

Bu bot uzun vadeli trendleri yakalar. Haftada birkaç işlem açar, günlerce-haftalarca tutar. Gürültüyü filtreler, büyük hamleleri bekler.

## 2.1 Sinyal Skoru Ağırlıkları

Toplam skor 0–100 arasıdır. Her katman kendi alt bileşenlerinden ağırlıklı puan üretir:

| Katman | Ağırlık | Bileşenler | Timeframe |
|---|---|---|---|
| **Teknik Analiz** | %40 | MA50/200 kesişimi · MACD · EMA21/55 · ADX · Bollinger · OBV | Haftalık + Günlük |
| **Piyasaya Özgü** | %30 | BTC: exchange flow, whale, SOPR, MVRV · ABD: dark pool, options · FX: COT | Günlük + Haftalık |
| **Makro & Sentiment** | %20 | VIX · DXY · Fed faizi · CPI · Fear&Greed · NLP haberler · ETF akışı | Günlük (binary filtre) |
| **Evren Filtresi** | %10 | Temel analiz eşiği · hacim · likidite · market cap | Haftalık tarama |

## 2.2 Teknik Analiz Detayları (%40)

| Gösterge | Long Koşulu | Short Koşulu | Etki |
|---|---|---|---|
| **MA50/MA200** (Haftalık) | Golden cross + hacim onay | Death cross oluşumu | %12 |
| **EMA21/EMA55** (Günlük+4S) | EMA21 > EMA55, yükseliyor | EMA21 < EMA55, düşüyor | %8 |
| **MACD** (12/26/9) | Histogram negatif → pozitif | Histogram pozitif → negatif | %8 |
| **ADX** (14, Günlük) | > 25 = güçlü trend | < 20 = trend yok, bekle | %5 |
| **Bollinger** (20p, 2std) | Alt banda dokunma + iç kapanış | Üst banda temas + baskı | %4 |
| **OBV/Hacim** (Günlük) | Kırılış + hacim > ort. %130 | Düşen hacimle yükselen fiyat | %3 |

## 2.3 On-Chain — Bitcoin Özel (%30 içinde)

| Metrik | Al Sinyali | Sat Sinyali | Etki | Kaynak |
|---|---|---|---|---|
| **Exchange Net Flow** | Net çıkış — HODLing | Borsaya büyük giriş | %10 | CryptoQuant · Glassnode |
| **Whale Tx** (>100 BTC) | Akümülasyon cüzdanına | Exchange adresine | %8 | Glassnode · Whale Alert |
| **SOPR** | < 1 = zararına satış bitti | >> 1 = kâr realizasyon | %5 | Glassnode |
| **MVRV Z-Score** | < 0 = tarihi dip | > 7 = aşırı ısınma | %4 | LookIntoBitcoin |
| **Funding Rate** | Negatif = aşırı short | > %0.1 = aşırı kaldıraç | %3 | CoinGlass · Bybit |

## 2.4 Al / Sat Koşulları

**AL koşulları** (yukarıdan aşağıya öncelik sırası):

- `Zorunlu` → Toplam sinyal skoru > 65 (güçlü) veya > 58 (standart)
- `Çok yüksek` → MA50/MA200 golden cross, haftalık grafikte + hacim teyidi
- `Yüksek` → Exchange flow 3+ gün negatif (kripto), MACD histogram pozitife döndü
- `Orta` → RSI < 38 + yukarı dönüş (günlük), Funding rate nötr/negatif

**SAT koşulları:**

- `Zorunlu` → Toplam skor < 40 ise açık long kapat
- `Çok yüksek` → MA50 death cross haftalık grafikte
- `Yüksek` → MVRV Z > 6 (aşırı ısınma)
- `Otomatik` → ATR trailing stop TP2 sonrası tetiklenir

## 2.5 Risk Parametreleri (Piyasa Bazlı)

| Kural | Kripto | ABD Hissesi | FX / Emtia |
|---|---|---|---|
| **Tek pozisyon risk** | %1.0–1.5 | %1.0 | %0.8 |
| **Stop** | ATR(14,G) × 2.5 | ATR(14,G) × 2.0 | ATR(14,G) × 1.8 |
| **Trailing başlangıç** | +%3 kâr | +%2 kâr | +%2 kâr |
| **Trailing mesafe** | ATR × 1.5 | ATR × 1.2 | ATR × 1.0 |
| **TP1** (%40 çıkış) | +%5–7 | +%3–5 | +%2–4 |
| **TP2** (%40 çıkış) | +%12–15 | +%8–10 | +%6–8 |
| **TP3** (%20 trailing) | +%25–40 | +%15–20 | +%12–16 |
| **Günlük kayıp** | -%5 | -%5 | -%4 |
| **Hisse limit** | %3.0 | %3.0 | %2.5 |
| **Max açık poz.** | 5–8 | 5–8 | 4–6 |

---

# ⚡ 3. Day Trading Bot

> **5m + 15m** · Günlük %1 Taban + Kill Switch · Intraday — Gece Pozisyonu Yok

Gün içi fırsatları yakalar. Hızlı giriş-çıkış, sıkı risk, mekanik disiplin. Gece asla açık pozisyon bırakmaz.

## 3.1 Günlük %1 Hedefinin Matematiği

```
Beklenti = (Win Rate × Ort. Kazanç) − (Loss Rate × Ort. Kayıp)
```

> ⚠️ Mimari %45 WR üzerine inşa edilmiştir. %55 hedef olarak korunur.

**Seçilen senaryo (B):** 5–7 işlem/gün · %0.30 risk · 1:2.5 R:R · %55 hedef WR

| Senaryo | İşlem | Risk | R:R | WR | Beklenti | Günlük | Durum |
|---|---|---|---|---|---|---|---|
| A — Az işlem | 3–4 | %0.50 | 1:3 | %45 | +%0.225 | +%0.67–0.90 | Sınırda |
| **B — Dengeli** ✅ | **5–7** | **%0.30** | **1:2.5** | **%55** | **+%0.2775** | **+%1.0–1.7** | **Hedef** |
| C — Scalp | 10–15 | %0.20 | 1:1.5 | %60 | +%0.09 | +%0.9–1.35 | Komisyon riski |
| D — Yüksek risk | 2–3 | %1.00 | 1:2 | %50 | +%0.50 | +%1.0 | Drawdown riski |

## 3.2 Yön Belirleme — 15m Çerçevesi

Bu katmanda emir açılmaz — sadece long mu short mu belirlenir:

| Gösterge | Long | Short | Ağırlık |
|---|---|---|---|
| **EMA Yapısı** (9/21/55) | EMA9 > 21 > 55 | EMA9 < 21 < 55 | %25 |
| **VWAP Konumu** | Fiyat üstünde | Fiyat altında | %20 |
| **Piyasa Yapısı** (HH/HL) | Higher High + Higher Low | Lower High + Lower Low | %25 |
| **Order Block** | Bullish OB üstünde | Bearish OB altında | %15 |
| **ADX** (14, 15m) | > 20 = devam | < 18 = pencere kapanır | %15 |

## 3.3 Giriş Sinyalleri — 5m Puan Sistemi

Minimum **4 puan** gerekli. Maksimum toplam: 12 puan.

| Sinyal | Long Tetik | Short Tetik | Puan |
|---|---|---|---|
| **Momentum** (EMA9/21 kesişim) | Yukarı kesişim | Aşağı kesişim | 2 |
| **VWAP dönüşü** (±%0.1 bant) | Yaklaşım + yukarı fırlama | Yaklaşım + aşağı kırılış | 2 |
| **Hacim spike** (>20m ort. ×1.5) | Yükselen hacim + fiyat | Yükselen hacim − fiyat | 2 |
| **RSI momentum** (RSI 7) | > 50 + yukarı ivme | < 50 + aşağı ivme | 1 |
| **Likidite avı** (sweep) | Dip süpürme + yukarı kapanış | Zirve süpürme + aşağı kapanış | 2 |
| **BOS / CHoCH** (yapı kırılımı) | Son LL yukarı kırıldı | Son HH aşağı kırıldı | 2 |
| **Stochastic RSI** (3,3,14) | <20'den yukarı kesim | >80'den aşağı kesim | 1 |

**Puana göre pozisyon boyutu:**

| Puan | Risk | Kalite |
|---|---|---|
| < 4 | ❌ İşlem yok | Yetersiz |
| 4 | %0.20 — küçük lot | Zayıf ama geçerli |
| 5 | %0.25 — standart | Normal |
| 6 | %0.30 — tam lot | Güçlü |
| 7+ | %0.30 — değişmez | Mükemmel (limit aşılmaz) |

## 3.4 Adaptif ATR Çarpanı

Sabit çarpan yerine **volatilite rejimine bağlı** dinamik stop:

```
ATR medyanı = son 100 periyodun medyan ATR(14, 5m) değeri

Eğer ATR şu an < medyan  → çarpan = 1.2 (dar stop, yüksek R:R)
Eğer ATR şu an ≥ medyan  → çarpan = 1.5 (geniş stop, sweep koruması)
```

Pozisyon büyüklüğü ATR çarpanına **ters orantılıdır** — geniş stop kullanıldığında lot otomatik küçülür, %0.30 sabit risk her zaman korunur.

## 3.5 Time-in-Trade

Hareketsiz işlem = ölü sermaye. Momentum kaybeden pozisyonlar otomatik kapatılır:

```
Giriş sonrası 6 mum (30dk) geçti
  VE fiyat giriş fiyatının ±0.3 ATR bandı içinde kaldıysa
    → Piyasa fiyatından kapat (TP/SL beklenmez)

İstisna: Fiyat bant dışına çıktıysa süre sıfırlanır, işlem devam eder
```

## 3.6 Kill Switch — Değiştirilemez

Bu kurallar sistem tarafından zorlanır, override edilemez:

| Koşul | Eşik | Eylem |
|---|---|---|
| Günlük toplam kayıp | -%1.5 | Gün kapanır, sistem stop |
| Ardışık kayıp | 3 üst üste | Gün kapanır, soğuma |
| Günlük hedef | +%1.0 | Pasif moda geç |
| ADX çok düşük | < 18 + BB daralma | O 15dk penceresi atlanır |
| Spread genişleme | > 2.5× normal | Yeni emir gönderilmez |
| Flash crash | 60sn içinde > -%2 | Tüm emirler iptal · 2 saat bekle |
| Spread kontrolü (evrensel) | > 2× ort. spread | İşlem reddedilir |

## 3.7 Kademeli TP & Trailing

| Seviye | Hedef | Pozisyon Kapat | Sonraki Adım |
|---|---|---|---|
| **TP1** — hızlı kâr | 1R | %50 kapat | Stop → giriş fiyatı (BE) |
| **TP2** — ana hedef | 2R | %35 kapat | Trailing başlat (ATR × 0.8) |
| **TP3** — trailing | 3R+ | %15 trailing ile sür | Tetiklenene kadar tut |

> Min R:R eşiği: **1.8R** — altında sinyal reddedilir, işlem açılmaz.

## 3.8 Günlük P&L — Komisyon Dahil

| Senaryo | İşlemler | Brüt | Komisyon | Net |
|---|---|---|---|---|
| 🟢 Harika gün | 4W 2L (6) | +%1.80 | -%0.48 | **+%1.32** |
| 🟢 Hedef gün | 3W 2L (5) | +%1.65 | -%0.40 | **+%1.25** |
| 🟡 Breakeven | 2W 2L (4) | +%0.90 | -%0.32 | +%0.58 |
| 🔴 Kötü gün | 1W 3L (4) | -%0.15 | -%0.32 | -%0.47 |
| 🔴 Kill Switch | 0W 3L (dur) | -%0.90 | -%0.24 | **-%1.14 maks** |

## 3.9 Filtreler — 3 Seviye

| Seviye | Kapsam | Koşul | Eylem |
|---|---|---|---|
| **1 — Makro haber** | FOMC · CPI · NFP · GDP · ETF · hack | ±30–60dk yasak | İşlem açılmaz |
| **2 — Piyasa rejimi** | ADX + BB + ATR + Hacim + Spread | 5'ten 2+ bloke | Pencere atlanır |
| **3 — Seans likidite** | Hacim · Pazar günü · Flash crash | Düşük hacim / ani hareket | Yasak veya 30dk bekle |

---

# 🎯 4. Fırsat Kovalayan Bot

> **5m + 15m** · Günlük %1 Taban — Tavan Yok · 4–5 Aktif Sepet · Dinamik Mod Geçişi

Bu bot "ev parası" mantığıyla çalışır. %1 taban hedefini vurduktan sonra kazancı koruyarak agresifleşir. Tavan yok — iyi günlerde %3–5 çıkabilir.

## 4.1 Dört Mod

Mod geçişi günlük kümülatif kâra göre **otomatik** yapılır:

```
%0 – %1  →  NORMAL     Standart kurallar
%1 – %2  →  SERBEST    Lot ×1.2, BE stop kilitlenir
%2 – %3  →  AGRESİF    Lot ×1.3, sıkı stop, seçici sinyal
%3+      →  MOMENTUM   Lot ×1.0'a döner, en sıkı stop, çok seçici
```

Detaylı parametre tablosu:

| Parametre | Normal | Serbest | Agresif | Momentum |
|---|---|---|---|---|
| **Lot** | ×1.0 | ×1.2 | ×1.3 | ×1.0 (geri döner) |
| **Risk/işlem** | %0.20–0.30 | %0.24–0.36 | %0.26–0.39 | %0.20–0.30 |
| **Min sinyal** | 4p | 4p | 5p ↑ | 6p ↑↑ |
| **Stop** | ATR ×1.2 | ATR ×1.2 | ATR ×0.8 ↓ | ATR ×0.6 ↓↓ |
| **TP1 kapat** | %50 | %50 | %40 | %50 |
| **TP3 trailing** | ATR ×1.2 | ATR ×1.0 | ATR ×0.8 | ATR ×0.6 |
| **Max açık poz.** | 5 | 5 | 4 | 3 |
| **BE stop** | Hayır | ✅ Tüm kârlılar | ✅ Tüm kârlılar | ✅ Tüm kârlılar |

## 4.2 Adaptif Karar Motoru

Hisse başı %1.5 zarar sonrası sabit "dur" yerine **4 metrik okunur**, 0–100 puan üretilir:

| Metrik | İyi (>65) | Kötü (<40) | Ağırlık |
|---|---|---|---|
| **ATR Trendi** | Son 3 değer düşüyor | Hızla yükseliyor | %30 |
| **Hacim** | Vol/20m ort: 0.8–2.0× | < 0.5× | %25 |
| **ADX** | 22–35 ideal bant | <15 veya >40 | %25 |
| **Spread** | Anlık/1h ort < 1.3× | > 2.0× | %20 |

**Karar:**

| Skor | Karar | Lot |
|---|---|---|
| ≥ 65 | ▶️ DEVAM ET | ×1.0 |
| 40–64 | ⚠️ LOT KÜÇÜLT | ×0.5 |
| < 40 | ⛔ ASKIYA AL | ×0.0 (o hisse dondu) |

> ❌ **Averaging down (zarara ekleme) her senaryoda kesinlikle yasak.**

## 4.3 %1 Eşiği — Kazanç Kilidi

Günlük kümülatif kâr %1'e ulaştığında:

1. Tüm kârdaki açık pozisyonlara **BE stop** kilitlenir (artık zarar edemez)
2. Sistem **Serbest Mod**'a geçer (lot ×1.2, TP3 ağırlık kazanır)
3. Gün en kötü ~%0.5+ ile kapanır (BE stop garantisi)

## 4.4 Sepet Risk Dağılımı

| Slot | Risk Bütçesi | Max Poz. | Korelasyon Kuralı |
|---|---|---|---|
| **Slot 1** (Ana) | %2.5 | 2 | Diğer slotlarla <%70 korelasyon |
| **Slot 2** | %2.5 | 2 | Slot 1 ile sektör farkı |
| **Slot 3** | %2.0 | 1 | Düşük korelasyon |
| **Slot 4** | %2.0 | 1 | Tercihen ters korelasyon |
| **Slot 5** (opsiyonel) | %1.0 | 1 | Deney slotu |
| **TOPLAM** | **%10** | **7** | Günlük hard kill |

---

# 🛡️ 5. Çok Katmanlı Risk Hiyerarşisi

```
Katman 1: PORTFÖY (Net PnL)     ← Tüm botları etkiler
Katman 2: STRATEJİ (Bağımsız)   ← Sadece kendi botunu etkiler
Katman 3: HİSSE/COİN            ← Sadece o enstrümanı etkiler
```

Her katman bağımsız çalışır. Üst katman tetiklendiğinde alt katmanları ezer.

## 5.1 Katman 1 — Portföy (Kümülatif Net PnL)

> Hard Kill **kümülatif Net PnL** üzerinden hesaplanır. Fırsat Botu -%7, Universal Bot +%3 ise net kayıp -%4'tür — Hard Kill tetiklenmez.

| Portföy Kaybı | Universal | Day Trading | Fırsat |
|---|---|---|---|
| **%0–2.5** Normal | Tam aktif | Tam aktif | Tam aktif (Normal) |
| **%2.5–5** Uyarı | Lot -%20, +1 skor | Lot -%20, +1 skor | Lot -%20, Serbest kısıtlı |
| **%5–8** Kırmızı | Yeni poz. yok | Max 2 açık, lot -%40 | Savunma, max 2 slot |
| **%8–10** Acil | Donduruldu | Donduruldu | Çok küçük lot |
| **>%10** 🔴 KILL | 🚫 Tüm sistem kapanır | 🚫 Tüm sistem kapanır | 🚫 Tüm sistem kapanır |

## 5.2 Katman 2 — Strateji (Bağımsız)

Her botun kendi günlük limiti var. Birinin patlaması diğerlerini etkilemez:

| Strateji | Günlük Limit | Limit Aşılınca | Diğerlerine Etkisi |
|---|---|---|---|
| Universal | -%5 | Yeni pozisyon açmaz | ❌ Yok |
| Day Trading | -%1.5 | O gün tamamen kapanır | ❌ Yok |
| Fırsat | -%7 | Bot kapanır | ❌ Yok |

## 5.3 Katman 3 — Hisse/Coin

| Strateji | Limit | Aşılınca |
|---|---|---|
| Universal | %3.0 | Adaptif karar (mevcut poz. korunur) |
| Day Trading | %1.5 | Kill — o hisse o gün kapanır |
| Fırsat | %1.5 | Adaptif karar → DEVAM / KÜÇÜLT / ASKIYA |

## 5.4 Recovery Modu

Kill Switch tetiklendikten sonra ertesi gün **otomatik koruma**:

```
Önceki gün Kill Switch çalıştıysa:
  → Lot büyüklüğü %50 küçülür (%0.30 → %0.15)
  → 3 ardışık kârlı işlem = normal moda dön
  → 3 kâr olmazsa gün boyunca Recovery'de kal
```

Bu mekanizma **intikam trading'ini mekanik olarak engeller.** Duygusal karar alma riski minimize edilir.

## 5.5 Spread Kontrolü — Evrensel

Tüm botlar için geçerli, sinyal skoru ne olursa olsun:

```
Anlık spread / 60dk ortalama spread > 2.0×  →  İŞLEM REDDEDİLİR

7 puanlık mükemmel sinyal bile olsa, spread şişmişse girilmez.
```

> **Faz 2:** Orderbook derinliği (Liquidity Depth) — lot büyüklüğünün 3× likiditesi kontrol edilecek.

---

# 🔗 6. Unified Sinyal Skoru & Piyasa Ayarları

## 6.1 Ortak Formül

3 bot aynı sinyal altyapısını kullanır. Fark: ağırlıklar ve eşikler.

| Katman | Universal | Day | Fırsat |
|---|---|---|---|
| **Teknik Analiz** | %40 | %55 | %55 |
| **Piyasaya Özgü** | %30 | %25 | %25 |
| **Makro & Sentiment** | %20 | %10 | %10 |
| **Rejim Bonusu** | %10 | %10 | %10 |
| **Haber Kill** | Binary geçit (skora girmez) | Binary geçit | Binary geçit |

**Karar eşikleri:**

| Skor | Universal | Day / Fırsat |
|---|---|---|
| **> 70** | AL — çok güçlü | AL — Momentum eşiği (6p) |
| **65–70** | AL — güçlü | AL — Agresif eşiği (5p) |
| **58–65** | AL — minimum | AL — Normal/Serbest (4p) |
| **45–58** | Bekle | Bekle — işlem yok |
| **< 45** | Bekle | Bekle |
| **< 35** | SAT / Kapat | Açık long kapat |

## 6.2 Bitcoin & Büyük Kriptolar

| | Universal | Day / Fırsat |
|---|---|---|
| **Ekstra veri** | On-chain: exchange flow · whale · SOPR · MVRV · funding | Funding rate · OI değişimi · liquidation haritası |
| **Timeframe** | Haftalık + Günlük + 4S | 15m + 5m |
| **Stop** | ATR(14,G) × 2.5 | ATR(14,5m) × 1.2–1.5 (adaptif) |
| **Saatler** | 7/24 | UTC 07–22 |
| **BTC Dom. filtresi** | Altcoin: Dom < %55 gerekli | Günlük kontrol |
| **Özel** | BTC Dominance + Fear&Greed | Funding spike > %0.1 = dur |

## 6.3 ABD Hisseleri (NASDAQ/NYSE)

| | Universal | Day / Fırsat |
|---|---|---|
| **Ekstra veri** | Dark pool · options flow · insider · SEC | Pre-market hacim · gap analizi · options chain |
| **Saatler** | EST 09:30–16:00 | EST 09:30–11:30 + 14:30–16:00 |
| **Earnings** | 2 gün önce/sonra küçük lot | Earnings günü yasak |
| **Haberler** | CPI · Fed · NFP ±45dk yasak | Aynı + bireysel hisse haberleri |
| **Özel** | Options flow takibi | Opening range breakout |

## 6.4 Forex & Emtia

| | Universal | Day / Fırsat |
|---|---|---|
| **Ekstra veri** | COT raporu · haftalık faiz farkı | Ekonomik takvim · merkez bankası |
| **Timeframe** | Haftalık + Günlük + 4S | 4S + 1S + 15m |
| **En iyi seans** | Londra+NY overlap: UTC 13–17 | UTC 13–17 |
| **Merkez bankası** | Toplantı ±60dk yasak | Aynı + tahmin sapmaları |
| **Spread** | Normal + evrensel kural | Asya seansında ekstra dikkat |

---

# 💻 7. Master Pseudocode

## 7.1 Strateji Seçim Motoru

```python
def select_active_strategies(portfolio_net_pnl, hour_utc):
    active = []
    regime = detect_global_regime()
    is_recovery = previous_day_kill_switch_triggered()
    lot_multiplier = 0.5 if is_recovery and not recovery_cleared() else 1.0

    # Universal — haftalık trend onay gerekli
    if weekly_trend_confirmed() and regime != 'KAOTIK':
        if portfolio_net_pnl < 0.05:
            active.append('UNIVERSAL')

    # Day Trading — likid seans + trend rejimi
    if regime == 'TREND' and 7 <= hour_utc <= 22:
        if portfolio_net_pnl < 0.015:
            active.append('DAY_TRADING')

    # Fırsat — her zaman aktif (kendi mod sistemiyle)
    if portfolio_net_pnl < 0.07:
        active.append('FIRSAT')

    return active, lot_multiplier
```

## 7.2 Risk Gatekeeper (Net PnL)

```python
def portfolio_risk_gate(net_pnl_loss):
    if net_pnl_loss >= 0.10:
        kill_all_positions()
        set_next_day_recovery_mode()
        return {'lot_mult': 0, 'extra_score': 99, 'max_pos': 0}
    if net_pnl_loss >= 0.08:
        return {'lot_mult': 0.3, 'extra_score': 2, 'max_pos': 2}
    if net_pnl_loss >= 0.05:
        return {'lot_mult': 0.6, 'extra_score': 1, 'max_pos': 3}
    if net_pnl_loss >= 0.025:
        return {'lot_mult': 0.8, 'extra_score': 0, 'max_pos': 5}
    return {'lot_mult': 1.0, 'extra_score': 0, 'max_pos': 8}
```

## 7.3 Recovery Modu

```python
def recovery_cleared():
    return get_today_consecutive_wins() >= 3

def get_effective_lot(base_lot, recovery_active):
    if recovery_active and not recovery_cleared():
        return base_lot * 0.50
    return base_lot
```

## 7.4 Adaptif ATR Stop

```python
def get_atr_multiplier(symbol):
    atr_current = ATR(14, '5m')
    atr_median = median(ATR(14, '5m', lookback=100))

    if atr_current < atr_median:
        return 1.2   # sakin piyasa — dar stop
    else:
        return 1.5   # çalkantılı — geniş stop
```

## 7.5 Time-in-Trade

```python
def check_time_in_trade(position):
    bars = count_bars_since(position.entry_time, '5m')
    atr = ATR(14, '5m')
    drift = abs(current_price - position.entry_price)

    if bars >= 6 and drift < atr * 0.3:
        close_at_market(position)
        log('TIME_EXIT: momentum yok')
        return True
    return False
```

## 7.6 Spread Kontrolü

```python
def spread_gate(symbol):
    ratio = get_current_spread(symbol) / get_avg_spread(symbol, '60m')

    if ratio > 2.0:
        log(f'SPREAD_BLOCK: {symbol} {ratio:.1f}x')
        return False
    return True
```

## 7.7 Fırsat Mod Seçimi

```python
def get_firsat_mode(daily_pnl):
    if daily_pnl >= 0.030:
        return 'MOMENTUM', {'lot': 1.0, 'score': 6, 'trail': 0.6}
    elif daily_pnl >= 0.020:
        return 'AGRESIF',  {'lot': 1.3, 'score': 5, 'trail': 0.8}
    elif daily_pnl >= 0.010:
        return 'SERBEST',  {'lot': 1.2, 'score': 4, 'trail': 1.0}
    else:
        return 'NORMAL',   {'lot': 1.0, 'score': 4, 'trail': 1.2}

def on_1pct_threshold_crossed():
    for pos in open_positions:
        if pos.unrealized_pnl > 0:
            pos.stop_loss = pos.entry_price   # BE stop
    set_mode('SERBEST')
```

## 7.8 Adaptif Karar Motoru

```python
def adaptive_decision(symbol):
    atr = ATR(14, '5m', count=3)
    atr_score = 80 if atr[-1] < atr[0] else 40 if atr[-1] == atr[0] else 15

    vol_ratio = current_volume() / avg_volume(20)
    vol_score = 80 if 0.8 <= vol_ratio <= 2.0 else 40 if vol_ratio > 2.0 else 10

    adx = ADX(14)
    adx_score = 85 if 22 <= adx <= 35 else 50 if 15 <= adx < 22 else 10

    spread_ratio = current_spread() / avg_spread(60)
    spread_score = 90 if spread_ratio < 1.3 else 50 if spread_ratio < 2.0 else 5

    total = atr_score*.30 + vol_score*.25 + adx_score*.25 + spread_score*.20

    if total >= 65: return 'DEVAM', 1.0
    if total >= 40: return 'KUC_LOT', 0.5
    return 'ASKIYA', 0.0
```

---

# 🚀 8. Deployment Yol Haritası

| Adım | Hedef | Süre | Teknoloji |
|---|---|---|---|
| **1** Veri katmanı | Canlı fiyat + on-chain + haber | 1 hafta | asyncio · WebSocket · CCXT |
| **2** Gösterge motoru | TA her 5m kapanışında | 3–5 gün | TA-Lib · pandas-ta · pytest |
| **3** Sinyal motoru | Puan sistemi 3 strateji | 1 hafta | Pure Python modüler |
| **4** Risk motoru | Kill + mod + adaptif + Recovery + Spread | 1 hafta | Ayrı process (kritik) |
| **5** Backtesting | 2020–2024 BTC tüm stratejiler | 2 hafta | Backtrader / vectorbt |
| **6** Monte Carlo | 1000× rastgele senaryo | 3–5 gün | NumPy simülasyon |
| **7** Paper trading | 30 gün canlı simüle | 30 gün | Broker sandbox |
| **8** Canlı küçük lot | Portföyün %10'u · 30 gün | 30 gün | Gerçek hesap · min risk |

> **Faz 2 notu:** Self-Learning ağırlık sistemi 500+ işlem verisi toplandıktan sonra implement edilecek. Faz 1'de veri toplama altyapısı hazırlanacak — her işlemin tetik indikatörü, sonucu ve piyasa rejimi kaydedilecek.

---

# 📊 9. Stres Testi & Kârlılık Modeli

> Mimariyi **%45 WR'a göre inşa et**, %55'i hedef olarak tut. Kötümser senaryo bile pozitif EV üretmeliyse sistem sağlamdır.

## 9.1 Üç Senaryo — Day Trading

| Metrik | İyimser | Baz (Gerçekçi) | Kötümser |
|---|---|---|---|
| Win Rate | %50 | %45 | %40 |
| R:R | 1:2.5 | 1:2.2 (slippage dahil) | 1:2.0 |
| Risk/İşlem | %0.30 | %0.30 | %0.30 |
| İşlem/Gün | 6 | 5 | 4 |
| Beklenti/İşlem | +%0.225 | +%0.132 | +%0.060 |
| Günlük Brüt | +%1.35 | +%0.66 | +%0.24 |
| Komisyon (~%0.16) | -%0.96 | -%0.80 | -%0.64 |
| **Günlük Net** | **+%0.39** | **-%0.14** | **-%0.40** |
| Aylık (22 gün) | +%8.58 | -%3.08 | -%8.80 |
| **Pozitif EV?** | ✅ Güçlü | ⚠️ Sınırda | ❌ Optimizasyon gerek |

> ⚠️ **Kritik bulgu:** Baz senaryo komisyon dahil negatife dönüyor. Maker rebate veya düşük komisyonlu borsa seçimi **stratejik önceliktir.**

## 9.2 Komisyon Hassasiyeti

| Komisyon (RT) | İyimser | Baz | Kötümser | Not |
|---|---|---|---|---|
| %0.16 (standart) | +%0.39 | -%0.14 | -%0.40 | Mevcut varsayım |
| %0.10 (maker rebate) | +%0.75 | **+%0.16** | -%0.16 | Baz pozitife döner |
| %0.06 (VIP tier) | +%0.99 | +%0.36 | ±%0.00 | Kötümser bile breakeven |

## 9.3 Recovery Stres Testi

| Senaryo | Gün 1 | Gün 2 | Gün 2 Etki |
|---|---|---|---|
| Normal başlangıç | -%1.14 Kill | %0.30 risk → normal | Telafi süresi uzar |
| Recovery başlangıç | -%1.14 Kill | %0.15 risk → yarı lot | Max kayıp -%0.57 |
| Recovery + 3 kâr | -%1.14 Kill | 3 kârlı → normal mod | Kontrollü toparlanma |

---

**Sonraki adım → Python implementasyonu:**

```
Veri Katmanı → Gösterge Motoru → Sinyal Motoru → Risk Motoru → Emir Motoru → İzleme
```

Her modül bağımsız test edilebilir, modüler mimari.
