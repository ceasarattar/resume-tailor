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

// Autofill the application on the current tab. Works on the built-in ATS list
// automatically; for any other portal we inject the content script on demand.
document.getElementById("autofill").addEventListener("click", async () => {
  const btn = document.getElementById("autofill");
  btn.disabled = true;
  setStatus("Starting autofill…");
  try {
    const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
    const send = () =>
      new Promise((resolve) =>
        chrome.tabs.sendMessage(tab.id, { type: "rt-autofill" }, (resp) =>
          resolve(chrome.runtime.lastError ? null : resp)
        )
      );
    let resp = await send();
    if (!resp) {
      // No content script on this (unknown) site yet — inject, then retry.
      setStatus("Injecting on this page…");
      await chrome.scripting.insertCSS({ target: { tabId: tab.id, allFrames: true }, files: ["overlay.css"] });
      await chrome.scripting.executeScript({
        target: { tabId: tab.id, allFrames: true },
        files: ["adapters.js", "content.js"],
      });
      resp = await send();
    }
    setStatus(resp && resp.ok ? "Autofill running — see the panel on the page." : "Couldn't start. Reload the page and retry.", resp && resp.ok ? "ok" : "bad");
    if (resp && resp.ok) window.close();
  } catch (e) {
    setStatus("Failed: " + e.message, "bad");
  } finally {
    btn.disabled = false;
  }
});

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
