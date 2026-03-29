# Trade Bot v3.0 - Canliya Gecis Plani

## Mevcut Durum
- 4 servis testnet'te calisiyor (Data Feed, Strategy Engine, Risk Gatekeeper, Execution Engine)
- Docker altyapisi hazir (Redis + TimescaleDB)
- ML engine ayri repoda yapilacak

## Canliya Gecis Adimlari

### Adim 1: Testnet'te Uçtan Uca Test
1. 4 terminalde 4 servisi ayni anda calistir:
   ```
   py -3.13 -m services.data_feed.main
   py -3.13 -m services.strategy_engine.main
   py -3.13 -m services.risk_gatekeeper.main
   py -3.13 -m services.execution_engine.main
   ```
2. Data Feed'in tick gondermesini bekle
3. Strategy Engine'in mum olusturup sinyal uretmesini izle (5m mum = 5 dakika bekle)
4. Risk Gatekeeper'in sinyali alip degerlendirmesini gor
5. Execution Engine'in testnet'te emir gondermesini dogrula

### Adim 2: Binance Gercek API Key Al
1. binance.com → Profil → API Management → Create API
2. "Enable Spot & Margin Trading" AC
3. "Enable Withdrawals" KAPALI birak
4. IP Restriction ekle (kendi IP adresin)
5. `.env` dosyasina yapistir

### Adim 3: `.env` Dosyasini Guncelle
```env
ENVIRONMENT=production
BINANCE_API_KEY=gercek_key
BINANCE_API_SECRET=gercek_secret
BINANCE_TESTNET=false
```

### Adim 4: Docker Production Ayarlari
- Redis sifresi ekle (REDIS_PASSWORD)
- PostgreSQL sifresi guclendir
- Backup stratejisi belirle

### Adim 5: Servisleri Baslat
Ayni sirayla 4 servisi calistir. Loglarda "Testnet" yerine "Mainnet" yazdigini dogrula.

## Guvenlik Kontrol Listesi
- [ ] API key'ler .env'de, git'e COMMIT EDILMEDI
- [ ] Withdrawals kapalı (sadece trade izni)
- [ ] IP restriction eklendi
- [ ] BINANCE_TESTNET=false ayarlandi
- [ ] Redis sifreli
- [ ] PostgreSQL sifresi guclu

## Servis Mimarisi
```
Data Feed → Redis (tick) → Strategy Engine → Redis (sinyal) → Risk Gatekeeper → Redis (onay) → Execution Engine → Binance
```

## Sorun Giderme
- "ModuleNotFoundError" → `py -3.13 -m pip install -r requirements.txt`
- Docker container baslamıyor → `docker rm -f trading-redis trading-timescaledb && docker compose up -d`
- PostgreSQL tablo hatası → `docker exec trading-timescaledb psql -U trading -d trading_bot -c "\dt"`
- API key hatası → .env dosyasini kontrol et, BINANCE_TESTNET degerini dogrula
