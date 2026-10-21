# Auto Apply

Upload your CV, check your details, press **Start**. The app finds matching **remote jobs** and applies for you on:

LinkedIn (Easy Apply) · Indeed · Wellfound · Remote OK · Himalayas · We Work Remotely

No coding needed. Free.

<p align="center">
  <a href="docs/autoapply-demo.mp4"><img src="docs/autoapply-demo.gif" alt="How to use Auto Apply: upload your CV, check your details and salary, start applying" width="800"></a>
  <br><sub>How it works in 40 seconds. Click for the full-quality video.</sub>
</p>

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
2. **Check your details:** fix anything wrong and set your English level and your **monthly** salary expectation three ways: in your own currency (you pick it), in EUR and in USD.
3. **Start applying.** **Test mode** is off by default, so real applications are sent. If you only want to try the app first, tick **Test mode**: forms are filled in but nothing is sent. Untick it again to really apply.

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
- Date and workplace checks apply to every source, including LinkedIn recommendations. Missing, invalid or ambiguous metadata goes to `manual_apply.csv` as `metadata_manual`; old, hybrid and on-site jobs are skipped. Search filters and a remote board's name alone are not evidence. Relative dates are interpreted conservatively, so borderline jobs may be skipped. Explicit geographic restrictions use your configured accepted-region policy; this does not establish legal work authorization.
- Company websites are visited only a few times per run, slowly, so you don't get flagged as spam.
- The external-form attempt quota resets on each run. Any remaining cooldown between attempts is preserved across runs in the same app process, including after stopping a run.
- Only one GUI run or CV-changing operation can be active at a time. Repeated Start requests cannot change an accepted run's settings or mode; uploads and CV analysis are blocked while it runs. Stop also cancels a run whose worker has not started yet. This reservation is local to one app process, not a multi-process lock.
- Unknown answers are not guessed. Forms with unresolved questions go to `manual_apply.csv` without being submitted (including in test mode). This is conservative: even an unknown optional question may require manual review. Existing answers on the site should still be checked by you.
- Unknown/conflicting application-button labels and native submit-capable controls encountered while filling are also routed to manual review. Only explicitly recognized, non-submitting Next/Review controls are used for navigation; final sending goes through the durable submission guard, which also enforces test mode itself.
- Company-site forms are filled only on explicitly approved HTTPS hosts for Greenhouse, Lever and Ashby (listed in `apply.py`, `ATS`). Lookalike domains, HTTP, credentials in URLs, and nonstandard ports are not trusted. Redirects and native form submission targets are checked; unsupported sites go to `manual_apply.csv`. A company page may be visited to discover a trusted embedded ATS form, but is not itself filled.
- LinkedIn applications count as sent only after a new explicit confirmation appears. Validation failures (`submission_rejected`) and uncertain results (`submission_unconfirmed`) go to manual review without automatic retries. A `submission_pending` record is saved before clicking Submit so an interrupted run cannot blindly repeat the attempt. Check LinkedIn's application history before submitting these jobs manually; uncertain/pending attempts also block duplicates on other sources.
- The same durable Submit guard now covers Indeed, Wellfound and company-site forms. Only an explicit list of pre-submission failures may be retried; unknown statuses are not retryable. On loading history, legacy `skipped_unconfirmed`, `external_unconfirmed`, `external_unanswered` and `skipped_error` records are conservatively migrated to manual review (keeping their original status). Check the site's application history before any manual retry. Corrupt history stops the bot instead of silently starting with an empty history; restore a valid backup rather than deleting history to bypass this safeguard.
- Salary questions get the amount you entered for that currency (EUR, USD or your own; multiplied by 12 if the form asks per year). Other currencies and hourly rates are converted using daily exchange rates. A Turkish-language question without a currency is answered in TL per month.
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
- **Profile validation:** AI output, GUI submissions and cached profiles are validated locally against the same schema before use. Experience must be an integer from 0 to 80; profiles allow 1–20 nonblank job titles, up to 50 keywords and up to 100 correctly structured skills. Invalid model output triggers the next free model; invalid saved profiles must be re-read or replaced by manual entry. Unknown optional text may be empty and unknown experience is represented as 0. Validation checks structure and bounds, not factual accuracy—review the extracted details yourself.
- **Advanced settings** (sources, limits, form-answer rules) are in `config.yaml`.
- **Sensitive answers:** visas, work authorization, qualifications, relocation and consent are not inferred from your CV or location. To confirm an answer, add the complete question and your answer to `confirmed_answers` in `config.yaml`, for example:

  ```yaml
  confirmed_answers:
    "Will you require visa sponsorship?": "Yes"
  ```

  Questions must match exactly (ignoring case and surrounding whitespace). These answers are your explicit instructions; do not add a generic default. A `manual_review` result is not automatically retried: apply manually using the saved link.
- **Experience:** configure confirmed skill years in `skill_years` (for example, `python: 3`). AI estimates from the CV do not overwrite these values. Unknown skills and unrelated numeric questions stay unanswered.
- **Tests:** install `requirements.txt`, then run `python -m unittest discover -s tests -v`. Tests use temporary application data, mocked submission controls, and no real job sites. Install Node.js 22 to include the offline JavaScript UI tests (otherwise those tests are skipped). Local-HTML Chrome tests: `AUTOAPPLY_BROWSER_TESTS=1 python -m unittest discover -s tests -v` (requires Chrome). CI installs Chrome and runs the complete suite with Node.js 22. Python 3.12 is used for verification; the pinned Playwright dependency does not install successfully on Python 3.14.
- **Building the apps:** push a tag like `v1.0.0` and GitHub Actions attaches the Windows and Mac builds to the release.
