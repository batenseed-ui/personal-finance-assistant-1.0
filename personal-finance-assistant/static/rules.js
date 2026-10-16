(function () {
  "use strict";

  const body = document.getElementById("method-body");

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

  function render(m) {
    body.textContent = "";
    body.append(el("p", { text: m.summary }));

    const ul = el("ul");
    m.components.forEach((c) => {
      ul.append(el("li", {}, [
        el("strong", { text: `${c.name} (${c.max} points)` }),
        el("br"),
        el("span", { class: "formula", text: c.formula }),
        el("br"),
        el("span", { class: "muted", text: "Target: " + c.target }),
      ]));
    });
    body.append(ul);

    body.append(el("p", { text: "Assumptions: " + Object.values(m.assumptions).join("; ") + "." }));
    body.append(el("p", { text: "Grades: " + m.grades.map((g) => `${g.label} (${g.min}+)`).join(", ") + "." }));
    body.append(el("p", { class: "fine", text: m.limits }));
  }

  async function load() {
    try {
      const response = await fetch("/api/methodology");
      const data = await response.json();
      if (!response.ok || !data || !data.methodology) throw new Error("Unexpected response");
      render(data.methodology);
    } catch (err) {
      body.textContent = "";
      const retry = el("button", { type: "button", class: "link-btn", text: "Try again" });
      retry.addEventListener("click", load);
      body.append(el("p", { class: "muted", text: "The scoring method could not be loaded from the server. " }), retry);
    }
  }

  load();
})();
