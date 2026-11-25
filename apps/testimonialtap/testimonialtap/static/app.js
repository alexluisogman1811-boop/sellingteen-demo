// TestimonialTap progressive enhancements. Everything works without JS except copy buttons.
(function () {
  "use strict";

  function copyText(text, button) {
    function done() {
      var original = button.textContent;
      button.textContent = "Copied!";
      button.classList.add("copied");
      setTimeout(function () { button.textContent = original; button.classList.remove("copied"); }, 1600);
    }
    if (navigator.clipboard && window.isSecureContext) {
      navigator.clipboard.writeText(text).then(done, function () { fallback(text); done(); });
    } else {
      fallback(text);
      done();
    }
  }

  function fallback(text) {
    var ta = document.createElement("textarea");
    ta.value = text;
    ta.setAttribute("readonly", "");
    ta.style.position = "fixed";
    ta.style.opacity = "0";
    document.body.appendChild(ta);
    ta.select();
    try { document.execCommand("copy"); } catch (e) { /* ignore */ }
    document.body.removeChild(ta);
  }

  document.addEventListener("click", function (e) {
    var btn = e.target.closest("[data-copy-target], [data-copy-text]");
    if (!btn) return;
    e.preventDefault();
    var text = btn.getAttribute("data-copy-text");
    if (text === null) {
      var el = document.getElementById(btn.getAttribute("data-copy-target"));
      if (!el) return;
      text = "value" in el ? el.value : el.textContent;
    }
    copyText(text, btn);
  });

  // Confirmation prompts for destructive forms.
  document.addEventListener("submit", function (e) {
    var form = e.target;
    var msg = form.getAttribute("data-confirm");
    if (form.id === "bulk-form") {
      var action = form.querySelector("select[name=action]").value;
      var count = form.querySelectorAll("input[name=ids]:checked").length;
      if (count === 0) { e.preventDefault(); alert("Select at least one testimonial first."); return; }
      if (action === "delete") msg = "Permanently delete " + count + " testimonial(s)? This cannot be undone.";
    }
    if (msg && !window.confirm(msg)) { e.preventDefault(); return; }
    // Prevent double submissions (the server also ignores repeats of the same form).
    if (form.hasAttribute("data-submit-once")) {
      var btn = form.querySelector("button[type=submit]");
      if (btn) { btn.disabled = true; btn.textContent = "Sending…"; }
    }
  });

  // Re-enable buttons when the page is restored from the back/forward cache.
  window.addEventListener("pageshow", function (e) {
    if (!e.persisted) return;
    document.querySelectorAll("form[data-submit-once] button[type=submit]").forEach(function (btn) {
      btn.disabled = false;
      btn.textContent = "Send feedback";
    });
  });

  // Select-all checkbox.
  document.addEventListener("change", function (e) {
    var name = e.target.getAttribute("data-select-all");
    if (!name) return;
    var form = e.target.form || document;
    form.querySelectorAll("input[name='" + name + "']").forEach(function (cb) { cb.checked = e.target.checked; });
  });

  // Character counter for textareas.
  document.querySelectorAll("textarea[data-counter]").forEach(function (ta) {
    var out = document.getElementById(ta.getAttribute("data-counter"));
    if (!out) return;
    var max = ta.getAttribute("maxlength");
    var min = ta.getAttribute("minlength");
    function update() {
      var n = ta.value.trim().length;
      out.textContent = n + " / " + max + " characters" + (min && n < +min ? " (minimum " + min + ")" : "");
    }
    ta.addEventListener("input", update);
    if (ta.value) update();
  });
})();
