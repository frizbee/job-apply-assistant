# LinkedIn Auto Apply

Her gün 12:00'de (TR) GitHub Actions üzerinde çalışır. Son 24 saatte yayınlanan remote AI ilanlarını tarar ve **Easy Apply** ile başvurur.

## Kurulum (bir kerelik)

1. **LinkedIn çerezini al:** Chrome'da linkedin.com'a giriş yap → `F12` → Application → Cookies → `https://www.linkedin.com` → `li_at` değerini kopyala.
2. **Secret olarak ekle:** Repo → Settings → Secrets and variables → Actions → New secret → isim `LI_AT`, değer kopyaladığın çerez.
   (Terminalden: `gh secret set LI_AT`)
3. **CV'ni LinkedIn'e yükle:** LinkedIn → Settings → Data privacy → Job application settings. Bot, LinkedIn'de kayıtlı CV'yi kullanır; hiç CV yoksa `resume.pdf` dosyasını yükler.
4. **LinkedIn dilini İngilizce yap** (bot buton metinlerini İngilizce okur).
5. **Test et:** Actions → LinkedIn Auto Apply → Run workflow → `dry_run` işaretli. Formlar doldurulur ama gönderilmez; ekran görüntüleri `screenshots` artifact'ında.

Sonrasında her gün otomatik gerçek başvuru yapar.

## Ayarlar — `config.yaml`

| Bölüm | Ne işe yarar |
|---|---|
| `search` | Anahtar kelimeler, lokasyonlar, remote filtresi, seviye |
| `filters` | Başlık/ilan metni/şirket bazlı dahil etme–hariç tutma |
| `limits` | Günlük maksimum başvuru (varsayılan 25) |
| `answers` | Form sorularına cevap kuralları (regex → cevap) |
| `skill_years` | "Kaç yıl deneyim?" sorularında teknolojiye göre yıl |

Bot bir soruya cevap bulamazsa başvuruyu iptal edip atlar ve `screenshots/` içine görüntü kaydeder. Loglarda `? cevapsız soru:` satırlarına bakıp `answers` listesine yeni kural ekle.

## Kayıtlar

- `data/applications.csv` — tüm işlenen ilanlar ve sonuçları (`applied`, `skipped_unanswered`, `title_not_relevant`, …)
- `data/applied.json` — tekrar başvurmayı önleyen liste

## Lokal çalıştırma

```bash
python -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/playwright install chromium
LI_AT=... DRY_RUN=true HEADLESS=false .venv/bin/python apply.py
```

## Bilinmesi gerekenler

- Otomasyon LinkedIn kullanım şartlarına aykırıdır; hesabın kısıtlanabilir. Günlük limiti düşük tut.
- `li_at` çerezi zamanla geçersizleşir (özellikle GitHub sunucularının ABD IP'sinden kullanıldığı için). Workflow "Giriş başarısız" ile düşerse yeni çerezi secret'a ekle.
- LinkedIn arayüzü değişirse seçiciler (`apply.py`) güncellenmeli.
