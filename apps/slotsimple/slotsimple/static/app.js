// Progressive enhancement only: every page works without JavaScript.
document.addEventListener("DOMContentLoaded", function () {
  document.querySelectorAll("[data-print]").forEach(function (btn) {
    btn.addEventListener("click", function () { window.print(); });
  });

  document.querySelectorAll("form[data-confirm]").forEach(function (form) {
    form.addEventListener("submit", function (event) {
      if (!window.confirm(form.getAttribute("data-confirm"))) event.preventDefault();
    });
  });

  // Prevent double submission of any POST form. (The server is idempotent for
  // booking forms anyway; this just avoids a confusing second request.)
  document.querySelectorAll("form[method=post]").forEach(function (form) {
    form.addEventListener("submit", function (event) {
      if (event.defaultPrevented) return;
      if (form.dataset.submitted) { event.preventDefault(); return; }
      form.dataset.submitted = "1";
      form.querySelectorAll("button[type=submit]").forEach(function (b) {
        b.setAttribute("aria-busy", "true");
        if (b.dataset.busyLabel) { b.dataset.idleLabel = b.textContent; b.textContent = b.dataset.busyLabel; }
      });
    });
  });
  // Re-enable forms restored from the back/forward cache.
  window.addEventListener("pageshow", function (event) {
    if (!event.persisted) return;
    document.querySelectorAll("form[data-submitted]").forEach(function (form) {
      delete form.dataset.submitted;
      form.querySelectorAll("button[data-idle-label]").forEach(function (b) {
        b.textContent = b.dataset.idleLabel;
        b.removeAttribute("aria-busy");
      });
    });
  });

  // Copy booking reference.
  document.querySelectorAll("[data-copy]").forEach(function (btn) {
    if (!navigator.clipboard) return;
    btn.hidden = false;
    btn.addEventListener("click", function () {
      var target = document.querySelector(btn.getAttribute("data-copy"));
      navigator.clipboard.writeText(target.textContent.trim()).then(function () {
        btn.textContent = "Copied ✓";
        setTimeout(function () { btn.textContent = "Copy reference"; }, 2000);
      });
    });
  });

  // Move focus to the error summary so screen readers and small screens see it.
  var summary = document.querySelector(".error-summary");
  if (summary && !document.querySelector("[autofocus]")) summary.focus();

  // Time inputs on the block form are only relevant when "All day" is unticked.
  var allDay = document.querySelector("input[name=all_day]");
  if (allDay) {
    var toggle = function () {
      allDay.form.querySelectorAll("input[type=time]").forEach(function (i) { i.disabled = allDay.checked; });
    };
    allDay.addEventListener("change", toggle);
    toggle();
  }
});
