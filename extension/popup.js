"use strict";

const SERVER = "http://localhost:8000";

// Runs in the page context: prefer the user's selection, else the main content.
function extractJD() {
  const sel = (window.getSelection && window.getSelection().toString() || "").trim();
  if (sel.length > 150) return sel.slice(0, 20000);
  const main =
    document.querySelector("main, article, [role='main'], .job, .jobsearch-JobComponent") ||
    document.body;
  return (main.innerText || "").trim().slice(0, 20000);
}

function setStatus(msg, cls) {
  const el = document.getElementById("status");
  el.textContent = msg;
  el.className = cls || "";
}

document.getElementById("capture").addEventListener("click", async () => {
  const btn = document.getElementById("capture");
  btn.disabled = true;
  setStatus("Reading page…");
  try {
    const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
    const [{ result: jd }] = await chrome.scripting.executeScript({
      target: { tabId: tab.id },
      func: extractJD,
    });
    if (!jd || jd.length < 60) {
      setStatus("Couldn't find enough text — try selecting the JD first.", "bad");
      btn.disabled = false;
      return;
    }
    setStatus("Sending to Resume Tailor…");
    const res = await fetch(`${SERVER}/ingest`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        jd_text: jd,
        company: document.getElementById("company").value.trim() || null,
        role: document.getElementById("role").value.trim() || null,
      }),
    });
    if (!res.ok) throw new Error(`server ${res.status}`);
    const data = await res.json();
    setStatus(`✓ Captured ${data.chars} chars. Open Resume Tailor → "Load from extension".`, "ok");
  } catch (e) {
    setStatus(
      "Failed: " + e.message + ". Is the app running (run.bat)?",
      "bad"
    );
  } finally {
    btn.disabled = false;
  }
});
