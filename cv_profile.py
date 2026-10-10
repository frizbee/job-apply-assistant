"""CV'yi OpenRouter'daki ÜCRETSİZ bir dil modeliyle okuyup başvuru profili çıkarır.

Sadece fiyatı 0 olan (":free") modeller kullanılır; bu, her çağrıdan önce OpenRouter'ın
model listesinden doğrulanır. Ücretli modele hiçbir koşulda istek gönderilmez.
"""
import json
import re
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

API = "https://openrouter.ai/api/v1"
# Tercih sırası; listede olmayan/ücretli hale gelenler atlanır, hiçbiri yoksa herhangi bir ücretsiz model
PREFERRED = [
    "nvidia/nemotron-3-super-120b-a12b:free",
    "google/gemma-4-31b-it:free",
    "nvidia/nemotron-3-ultra-550b-a55b:free",
    "apodex/apodex-1.1-mini:free",
]

SCHEMA = {
    "type": "object",
    "properties": {
        "first_name": {"type": "string"},
        "last_name": {"type": "string"},
        "email": {"type": "string"},
        "phone_intl": {"type": "string", "description": "With country code, e.g. +90 534 000 00 00; empty if missing"},
        "city": {"type": "string", "description": "City, Country"},
        "country": {"type": "string", "description": "Country name in English, e.g. Turkey"},
        "linkedin": {"type": "string"},
        "github": {"type": "string", "description": "GitHub or portfolio URL; empty if missing"},
        "current_title": {"type": "string"},
        "total_years": {"type": "integer", "description": "Total years of professional experience"},
        "job_titles": {"type": "array", "items": {"type": "string"},
                       "description": "4-8 standard English job titles exactly as companies post them "
                                      "(e.g. 'Machine Learning Engineer'), best fit first; no 'Freelance', 'Mentor'"},
        "title_keywords": {"type": "array", "items": {"type": "string"},
                           "description": "5-15 lowercase words that appear in relevant job TITLES "
                                          "(e.g. 'engineer', 'machine learning', 'data'), not tools or technologies"},
        "skills": {
            "type": "array",
            "items": {"type": "object", "properties": {"skill": {"type": "string"}, "years": {"type": "integer"}},
                      "required": ["skill", "years"], "additionalProperties": False},
            "description": "8-20 technologies/skills with years of experience",
        },
        "cover_letter": {"type": "string",
                         "description": "3-5 sentence generic English cover letter, first person, no company name"},
    },
    "required": ["first_name", "last_name", "email", "phone_intl", "city", "country", "linkedin", "github",
                 "current_title", "total_years", "job_titles", "title_keywords", "skills", "cover_letter"],
    "additionalProperties": False,
}

PROMPT = (
    "The owner of this CV is looking for jobs. Extract their application profile from the CV below. "
    "Pick job titles that match their real experience, do not exaggerate. If information is missing, "
    "leave the field empty; never invent it. Reply with a single JSON object matching this schema, "
    "no other text:\n{schema}\n\nCV:\n{cv}"
)


class NoFreeModel(Exception):
    pass


def cv_text(path: Path) -> str:
    if path.suffix.lower() == ".pdf":
        from pypdf import PdfReader
        return "\n".join(page.extract_text() or "" for page in PdfReader(str(path)).pages)
    with zipfile.ZipFile(path) as z:
        xml = z.read("word/document.xml").decode("utf-8", "ignore")
    return re.sub(r"<[^>]+>", "", re.sub(r"</w:p>", "\n", xml))


def request(path: str, key: str, body: dict | None = None) -> dict:
    req = urllib.request.Request(
        API + path, data=json.dumps(body).encode() if body else None,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json",
                 "HTTP-Referer": "https://github.com/klncgty/linkedin-auto-apply", "X-Title": "Auto Apply"})
    with urllib.request.urlopen(req, timeout=180) as r:
        return json.loads(r.read())


def free_models(key: str) -> list[tuple[str, bool]]:
    """(model, structured_outputs) listesi; sadece istem ve yanıt fiyatı 0 olan modeller."""
    free = {}
    for m in request("/models", key)["data"]:
        p = m.get("pricing", {})
        if m["id"].endswith(":free") and all(float(p.get(k) or 0) == 0 for k in ("prompt", "completion", "request")):
            free[m["id"]] = "structured_outputs" in m.get("supported_parameters", [])
    ordered = [m for m in PREFERRED if m in free] + sorted(m for m in free if m not in PREFERRED and free[m])
    if not ordered:
        raise NoFreeModel("OpenRouter'da şu an ücretsiz model yok")
    return [(m, free[m]) for m in ordered]


def parse_json(text: str) -> dict:
    m = re.search(r"\{.*\}", text, re.S)
    return json.loads(m.group(0) if m else text)


def valid(prof: dict) -> bool:
    return all(k in prof for k in SCHEMA["required"]) and prof["job_titles"] and isinstance(prof["skills"], list)


def extract(path: Path, key: str, log=print) -> dict:
    text = cv_text(path).strip()
    if len(text) < 200:
        raise ValueError("CV'den metin okunamadı (taranmış/resim PDF olabilir)")
    prompt = PROMPT.format(schema=json.dumps(SCHEMA), cv=text[:60000])
    errors = []
    for model, structured in free_models(key)[:4]:
        assert model.endswith(":free")
        body = {"model": model, "messages": [{"role": "user", "content": prompt}], "temperature": 0.2}
        if structured:
            body["response_format"] = {"type": "json_schema",
                                       "json_schema": {"name": "profile", "strict": True, "schema": SCHEMA}}
        try:
            resp = request("/chat/completions", key, body)
            prof = parse_json(resp["choices"][0]["message"]["content"] or "")
            if valid(prof):
                prof["total_years"] = int(prof.get("total_years") or 0)
                return prof
            errors.append(f"{model}: eksik alan")
        except urllib.error.HTTPError as e:
            errors.append(f"{model}: HTTP {e.code}")  # 429 = ücretsiz kota doldu, sonraki modele geç
        except (KeyError, IndexError, ValueError, TimeoutError, urllib.error.URLError) as e:
            errors.append(f"{model}: {e.__class__.__name__}")
        log(f"  {errors[-1]}, başka ücretsiz model deneniyor...")
    raise RuntimeError("; ".join(errors))
