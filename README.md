# TCDD YHT Bos Koltuk Takip Botu

Birden fazla TCDD/YHT seferini belirli araliklarla kontrol eder. Bos koltuk
bulursa Telegram uzerinden bildirim gonderir.

## Kurulum

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

`.env` dosyasini `.env.example` formatinda doldur:

```env
TELEGRAM_TOKEN=...
CHAT_ID=...
CHECK_INTERVAL=120
NOTIFICATION_COOLDOWN=1800
```

`searches.json` icindeki seferleri istedigin rota, tarih ve saat araligina gore
duzenle.

## Calistirma

Tek tur kontrol:

```bash
python3 tcdd_bot.py --once --dry-run --no-start-message
```

Surekli takip:

```bash
python3 tcdd_bot.py
```

Lokal web panel:

```bash
python3 web_app.py
```

Tarayicida `http://127.0.0.1:8765` adresini ac.
Panelde `Çalıştır` tek sefer sorgular. `Takibi Başlat`, `.env.local`
icindeki `CHECK_INTERVAL` degerine gore, varsayilan 120 saniyede bir sorgular.
`Takibi Durdur` ile arka plan takibini kapatabilirsin.

## Notlar

- TCDD'nin kullandigi endpoint resmi ve sabit bir public API gibi
  dokumante edilmedigi icin zaman zaman payload veya header degisebilir.
- `401` veya `403` alirsan `https://ebilet.tcddtasimacilik.gov.tr` sitesinde
  DevTools > Network altinda `train-availability` istegini bulup guncel
  `Authorization` header'ini `.env` dosyasina `TCDD_AUTHORIZATION=...` olarak
  birebir ekle. Header `Bearer ...` ile basliyorsa o haliyle yaz.
  Hala `403` alirsan ayni istekteki `Cookie` header'ini `TCDD_COOKIE=...`
  olarak eklemeyi dene.
- Safari/Chrome'da gordugun `User-Agent` farkliysa `.env` icinde
  `TCDD_USER_AGENT=...` olarak birebir ekleyebilirsin.
- Sorgu araligini cok kisa tutma; 120 saniye ve uzeri daha saglikli olur.
- Web panel ekonomi disindaki kabinleri bildirmez.
