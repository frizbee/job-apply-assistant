# LinkedIn Auto Apply

Remote AI ilanlarını tarar ve sadece **Kolay Başvuru (Easy Apply)** olan ilanlara başvurur.
Şirket sitelerindeki başvuru formlarına girilmez (spam olarak algılanıyor).

## Gereksinimler

- macOS veya Linux, Python 3.10+
- Google Chrome
- Claude Code vb. gerekmez; terminalden çalışır.

## Çalıştırma

```
git clone https://github.com/klncgty/linkedin-auto-apply.git
cd linkedin-auto-apply
./run.sh                 # gerçek başvuru (ilk seferde bağımlılıkları kurar)
DRY_RUN=true ./run.sh    # formları doldurur, göndermez
```

Başlarken terminalde şunlar sorulur (Enter = önceki cevap):

1. **CV dosyası** – PDF/DOCX yolu (dosyayı terminale sürükleyip bırakabilirsin)
2. **Yıllık maaş beklentisi** (USD/EUR)
3. **İngilizce seviyesi** – Native / Akıcı / Orta / Temel
4. **İhbar süresi** (gün)

Cevaplar `data/user_settings.json`'da saklanır. Ardından Chrome penceresi açılır; ilk seferde
LinkedIn'e normal şekilde giriş yap. Oturum `.browser-profile/` klasöründe kalır, sonraki
çalıştırmalarda tekrar giriş gerekmez. LinkedIn dili İngilizce olsun.

Seçilen CV LinkedIn'deki kayıtlı CV listende yoksa başvuru formunda yüklenir.

## Ayarlar

Diğer her şey `config.yaml`'da: arama kelimeleri, ülkeler, filtreler, kişisel bilgiler,
form sorularına cevap kuralları ve günlük limit. Başka biri kullanacaksa `profile` bölümünü
ve `answers` içindeki kişisel cevapları (telefon, LinkedIn/GitHub linki) kendine göre değiştirmeli.

Sonuçlar `data/applications.csv`'de, takılan formların ekran görüntüleri `screenshots/`'ta.

Not: Otomasyon LinkedIn kullanım şartlarına aykırıdır; günlük limiti (`limits.max_applications_per_run`) düşük tut.
