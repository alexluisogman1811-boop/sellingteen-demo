// Small progressive enhancements; every feature works without JavaScript.
document.addEventListener("DOMContentLoaded", function () {
  document.querySelectorAll("[data-print]").forEach(function (btn) {
    btn.addEventListener("click", function () { window.print(); });
  });
  document.querySelectorAll("form[data-confirm]").forEach(function (form) {
    form.addEventListener("submit", function (event) {
      if (!window.confirm(form.getAttribute("data-confirm"))) event.preventDefault();
    });
  });
  // Live "after this movement" preview on stock forms (the server re-checks everything).
  document.querySelectorAll("form[data-stock-form]").forEach(function (form) {
    var kind = form.getAttribute("data-kind");
    var current = parseInt(form.getAttribute("data-current"), 10);
    var threshold = parseInt(form.getAttribute("data-threshold"), 10);
    var input = form.querySelector(kind === "adjustment" ? "[name=counted_quantity]" : "[name=quantity]");
    var out = form.querySelector("[data-after-preview]");
    if (!input || !out) return;
    function update() {
      var raw = input.value.trim();
      out.className = "after-preview";
      if (!/^[0-9]+$/.test(raw)) { out.textContent = ""; return; }
      var n = parseInt(raw, 10);
      var after = kind === "in" ? current + n : kind === "out" ? current - n : n;
      if (after < 0) {
        out.textContent = "Not possible: only " + current + " in stock.";
        out.className += " after-error";
        return;
      }
      var change = after - current;
      var status = after === 0 ? "out of stock" : after <= threshold ? "low stock" : "in stock";
      out.textContent = "After this: " + after + " on hand (" + (change > 0 ? "+" : "") + change + ", " + status + ").";
      if (after <= threshold) out.className += " after-warn";
    }
    input.addEventListener("input", update);
    update();
  });

  // A page restored with the Back button keeps disabled buttons; re-enable them.
  window.addEventListener("pageshow", function (event) {
    if (event.persisted) {
      document.querySelectorAll("button[type='submit']").forEach(function (b) { b.disabled = false; });
    }
  });

  // Prevent double submission of stock movements and imports.
  document.querySelectorAll("form[method='post']").forEach(function (form) {
    form.addEventListener("submit", function (event) {
      if (event.defaultPrevented) return;
      form.querySelectorAll("button[type='submit']").forEach(function (b) {
        setTimeout(function () { b.disabled = true; }, 0);
      });
    });
  });
});
