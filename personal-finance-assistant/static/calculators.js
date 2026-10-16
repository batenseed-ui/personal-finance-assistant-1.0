(function () {
  "use strict";

  // ---------- small helpers ----------
  const $ = (sel, root) => (root || document).querySelector(sel);
  const $$ = (sel, root) => Array.from((root || document).querySelectorAll(sel));

  function el(tag, props, children) {
    const node = document.createElement(tag);
    Object.entries(props || {}).forEach(([k, v]) => {
      if (k === "class") node.className = v;
      else if (k === "text") node.textContent = v;
      else node.setAttribute(k, v);
    });
    (children || []).forEach((c) => node.append(c));
    return node;
  }

  const store = {
    get(key) {
      try { return window.localStorage.getItem(key); } catch (_) { return null; }
    },
    set(key, value) {
      try { window.localStorage.setItem(key, value); } catch (_) { /* storage may be blocked */ }
    },
  };

  // ---------- input persistence (same "wa:" keys as before, so saved numbers carry over) ----------
  function formValues(form) {
    const out = {};
    $$("input, select", form).forEach((f) => {
      if (!f.name) return;
      out[f.name] = f.type === "checkbox" ? f.checked : f.value;
    });
    return out;
  }

  function persistForm(form) {
    const key = "wa:" + form.dataset.persist;
    const raw = store.get(key);
    if (raw) {
      try {
        const saved = JSON.parse(raw);
        $$("input, select", form).forEach((f) => {
          if (!f.name || !(f.name in saved)) return;
          if (f.type === "checkbox") f.checked = !!saved[f.name];
          else f.value = saved[f.name];
        });
      } catch (_) { /* ignore corrupt data */ }
    }
    const save = () => store.set(key, JSON.stringify(formValues(form)));
    form.addEventListener("input", save);
    form.addEventListener("change", save);
  }

  // ---------- currency (shared with the main page) ----------
  const currencySel = $("#currency");
  const savedCurrency = store.get("wa:currency");
  if (savedCurrency && $$("option", currencySel).some((o) => o.value === savedCurrency)) currencySel.value = savedCurrency;
  currencySel.addEventListener("change", () => store.set("wa:currency", currencySel.value));

  function money(n, digits) {
    if (n === null || n === undefined || Number.isNaN(n)) return "n/a";
    const abs = Math.abs(n).toLocaleString(undefined, { maximumFractionDigits: digits || 0, minimumFractionDigits: digits || 0 });
    return (n < 0 ? "-" : "") + currencySel.value + abs;
  }

  // ---------- banner ----------
  const banner = $("#banner");
  const bannerText = $("#banner-text");
  const bannerRetry = $("#banner-retry");
  let retryFn = null;

  function showBanner(message, retry) {
    bannerText.textContent = message;
    retryFn = retry || null;
    bannerRetry.hidden = !retry;
    banner.hidden = false;
  }
  function hideBanner() { banner.hidden = true; retryFn = null; }
  $("#banner-close").addEventListener("click", hideBanner);
  bannerRetry.addEventListener("click", () => { const fn = retryFn; hideBanner(); if (fn) fn(); });

  // ---------- API wrapper ----------
  class ApiError extends Error {
    constructor(kind, message, fieldErrors) {
      super(message);
      this.kind = kind;
      this.fieldErrors = fieldErrors || null;
    }
  }

  async function api(path, options) {
    const opts = options || {};
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), opts.timeout || 20000);
    let response;
    try {
      response = await fetch(path, {
        method: opts.method || "GET",
        headers: opts.body ? { "Content-Type": "application/json" } : undefined,
        body: opts.body ? JSON.stringify(opts.body) : undefined,
        signal: controller.signal,
      });
    } catch (err) {
      if (err && err.name === "AbortError") {
        throw new ApiError("timeout", "The server took too long to respond. Your inputs are saved; please try again.");
      }
      throw new ApiError("network", "Could not reach the server. Check that the Flask app is running and your connection is up. Your inputs are saved.");
    } finally {
      clearTimeout(timer);
    }

    let data = null;
    try { data = await response.json(); } catch (_) { /* handled below */ }

    if (!data || typeof data !== "object") {
      throw new ApiError("badresponse", `The server sent an unexpected response (HTTP ${response.status}). Please try again.`);
    }
    if (!response.ok || data.ok === false) {
      if (response.status === 400 && data.field_errors) {
        throw new ApiError("validation", data.error || "Please fix the highlighted fields.", data.field_errors);
      }
      throw new ApiError("server", data.error || `The server returned HTTP ${response.status}.`);
    }
    return data;
  }

  // ---------- field errors ----------
  function clearFieldErrors(scope) {
    $$("[data-error-for]", scope).forEach((p) => { p.textContent = ""; });
    $$("[aria-invalid]", scope).forEach((i) => i.removeAttribute("aria-invalid"));
  }
  function showFieldErrors(scope, errors) {
    let first = null;
    Object.entries(errors).forEach(([name, message]) => {
      const p = $(`[data-error-for="${name}"]`, scope);
      const input = $(`[name="${name}"]`, scope);
      if (p) p.textContent = message;
      if (input) {
        input.setAttribute("aria-invalid", "true");
        if (!first) first = input;
      }
    });
    if (first) first.focus();
  }

  function handleError(err, retry, scope) {
    if (err instanceof ApiError && err.kind === "validation" && err.fieldErrors) {
      showFieldErrors(scope, err.fieldErrors);
      showBanner(err.message);
    } else if (err instanceof ApiError) {
      showBanner(err.message, retry);
    } else {
      showBanner("Something went wrong in the page. Your inputs are saved; please try again.", retry);
      console.error(err);
    }
  }

  // ---------- calculators ----------
  function rows(pairs) {
    const dl = el("dl");
    pairs.forEach(([k, v]) => { dl.append(el("dt", { text: k }), el("dd", { text: v })); });
    return dl;
  }

  const CALC_RENDER = {
    compound(r) {
      const dl = rows([
        ["Final balance", money(r.final_balance)],
        ["You contributed", money(r.total_contributed)],
        ["Growth earned", money(r.interest_earned)],
      ]);
      const table = el("table", {}, [
        el("thead", {}, [el("tr", {}, [el("th", { text: "Year" }), el("th", { text: "Balance" })])]),
        el("tbody", {}, r.schedule.map((s) => el("tr", {}, [el("td", { text: String(s.year) }), el("td", { text: money(s.balance) })]))),
      ]);
      return [dl, el("details", {}, [el("summary", { text: "Year by year" }), table])];
    },
    loan(r) {
      return [rows([
        ["Monthly payment", money(r.monthly_payment, 2)],
        ["Total paid", money(r.total_paid)],
        ["Total interest", money(r.total_interest)],
        ["Number of payments", String(r.months)],
      ])];
    },
    goal(r) {
      if (!r.reached) {
        return [el("p", { text: `Not reached within 100 years at this pace (balance ${money(r.final_balance)}). Try saving more each month.` })];
      }
      if (r.months === 0) return [el("p", { text: "You have already reached this goal." })];
      return [rows([
        ["Time to goal", `${r.years} years (${r.months} months)`],
        ["Balance at that point", money(r.final_balance)],
      ])];
    },
  };

  $$("form.calc").forEach((form) => {
    persistForm(form);
    const kind = form.dataset.calc;
    const out = $(".calc-out", form);
    const submit = $("button[type=submit]", form);

    async function run() {
      hideBanner();
      clearFieldErrors(form);
      out.textContent = "";
      const original = submit.textContent;
      submit.disabled = true;
      submit.textContent = "Calculating…";
      try {
        const data = await api("/api/calc/" + kind, { method: "POST", body: formValues(form) });
        CALC_RENDER[kind](data.result).forEach((n) => out.append(n));
      } catch (err) {
        handleError(err, run, form);
      } finally {
        submit.disabled = false;
        submit.textContent = original;
      }
    }
    form.addEventListener("submit", (e) => { e.preventDefault(); run(); });
  });
})();
