# LinkedIn Auto Apply

Mac'te çalışır. Son 24 saatte yayınlanan remote AI ilanlarını tarar ve **Easy Apply** ile başvurur.

## Kurulum (bir kerelik)

1. Safari'de linkedin.com'a giriş yap → ⌥⌘I → Depolama → Çerezler → `li_at` değerini kopyala.
2. Terminalde Anahtar Zinciri'ne kaydet:
   ```
   read -s "?li_at: " v && security add-generic-password -a "$USER" -s linkedin-li_at -w "$v" -U
   ```
3. LinkedIn dili İngilizce olsun, CV'n LinkedIn'de kayıtlı olsun.

## Çalıştırma

```
./run.sh                 # gerçek başvuru
DRY_RUN=true ./run.sh    # formları doldurur, göndermez
```

Ayarlar `config.yaml`'da. Sonuçlar `data/applications.csv`'de, başarısız formların ekran görüntüleri `screenshots/`'ta.
Bot "Giriş başarısız" derse çerezin süresi dolmuştur; 1. ve 2. adımı tekrarla.

Not: Otomasyon LinkedIn kullanım şartlarına aykırıdır; günlük limiti (`limits.max_applications_per_run`) düşük tut.
