"use strict";

const $ = (id) => document.getElementById(id);
let lastParse = null; // { jd, company_slug, role_slug }

async function api(path, opts) {
  const res = await fetch(path, opts);
  if (!res.ok) {
    let detail = res.statusText;
    try { detail = (await res.json()).detail || detail; } catch (_) {}
    throw new Error(detail);
  }
  return res.json();
}

function overlay(on, msg) {
  $("overlayMsg").textContent = msg || "Working…";
  $("overlay").hidden = !on;
}
function toast(msg, bad) {
  const t = $("toast");
  t.textContent = msg;
  t.className = "toast" + (bad ? " bad" : "");
  t.hidden = false;
  clearTimeout(toast._t);
  toast._t = setTimeout(() => (t.hidden = true), 4000);
}
function fillList(id, items) {
  const ul = $(id);
  ul.innerHTML = "";
  (items && items.length ? items : ["—"]).forEach((x) => {
    const li = document.createElement("li");
    li.textContent = x;
    ul.appendChild(li);
  });
}

async function refreshHealth() {
  const el = $("health");
  try {
    const h = await api("/api/health");
    const ok = h.ollama && h.tectonic;
    el.className = "health " + (ok ? "ok" : "bad");
    el.textContent = ok
      ? `ready · ${h.tier} · ollama ${h.ollama}`
      : `not ready · ollama:${h.ollama || "down"} tectonic:${h.tectonic}`;
  } catch (e) {
    el.className = "health bad";
    el.textContent = "backend unreachable";
  }
}

async function doParse() {
  const jd = $("jd").value.trim();
  if (jd.length < 30) return toast("Paste a job description first.", true);
  overlay(true, "Parsing job description…");
  try {
    lastParse = await api("/api/parse", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ jd_text: jd }),
    });
    const j = lastParse.jd;
    $("company").value = j.company || "";
    $("role").value = j.title || "";
    $("seniority").textContent = j.seniority || "—";
    $("keywords").textContent = (j.keywords || []).join(", ") || "—";
    fillList("musts", j.must_haves);
    fillList("nices", j.nice_to_haves);
    $("confirmCard").hidden = false;
    $("confirmCard").scrollIntoView({ behavior: "smooth" });
  } catch (e) {
    toast("Parse failed: " + e.message, true);
  } finally {
    overlay(false);
  }
}

async function doGenerate() {
  const jd = $("jd").value.trim();
  overlay(true, "Tailoring & compiling — this can take a minute on the local model…");
  try {
    const r = await api("/api/generate", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        jd_text: jd,
        company: $("company").value.trim() || null,
        role: $("role").value.trim() || null,
      }),
    });
    const bust = "?t=" + Date.now();
    $("pdfFrame").src = r.pdf_url + bust;
    $("pdfLink").href = r.pdf_url + bust;
    $("texLink").href = r.tex_url + bust;
    let status = r.ats_ok
      ? "✓ ATS text-layer check passed (1 page, clean extraction)."
      : "⚠ ATS issues: " + r.ats_issues.join("; ");
    if (r.grounding_ok === false) {
      status += "  |  ⛔ HONESTY CHECK FAILED — possible fabrication: " +
        (r.grounding_violations || []).join("; ") + ". Review before using.";
    } else if (r.grounding_ok === true) {
      status += "  |  ✓ Honesty check passed.";
    }
    $("atsStatus").textContent = status;
    fillList("changelog", r.changelog);
    fillList("missing", r.missing_requirements);
    $("resultCard").hidden = false;
    $("correctCard").hidden = false;
    $("resultCard").scrollIntoView({ behavior: "smooth" });
    toast("Résumé generated → " + r.out_dir);
  } catch (e) {
    toast("Generate failed: " + e.message, true);
  } finally {
    overlay(false);
  }
}

async function doCorrect() {
  const text = $("correction").value.trim();
  if (!text) return toast("Write a correction first.", true);
  try {
    const r = await api("/api/correct", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        text,
        company: $("company").value.trim(),
        role: $("role").value.trim(),
        jd_context: $("jd").value.trim().slice(0, 4000),
      }),
    });
    $("correction").value = "";
    $("correctMsg").textContent = "Saved: " + r.line;
    toast("Correction saved to corrections.md");
  } catch (e) {
    toast("Save failed: " + e.message, true);
  }
}

async function loadPending() {
  try {
    const r = await api("/api/pending");
    if (!r.pending) return toast("Nothing captured from the extension yet.");
    $("jd").value = r.pending.jd_text;
    if (r.pending.company) $("company").value = r.pending.company;
    if (r.pending.role) $("role").value = r.pending.role;
    toast("Loaded JD captured by the extension.");
  } catch (e) {
    toast("Could not load pending JD: " + e.message, true);
  }
}

$("parseBtn").addEventListener("click", doParse);
$("generateBtn").addEventListener("click", doGenerate);
$("correctBtn").addEventListener("click", doCorrect);
$("loadPending").addEventListener("click", loadPending);
refreshHealth();
