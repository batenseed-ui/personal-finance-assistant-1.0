/* Shows who is signed in, adds a Log out link, and helps when the session has ended.

   Needs ONE line in templates/index.html, just before </body>:
       <script src="/static/auth-widget.js" defer></script>

   - "Signed in as ... Log out" is placed at the top of the page header (ids/classes: #wa-auth-bar).
   - When any /api/ request comes back 401 (logged out or session expired), a "Log in again" link
     is added to the app's own red message bar (#banner), next to its Retry button. The link opens
     the login page in a NEW tab, so nothing typed on this page is lost; after logging in, press Retry.
     If the app's message bar is not showing, a bar of the same look is shown at the top instead.
   - Clicking Log out also removes the numbers app.js saved in this browser (see forgetSavedInputs).
   Colours come from the app's own CSS variables (style.css), so light and dark mode match. */
(function () {
  "use strict";

  var SKIP = ["/api/login", "/api/register", "/api/logout"];
  var nativeFetch = window.fetch.bind(window);
  var bar = null;
  var ownBanner = null;

  // ---- styles (use the app's CSS variables, with fallbacks if they are missing) -------------
  var style = document.createElement("style");
  style.id = "wa-auth-style";
  style.textContent =
    "#wa-auth-bar{display:flex;justify-content:flex-end;align-items:center;gap:8px;flex-wrap:wrap;" +
    "margin:0 0 6px;font-size:.88rem;color:var(--muted,#5d6a63)}" +
    "#wa-auth-bar strong{color:var(--text,#1c2420);font-weight:600;overflow-wrap:anywhere}" +
    "#wa-auth-bar button,.wa-login-link{background:none;border:0;padding:0;font:inherit;color:inherit;" +
    "text-decoration:underline;cursor:pointer}" +
    "#wa-auth-bar button[disabled]{opacity:.6;cursor:progress}" +
    "#wa-auth-bar.wa-floating{position:fixed;top:8px;right:8px;z-index:9998;padding:6px 12px;border-radius:999px;" +
    "background:var(--surface,#fff);border:1px solid var(--border,#dde2dc)}" +
    ".wa-login-link{margin:0 4px}" +
    "#wa-auth-banner{display:flex;gap:12px;align-items:center;flex-wrap:wrap;padding:10px 16px;" +
    "background:var(--err-bg,#fdecea);color:var(--err-text,#7a1f16);border-bottom:1px solid var(--poor,#b3382c)}" +
    "#wa-auth-banner[hidden]{display:none}";
  document.head.appendChild(style);

  // ---- remove the saved inputs on logout ------------------------------------------------------
  // app.js keeps the typed finance numbers in this browser (localStorage, keys starting "wa:")
  // so a refresh never loses them. On Log out they are removed, otherwise the next person who
  // logs in on this browser would see the previous person's numbers pre-filled.
  // Only the app's own "wa:" keys are removed; anything else stored for this site is left alone.
  // (To keep inputs between logins on your own private computer, delete the call below.)
  function forgetSavedInputs() {
    try {
      var storage = window.localStorage;
      for (var i = storage.length - 1; i >= 0; i--) {
        var key = storage.key(i);
        if (key && key.indexOf("wa:") === 0) { storage.removeItem(key); }
      }
    } catch (err) { /* storage blocked: nothing to clear */ }
  }

  // ---- "Signed in as" bar -------------------------------------------------------------------
  function showBar(username) {
    if (bar) { bar.remove(); }
    bar = document.createElement("div");
    bar.id = "wa-auth-bar";
    var label = document.createElement("span");
    label.appendChild(document.createTextNode("Signed in as "));
    var name = document.createElement("strong");
    name.textContent = username;
    label.appendChild(name);
    var button = document.createElement("button");
    button.type = "button";
    button.textContent = "Log out";
    button.addEventListener("click", function () {
      button.disabled = true;
      var done = function () { forgetSavedInputs(); window.location.href = "/login"; };
      nativeFetch("/api/logout", { method: "POST", credentials: "same-origin" }).then(done, done);
    });
    bar.appendChild(label);
    bar.appendChild(button);
    var holder = document.querySelector(".site-header .wrap") || document.querySelector("header");
    if (holder) {
      holder.insertBefore(bar, holder.firstChild);
    } else {                      // an unfamiliar page: float it in the corner instead
      bar.className = "wa-floating";
      document.body.appendChild(bar);
    }
  }

  // ---- "session ended" help -------------------------------------------------------------------
  function makeLoginLink() {
    var link = document.createElement("a");
    link.className = "wa-login-link";
    link.href = "/login?popup=1";
    link.target = "_blank";
    link.rel = "noopener";
    link.textContent = "Log in again (opens in a new tab)";
    return link;
  }

  function appBannerVisible() {
    var appBanner = document.getElementById("banner");
    return !!appBanner && !appBanner.hidden;
  }

  function showOwnBanner() {
    if (ownBanner) { return; }
    ownBanner = document.createElement("div");
    ownBanner.id = "wa-auth-banner";
    ownBanner.setAttribute("role", "alert");
    ownBanner.appendChild(document.createTextNode("Your session has ended. What you typed on this page is kept."));
    ownBanner.appendChild(makeLoginLink());
    document.body.insertBefore(ownBanner, document.body.firstChild);
  }

  function hideOwnBanner() {
    if (ownBanner) { ownBanner.remove(); ownBanner = null; }
  }

  function onSessionEnded() {
    var appBanner = document.getElementById("banner");
    if (appBanner && !appBanner.querySelector(".wa-login-link")) {
      appBanner.insertBefore(makeLoginLink(), document.getElementById("banner-retry"));
    }
    // app.js shows its banner a moment after the 401 arrives; if it did not, show ours.
    setTimeout(function () { if (!appBannerVisible()) { showOwnBanner(); } }, 400);
  }

  function onRequestSucceeded() {
    var appBanner = document.getElementById("banner");
    var link = appBanner && appBanner.querySelector(".wa-login-link");
    if (link) { link.remove(); }
    hideOwnBanner();
  }

  function isTrackedApiCall(input) {
    var url = typeof input === "string" ? input : (input && input.url) || "";
    var path;
    try { path = new URL(url, window.location.href).pathname; } catch (err) { return false; }
    return path.indexOf("/api/") === 0 && SKIP.indexOf(path) === -1;
  }

  window.fetch = function (input, init) {
    return nativeFetch(input, init).then(function (response) {
      if (isTrackedApiCall(input)) {
        if (response.status === 401) { onSessionEnded(); }
        else if (response.ok) { onRequestSucceeded(); }
      }
      return response;
    });
  };

  function start() {
    nativeFetch("/api/me", { credentials: "same-origin" })
      .then(function (response) {
        if (response.status === 401) { showOwnBanner(); return null; }
        return response.json();
      })
      .then(function (data) { if (data && data.ok) { showBar(data.username); } })
      .catch(function () { /* offline: the app's own error handling covers it */ });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", start);
  } else {
    start();
  }
})();
