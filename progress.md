# Master Bot v3.0 - Deployment & Progress Roadmap

## [cite_start]Faz 1: Altyapı ve Veri Katmanı (Süre: 1 Hafta) 
- [x] GitHub reposunun ve klasör yapısının oluşturulması.
- [x] Docker-compose ile Redis ve PostgreSQL (TimescaleDB) ayağa kaldırılması.
- [ ] `ccxt.pro` entegrasyonu ile Binance/Bybit üzerinden async WebSocket bağlantılarının kurulması.
- [ ] Veri kopmalarına (disconnect) karşı `auto-reconnect` mekanizmasının yazılması.
- [ ] Gelen canlı verinin (Fiyat, Hacim, Spread) Redis Pub/Sub'a aktarılması.

## [cite_start]Faz 2: Gösterge ve Sinyal Motoru (Süre: 1-2 Hafta) 
- [ ] Redis'ten tick datasını alıp 5m ve 15m mumları (OHLCV) oluşturan sınıfın yazılması.
- [ ] `TA-Lib` ile temel göstergelerin (MA, MACD, ADX, ATR, RSI, VWAP) hesaplanması.
- [ ] [cite_start]Unified Sinyal Skoru formülünün (0-100 puan arası) kodlanması[cite: 130].

## [cite_start]Faz 3: Risk ve Karar Motoru (Süre: 1 Hafta) - KRİTİK BİLEŞEN 
- [ ] [cite_start]Portföy düzeyinde Kümülatif Net PnL hesaplayıcısının (Layer 1) yazılması[cite: 116].
- [ ] [cite_start]Hard Kill (-%10) mekanizmasının entegrasyonu[cite: 118].
- [ ] [cite_start]Spread Gatekeeper (Anlık > 2.0x ortalama ise işlemi reddet) kodlaması[cite: 127].
- [ ] [cite_start]Adaptif ATR (Düşük/Yüksek volatilite çarpanları x1.2 / x1.5) hesaplanması[cite: 87].
- [ ] [cite_start]Recovery Mode (Ertesi gün Lot %50) mantığının state olarak tutulması[cite: 125].

## Faz 4: Emir ve Uygulama (Execution) Motoru (Süre: 1 Hafta)
- [ ] Risk onayından geçen sinyallerin borsaya Async Emir (Market/Limit) olarak iletilmesi.
- [ ] [cite_start]TP1 (%50), TP2 (%35) ve Trailing Stop (%15) kademeli çıkış mekanizmasının kodlanması[cite: 95].
- [ ] [cite_start]Time-in-Trade (6 mum / 30dk hareketsizlikte kapat) kontrol döngüsünün yazılması[cite: 91].
- [ ] Gerçekleşen işlemlerin PostgreSQL veritabanına kaydedilmesi.

## [cite_start]Faz 5: Test ve Canlıya Alma (Süre: ~2 Ay) 
- [ ] [cite_start]Backtrader veya VectorBT ile 2020-2024 verisi üzerinde backtest çalıştırılması.
- [ ] [cite_start]1000 iterasyonlu Monte Carlo simülasyonu ile stres testi yapılması.
- [ ] [cite_start]30 gün Paper Trading (Simülasyon) çalıştırılması.
- [ ] [cite_start]Kasanın %10'u ile canlı piyasaya çıkış (Live Deployment).