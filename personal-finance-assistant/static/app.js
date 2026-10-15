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
    remove(key) {
      try { window.localStorage.removeItem(key); } catch (_) { /* ignore */ }
    },
  };

  // ---------- input persistence ----------
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

  // ---------- currency ----------
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

  // ---------- busy button helper ----------
  async function withBusy(button, busyLabel, fn) {
    const original = button.textContent;
    const siblings = $$(".actions button.btn", button.closest("form") || document);
    siblings.forEach((b) => { b.disabled = true; });
    button.textContent = busyLabel;
    try {
      await fn();
    } finally {
      button.textContent = original;
      siblings.forEach((b) => { b.disabled = false; });
    }
  }

  // ---------- Markdown -> safe page elements ----------
  // The AI answers in light Markdown (## headings, **bold**, - bullets). Showing that text raw leaves
  // the # and * symbols on screen, so it is turned into real headings, bold text and lists here.
  // Only textContent and text nodes are used, never innerHTML, so nothing the AI sends can inject HTML.
  function appendInline(parent, text) {
    const re = /\*\*(.+?)\*\*|__(.+?)__|`([^`]+)`|\*(?!\s)([^*]+?)\*/g;
    const plain = (s) => s.replace(/\*\*/g, ""); // drop a stray, unmatched ** instead of showing it
    let last = 0;
    let m;
    while ((m = re.exec(text)) !== null) {
      if (m.index > last) parent.append(plain(text.slice(last, m.index)));
      if (m[1] !== undefined || m[2] !== undefined) {
        const strong = el("strong");
        appendInline(strong, m[1] !== undefined ? m[1] : m[2]);
        parent.append(strong);
      } else if (m[3] !== undefined) {
        parent.append(el("code", { text: m[3] }));
      } else {
        const em = el("em");
        appendInline(em, m[4]);
        parent.append(em);
      }
      last = re.lastIndex;
    }
    if (last < text.length) parent.append(plain(text.slice(last)));
  }

  function renderMarkdown(text, container) {
    container.textContent = "";
    let para = [];
    let list = null; // { type: "ul" | "ol", node }

    const flushPara = () => {
      if (!para.length) return;
      const p = el("p");
      appendInline(p, para.join(" "));
      container.append(p);
      para = [];
    };
    const addItem = (type, content, number) => {
      flushPara();
      if (!list || list.type !== type) {
        const node = el(type);
        if (type === "ol" && number > 1) node.setAttribute("start", String(number));
        container.append(node);
        list = { type, node };
      }
      const li = el("li");
      appendInline(li, content.trim());
      list.node.append(li);
    };

    String(text || "").replace(/\r\n?/g, "\n").split("\n").forEach((raw) => {
      const line = raw.replace(/\s+$/, "");
      let m;
      if (!line.trim()) { flushPara(); list = null; return; }
      if ((m = line.match(/^\s{0,3}(#{1,6})\s+(.*?)\s*#*\s*$/))) {            // ## Heading
        flushPara(); list = null;
        const h = el(m[1].length <= 2 ? "h3" : "h4");
        appendInline(h, m[2]);
        container.append(h);
        return;
      }
      if (/^\s{0,3}([-*_])(\s*\1){2,}\s*$/.test(line)) {                       // --- divider
        flushPara(); list = null;
        container.append(el("hr"));
        return;
      }
      if ((m = line.match(/^\s*[-*+\u2022]\s+(.*)$/))) { addItem("ul", m[1]); return; }          // - bullet
      if ((m = line.match(/^\s*(\d+)[.)]\s+(.*)$/))) { addItem("ol", m[2], parseInt(m[1], 10)); return; } // 1. step
      if (list && /^\s+\S/.test(raw)) {                                         // indented continuation
        const li = list.node.lastChild;
        li.append(" ");
        appendInline(li, line.trim());
        return;
      }
      list = null;
      para.push(line.trim().replace(/^>\s?/, ""));
    });
    flushPara();
  }

  // ---------- profile form ----------
  const profileForm = $("#profile-form");
  persistForm(profileForm);

  function readProfile() {
    const v = formValues(profileForm);
    delete v.currency;
    return v;
  }

  const CIRC = 2 * Math.PI * 52;
  const ring = $("#ring-fg");
  ring.style.strokeDasharray = String(CIRC);
  ring.style.strokeDashoffset = String(CIRC);

  function gradeColor(score) {
    if (score >= 65) return "var(--good)";
    if (score >= 50) return "var(--ok)";
    return "var(--poor)";
  }

  function renderScore(analysis, guidance) {
    $("#empty-state").hidden = true;
    $("#score-card").hidden = false;

    $("#score-num").textContent = String(analysis.score);
    ring.style.stroke = gradeColor(analysis.score);
    ring.style.strokeDashoffset = String(CIRC * (1 - analysis.score / 100));
    $("#score-grade").textContent = analysis.grade;
    $("#score-summary").textContent = guidance.summary;

    const body = $("#breakdown-body");
    body.textContent = "";
    analysis.components.forEach((c) => {
      const bar = el("div", { class: "bar" }, [el("span", { class: "st-" + c.status })]);
      bar.firstChild.style.width = Math.round((c.points / c.max) * 100) + "%";
      const nameCell = el("td", {}, [
        el("strong", { text: c.name }),
        el("span", { class: "detail", text: c.detail }),
      ]);
      const ptsCell = el("td", { text: `${c.points} / ${c.max}` });
      const barCell = el("td", { class: "bar-col" }, [bar]);
      body.append(el("tr", {}, [nameCell, ptsCell, barCell]));
    });

    const g = $("#guidance");
    g.textContent = "";
    g.append(el("h3", { text: "Where to focus first" }));
    if (guidance.recommendations.length) {
      const ol = el("ol");
      guidance.recommendations.forEach((r) => {
        ol.append(el("li", {}, [
          el("strong", { text: r.title + ". " }),
          document.createTextNode(r.action + ` (up to ${r.points_available} points available)`),
        ]));
      });
      g.append(ol);
    } else {
      g.append(el("p", { class: "muted", text: "Every area is on target." }));
    }
    g.append(el("p", { class: "fine", text: guidance.risk_note }));
    setupWhatIf();
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

  async function analyze() {
    hideBanner();
    clearFieldErrors(profileForm);
    await withBusy($("#btn-analyze"), "Analyzing…", async () => {
      try {
        const data = await api("/api/analyze", { method: "POST", body: readProfile() });
        renderScore(data.analysis, data.guidance);
      } catch (err) {
        handleError(err, analyze, profileForm);
      }
    });
  }

  async function getAdvice() {
    hideBanner();
    clearFieldErrors(profileForm);
    await withBusy($("#btn-advice"), "Getting advice…", async () => {
      try {
        const data = await api("/api/advice", { method: "POST", body: readProfile(), timeout: 45000 });
        renderScore(data.analysis, data.guidance);
        const card = $("#advice-card");
        card.hidden = false;
        const badge = $("#advice-badge");
        const notice = $("#advice-notice");
        const text = $("#advice-text");
        if (data.source !== "rule-based") {
          badge.textContent = data.source === "gemini" ? "AI advice (Gemini)" : "AI advice (Claude)";
          notice.hidden = true;
          renderMarkdown(data.advice_text, text);
        } else {
          badge.textContent = "Rule-based fallback";
          notice.hidden = false;
          notice.textContent = data.notice || "AI advice is unavailable right now.";
          renderMarkdown(
            data.guidance.summary + "\n\n" +
            "The prioritised actions, with amounts worked out from your numbers, are listed under your score above.\n\n" +
            data.guidance.risk_note,
            text
          );
        }
        card.scrollIntoView({ behavior: "smooth", block: "nearest" });
      } catch (err) {
        handleError(err, getAdvice, profileForm);
      }
    });
  }

  $("#btn-analyze").addEventListener("click", analyze);
  $("#btn-advice").addEventListener("click", getAdvice);
  profileForm.addEventListener("submit", (e) => { e.preventDefault(); analyze(); });

  const SAMPLE = {
    age: "34", retirement_age: "62", monthly_income: "5500", monthly_expenses: "3200",
    monthly_debt_payments: "600", monthly_investment: "400", liquid_savings: "9000",
    investments: "42000", total_debt: "38000", risk_tolerance: "medium",
    has_health_insurance: true, has_life_insurance: false,
  };
  $("#btn-sample").addEventListener("click", () => {
    $$("input, select", profileForm).forEach((f) => {
      if (!(f.name in SAMPLE)) return;
      if (f.type === "checkbox") f.checked = SAMPLE[f.name]; else f.value = SAMPLE[f.name];
    });
    profileForm.dispatchEvent(new Event("input", { bubbles: true }));
    clearFieldErrors(profileForm);
  });
  $("#btn-clear").addEventListener("click", () => {
    store.remove("wa:profile");
    profileForm.reset();
    clearFieldErrors(profileForm);
  });

  // ---------- what-if simulator ----------
  const wiCard = $("#whatif-card");
  const wiSliders = $$("input[data-adj]", wiCard);
  let wiTimer = null;
  let wiSeq = 0;

  function niceStep(max) { return max <= 1000 ? 10 : max <= 10000 ? 50 : 100; }

  function updateWiOutput(slider) {
    const out = $("#wi-out-" + slider.dataset.adj);
    const v = parseFloat(slider.value) || 0;
    out.textContent = slider.dataset.adj === "delay_retirement_years" ? `${v} year${v === 1 ? "" : "s"}` : money(v);
  }

  function currentAdjustments() {
    const a = {};
    wiSliders.forEach((s) => { a[s.dataset.adj] = parseFloat(s.value) || 0; });
    return a;
  }

  // Called after every successful score: sizes the sliders to the user's own numbers and refreshes.
  function setupWhatIf() {
    const p = readProfile();
    const income = parseFloat(p.monthly_income) || 0;
    const expenses = parseFloat(p.monthly_expenses) || 0;
    const retireAge = parseFloat(p.retirement_age) || 0;
    const maxes = {
      expense_cut: Math.max(0, Math.floor(expenses)),
      extra_investment: Math.max(0, Math.floor(income)),
      extra_cash: Math.max(0, Math.floor(income * 6)),
      delay_retirement_years: Math.max(0, Math.min(10, 100 - retireAge)),
    };
    wiSliders.forEach((s) => {
      const name = s.dataset.adj;
      s.max = String(maxes[name]);
      s.step = name === "delay_retirement_years" ? "1" : String(niceStep(maxes[name]));
      if (parseFloat(s.value) > maxes[name]) s.value = String(maxes[name]);
      updateWiOutput(s);
    });
    wiCard.hidden = false;
    refreshWhatIf();
  }

  async function refreshWhatIf() {
    clearFieldErrors(wiCard);
    const seq = ++wiSeq; // ignore answers that arrive after a newer request
    try {
      const data = await api("/api/whatif", { method: "POST", body: { profile: readProfile(), adjustments: currentAdjustments() } });
      if (seq === wiSeq) renderWhatIf(data);
    } catch (err) {
      if (seq !== wiSeq) return;
      if (err instanceof ApiError && err.kind === "validation" && err.fieldErrors) {
        const adjErrors = {};
        Object.entries(err.fieldErrors).forEach(([k, v]) => {
          if (wiSliders.some((s) => s.dataset.adj === k)) adjErrors[k] = v;
        });
        showFieldErrors(wiCard, adjErrors);
        showBanner(Object.keys(adjErrors).length ? err.message : "Your numbers above need fixing before the simulator can run. Use Analyze My Finances to see which.");
      } else {
        handleError(err, refreshWhatIf, wiCard);
      }
    }
  }

  function renderWhatIf(d) {
    $("#wi-base").textContent = String(d.base_score);
    $("#wi-new").textContent = String(d.scenario_score);
    const delta = $("#wi-delta");
    delta.textContent = (d.delta > 0 ? "+" : "") + d.delta;
    delta.className = "wi-delta" + (d.delta > 0 ? " up" : d.delta < 0 ? " down" : "");
    $("#wi-grade").textContent = d.base_grade === d.scenario_grade
      ? `Grade stays ${d.scenario_grade}.`
      : `Grade changes from ${d.base_grade} to ${d.scenario_grade}.`;

    const warn = $("#wi-warnings");
    warn.textContent = "";
    d.warnings.forEach((w) => warn.append(el("li", { text: w })));
    warn.hidden = d.warnings.length === 0;

    const body = $("#wi-body");
    body.textContent = "";
    d.components.forEach((c) => {
      const cls = c.change > 0 ? "chg-up" : c.change < 0 ? "chg-down" : "chg-flat";
      const text = c.change > 0 ? "+" + c.change : c.change < 0 ? String(c.change) : "no change";
      body.append(el("tr", {}, [
        el("td", { text: c.name }),
        el("td", { text: `${c.before} / ${c.max}` }),
        el("td", { text: `${c.after} / ${c.max}` }),
        el("td", { class: cls, text }),
      ]));
    });
  }

  wiSliders.forEach((s) => {
    s.addEventListener("input", () => {
      updateWiOutput(s);
      clearTimeout(wiTimer);
      wiTimer = setTimeout(refreshWhatIf, 250);
    });
  });
  $("#wi-reset").addEventListener("click", () => {
    wiSliders.forEach((s) => { s.value = "0"; updateWiOutput(s); });
    refreshWhatIf();
  });

  // The calculators live on their own page (calculators.js) and the scoring rules on theirs (rules.js).
})();
