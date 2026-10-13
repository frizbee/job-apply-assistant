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

**Linux?** There is no download file yet. Install it with these commands in a terminal instead (one time):

```bash
git clone https://github.com/klncgty/linkedin-auto-apply.git
cd linkedin-auto-apply
./install-linux.sh
```

The installer sets up everything, including Google Chrome if it's missing (it may ask for your password). Afterwards, open **Auto Apply** from your app menu (or run `./run.sh` in that folder) and continue with step 3 below.

**2. Open it**

- **Windows:** Right-click the zip → **Extract All**. Open the folder and double-click **AutoApply.exe**.
  If a blue "Windows protected your PC" box appears, click **More info → Run anyway**.
- **Mac:** Double-click the zip, then drag **AutoApply** into your **Applications** folder and open it.
  If you see *"Apple could not verify "AutoApply" is free of malware..."*:
  1. Click **Done** (not "Move to Trash").
  2. Open **System Settings → Privacy & Security**, scroll down and click **Open Anyway** next to "AutoApply was blocked". Enter your Mac password.
  3. Open AutoApply again and click **Open**.

You only need to do this once. The app is free and not signed, so your computer warns you the first time.

**3. Follow the 3 steps on screen**

A page opens in your browser:
1. **Your CV:** choose your CV (PDF or Word) and click **Read my CV**.
   The first time, the app asks for a free key (see below).
2. **Check your details:** fix anything wrong and set your salary and English level.
3. **Start applying.** Leave **Test mode** on for your first try: it fills forms but does not send them.

A Chrome window opens. The first time, **log in to LinkedIn** (and Indeed / Wellfound) in that window. If you see a "verify you are human" box, tick it. The app waits for you and remembers your login.

> You need **Google Chrome** installed: [google.com/chrome](https://www.google.com/chrome)

### Your free key (one time, about 1 minute)

The app reads your CV with a **free** AI model. You will **not pay anything**, and no credit card is needed.

1. In the app, click **Open OpenRouter** (or go to [openrouter.ai/settings/keys](https://openrouter.ai/settings/keys)) and sign in (Google sign-in works).
2. Click **Create**, type any name, leave the other fields as they are, and create the key.
3. Click **Copy** in the window that shows your new key (it's shown only once; the shortened key in the list won't work), paste it into the app and click **Read my CV**.

The app remembers it. To use a different key later, click **Change key** in step 1.

**Results:** click **Open results folder**.
- `applications.csv` lists every job it applied to.
- `manual_apply.csv` lists jobs you need to apply to yourself.
- `run.log` is a detailed log. If something goes wrong, send this file when you report it.

---

## Good to know

- Only remote jobs posted in the last 7 days are considered. The same job is never applied to twice, even if it appears on several sites.
- Company websites are visited only a few times per run, slowly, so you don't get flagged as spam.
- Salary questions are answered in the currency and period the form asks for (e.g. monthly in EUR), converted from your yearly amount using daily exchange rates.
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

**Linux:** run `./install-linux.sh` once. It installs the Python packages and Google Chrome if missing (automatic on x86_64 Debian/Ubuntu/Fedora; may ask for your sudo password) and adds **Auto Apply** to the app menu. A desktop session is needed because the app opens a Chrome window.

- **CV reading:** set `OPENROUTER_API_KEY` in `.env` (a free key from [openrouter.ai/keys](https://openrouter.ai/keys)). Only `:free` models are ever called.
- **Advanced settings** (sources, limits, form-answer rules) are in `config.yaml`.
- **Building the apps:** push a tag like `v1.0.0` and GitHub Actions attaches the Windows and Mac builds to the release.
