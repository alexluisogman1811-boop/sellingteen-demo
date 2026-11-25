// Small progressive enhancements. Everything works without JS; no inline JS (strict CSP).
(function () {
  // Confirm destructive actions.
  document.addEventListener("submit", function (e) {
    var form = e.target;
    var msg = form.getAttribute("data-confirm");
    if (msg && !window.confirm(msg)) { e.preventDefault(); return; }
    // Loading state: disable the submit button so a double-click can't submit twice.
    if (form.hasAttribute("data-loading")) {
      if (form.dataset.submitting) { e.preventDefault(); return; }
      form.dataset.submitting = "1";
      var btn = e.submitter || form.querySelector("[type=submit]");
      if (btn) {
        btn.dataset.originalText = btn.textContent;
        if (btn.dataset.loadingText) btn.textContent = btn.dataset.loadingText;
        btn.classList.add("is-loading");
        btn.setAttribute("aria-busy", "true");
        // Disable after the browser has captured the submitter's value.
        setTimeout(function () { btn.disabled = true; }, 0);
      }
    }
  });

  // Back/forward cache can restore a page with buttons still "loading".
  window.addEventListener("pageshow", function () {
    document.querySelectorAll("form[data-submitting]").forEach(function (form) {
      delete form.dataset.submitting;
      form.querySelectorAll(".is-loading").forEach(function (btn) {
        btn.disabled = false;
        btn.classList.remove("is-loading");
        btn.removeAttribute("aria-busy");
        if (btn.dataset.originalText) btn.textContent = btn.dataset.originalText;
      });
    });
  });

  // Copy a textarea's text to the clipboard, with a fallback for blocked clipboard access.
  document.addEventListener("click", function (e) {
    var btn = e.target.closest("[data-copy]");
    if (!btn) return;
    var field = document.querySelector(btn.getAttribute("data-copy"));
    if (!field) return;
    var original = btn.dataset.label || btn.textContent;
    btn.dataset.label = original;
    var done = function (ok) {
      btn.textContent = ok ? "✓ Copied" : "Press Ctrl+C / ⌘C to copy";
      setTimeout(function () { btn.textContent = original; }, 2500);
    };
    var legacy = function () {
      field.focus({ preventScroll: true });  // don't jump the page away from the button
      field.select();
      try { done(document.execCommand("copy")); } catch (err) { done(false); }
    };
    if (navigator.clipboard && window.isSecureContext) {
      navigator.clipboard.writeText(field.value).then(function () { done(true); }, legacy);
    } else {
      legacy();
    }
  });

  // Keep the "Open in email app" link in sync with any edits to the message.
  document.addEventListener("click", function (e) {
    var link = e.target.closest("[data-mailto-body]");
    if (!link) return;
    var field = document.querySelector(link.getAttribute("data-mailto-body"));
    if (!field) return;
    var href = link.getAttribute("href").replace(/([?&])body=[^&]*/, "$1body=" + encodeURIComponent(field.value));
    link.setAttribute("href", href);
  });

  // Fill the signup form's timezone from the browser, so "today" is the user's today.
  document.querySelectorAll("[data-browser-timezone]").forEach(function (input) {
    try {
      if (!input.value) input.value = Intl.DateTimeFormat().resolvedOptions().timeZone || "";
    } catch (err) { /* server falls back to UTC */ }
  });

  // Due-date shortcuts: issue date + N days.
  document.addEventListener("click", function (e) {
    var b = e.target.closest("[data-due-shortcuts] [data-days]");
    if (!b) return;
    var issue = document.getElementById("issue_date");
    var due = document.getElementById("due_date");
    if (!issue || !due || !issue.value) return;
    var d = new Date(issue.value + "T00:00:00Z");
    d.setUTCDate(d.getUTCDate() + parseInt(b.dataset.days, 10));
    due.value = d.toISOString().slice(0, 10);
  });

  // Filter selects apply immediately.
  document.addEventListener("change", function (e) {
    if (e.target.matches("select[data-autosubmit]")) e.target.form.submit();
  });
})();
