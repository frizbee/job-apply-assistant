"""CV'yi OpenRouter'daki ÜCRETSİZ bir dil modeliyle okuyup başvuru profili çıkarır.

Sadece fiyatı 0 olan (":free") modeller kullanılır; bu, her çağrıdan önce OpenRouter'ın
model listesinden doğrulanır. Ücretli modele hiçbir koşulda istek gönderilmez.
"""
import json
import re
import ssl
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

API = "https://openrouter.ai/api/v1"
# Derlenmiş uygulamada sistem sertifikaları bulunamayabiliyor; certifi paketiyle gelenleri kullan
try:
    import certifi
    SSL_CTX = ssl.create_default_context(cafile=certifi.where())
except ImportError:
    SSL_CTX = ssl.create_default_context()
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
        "first_name": {"type": "string", "maxLength": 200},
        "last_name": {"type": "string", "maxLength": 200},
        "email": {"type": "string", "maxLength": 320},
        "phone_intl": {"type": "string", "maxLength": 100, "description": "With country code, e.g. +90 534 000 00 00; empty if missing"},
        "city": {"type": "string", "maxLength": 200, "description": "City, Country"},
        "country": {"type": "string", "maxLength": 200, "description": "Country name in English, e.g. Turkey"},
        "linkedin": {"type": "string", "maxLength": 2048},
        "github": {"type": "string", "maxLength": 2048, "description": "GitHub or portfolio URL; empty if missing"},
        "current_title": {"type": "string", "maxLength": 200},
        "total_years": {"type": "integer", "minimum": 0, "maximum": 80, "description": "Total years of professional experience; 0 if unknown"},
        "job_titles": {"type": "array", "minItems": 1, "maxItems": 20,
                       "items": {"type": "string", "minLength": 1, "maxLength": 200, "pattern": r"\S"},
                       "description": "4-8 standard English job titles exactly as companies post them "
                                      "(e.g. 'Machine Learning Engineer'), best fit first; no 'Freelance', 'Mentor'"},
        "title_keywords": {"type": "array", "maxItems": 50,
                           "items": {"type": "string", "minLength": 1, "maxLength": 100, "pattern": r"\S"},
                           "description": "5-15 lowercase words that appear in relevant job TITLES "
                                          "(e.g. 'engineer', 'machine learning', 'data'), not tools or technologies"},
        "skills": {
            "type": "array",
            "maxItems": 100,
            "items": {"type": "object", "properties": {
                "skill": {"type": "string", "minLength": 1, "maxLength": 200, "pattern": r"\S"},
                "years": {"type": "integer", "minimum": 0, "maximum": 80}},
                      "required": ["skill", "years"], "additionalProperties": False},
            "description": "8-20 technologies/skills with years of experience",
        },
        "cover_letter": {"type": "string", "maxLength": 10000,
                         "description": "3-5 sentence generic English cover letter, first person, no company name"},
    },
    "required": ["first_name", "last_name", "email", "phone_intl", "city", "country", "linkedin", "github",
                 "current_title", "total_years", "job_titles", "title_keywords", "skills", "cover_letter"],
    "additionalProperties": False,
}

PROMPT = (
    "The owner of this CV is looking for jobs. Extract their application profile from the CV below. "
    "Pick job titles that match their real experience, do not exaggerate. For missing information use "
    "empty strings for text, empty lists for skills/keywords, and 0 for unknown experience; never invent it. "
    "Reply with a single JSON object matching this schema, "
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


def request(path: str, key: str, body: dict | None = None, *, timeout_seconds: int = 180) -> dict:
    req = urllib.request.Request(
        API + path, data=json.dumps(body).encode() if body else None,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json",
                 "HTTP-Referer": "https://github.com/klncgty/linkedin-auto-apply", "X-Title": "Auto Apply"})
    with urllib.request.urlopen(req, timeout=timeout_seconds, context=SSL_CTX) as r:
        return json.loads(r.read())


def free_models(key: str, *, timeout_seconds: int = 180) -> list[tuple[str, bool]]:
    """(model, structured_outputs) listesi; sadece istem ve yanıt fiyatı 0 olan modeller."""
    free = {}
    for m in request("/models", key, timeout_seconds=timeout_seconds)["data"]:
        p = m.get("pricing", {})
        if m["id"].endswith(":free") and all(float(p.get(k) or 0) == 0 for k in ("prompt", "completion", "request")):
            free[m["id"]] = "structured_outputs" in m.get("supported_parameters", [])
    ordered = [m for m in PREFERRED if m in free] + sorted(m for m in free if m not in PREFERRED and free[m])
    if not ordered:
        raise NoFreeModel("OpenRouter'da şu an ücretsiz model yok")
    return [(m, free[m]) for m in ordered]


class ProfileValidationError(ValueError):
    """Invalid profile; messages identify field paths without echoing personal data."""


def parse_json(text: str) -> dict:
    if not isinstance(text, str):
        raise ProfileValidationError("response: expected JSON text")
    text = text.strip()
    fence = re.fullmatch(r"```(?:json)?\s*([\s\S]*?)\s*```", text, re.I)
    if fence:
        text = fence[1]
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ProfileValidationError("response: duplicate JSON field")
            result[key] = value
        return result
    def invalid_constant(value):
        raise ProfileValidationError("response: non-finite JSON number")
    prof = json.loads(text, object_pairs_hook=pairs, parse_constant=invalid_constant)
    if not isinstance(prof, dict):
        raise ProfileValidationError("profile: expected object")
    return prof


def _validate_value(value, schema: dict, path: str) -> None:
    """Validate the JSON Schema vocabulary used by SCHEMA, also for non-structured models."""
    kind = schema["type"]
    correct = {"object": isinstance(value, dict), "array": isinstance(value, list),
               "string": isinstance(value, str), "integer": type(value) is int}
    if not correct[kind]:
        raise ProfileValidationError(f"{path}: expected {kind}")
    if kind == "object":
        for key in schema.get("required", []):
            if key not in value:
                raise ProfileValidationError(f"{path}.{key}: required field missing")
        properties = schema.get("properties", {})
        if schema.get("additionalProperties") is False and any(key not in properties for key in value):
            raise ProfileValidationError(f"{path}: unexpected field")
        for key, rule in properties.items():
            if key in value:
                _validate_value(value[key], rule, f"{path}.{key}")
    elif kind == "array":
        if len(value) < schema.get("minItems", 0) or len(value) > schema.get("maxItems", float("inf")):
            raise ProfileValidationError(f"{path}: expected {schema.get('minItems', 0)} to {schema['maxItems']} items")
        for i, item in enumerate(value):
            _validate_value(item, schema["items"], f"{path}[{i}]")
    elif kind == "string":
        if len(value) < schema.get("minLength", 0) or len(value) > schema.get("maxLength", float("inf")):
            raise ProfileValidationError(f"{path}: text length must be {schema.get('minLength', 0)} to {schema['maxLength']}")
        if "pattern" in schema and not re.search(schema["pattern"], value):
            raise ProfileValidationError(f"{path}: text must not be blank")
    elif kind == "integer":
        if value < schema.get("minimum", -float("inf")) or value > schema.get("maximum", float("inf")):
            raise ProfileValidationError(f"{path}: must be between {schema['minimum']} and {schema['maximum']}")


def validate(prof) -> dict:
    _validate_value(prof, SCHEMA, "profile")
    return prof


def empty_profile() -> dict:
    """Transport defaults for manual entry; job_titles still must be supplied."""
    return {key: "" if rule["type"] == "string" else [] if rule["type"] == "array" else 0
            for key, rule in SCHEMA["properties"].items()}


def valid(prof) -> bool:
    try:
        validate(prof)
    except ProfileValidationError:
        return False
    return True


def extract(path: Path, key: str, log=print, *, max_output_tokens: int = 4096, timeout_seconds: int = 180) -> dict:
    if type(max_output_tokens) is not int or not 256 <= max_output_tokens <= 4096:
        raise ValueError('Invalid output token limit')
    if type(timeout_seconds) is not int or not 10 <= timeout_seconds <= 180:
        raise ValueError('Invalid request timeout')
    text = cv_text(path).strip()
    if len(text) < 200:
        raise ValueError("CV'den metin okunamadı (taranmış/resim PDF olabilir)")
    prompt = PROMPT.format(schema=json.dumps(SCHEMA), cv=text[:60000])
    errors = []
    for model, structured in free_models(key, timeout_seconds=timeout_seconds)[:4]:
        assert model.endswith(":free")
        body = {"model": model, "messages": [{"role": "user", "content": prompt}], "temperature": 0.2,
                "max_tokens": max_output_tokens}
        if structured:
            body["response_format"] = {"type": "json_schema",
                                       "json_schema": {"name": "profile", "strict": True, "schema": SCHEMA}}
        try:
            resp = request("/chat/completions", key, body, timeout_seconds=timeout_seconds)
            prof = parse_json(resp["choices"][0]["message"]["content"] or "")
            return validate(prof)
        except urllib.error.HTTPError as e:
            errors.append(f"{model}: HTTP {e.code}")  # 429 = ücretsiz kota doldu, sonraki modele geç
        except ProfileValidationError as e:
            errors.append(f"{model}: {e}")
        except (KeyError, IndexError, TypeError, ValueError, TimeoutError, urllib.error.URLError) as e:
            errors.append(f"{model}: {e.__class__.__name__}")
        log(f"  {errors[-1]}, başka ücretsiz model deneniyor...")
    raise RuntimeError("; ".join(errors))
