"use strict";
/* Resume Tailor — Apply Copilot content script.
 *
 * Flow: detect ATS -> scan the form for fields (piercing shadow DOM) -> ask the
 * local backend to resolve each field to a truthful answer -> fill the easy ones,
 * highlight the ones that need review -> show a floating panel. It NEVER clicks
 * submit; you review and submit. On submit it learns what you entered.
 *
 * Honesty is enforced server-side (app/answers.py): work authorization is never
 * guessed, EEO defaults to "decline", metrics/skills are never invented.
 */

const SERVER = "http://localhost:8000";
const RT = {
  adapter: detectAdapter(),
  fields: new Map(),   // rtId -> { el | inputs, type, label, options }
  rtSeq: 0,
  jd: null,            // { company, role, jd_text }
  lastResults: [],
  injected: false,
};

const isTop = window === window.top;

// ----------------------------------------------------------------- utilities
const norm = (s) => (s || "").replace(/[^a-z0-9]+/gi, " ").trim().toLowerCase();
const visible = (el) => {
  if (!el) return false;
  if (el.disabled || el.readOnly) return false;
  const t = (el.type || "").toLowerCase();
  if (t === "hidden") return false;
  const st = getComputedStyle(el);
  if (st.display === "none" || st.visibility === "hidden") return false;
  return true;
};

// querySelectorAll that descends into open shadow roots.
function deepAll(selector, root = document, acc = []) {
  root.querySelectorAll(selector).forEach((n) => acc.push(n));
  root.querySelectorAll("*").forEach((n) => {
    if (n.shadowRoot) deepAll(selector, n.shadowRoot, acc);
  });
  return acc;
}

// ------------------------------------------------------------- label extraction
function labelFor(el) {
  // Adapter-specific (e.g. Workday data-automation-id) wins.
  if (RT.adapter.labelFor) {
    const l = RT.adapter.labelFor(el);
    if (l) return l;
  }
  // <label for="id">
  if (el.id) {
    const lab = (el.getRootNode().querySelector?.(`label[for="${CSS.escape(el.id)}"]`));
    if (lab && lab.textContent.trim()) return clean(lab.textContent);
  }
  // wrapping <label>
  const wrap = el.closest("label");
  if (wrap && wrap.textContent.trim()) return clean(wrap.textContent);
  // aria-label / aria-labelledby
  if (el.getAttribute("aria-label")) return clean(el.getAttribute("aria-label"));
  const lb = el.getAttribute("aria-labelledby");
  if (lb) {
    const parts = lb.split(/\s+/).map((id) => {
      const n = el.getRootNode().getElementById?.(id) || document.getElementById(id);
      return n ? n.textContent : "";
    });
    const txt = clean(parts.join(" "));
    if (txt) return txt;
  }
  // a label/legend within the field's container
  const grp = el.closest("[class*='field'], [class*='question'], fieldset, [data-automation-id]");
  if (grp) {
    const lab = grp.querySelector("label, legend, .label, [class*='label']");
    if (lab && lab.textContent.trim()) return clean(lab.textContent);
  }
  // placeholder / name as last resort
  if (el.placeholder) return clean(el.placeholder);
  if (el.name) return clean(el.name.replace(/[_\-\[\]]+/g, " "));
  return "";
}

function clean(s) {
  return (s || "").replace(/\*/g, "").replace(/\(required\)|\(optional\)/gi, "")
    .replace(/\s+/g, " ").trim().slice(0, 200);
}

function isRequired(el) {
  return !!(el.required || el.getAttribute("aria-required") === "true" ||
    (el.closest("[class*='field']")?.querySelector(".required, [class*='required']")));
}

// ------------------------------------------------------------------- scanning
function tag(el) {
  let id = el.getAttribute("data-rt-id");
  if (!id) { id = String(++RT.rtSeq); el.setAttribute("data-rt-id", id); }
  return id;
}

function optionTexts(el) {
  if (el.tagName === "SELECT") {
    return [...el.options].map((o) => o.text.trim()).filter((t) => t && !/^select/i.test(t));
  }
  return [];
}

function scan() {
  RT.fields.clear();
  const descriptors = [];
  const root = RT.adapter.formRoot() || document;

  // 1) text-like inputs, textareas, selects
  const controls = deepAll(
    "input, textarea, select", document
  ).filter((el) => root.contains(el) || root === document);

  const radioGroups = new Map();   // name -> [els]
  for (const el of controls) {
    if (!visible(el)) continue;
    const type = (el.type || el.tagName).toLowerCase();
    if (["submit", "button", "reset", "image", "hidden", "password"].includes(type)) continue;
    if (type === "radio") {
      const key = el.name || tag(el);
      if (!radioGroups.has(key)) radioGroups.set(key, []);
      radioGroups.get(key).push(el);
      continue;
    }
    if (type === "checkbox") {
      // Don't auto-toggle consent/legal checkboxes — surface for review only.
      const id = tag(el);
      RT.fields.set(id, { el, type: "checkbox", label: labelFor(el), options: ["Yes", "No"], review: true });
      continue;
    }
    if (type === "file") {
      const id = tag(el);
      RT.fields.set(id, { el, type: "file", label: labelFor(el) || "Resume", options: [] });
      continue;
    }
    const id = tag(el);
    const ftype = el.tagName === "SELECT" ? "select" : (el.tagName === "TEXTAREA" ? "textarea" : "text");
    const label = labelFor(el);
    if (!label) continue;
    const options = optionTexts(el);
    RT.fields.set(id, { el, type: ftype, label, options, required: isRequired(el) });
    descriptors.push({ rtId: id, label, field_type: ftype, options });
  }

  // 2) radio groups -> one descriptor each
  for (const [, els] of radioGroups) {
    if (!els.length) continue;
    const groupLabel = groupLabelFor(els[0]);
    const opts = els.map((e) => ({ el: e, label: radioOptionLabel(e) }));
    const id = String(++RT.rtSeq);
    RT.fields.set(id, { inputs: opts, type: "radio", label: groupLabel, options: opts.map((o) => o.label) });
    descriptors.push({ rtId: id, label: groupLabel, field_type: "radio", options: opts.map((o) => o.label) });
  }

  // 3) custom comboboxes (react-select / Workday listbox)
  if (RT.adapter.comboboxSelector) {
    for (const el of deepAll(RT.adapter.comboboxSelector)) {
      if (!visible(el) || el.getAttribute("data-rt-id")) continue;
      const id = tag(el);
      const label = labelFor(el) || comboLabel(el);
      if (!label) continue;
      RT.fields.set(id, { el, type: "combobox", label, options: [] });
      descriptors.push({ rtId: id, label, field_type: "text", options: [] });
    }
  }

  return descriptors;
}

function groupLabelFor(radio) {
  const fs = radio.closest("fieldset");
  if (fs) { const lg = fs.querySelector("legend"); if (lg) return clean(lg.textContent); }
  const grp = radio.closest("[class*='field'], [class*='question'], [data-automation-id]");
  if (grp) { const lab = grp.querySelector("label, legend, [class*='label']"); if (lab) return clean(lab.textContent); }
  return clean(radio.name || "");
}
function radioOptionLabel(radio) {
  if (radio.id) { const l = document.querySelector(`label[for="${CSS.escape(radio.id)}"]`); if (l) return clean(l.textContent); }
  const w = radio.closest("label"); if (w) return clean(w.textContent);
  return clean(radio.value || "");
}
function comboLabel(el) {
  const grp = el.closest("[class*='field'], [class*='question'], [data-automation-id]");
  if (grp) { const lab = grp.querySelector("label, legend, [class*='label']"); if (lab) return clean(lab.textContent); }
  return "";
}

// --------------------------------------------------------------- backend calls
async function api(path, opts) {
  const res = await fetch(`${SERVER}${path}`, opts);
  if (!res.ok) throw new Error(`server ${res.status}`);
  return res.json();
}
async function loadJD() {
  try {
    const d = await api("/api/pending");
    RT.jd = d.pending || null;
  } catch (_) { RT.jd = null; }
}
async function resolveAll(descriptors) {
  const body = {
    url: location.href,
    jd_context: RT.jd?.jd_text || "",
    fields: descriptors.map((d) => ({ ...d, url: location.href })),
  };
  const d = await api("/api/answer/batch", {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
  });
  return d.results || [];
}

// ------------------------------------------------------------------ injection
function ensureInjected() {
  if (RT.injected) return;
  const s = document.createElement("script");
  s.src = chrome.runtime.getURL("injected.js");
  s.onload = () => s.remove();
  (document.head || document.documentElement).appendChild(s);
  RT.injected = true;
}
function injectedFill(rtId, value, timeout = 1500) {
  return new Promise((resolve) => {
    const id = Math.random().toString(36).slice(2);
    const onResult = (ev) => {
      if (ev.detail && ev.detail.id === id) {
        document.removeEventListener("rt-fill-result", onResult);
        resolve(!!ev.detail.ok);
      }
    };
    document.addEventListener("rt-fill-result", onResult);
    document.dispatchEvent(new CustomEvent("rt-fill-request", { detail: { id, rtId, value } }));
    setTimeout(() => { document.removeEventListener("rt-fill-result", onResult); resolve(false); }, timeout);
  });
}

// --------------------------------------------------------------- fill primitives
function nativeSet(el, value) {
  const proto = Object.getPrototypeOf(el);
  const desc = Object.getOwnPropertyDescriptor(proto, "value") || Object.getOwnPropertyDescriptor(el, "value");
  if (desc && desc.set) desc.set.call(el, value); else el.value = value;
}
function fire(el, type) { el.dispatchEvent(new Event(type, { bubbles: true })); }

async function fillText(field, value) {
  const el = field.el;
  el.focus();
  nativeSet(el, value);
  fire(el, "input"); fire(el, "change"); fire(el, "blur");
  if (String(el.value) === String(value)) return true;
  // Didn't commit (framework value tracker) -> MAIN-world fallback.
  ensureInjected();
  return injectedFill(field.el.getAttribute("data-rt-id"), value);
}

function fillSelect(field, optionText) {
  const el = field.el;
  const want = norm(optionText);
  const opt = [...el.options].find((o) => norm(o.text) === want) ||
              [...el.options].find((o) => norm(o.text).includes(want) || want.includes(norm(o.text)));
  if (!opt) return false;
  nativeSet(el, opt.value);
  fire(el, "input"); fire(el, "change");
  return true;
}

function fillRadio(field, optionText) {
  const want = norm(optionText);
  const pick = field.inputs.find((o) => norm(o.label) === want) ||
               field.inputs.find((o) => norm(o.label).includes(want) || want.includes(norm(o.label)));
  if (!pick) return false;
  pick.el.click();
  fire(pick.el, "change");
  return true;
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
async function fillCombobox(field, value) {
  const ctrl = field.el;
  ctrl.scrollIntoView({ block: "center" });
  ctrl.click();
  // If there's a search input, filter by typing.
  await sleep(120);
  const search = deepAll("input", ctrl.parentElement || document)
    .find((i) => visible(i) && (i.getAttribute("role") === "combobox" || i.className.includes("select__input") || i.type === "text"));
  if (search) { nativeSet(search, value); fire(search, "input"); await sleep(250); }
  // Poll for option list.
  const want = norm(value);
  for (let i = 0; i < 12; i++) {
    const opts = deepAll("[role='option'], [class*='select__option'], [class*='option'], [data-automation-id*='promptOption']")
      .filter(visible);
    const hit = opts.find((o) => norm(o.textContent) === want) ||
                opts.find((o) => norm(o.textContent).includes(want) || want.includes(norm(o.textContent)));
    if (hit) { hit.scrollIntoView({ block: "nearest" }); hit.click(); return true; }
    await sleep(150);
  }
  return false;
}

async function attachResume(field) {
  try {
    let q = "";
    if (RT.jd?.company || RT.jd?.role)
      q = `?company=${encodeURIComponent(RT.jd.company || "")}&role=${encodeURIComponent(RT.jd.role || "")}`;
    const m = await api(`/api/resume/match${q}`);
    if (!m.match) return false;
    const blob = await (await fetch(`${SERVER}${m.match.pdf_url}`)).blob();
    const file = new File([blob], "resume.pdf", { type: "application/pdf" });
    const dt = new DataTransfer();
    dt.items.add(file);
    field.el.files = dt.files;
    fire(field.el, "input"); fire(field.el, "change");
    return true;
  } catch (_) { return false; }
}

// ------------------------------------------------------------------- orchestrate
async function autofill() {
  panel.setStatus("Scanning form…");
  await loadJD();
  ensureInjected();
  const descriptors = scan();
  if (!descriptors.length && ![...RT.fields.values()].some((f) => f.type === "file")) {
    panel.setStatus("No fillable fields found on this page.", "warn");
    return;
  }
  panel.setStatus(`Resolving ${descriptors.length} fields…`);
  let results = [];
  try {
    results = await resolveAll(descriptors);
  } catch (e) {
    panel.setStatus(`Backend unreachable (${e.message}). Is run.bat running?`, "bad");
    return;
  }

  const summary = { filled: 0, review: 0, skipped: 0, attached: false };
  const reviewItems = [];

  for (const r of results) {
    const field = RT.fields.get(r.rtId);
    if (!field) continue;
    const display = r.option || r.value;
    if (r.needs_review || display == null || display === "") {
      summary.review++;
      markReview(field);
      reviewItems.push({ label: r.label, note: r.note || "needs review", source: r.source });
      continue;
    }
    let ok = false;
    try {
      if (field.type === "select") ok = fillSelect(field, display);
      else if (field.type === "radio") ok = fillRadio(field, display);
      else if (field.type === "combobox") ok = await fillCombobox(field, display);
      else ok = await fillText(field, display);
    } catch (_) { ok = false; }
    if (ok) { summary.filled++; markFilled(field); }
    else { summary.review++; markReview(field); reviewItems.push({ label: r.label, note: "couldn't set automatically", source: r.source }); }
  }

  // Resume attach (separate from text resolution).
  for (const field of RT.fields.values()) {
    if (field.type === "file") {
      const lab = norm(field.label);
      if (lab.includes("resume") || lab.includes("cv") || lab.includes("cover") === false) {
        summary.attached = await attachResume(field) || summary.attached;
      }
    }
    if (field.type === "checkbox") { summary.review++; reviewItems.push({ label: field.label, note: "consent/checkbox — confirm manually", source: "skip" }); markReview(field); }
  }

  RT.lastResults = results;
  panel.render(summary, reviewItems);
}

// learn what the user actually has in the form now (call before/after they submit)
async function learn() {
  const fields = [];
  for (const f of RT.fields.values()) {
    if (f.type === "radio") {
      const checked = f.inputs.find((o) => o.el.checked);
      if (checked) fields.push({ label: f.label, value: checked.label, field_type: "radio" });
    } else if (f.el && f.type !== "file") {
      const v = f.el.value;
      if (v && String(v).trim()) fields.push({ label: f.label, value: String(v), field_type: f.type });
    }
  }
  if (!fields.length) return { learned: 0 };
  return api("/api/answer/learn", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ url: location.href, fields }),
  });
}

// ----------------------------------------------------------------- highlighting
function markFilled(field) { highlight(field, "rt-ok"); }
function markReview(field) { highlight(field, "rt-review"); }
function highlight(field, cls) {
  const el = field.el || (field.inputs && field.inputs[0].el);
  if (!el) return;
  const target = el.closest("[class*='field'], [data-automation-id], label") || el;
  target.classList.remove("rt-ok", "rt-review");
  target.classList.add(cls);
}

// --------------------------------------------------------------------- overlay
const panel = (() => {
  let root, statusEl, body;
  function ensure() {
    if (root || !isTop) return;
    root = document.createElement("div");
    root.id = "rt-panel";
    root.innerHTML = `
      <div id="rt-head"><span id="rt-title">Apply Copilot</span><span id="rt-close">×</span></div>
      <div id="rt-status">Ready.</div>
      <div id="rt-body"></div>
      <div id="rt-actions">
        <button id="rt-fill">Autofill</button>
        <button id="rt-learn" title="Save my current answers to the bank">Learn answers</button>
      </div>
      <div id="rt-foot">Reviews before submit · never auto-submits</div>`;
    document.body.appendChild(root);
    statusEl = root.querySelector("#rt-status");
    body = root.querySelector("#rt-body");
    root.querySelector("#rt-close").onclick = () => root.remove();
    root.querySelector("#rt-fill").onclick = () => autofill();
    root.querySelector("#rt-learn").onclick = async () => {
      setStatus("Saving answers…");
      try { const d = await learn(); setStatus(`Saved ${d.learned} answers to the bank.`, "ok"); }
      catch (e) { setStatus(`Couldn't save (${e.message}).`, "bad"); }
    };
  }
  function setStatus(msg, cls) { ensure(); if (statusEl) { statusEl.textContent = msg; statusEl.className = cls || ""; } }
  function render(summary, reviewItems) {
    ensure();
    setStatus(`Filled ${summary.filled} · ${summary.review} to review${summary.attached ? " · résumé attached" : ""}`,
      summary.review ? "warn" : "ok");
    if (!body) return;
    body.innerHTML = reviewItems.length
      ? `<div id="rt-reviewhdr">Review these (${reviewItems.length}):</div>` +
        reviewItems.slice(0, 25).map((r) =>
          `<div class="rt-item"><span class="rt-lbl">${escapeHtml(r.label)}</span><span class="rt-note">${escapeHtml(r.note)}</span></div>`).join("")
      : `<div class="rt-allgood">All resolved. Review the form, then submit.</div>`;
  }
  return { ensure, setStatus, render };
})();

function escapeHtml(s) { const d = document.createElement("div"); d.textContent = s || ""; return d.innerHTML; }

// --------------------------------------------------------------- re-scan + msgs
let rescanTimer = null;
const mo = new MutationObserver(() => {
  if (rescanTimer) clearTimeout(rescanTimer);
  rescanTimer = setTimeout(() => { /* fields appear lazily; next Autofill picks them up */ }, 400);
});
try { mo.observe(document.documentElement, { childList: true, subtree: true }); } catch (_) {}

chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  if (msg && msg.type === "rt-autofill") { autofill().then(() => sendResponse({ ok: true })); return true; }
  if (msg && msg.type === "rt-ping") { sendResponse({ ok: true, adapter: RT.adapter.name, top: isTop }); return true; }
});

// Auto-offer on a recognized application page (top frame only): show the panel,
// but do not fill until the user clicks (human-in-the-loop).
if (isTop && RT.adapter.name !== "generic") {
  panel.setStatus(`${RT.adapter.name} detected. Click Autofill when ready.`);
}
