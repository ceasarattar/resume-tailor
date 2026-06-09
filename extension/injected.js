"use strict";
/* Resume Tailor — MAIN-world helper (the "expert" fallback).
 *
 * Runs in the PAGE's JS world (not the isolated content-script world), so it can
 * reach React/Vue internals that the content script can't. The content script
 * does the easy 90% (native value setter + bubbling events). When a value won't
 * "stick" — frameworks that track input via a private value tracker or that only
 * commit on a specific handler — content.js tags the element with data-rt-id and
 * asks us, over a CustomEvent bridge, to set it the hard way:
 *
 *   1. native HTMLInputElement value setter (bypasses the framework's override),
 *   2. fire input/change/blur so the framework re-renders,
 *   3. if a React fiber is present, call its onChange/onInput/onBlur prop directly
 *      with a synthetic-ish event, which is what frameworks with frontend
 *      validation (Workday's onBlur commit) actually listen to.
 *
 * It answers with a result event so content.js can verify the value committed.
 */
(() => {
  if (window.__rtInjected) return;
  window.__rtInjected = true;

  function deepQuery(selector, root = document) {
    const el = root.querySelector(selector);
    if (el) return el;
    const nodes = root.querySelectorAll("*");
    for (const node of nodes) {
      if (node.shadowRoot) {
        const found = deepQuery(selector, node.shadowRoot);
        if (found) return found;
      }
    }
    return null;
  }

  function nativeSet(el, value) {
    const proto = Object.getPrototypeOf(el);
    const desc =
      Object.getOwnPropertyDescriptor(proto, "value") ||
      Object.getOwnPropertyDescriptor(el, "value");
    if (desc && desc.set) desc.set.call(el, value);
    else el.value = value;
  }

  // Find the React props object on a DOM node across React versions.
  function reactProps(el) {
    const key = Object.keys(el).find(
      (k) => k.startsWith("__reactProps$") || k.startsWith("__reactEventHandlers$")
    );
    return key ? el[key] : null;
  }

  function fire(el, type) {
    el.dispatchEvent(new Event(type, { bubbles: true }));
  }

  function setValue(el, value) {
    el.focus && el.focus();
    nativeSet(el, value);
    fire(el, "input");
    fire(el, "change");

    // Framework-handler fallback (React onChange/onBlur with validation).
    const props = reactProps(el);
    if (props) {
      const synthetic = { target: el, currentTarget: el, bubbles: true, type: "change" };
      try { props.onChange && props.onChange(synthetic); } catch (_) {}
      try { props.onInput && props.onInput(synthetic); } catch (_) {}
      try { props.onBlur && props.onBlur({ ...synthetic, type: "blur" }); } catch (_) {}
    }
    fire(el, "blur");
    return el.value === String(value);
  }

  document.addEventListener("rt-fill-request", (ev) => {
    const { id, rtId, value } = ev.detail || {};
    let ok = false;
    let msg = "";
    try {
      const el = deepQuery(`[data-rt-id="${CSS.escape(String(rtId))}"]`);
      if (!el) {
        msg = "element not found in main world";
      } else {
        ok = setValue(el, value);
        if (!ok) msg = "value did not commit";
      }
    } catch (e) {
      msg = String(e);
    }
    document.dispatchEvent(
      new CustomEvent("rt-fill-result", { detail: { id, ok, msg } })
    );
  });

  // Announce readiness so content.js knows the bridge is live.
  document.dispatchEvent(new CustomEvent("rt-injected-ready"));
})();
