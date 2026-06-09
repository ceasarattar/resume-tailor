"use strict";
/* Resume Tailor — ATS adapters.
 *
 * Each adapter teaches the generic scanner how to recognize ONE applicant
 * tracking system: how to detect it, which container holds the form, and how to
 * pull a clean label + options off a field. The scanner (content.js) falls back
 * to a generic adapter for unknown portals, so a missing adapter degrades
 * gracefully rather than breaking.
 *
 * Selectors WILL drift (especially Workday). Treat them as starting points and
 * tune against a live form. Keep detection cheap and label extraction generous.
 */

// Readable label from Workday's data-automation-id (e.g. "legalNameSection_firstName"
// -> "first name"). Strip section prefixes and camelCase-split the tail.
function humanizeAutomationId(id) {
  if (!id) return "";
  const tail = id.split(/[_-]/).pop() || id;
  return tail
    .replace(/([a-z0-9])([A-Z])/g, "$1 $2")
    .replace(/[_-]+/g, " ")
    .toLowerCase()
    .trim();
}

const RT_ADAPTERS = {
  greenhouse: {
    name: "greenhouse",
    test: () => /greenhouse\.io/.test(location.host) ||
                !!document.querySelector("#application_form, [id^='job_application'], .application--form"),
    formRoot: () =>
      document.querySelector("#application_form, form#application-form, .application--form") ||
      document.querySelector("form"),
    // Greenhouse uses react-select for dropdowns (div.select__control).
    comboboxSelector: ".select__control, [class*='select__control']",
  },

  lever: {
    name: "lever",
    test: () => /lever\.co/.test(location.host) ||
                !!document.querySelector(".application-form, form[action*='lever']"),
    formRoot: () => document.querySelector(".application-form, form") || document.body,
    comboboxSelector: "[role='combobox']",
  },

  ashby: {
    name: "ashby",
    test: () => /ashbyhq\.com/.test(location.host) ||
                !!document.querySelector("[class*='ashby']"),
    formRoot: () => document.querySelector("form") || document.body,
    comboboxSelector: "[role='combobox'], [class*='_select_']",
  },

  workday: {
    name: "workday",
    test: () => /myworkdayjobs\.com/.test(location.host) ||
                !!document.querySelector("[data-automation-id]"),
    formRoot: () =>
      document.querySelector("[data-automation-id='applyFlowPage'], [data-automation-id='jobApplicationPage']") ||
      document.querySelector("form") || document.body,
    // Workday dropdowns are buttons that open a [role='listbox'].
    comboboxSelector: "button[aria-haspopup='listbox'], [data-automation-id='multiSelectContainer']",
    // Prefer the automation id as the label source.
    labelFor: (el) => {
      const owner = el.closest("[data-automation-id]");
      const id = owner && owner.getAttribute("data-automation-id");
      return humanizeAutomationId(id);
    },
    // The submit button — never auto-clicked, used only to position the review gate.
    submitSelector: "[data-automation-id='bottom-navigation-next-button'], button[type='submit']",
  },

  icims: {
    name: "icims",
    test: () => /icims\.com/.test(location.host),
    formRoot: () => document.querySelector("form") || document.body,
    comboboxSelector: "[role='combobox']",
  },

  smartrecruiters: {
    name: "smartrecruiters",
    test: () => /smartrecruiters\.com/.test(location.host),
    formRoot: () => document.querySelector("form") || document.body,
    comboboxSelector: "[role='combobox'], [class*='select']",
  },

  generic: {
    name: "generic",
    test: () => true,
    formRoot: () => document.querySelector("form") || document.body,
    comboboxSelector: "[role='combobox']",
  },
};

function detectAdapter() {
  for (const key of ["greenhouse", "lever", "ashby", "workday", "icims", "smartrecruiters"]) {
    try {
      if (RT_ADAPTERS[key].test()) return RT_ADAPTERS[key];
    } catch (_) { /* keep probing */ }
  }
  return RT_ADAPTERS.generic;
}
