"""Kodlama bilmeyenler için arayüz: tarayıcıda açılan yerel bir sayfa.

Çift tıklanınca (veya `python gui.py`) bilgisayarda küçük bir sunucu başlar ve varsayılan
tarayıcıda sayfa açılır. CV yükleme, bilgileri onaylama, başlat/durdur ve canlı kayıt buradadır.
Sunucu sadece bu bilgisayardan (127.0.0.1) erişilebilir.
"""
import base64
import hashlib
import json
import os
import re
import secrets
import subprocess
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import apply
import cv_profile

TOKEN = secrets.token_urlsafe(16)
STATE = {"running": False, "analyzing": False}
SOURCE_NAMES = ["linkedin", "wellfound", "indeed", "remoteok", "himalayas", "weworkremotely"]


def open_folder(path: Path) -> None:
    if sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
    elif sys.platform == "win32":
        os.startfile(str(path))  # noqa: S606
    else:
        subprocess.Popen(["xdg-open", str(path)])


def state() -> dict:
    s = apply.SETTINGS
    return {
        "settings": {k: s.get(k) for k in ("salary", "english", "notice", "locations", "sources", "test_mode")},
        "cv_name": Path(s["resume"]).name if s.get("resume") and Path(s["resume"]).exists() else "",
        "profile": s.get("cv_profile"),
        "has_key": bool(apply.find_key()),
        "english_levels": {k: v[0] for k, v in apply.ENGLISH_LEVELS.items()},
        "default_locations": apply.CFG["search"]["locations"],
        "running": STATE["running"],
        "analyzing": STATE["analyzing"],
        "home": str(apply.HOME),
    }


def save_cv(body: dict) -> dict:
    name = re.sub(r"[^\w.\- ]", "_", Path(body["name"]).name)
    if Path(name).suffix.lower() not in (".pdf", ".docx"):
        return {"error": "pdf_docx_only"}
    folder = apply.HOME / "cv"
    folder.mkdir(exist_ok=True)
    path = folder / name
    path.write_bytes(base64.b64decode(body["data"]))
    apply.SETTINGS["resume"] = str(path)
    return {"ok": True, "cv_name": name}


def analyze(body: dict) -> dict:
    if body.get("key"):
        try:
            cv_profile.request("/key", body["key"].strip())
        except cv_profile.urllib.error.HTTPError as e:
            if e.code in (401, 403):
                return {"error": "bad_key"}
            return {"error": "analyze_failed", "detail": f"OpenRouter HTTP {e.code}"}
        except Exception as e:
            return {"error": "analyze_failed", "detail": f"{e.__class__.__name__}: {str(e)[:150]}"}
        apply.save_key(body["key"].strip())
    key = apply.find_key()
    if not key:
        return {"error": "no_key"}
    resume = Path(apply.SETTINGS.get("resume") or "missing")
    if not resume.is_file():
        return {"error": "no_cv"}
    STATE["analyzing"] = True
    try:
        prof = cv_profile.extract(resume, key, log=apply.log)
    except Exception as e:
        return {"error": "analyze_failed", "detail": f"{e.__class__.__name__}: {str(e)[:200]}"}
    finally:
        STATE["analyzing"] = False
    apply.SETTINGS["cv_profile"] = prof
    apply.SETTINGS["cv_hash"] = hashlib.sha256(resume.read_bytes()).hexdigest()
    return {"ok": True, "profile": prof}


def start(body: dict) -> dict:
    if STATE["running"]:
        return {"error": "already_running"}
    s = apply.SETTINGS
    if not Path(s.get("resume") or "missing").is_file():
        return {"error": "no_cv"}
    prof = {**(s.get("cv_profile") or {"title_keywords": [], "skills": [], "cover_letter": ""}), **body["profile"]}
    if not prof.get("job_titles"):
        return {"error": "no_titles"}
    prof["total_years"] = int(prof.get("total_years") or 0)
    s.update(cv_profile=prof, salary=re.sub(r"\D", "", str(body["salary"])) or s["salary"],
             english=body["english"], notice=re.sub(r"\D", "", str(body["notice"])) or s["notice"],
             locations=body["locations"] or apply.CFG["search"]["locations"],
             sources=body["sources"], test_mode=bool(body["test_mode"]))
    for name in SOURCE_NAMES:
        apply.CFG["sources"][name] = name in body["sources"]
    apply.DRY_RUN = s["test_mode"]
    apply.finalize_settings()
    apply.LOG_LINES.clear()

    def worker():
        STATE["running"] = True
        try:
            apply.run_bot()
        except BaseException as e:  # noqa: BLE001 - arayüzde göster
            apply.log(f"HATA: {e}")
        finally:
            STATE["running"] = False
    threading.Thread(target=worker, daemon=True).start()
    return {"ok": True}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def send(self, code: int, body: bytes, ctype: str = "application/json") -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/":
            return self.send(200, PAGE.replace("__TOKEN__", TOKEN).encode(), "text/html; charset=utf-8")
        if self.headers.get("X-Token") != TOKEN:
            return self.send(403, b"{}")
        if self.path == "/api/state":
            return self.send(200, json.dumps(state()).encode())
        m = re.match(r"/api/log\?from=(\d+)", self.path)
        if m:
            i = int(m.group(1))
            return self.send(200, json.dumps({"lines": apply.LOG_LINES[i:], "running": STATE["running"]}).encode())
        self.send(404, b"{}")

    def do_POST(self):
        if self.headers.get("X-Token") != TOKEN:
            return self.send(403, b"{}")
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        routes = {
            "/api/cv": lambda: save_cv(body),
            "/api/analyze": lambda: analyze(body),
            "/api/start": lambda: start(body),
            "/api/stop": lambda: (apply.STOP.set(), {"ok": True})[1],
            "/api/open": lambda: (open_folder(apply.HOME / body.get("folder", "data")), {"ok": True})[1],
            "/api/quit": lambda: (apply.STOP.set(), threading.Timer(0.5, lambda: os._exit(0)).start(), {"ok": True})[2],
        }
        if self.path not in routes:
            return self.send(404, b"{}")
        self.send(200, json.dumps(routes[self.path]()).encode())


PAGE = r"""<!doctype html>
<html><head><meta charset="utf-8"><title>Auto Apply</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
:root { --bg:#f6f7f9; --card:#fff; --text:#1d2433; --muted:#667085; --line:#e4e7ec; --accent:#2563eb; --ok:#16a34a; --warn:#b45309; }
* { box-sizing: border-box; }
body { margin:0; font:15px/1.5 -apple-system, "Segoe UI", Roboto, sans-serif; background:var(--bg); color:var(--text); }
main { max-width: 820px; margin: 0 auto; padding: 28px 20px 60px; }
header { display:flex; align-items:center; justify-content:space-between; margin-bottom: 18px; }
h1 { font-size: 24px; margin: 0; }
h2 { font-size: 17px; margin: 0 0 4px; }
.card { background:var(--card); border:1px solid var(--line); border-radius:12px; padding:20px; margin-bottom:16px; }
.step { color:var(--muted); font-size:13px; text-transform:uppercase; letter-spacing:.04em; }
p.hint { color:var(--muted); margin: 4px 0 14px; }
label { display:block; font-weight:600; font-size:13px; margin: 12px 0 4px; }
input[type=text], input[type=password], select, textarea { width:100%; padding:9px 11px; border:1px solid var(--line); border-radius:8px; font:inherit; background:#fff; }
textarea { min-height: 90px; resize: vertical; }
.grid { display:grid; grid-template-columns: 1fr 1fr; gap: 0 14px; }
.grid3 { display:grid; grid-template-columns: 1fr 1fr 1fr; gap: 0 14px; }
button { font:inherit; font-weight:600; border:0; border-radius:8px; padding:10px 16px; cursor:pointer; background:var(--accent); color:#fff; }
button.secondary { background:#eef2ff; color:var(--accent); }
button.danger { background:#dc2626; }
button:disabled { opacity:.5; cursor:default; }
.row { display:flex; gap:10px; align-items:center; flex-wrap:wrap; margin-top:14px; }
.drop { border:2px dashed var(--line); border-radius:12px; padding:22px; text-align:center; cursor:pointer; }
.drop.has { border-color: var(--ok); }
.checks label { display:inline-flex; align-items:center; gap:6px; font-weight:500; margin:6px 16px 0 0; }
.msg { margin-top:10px; font-size:14px; }
.msg.err { color:#b91c1c; } .msg.ok { color:var(--ok); }
#log { background:#0f172a; color:#e2e8f0; font:12.5px/1.45 ui-monospace, Menlo, Consolas, monospace; border-radius:8px; padding:12px; height:340px; overflow:auto; white-space:pre-wrap; }
.notice { background:#fffbeb; border:1px solid #fde68a; color:#78350f; border-radius:8px; padding:10px 12px; margin-top:12px; font-size:14px; }
.notice.free { background:#f0fdf4; border-color:#bbf7d0; color:#14532d; margin:14px 0 4px; }
ol.steps { margin:4px 0; padding-left:20px; color:var(--text); } ol.steps li { margin:3px 0; }
a.btn { display:inline-block; text-decoration:none; font-weight:600; border-radius:8px; padding:9px 14px; background:#eef2ff; color:var(--accent); }
.lang button { background:none; color:var(--muted); padding:4px 6px; }
.lang button.on { color:var(--text); text-decoration: underline; }
.hidden { display:none; }
@media (max-width: 640px) { .grid, .grid3 { grid-template-columns: 1fr; } }
</style></head>
<body><main>
<header><h1>Auto Apply</h1>
<div class="lang"><button data-l="en">EN</button><button data-l="tr">TR</button><button id="quit" data-t="quit"></button></div></header>

<section class="card" id="s1">
  <div class="step" data-t="step1"></div><h2 data-t="cv_title"></h2><p class="hint" data-t="cv_hint"></p>
  <div class="drop" id="drop"><span id="cvname" data-t="cv_drop"></span><input type="file" id="file" accept=".pdf,.docx" class="hidden"></div>
  <div id="keybox" class="hidden">
    <div class="notice free"><b data-t="free_title"></b><br><span data-t="free_text"></span></div>
    <label data-t="key_label"></label>
    <ol class="steps"><li data-t="key_s1"></li><li data-t="key_s2"></li><li data-t="key_s3"></li></ol>
    <div class="row" style="margin-top:6px"><a class="btn secondary" href="https://openrouter.ai/settings/keys" target="_blank" data-t="key_open"></a></div>
    <input type="password" id="key" placeholder="sk-or-v1-..." style="margin-top:10px">
  </div>
  <div class="row"><button id="analyze" data-t="analyze"></button><button class="secondary" id="manual" data-t="manual"></button>
    <button class="secondary hidden" id="changekey" data-t="change_key"></button></div>
  <div class="msg" id="m1"></div>
</section>

<section class="card hidden" id="s2">
  <div class="step" data-t="step2"></div><h2 data-t="review_title"></h2><p class="hint" data-t="review_hint"></p>
  <div class="grid">
    <div><label data-t="first_name"></label><input type="text" id="first_name"></div>
    <div><label data-t="last_name"></label><input type="text" id="last_name"></div>
    <div><label data-t="email"></label><input type="text" id="email"></div>
    <div><label data-t="phone"></label><input type="text" id="phone_intl" placeholder="+90 555 000 00 00"></div>
    <div><label data-t="city"></label><input type="text" id="city"></div>
    <div><label data-t="country"></label><input type="text" id="country"></div>
    <div><label>LinkedIn</label><input type="text" id="linkedin"></div>
    <div><label data-t="github"></label><input type="text" id="github"></div>
  </div>
  <label data-t="titles"></label><textarea id="job_titles"></textarea>
  <label data-t="locations"></label><textarea id="locations" style="min-height:60px"></textarea>
  <div class="grid3">
    <div><label data-t="years"></label><input type="text" id="total_years"></div>
    <div><label data-t="salary"></label><input type="text" id="salary"></div>
    <div><label data-t="notice"></label><input type="text" id="notice"></div>
  </div>
  <label data-t="english"></label><select id="english"></select>
  <label data-t="cover"></label><textarea id="cover_letter"></textarea>
</section>

<section class="card hidden" id="s3">
  <div class="step" data-t="step3"></div><h2 data-t="where_title"></h2><p class="hint" data-t="where_hint"></p>
  <div class="checks" id="sources"></div>
  <div class="checks"><label><input type="checkbox" id="test_mode"> <span data-t="test_mode"></span></label></div>
  <div class="notice" data-t="chrome_notice"></div>
  <div class="row"><button id="start" data-t="start"></button><button class="danger hidden" id="stop" data-t="stop"></button>
    <button class="secondary" id="results" data-t="results"></button></div>
  <div class="msg" id="m3"></div>
</section>

<section class="card hidden" id="s4">
  <h2 data-t="progress"></h2><div id="log"></div>
</section>
</main>
<script>
const TOKEN = "__TOKEN__";
const T = {
 en: { step1:"Step 1", cv_title:"Your CV", cv_hint:"Upload your CV (PDF or Word). It is read by a free AI model to fill in your details and pick matching jobs.",
  cv_drop:"Click to choose your CV (.pdf or .docx)", key_label:"Get your free key (one time, about 1 minute)",
  free_title:"100% free - you will not pay anything.", free_text:"Your CV is read by a free AI model on OpenRouter. The app only ever uses free models. No credit card needed, nothing to buy.",
  key_s1:"Click the button below and sign in to OpenRouter (Google sign-in is fine).", key_s2:"Click \"Create\", give it any name, leave the other fields as they are, and create the key.",
  key_s3:"Copy the key (it starts with sk-or-v1-) and paste it in the box below. Then click \"Read my CV\".", key_open:"Open OpenRouter",
  analyze:"Read my CV", manual:"Skip, I'll fill it in myself", analyzing:"Reading your CV... this can take up to a minute.",
  analyzed:"Done. Please check your details below.", step2:"Step 2", review_title:"Check your details", review_hint:"These are used to search jobs and fill application forms. Fix anything that is wrong.",
  first_name:"First name", last_name:"Last name", email:"E-mail", phone:"Phone (with country code)", city:"City, Country", country:"Country", github:"GitHub / portfolio",
  titles:"Job titles to apply for (one per line)", locations:"Countries / regions to search (one per line)", years:"Years of experience", salary:"Yearly salary expectation (USD/EUR)",
  notice:"Notice period (days)", english:"English level", cover:"Short cover letter (used when a form asks for one)",
  step3:"Step 3", where_title:"Where to apply", where_hint:"Only remote jobs posted in the last 7 days. Company websites are limited to a few per run so you are not flagged as spam.",
  test_mode:"Test mode (fill forms but do NOT send) - recommended for your first run",
  chrome_notice:"A Chrome window will open. The first time, log in to LinkedIn (and Indeed / Wellfound if selected) in that window, or tick a 'verify you are human' box if asked. The bot waits for you and remembers the login.",
  start:"Start applying", stop:"Stop", results:"Open results folder", progress:"Progress", running:"Running...", finished:"Finished. See the results folder for the full list.",
  err_no_cv:"Please choose your CV first.", err_no_key:"Please paste your free OpenRouter key above, or skip and fill in your details yourself.", err_bad_key:"This key doesn't work. Copy it again from OpenRouter and paste the whole key.", err_pdf_docx_only:"Only .pdf or .docx files.",
  change_key:"Change key", quit:"Quit", quit_done:"The app is closed. You can close this tab.",
  levels:{1:"Native / bilingual", 2:"Fluent / professional", 3:"Conversational / intermediate", 4:"Basic"},
  err_analyze_failed:"Could not read the CV automatically. You can fill in your details yourself.", err_no_titles:"Please enter at least one job title.", err_already_running:"Already running." },
 tr: { step1:"Adım 1", cv_title:"CV'n", cv_hint:"CV'ni yükle (PDF veya Word). Ücretsiz bir yapay zeka modeli okuyup bilgilerini ve sana uygun pozisyonları çıkarır.",
  cv_drop:"CV'ni seçmek için tıkla (.pdf veya .docx)", key_label:"Ücretsiz anahtarını al (bir kerelik, yaklaşık 1 dakika)",
  free_title:"Tamamen ücretsiz - hiçbir ücret ödemezsin.", free_text:"CV'n OpenRouter'daki ücretsiz bir yapay zeka modeliyle okunur. Uygulama sadece ücretsiz modelleri kullanır. Kredi kartı gerekmez, hiçbir şey satın almazsın.",
  key_s1:"Aşağıdaki butona tıkla ve OpenRouter'a giriş yap (Google ile giriş olur).", key_s2:"\"Create\" butonuna bas, istediğin bir isim ver, diğer alanlara dokunmadan anahtarı oluştur.",
  key_s3:"Anahtarı kopyala (sk-or-v1- ile başlar) ve aşağıdaki kutuya yapıştır. Sonra \"CV'mi oku\" butonuna bas.", key_open:"OpenRouter'ı aç",
  analyze:"CV'mi oku", manual:"Geç, kendim dolduracağım", analyzing:"CV okunuyor... bir dakika kadar sürebilir.",
  analyzed:"Tamam. Aşağıdaki bilgileri kontrol et.", step2:"Adım 2", review_title:"Bilgilerini kontrol et", review_hint:"İş aramak ve başvuru formlarını doldurmak için kullanılır. Yanlış olanı düzelt.",
  first_name:"Ad", last_name:"Soyad", email:"E-posta", phone:"Telefon (ülke koduyla)", city:"Şehir, Ülke", country:"Ülke", github:"GitHub / portfolyo",
  titles:"Başvurulacak pozisyonlar (her satıra bir tane)", locations:"Aranacak ülkeler / bölgeler (her satıra bir tane)", years:"Deneyim (yıl)", salary:"Yıllık maaş beklentisi (USD/EUR)",
  notice:"İhbar süresi (gün)", english:"İngilizce seviyesi", cover:"Kısa ön yazı (form isterse kullanılır)",
  step3:"Adım 3", where_title:"Nereye başvurulsun", where_hint:"Sadece son 7 günde yayınlanan remote ilanlar. Şirket sitelerine her çalıştırmada az sayıda başvurulur, spam sayılmamak için.",
  test_mode:"Test modu (formları doldurur ama GÖNDERMEZ) - ilk çalıştırmada önerilir",
  chrome_notice:"Bir Chrome penceresi açılacak. İlk seferde o pencerede LinkedIn'e (seçtiysen Indeed / Wellfound'a da) giriş yap; 'insan mısın' kutusu çıkarsa işaretle. Bot seni bekler ve girişi hatırlar.",
  start:"Başvurmaya başla", stop:"Durdur", results:"Sonuç klasörünü aç", progress:"İlerleme", running:"Çalışıyor...", finished:"Bitti. Tam liste sonuç klasöründe.",
  err_no_cv:"Önce CV'ni seç.", err_no_key:"Yukarıdaki kutuya ücretsiz OpenRouter anahtarını yapıştır ya da geçip bilgilerini kendin doldur.", err_bad_key:"Bu anahtar çalışmıyor. OpenRouter'dan tekrar kopyalayıp anahtarın tamamını yapıştır.", err_pdf_docx_only:"Sadece .pdf veya .docx.",
  change_key:"Anahtarı değiştir", quit:"Kapat", quit_done:"Uygulama kapandı. Bu sekmeyi kapatabilirsin.",
  levels:{1:"Ana dil / iki dilli", 2:"Akıcı / profesyonel", 3:"Orta / konuşma", 4:"Temel"},
  err_analyze_failed:"CV otomatik okunamadı. Bilgilerini kendin doldurabilirsin.", err_no_titles:"En az bir pozisyon yaz.", err_already_running:"Zaten çalışıyor." },
};
const SOURCES = { linkedin:"LinkedIn (Easy Apply)", wellfound:"Wellfound", indeed:"Indeed", remoteok:"Remote OK", himalayas:"Himalayas", weworkremotely:"We Work Remotely" };
let lang = localStorage.lang || (navigator.language.startsWith("tr") ? "tr" : "en"), S = null, logFrom = 0, poll = null;
const $ = id => document.getElementById(id);
const t = k => T[lang][k] || k;
function render() {
  document.querySelectorAll("[data-t]").forEach(e => { if (e.id !== "cvname" || !S || !S.cv_name) e.textContent = t(e.dataset.t); });
  document.querySelectorAll(".lang button").forEach(b => b.classList.toggle("on", b.dataset.l === lang));
  const v = $("english").value;
  $("english").innerHTML = Object.entries(t("levels")).map(([k, l]) => `<option value="${k}">${l}</option>`).join("");
  if (v) $("english").value = v;
}
async function api(path, body) {
  const r = await fetch(path, { method: body ? "POST" : "GET", headers: { "X-Token": TOKEN, "Content-Type": "application/json" }, body: body ? JSON.stringify(body) : undefined });
  return r.json();
}
function msg(id, text, cls) { const e = $(id); e.textContent = text; e.className = "msg " + (cls || ""); }
function err(id, r) { msg(id, t("err_" + r.error) + (r.detail ? " (" + r.detail + ")" : ""), "err"); }
function fill(p) {
  p = p || {};
  for (const k of ["first_name","last_name","email","phone_intl","city","country","linkedin","github","total_years","cover_letter"]) $(k).value = p[k] ?? "";
  $("job_titles").value = (p.job_titles || []).join("\n");
  $("s2").classList.remove("hidden"); $("s3").classList.remove("hidden");
}
async function load() {
  S = await api("/api/state");
  if (S.cv_name) { $("cvname").textContent = S.cv_name; $("drop").classList.add("has"); }
  $("keybox").classList.toggle("hidden", S.has_key);
  $("changekey").classList.toggle("hidden", !S.has_key);
  const st = S.settings; render();
  $("english").value = st.english || "3"; $("salary").value = st.salary || ""; $("notice").value = st.notice || "";
  $("locations").value = (st.locations || S.default_locations).join("\n");
  const on = st.sources || Object.keys(SOURCES);
  $("sources").innerHTML = Object.entries(SOURCES).map(([k, v]) => `<label><input type="checkbox" value="${k}" ${on.includes(k) ? "checked" : ""}> ${v}</label>`).join("");
  $("test_mode").checked = st.test_mode ?? true;
  if (S.profile) fill(S.profile);
  if (S.running) watch();
  render();
}
$("drop").onclick = () => $("file").click();
$("file").onchange = async () => {
  const f = $("file").files[0]; if (!f) return;
  const data = await new Promise(res => { const r = new FileReader(); r.onload = () => res(r.result.split(",")[1]); r.readAsDataURL(f); });
  const r = await api("/api/cv", { name: f.name, data });
  if (r.error) return err("m1", r);
  S.cv_name = r.cv_name; $("cvname").textContent = r.cv_name; $("drop").classList.add("has"); msg("m1", "");
};
$("analyze").onclick = async () => {
  if (!S.cv_name) return err("m1", { error: "no_cv" });
  $("analyze").disabled = true; msg("m1", t("analyzing"));
  const r = await api("/api/analyze", { key: $("key").value });
  $("analyze").disabled = false;
  if (r.error) { err("m1", r); if (r.error === "analyze_failed") fill(null); return; }
  $("keybox").classList.add("hidden"); $("changekey").classList.remove("hidden"); $("key").value = ""; msg("m1", t("analyzed"), "ok"); fill(r.profile); $("s2").scrollIntoView({ behavior: "smooth" });
};
$("changekey").onclick = () => { $("keybox").classList.remove("hidden"); $("changekey").classList.add("hidden"); $("key").focus(); };
$("manual").onclick = () => { fill(S.profile); $("s2").scrollIntoView({ behavior: "smooth" }); };
const lines = id => $(id).value.split("\n").map(s => s.trim()).filter(Boolean);
$("start").onclick = async () => {
  const profile = {};
  for (const k of ["first_name","last_name","email","phone_intl","city","country","linkedin","github","total_years","cover_letter"]) profile[k] = $(k).value.trim();
  profile.job_titles = lines("job_titles");
  const body = { profile, locations: lines("locations"), salary: $("salary").value, notice: $("notice").value, english: $("english").value,
    sources: [...document.querySelectorAll("#sources input:checked")].map(e => e.value), test_mode: $("test_mode").checked };
  const r = await api("/api/start", body);
  if (r.error) return err("m3", r);
  logFrom = 0; $("log").textContent = ""; watch();
};
$("stop").onclick = () => api("/api/stop", {});
$("quit").onclick = async () => { await api("/api/quit", {}); document.body.innerHTML = `<main><h1>Auto Apply</h1><p>${t("quit_done")}</p></main>`; };
$("results").onclick = () => api("/api/open", { folder: "data" });
function watch() {
  $("s4").classList.remove("hidden"); $("start").disabled = true; $("stop").classList.remove("hidden"); msg("m3", t("running"));
  clearInterval(poll);
  poll = setInterval(async () => {
    const r = await api("/api/log?from=" + logFrom);
    if (r.lines.length) { logFrom += r.lines.length; const l = $("log"); l.textContent += r.lines.join("\n") + "\n"; l.scrollTop = l.scrollHeight; }
    if (!r.running) { clearInterval(poll); $("start").disabled = false; $("stop").classList.add("hidden"); msg("m3", t("finished"), "ok"); }
  }, 1500);
}
document.querySelectorAll(".lang button").forEach(b => b.onclick = () => { lang = localStorage.lang = b.dataset.l; render(); });
load();
</script></body></html>
"""


def selftest() -> None:
    """Derlenen uygulamanın Chrome'u sürebildiğini doğrular (CI'da çalışır)."""
    os.environ["HEADLESS"] = "true"
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        ctx = apply.launch(pw)
        page = ctx.new_page()
        page.set_content("<title>selftest-ok</title>")
        title = page.title()
        # OpenRouter'a HTTPS bağlantısı (sertifikalar pakette mi): model listesi anahtarsız açılır
        models = cv_profile.request("/models", "")["data"]
        (apply.HOME / "selftest.txt").write_text(f"{title} models={len(models)}")  # pencereli uygulamada konsol yok
        ctx.close()


def main() -> None:
    if os.getenv("AUTOAPPLY_SELFTEST"):
        return selftest()
    apply.load_settings()
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    url = f"http://127.0.0.1:{server.server_port}/"
    apply.log(f"Arayüz: {url}")
    threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
