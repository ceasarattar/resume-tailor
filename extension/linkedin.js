"use strict";
/* Resume Tailor — LinkedIn helper.
 *
 * Scope: the jobs you asked for — LinkedIn postings whose Apply button REDIRECTS
 * to an external company/ATS site (NOT "Easy Apply", which you do by hand). On
 * those pages this:
 *   1. scrapes the JD + company + role and sends it to your local app (/ingest),
 *      so the resume-match and grounded answers downstream have the right context;
 *   2. follows LinkedIn's Apply link to the external application — where the
 *      autofill content script takes over.
 *
 * It stays out of the way on Easy Apply posts.
 */

const SERVER = "http://localhost:8000";

function q(sel) { return document.querySelector(sel); }
function text(sel) { const e = q(sel); return e ? e.innerText.trim() : ""; }

function jobTitle() {
  return text(".job-details-jobs-unified-top-card__job-title") ||
         text(".jobs-unified-top-card__job-title") ||
         text("h1");
}
function company() {
  return text(".job-details-jobs-unified-top-card__company-name a") ||
         text(".job-details-jobs-unified-top-card__company-name") ||
         text(".jobs-unified-top-card__company-name");
}
function jdText() {
  const el = q("#job-details, .jobs-description__content, .jobs-box__html-content, .jobs-description-content__text");
  return el ? el.innerText.trim().slice(0, 20000) : "";
}

function applyButton() {
  // The unified top-card apply control. Easy Apply and external share this hook;
  // we distinguish by label text.
  return q(".jobs-apply-button, button[aria-label*='Apply'], a[aria-label*='Apply']");
}
function isEasyApply(btn) {
  return /easy apply/i.test((btn && btn.innerText) || "");
}

async function capture() {
  const jd = jdText();
  if (jd.length < 60) return { ok: false, error: "JD text not found yet — scroll the description into view." };
  try {
    const res = await fetch(`${SERVER}/ingest`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ jd_text: jd, company: company() || null, role: jobTitle() || null }),
    });
    if (!res.ok) throw new Error(`server ${res.status}`);
    const d = await res.json();
    return { ok: true, chars: d.chars };
  } catch (e) {
    return { ok: false, error: e.message };
  }
}

function chip() {
  if (document.getElementById("rt-li-chip")) return;
  const btn = applyButton();
  if (!btn || isEasyApply(btn)) return;   // only external-redirect jobs

  const el = document.createElement("button");
  el.id = "rt-li-chip";
  el.textContent = "📋 Capture & Apply";
  Object.assign(el.style, {
    position: "fixed", bottom: "20px", right: "20px", zIndex: 2147483647,
    padding: "11px 16px", borderRadius: "10px", border: "none", cursor: "pointer",
    background: "#2f6fed", color: "#fff", font: "600 13px -apple-system,Segoe UI,Roboto,Arial",
    boxShadow: "0 8px 24px rgba(0,0,0,.35)",
  });
  el.onclick = async () => {
    el.disabled = true; el.textContent = "Capturing JD…";
    const r = await capture();
    if (!r.ok) { el.textContent = "App not running?"; el.title = r.error; setTimeout(() => { el.textContent = "📋 Capture & Apply"; el.disabled = false; }, 2500); return; }
    el.textContent = `✓ ${r.chars} chars · opening…`;
    // Follow LinkedIn's external apply link; the destination ATS page autofills.
    const b = applyButton();
    if (b) b.click();
    setTimeout(() => { el.textContent = "📋 Capture & Apply"; el.disabled = false; }, 3000);
  };
  document.body.appendChild(el);
}

// LinkedIn is a SPA — re-evaluate as the user navigates between postings.
let last = location.href;
setInterval(() => {
  if (location.href !== last) { last = location.href; const old = document.getElementById("rt-li-chip"); if (old) old.remove(); }
  chip();
}, 1200);
chip();
