"""CV'yi Claude API ile okuyup başvuru profili çıkarır.

Çıkan profil (pozisyonlar, yetenek yılları, iletişim bilgileri, ön yazı)
kullanıcıya terminalde gösterilir; onaylanınca arama ve form cevaplarında kullanılır.
"""
import base64
import json
import re
import zipfile
from pathlib import Path

import anthropic

MODEL = "claude-opus-5-5"

SCHEMA = {
    "type": "object",
    "properties": {
        "first_name": {"type": "string"},
        "last_name": {"type": "string"},
        "email": {"type": "string"},
        "phone_intl": {"type": "string", "description": "Ülke koduyla, ör. +90 534 000 00 00; yoksa boş"},
        "city": {"type": "string", "description": "Şehir, Ülke; ör. Istanbul, Türkiye"},
        "country": {"type": "string", "description": "İngilizce ülke adı, ör. Turkey"},
        "linkedin": {"type": "string"},
        "github": {"type": "string", "description": "GitHub veya portfolyo linki; yoksa boş"},
        "current_title": {"type": "string"},
        "total_years": {"type": "integer", "description": "Toplam profesyonel deneyim yılı"},
        "job_titles": {
            "type": "array", "items": {"type": "string"},
            "description": "LinkedIn'de aranacak 4-8 İngilizce pozisyon adı, en uygundan başlayarak",
        },
        "title_keywords": {
            "type": "array", "items": {"type": "string"},
            "description": "İlan başlığında geçmesi beklenen küçük harfli kelimeler (ör. 'data', 'python', 'machine learning'); 8-20 adet",
        },
        "skills": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"skill": {"type": "string"}, "years": {"type": "integer"}},
                "required": ["skill", "years"],
                "additionalProperties": False,
            },
            "description": "Formlardaki 'kaç yıl deneyim' soruları için 8-20 teknoloji/yetenek ve yılı",
        },
        "cover_letter": {
            "type": "string",
            "description": "İngilizce, 3-5 cümlelik, şirket adı geçmeyen genel ön yazı; birinci tekil şahıs",
        },
    },
    "required": ["first_name", "last_name", "email", "phone_intl", "city", "country", "linkedin", "github",
                 "current_title", "total_years", "job_titles", "title_keywords", "skills", "cover_letter"],
    "additionalProperties": False,
}

PROMPT = (
    "Bu CV'nin sahibi LinkedIn'de Kolay Başvuru ile iş arıyor. CV'den başvuru profilini çıkar. "
    "Pozisyon adlarını CV'deki gerçek deneyime göre seç, abartma. Bilgi CV'de yoksa alanı boş bırak; uydurma."
)


def cv_content(path: Path) -> dict:
    """PDF'i doğrudan, DOCX'i düz metin olarak mesaja koyar."""
    if path.suffix.lower() == ".pdf":
        data = base64.standard_b64encode(path.read_bytes()).decode()
        return {"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": data}}
    with zipfile.ZipFile(path) as z:
        xml = z.read("word/document.xml").decode("utf-8", "ignore")
    text = re.sub(r"<[^>]+>", "", re.sub(r"</w:p>", "\n", xml))
    return {"type": "text", "text": f"CV metni:\n{text}"}


def extract(path: Path, api_key: str) -> dict:
    client = anthropic.Anthropic(api_key=api_key)
    response = client.beta.messages.create(
        model=MODEL,
        max_tokens=16000,
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
        output_config={"effort": "low", "format": {"type": "json_schema", "schema": SCHEMA}},
        messages=[{"role": "user", "content": [cv_content(path), {"type": "text", "text": PROMPT}]}],
    )
    if response.stop_reason == "refusal":
        raise RuntimeError("Model CV'yi işlemeyi reddetti")
    if response.stop_reason == "max_tokens":
        raise RuntimeError("Yanıt yarıda kesildi")
    return json.loads(next(b.text for b in response.content if b.type == "text"))
