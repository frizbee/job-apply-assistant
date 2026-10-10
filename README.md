# Auto Apply

Upload your CV, check your details, press **Start**. The app finds matching **remote jobs** and applies for you on:

LinkedIn (Easy Apply) · Indeed · Wellfound · Remote OK · Himalayas · We Work Remotely

No coding needed. Free.

---

## How to use (no coding)

**1. Download**

Go to **[Releases](../../releases/latest)** and download the file for your computer:

| Your computer | File |
|---|---|
| Windows | `AutoApply-Windows.zip` |
| Mac with Apple chip (M1/M2/M3/M4) | `AutoApply-Mac-AppleSilicon.zip` |
| Older Mac (Intel) | `AutoApply-Mac-Intel.zip` |

Not sure which Mac you have?  → Apple menu  → **About This Mac** → look at "Chip".

**2. Open it**

- **Windows:** Right-click the zip → **Extract All**. Open the folder and double-click **AutoApply.exe**.
  If a blue "Windows protected your PC" box appears, click **More info → Run anyway**.
- **Mac:** Double-click the zip, then drag **AutoApply** into your **Applications** folder and open it.
  If it says *"cannot be opened"*: open **System Settings → Privacy & Security**, scroll down and click **Open Anyway**.

You only need to do this once. The app is free and not signed, so your computer warns you the first time.

**3. Follow the 3 steps on screen**

A page opens in your browser:
1. **Your CV:** choose your CV (PDF or Word) and click **Read my CV**.
   The first time, the app asks for a free OpenRouter key and shows how to get one (about 1 minute).
2. **Check your details:** fix anything wrong and set your salary and English level.
3. **Start applying.** Leave **Test mode** on for your first try: it fills forms but does not send them.

A Chrome window opens. The first time, **log in to LinkedIn** (and Indeed / Wellfound) in that window. If you see a "verify you are human" box, tick it. The app waits for you and remembers your login.

> You need **Google Chrome** installed: [google.com/chrome](https://www.google.com/chrome)

**Results:** click **Open results folder**.
- `applications.csv` lists every job it applied to.
- `manual_apply.csv` lists jobs you need to apply to yourself.

---

## Good to know

- Only remote jobs posted in the last 7 days are considered. The same job is never applied to twice, even if it appears on several sites.
- Company websites are visited only a few times per run, slowly, so you don't get flagged as spam.
- **It costs nothing.** Your CV is read by a free AI model on [OpenRouter](https://openrouter.ai). The app only uses free models, so you never pay. No credit card is needed. Your data stays on your computer except for that one request.
- Your files are stored in the **AutoApply** folder in your home folder.
- Automating applications is against some sites' terms of service. Use it at your own risk and keep the numbers low.

---

## For developers (run from source)

Requires Python 3.10+ and Google Chrome.

```bash
git clone https://github.com/klncgty/linkedin-auto-apply.git
cd linkedin-auto-apply
./run.sh            # Mac/Linux (Windows: run.bat). Opens the same web UI.
./run.sh --terminal # terminal-only mode
```

- **CV reading:** set `OPENROUTER_API_KEY` in `.env` (a free key from [openrouter.ai/keys](https://openrouter.ai/keys)). Only `:free` models are ever called.
- **Advanced settings** (sources, limits, form-answer rules) are in `config.yaml`.
- **Building the apps:** push a tag like `v1.0.0` and GitHub Actions attaches the Windows and Mac builds to the release.
