"use strict";

const SERVER = "http://localhost:8000";

// ----------------------------------------------------------------- queue
async function loadQueue() {
  const box = document.getElementById("queue");
  const applied = document.getElementById("appliedToday");
  box.innerHTML = '<div class="empty">Loading…</div>';
  let data;
  try {
    const res = await fetch(`${SERVER}/api/pipeline/queue`);
    if (!res.ok) throw new Error(`server ${res.status}`);
    data = await res.json();
  } catch (e) {
    box.innerHTML =
      '<div class="empty">Can\'t reach the app. Start it:<br><code>uvicorn app.main:app --port 8000</code></div>';
    applied.textContent = "";
    return;
  }
  const jobs = data.queue || [];
  applied.textContent = `${jobs.length} ready · ${data.applied_today || 0} applied today`;
  if (!jobs.length) {
    box.innerHTML =
      '<div class="empty">Nothing queued. Run <code>python -m app.pipeline run</code> to discover + tailor jobs.</div>';
    return;
  }
  box.innerHTML = "";
  jobs.forEach((job) => box.appendChild(renderJob(job)));
}

function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text != null) e.textContent = text;
  return e;
}

function renderJob(job) {
  const card = el("div", "job");

  const title = el("a", "title", `${job.company || "?"} — ${job.title || "Role"}`);
  title.href = job.apply_url || "#";
  title.target = "_blank";
  card.appendChild(title);

  const meta = el("div", "meta");
  if (job.score != null) meta.appendChild(el("span", "badge", `fit ${job.score}`));
  if (job.posted_at) meta.appendChild(el("span", "badge", job.posted_at));
  if (job.ats) meta.appendChild(el("span", "badge", job.ats));
  meta.appendChild(
    el("span", "badge " + (job.ready ? "ready" : "review"),
       job.ready ? "✓ all fields ready" : `${(job.review_items || []).length} to review`)
  );
  card.appendChild(meta);

  const acts = el("div", "acts");

  const open = el("a", "open", "Open & apply");
  open.href = job.apply_url || "#";
  open.target = "_blank";
  open.addEventListener("click", (ev) => {
    ev.preventDefault();
    if (job.apply_url) chrome.tabs.create({ url: job.apply_url, active: true });
  });
  acts.appendChild(open);

  if (job.resume_url) {
    const cv = el("a", "resume", "résumé");
    cv.href = SERVER + job.resume_url;
    cv.target = "_blank";
    acts.appendChild(cv);
  }

  const done = el("button", "applied", "Applied ✓");
  done.addEventListener("click", async () => {
    done.disabled = true;
    done.textContent = "…";
    try {
      const res = await fetch(`${SERVER}/api/pipeline/applied`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ uid: job.uid }),
      });
      if (!res.ok) throw new Error(`server ${res.status}`);
      card.style.transition = "opacity .25s";
      card.style.opacity = "0";
      setTimeout(loadQueue, 280);
    } catch (e) {
      done.disabled = false;
      done.textContent = "retry";
    }
  });
  acts.appendChild(done);

  card.appendChild(acts);
  return card;
}

document.getElementById("refresh").addEventListener("click", loadQueue);
document.addEventListener("DOMContentLoaded", loadQueue);

// --------------------------------------------------- capture / autofill (existing)
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
    setStatus("Failed: " + e.message + ". Is the app running (run.bat)?", "bad");
  } finally {
    btn.disabled = false;
  }
});
