# Resume Tailor — JD Capture (Chrome extension)

A tiny MV3 extension that grabs the job description from the page you're on and
sends it to your locally-running Resume Tailor app (`/ingest`).

## Load it (unpacked)

1. Start the app first (`run.bat` / `run.sh`) so it's listening on
   `http://localhost:8000`.
2. Open `chrome://extensions`, enable **Developer mode** (top-right).
3. Click **Load unpacked** and select this `extension/` folder.
4. Pin the extension. On any job posting, optionally select the JD text, click
   the extension, then **Capture JD → Resume Tailor**.
5. In the app, click **Load from extension** to pull in the captured JD, then
   generate.

## Notes

- The extension only talks to `localhost:8000` (see `host_permissions` in
  `manifest.json`); nothing leaves your machine.
- If capture grabs too much page chrome, select just the JD text first — a
  selection longer than ~150 chars is preferred over the whole page.
