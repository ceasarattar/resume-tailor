# APPLY_SPEC.md — the application-autofill ("delivery") half

`PROJECT_SPEC.md` covers turning a JD into an honest, ATS-safe résumé. This file
covers the other half: filling out and submitting the actual application. Same
cardinal rule — **honesty: never assert anything not in `profile/`** — extended
from the résumé to every form field.

## The shape of the system

```
Layer 4  Chrome extension (extension/)         the "hands"
         detect ATS -> scan fields -> fill -> review gate -> learn
Layer 3  Local API (app/main.py)               the shared brain's mouth
         /api/profile  /api/answer[/batch]  /api/answer/learn  /api/resume/match
Layer 2  Answer engine (app/answers.py)        the brain
         tiered resolution + grounded LLM + honesty contract + SQLite bank
Layer 1  Source of truth (profile/)            the facts
         experience.json (contact) + application.json (application facts)
                                              + data/answers.sqlite (learned)
```

The résumé pipeline (`app/tailor.py` …) and the apply pipeline (`app/answers.py`)
share the same profile and the same honesty philosophy, but are otherwise
independent. The extension never contains truth logic — it asks the backend.

## Field resolution (app/answers.py `resolve()`)

For each form field the backend tries the **cheapest tier that succeeds**:

1. **Learned bank** — exact match on a normalized field *signature* you've
   answered before (host-scoped first, then global). Instant, free.
2. **Profile rules** — deterministic mapping from label → profile value
   (name, email, links, address, work authorization, salary, EEO, screeners).
3. **Embedding bank** — fuzzy match to a past answer (best-effort, via Ollama
   embeddings; silently skipped if Ollama isn't running).
4. **Grounded LLM** — only for free-text questions ("why this company?"). Reuses
   the résumé honesty rules: rephrase real facts, never invent. Returns
   `grounded=false` → the field is flagged for review.

Every result carries `confidence`, `source`, and `needs_review`. The extension
fills high-confidence answers and **highlights the rest for you** — it does not
submit.

### The honesty contract (non-negotiable, mirrors the résumé)

- **Work authorization / sponsorship** is never guessed. If the booleans in
  `application.json` are `null`, the field is left blank and flagged.
- **EEO / demographics** default to "decline to self-identify" — always honest.
- **Free-text** answers may only use facts in `profile/`. No invented years of
  experience, metrics, employers, or skills. Ungrounded → review.
- **Consent / legal checkboxes** are never auto-checked.
- Anything unmatched is surfaced for review, never fabricated.

## Learning loop

When you click **Learn answers** (or on submit), the extension posts the form's
current field→value pairs to `/api/answer/learn`. The engine upserts them into
`data/answers.sqlite` (gitignored; may hold personal data), keyed by signature,
both host-scoped and global, embedding the label best-effort. Over a dozen
applications, tier-1 (exact bank) covers most fields and autofill becomes instant.

## ATS adapters (extension/adapters.js)

Each ATS is a small adapter: `test()` to detect it, `formRoot()`, a combobox
selector, and optional label hints. A **generic** adapter handles unknown
portals. Implemented: Greenhouse, Lever, Ashby, Workday, iCIMS, SmartRecruiters.

**Known hard parts (the expert nitty-gritty):**

- **React controlled inputs** (Greenhouse/Lever/Ashby/Workday): setting
  `el.value` is reverted on re-render. We set the value via the *native*
  `HTMLInputElement` value setter, then dispatch bubbling `input`/`change`/`blur`
  so React's value tracker fires `onChange`. `injected.js` adds a page-world
  fallback that calls the React fiber's handler directly for validated fields.
- **Workday**: a multi-step React wizard. Fields are keyed by
  `data-automation-id` (poll these; CSS classes are hashed and drift). Dropdowns
  are custom `role=listbox` buttons, not `<select>`. Tune `adapters.js.workday`
  against a live tenant — selectors *will* change.
- **Custom dropdowns** (react-select, Workday listbox): open the control, filter
  by typing into the search input if present, poll for `[role=option]` /
  `select__option`, click the text match.
- **Shadow DOM / iframes**: the scanner descends into open shadow roots; the
  content script runs in `all_frames` so ATS iframes (Greenhouse embeds, Workday)
  are covered in their own frame.

## Roadmap to full automation

You're starting human-in-the-loop (review before submit) on purpose — it's the
honest, ToS-safe, ban-resistant path, and reviewed applications beat spray-and-
pray. The system is built to graduate toward more automation as the bank learns:

1. **Now** — autofill + review gate + learn. You submit. (Shipped.)
2. **Assisted submit** — once the bank answers ~95% of a given ATS's fields with
   high confidence and zero review items, offer a one-click "fill + submit" for
   that ATS only, still showing a 3-second cancelable confirmation.
3. **Queue mode** — capture a list of JDs, pre-generate résumés, open each
   application in a tab, autofill, and present a review queue you approve in
   batch. Submission stays a deliberate click.
4. **Full auto (guardrailed)** — only for ATSes with a 100%-confidence profile,
   never LinkedIn, always in your real logged-in session (never headless — that's
   what triggers Akamai/DataDome and account bans), with a daily cap and a
   per-company de-dupe. Keep the honesty gate and a kill switch.

The deliberate line we don't cross: no headless mass-submission, no detection-
evasion against hostile sites, no submitting an application a human never saw at
least once for that employer. That keeps the tool legitimate and your accounts
safe while still removing ~95% of the manual typing.

## Files

- `app/answers.py` — resolution engine, bank, learning, grounded generation.
- `app/main.py` — `/api/profile`, `/api/answer`, `/api/answer/batch`,
  `/api/answer/learn`, `/api/answer/stats`, `/api/resume/match`.
- `profile/application.json` — your application facts (gitignored).
- `profile/application.example.json` — the template.
- `extension/` — manifest, adapters, content script, page-world helper, LinkedIn
  helper, overlay, popup.
- `HUMANIZATION.md` — how free-text answers (and résumé bullets) are made to read
  like a person wrote them.
