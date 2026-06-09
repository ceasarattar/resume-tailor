# Resume Tailor — Apply Copilot (Chrome extension)

Two jobs:

1. **Capture** a job description from any page and hand it to your local app
   (so it can tailor a résumé for that role).
2. **Autofill** the actual application on the company's ATS — Greenhouse, Lever,
   Ashby, Workday, iCIMS, SmartRecruiters, or any other portal — from your
   profile, then **stop so you can review and submit**. It never auto-submits,
   and it **learns** every answer you give so the next form is faster.

It talks only to your local app at `http://localhost:8000`. Nothing leaves your
machine except the Claude calls the app already makes.

## Install (developer mode)

1. Start the app: `run.bat` (Windows) / `./run.sh` (mac), or
   `.venv/Scripts/python.exe -m uvicorn app.main:app --port 8000`.
2. Fill `profile/application.json` (copy from `profile/application.example.json`).
   This holds the application-specific facts forms ask for — work authorization,
   address, EEO defaults, salary. **Leave anything you don't want to assert blank**;
   the autofiller skips blanks and flags them for you instead of guessing.
3. Chrome → `chrome://extensions` → enable **Developer mode** → **Load unpacked**
   → select this `extension/` folder.

## Use

**On a company application page** (Greenhouse/Lever/Workday/…): a panel appears
top-right. Click **Autofill**. Filled fields get a green outline; anything that
needs your judgment (work authorization, custom screener questions, consent
checkboxes) gets a dashed amber outline and is listed in the panel. Review, fix,
**you** submit. Then click **Learn answers** to save what you entered.

**On any other portal**: open the extension popup and click **Autofill this
application** — it injects on demand.

**On a LinkedIn job that redirects out** (not Easy Apply): a **Capture & Apply**
chip appears bottom-right. It captures the JD and follows the external apply link;
the autofiller takes over on the destination ATS. (Easy Apply posts are left
alone — do those by hand.)

## How it works (and where to tune it)

- `adapters.js` — per-ATS detection + label/dropdown hints. Selectors drift,
  especially **Workday** (`data-automation-id`); tune here against a live form.
- `content.js` — scans the form (descends into shadow DOM), asks the backend to
  resolve each field, fills text/selects/radios/custom dropdowns, attaches the
  matching résumé PDF, renders the review panel, and reports answers back to learn.
- `injected.js` — runs in the **page's** JS world as a fallback for framework-
  controlled inputs: native value setter + firing the React `onChange`/`onBlur`
  the component actually listens to.
- The truth/honesty logic lives server-side in `app/answers.py` — see
  `../APPLY_SPEC.md`.

## Honesty

The autofiller will never guess your work authorization, never invent metrics or
skills in free-text answers, and defaults EEO/demographic fields to
"decline to self-identify". Unknown or unsupported fields are flagged for review,
never fabricated.
