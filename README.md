# LinkedIn Auto Apply

Mac'te çalışır. Son 24 saatte yayınlanan remote AI ilanlarını tarar ve **Easy Apply** ile başvurur.

## Kurulum (bir kerelik)

İlk `./run.sh` çalıştırmasında bir Chrome penceresi açılır; orada LinkedIn'e normal şekilde giriş yap.
Oturum `.browser-profile/` klasöründe saklanır, sonraki çalıştırmalarda tekrar giriş gerekmez.
LinkedIn dili İngilizce olsun, CV'n LinkedIn'de kayıtlı olsun.

## Çalıştırma

```
./run.sh                 # gerçek başvuru
DRY_RUN=true ./run.sh    # formları doldurur, göndermez
```

Ayarlar `config.yaml`'da. Sonuçlar `data/applications.csv`'de, başarısız formların ekran görüntüleri `screenshots/`'ta.
Oturum düşerse bot pencerede tekrar giriş yapmanı bekler.

Not: Otomasyon LinkedIn kullanım şartlarına aykırıdır; günlük limiti (`limits.max_applications_per_run`) düşük tut.
