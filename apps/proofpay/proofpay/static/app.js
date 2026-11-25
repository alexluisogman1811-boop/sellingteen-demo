// Small progressive enhancements. Every feature works without JavaScript.
document.addEventListener("DOMContentLoaded", function () {
  // Confirm destructive or important actions.
  document.querySelectorAll("form[data-confirm]").forEach(function (form) {
    form.addEventListener("submit", function (e) {
      if (!window.confirm(form.getAttribute("data-confirm"))) e.preventDefault();
    });
  });

  // Copy the customer link.
  document.querySelectorAll("[data-copy]").forEach(function (btn) {
    btn.addEventListener("click", function () {
      var input = document.querySelector(btn.getAttribute("data-copy"));
      if (!input) return;
      var flashLabel = function (text) {
        var old = btn.getAttribute("data-label") || btn.textContent;
        btn.setAttribute("data-label", old);
        btn.textContent = text;
        setTimeout(function () { btn.textContent = old; }, 1800);
      };
      var fallback = function () {
        input.focus();
        input.select();
        var ok = false;
        try { ok = document.execCommand("copy"); } catch (e) { ok = false; }
        flashLabel(ok ? "Copied!" : "Press Ctrl+C");
      };
      if (navigator.clipboard && window.isSecureContext) {
        navigator.clipboard.writeText(input.value).then(function () { flashLabel("Copied!"); }, fallback);
      } else {
        fallback();
      }
    });
  });

  // On "New job": hide the new-customer fields when an existing customer is picked.
  var picker = document.querySelector("[data-toggle-new-customer]");
  var fields = document.getElementById("new-customer-fields");
  if (picker && fields) {
    var sync = function () { fields.hidden = picker.value !== ""; };
    picker.addEventListener("change", sync);
    sync();
  }
});
