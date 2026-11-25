// LicenseLedger progressive enhancements. Every page works without JavaScript.
(function () {
  "use strict";

  // Copy buttons: <button data-copy="#selector">
  document.addEventListener("click", function (event) {
    var button = event.target.closest("[data-copy]");
    if (!button) return;
    var target = document.querySelector(button.getAttribute("data-copy"));
    if (!target) return;
    var text = "value" in target && target.tagName !== "CODE" ? target.value : target.textContent;
    var done = function () {
      var label = button.textContent;
      button.classList.add("copied");
      button.textContent = "Copied";
      setTimeout(function () { button.classList.remove("copied"); button.textContent = label; }, 1500);
    };
    if (navigator.clipboard && window.isSecureContext) {
      navigator.clipboard.writeText(text.trim()).then(done, function () { selectText(target); });
    } else {
      selectText(target);
      try { document.execCommand("copy"); done(); } catch (e) { /* user can copy manually */ }
    }
  });

  function selectText(el) {
    if (el.select) { el.focus(); el.select(); return; }
    var range = document.createRange();
    range.selectNodeContents(el);
    var sel = window.getSelection();
    sel.removeAllRanges();
    sel.addRange(range);
  }

  // Confirmation for destructive forms: <form data-confirm="...">
  // One-shot forms: <form data-once> disables its buttons after submitting so a
  // double click can't send the action twice (the server also rejects repeats).
  document.addEventListener("submit", function (event) {
    var form = event.target;
    var message = form.getAttribute && form.getAttribute("data-confirm");
    if (message && !window.confirm(message)) { event.preventDefault(); return; }
    if (form.hasAttribute && form.hasAttribute("data-once")) {
      if (form.dataset.sent) { event.preventDefault(); return; }
      form.dataset.sent = "1";
      setTimeout(function () {
        form.querySelectorAll("button[type=submit], button:not([type])").forEach(function (b) { b.disabled = true; });
      }, 0);
    }
  });

  // Auto-submit selects: <select data-autosubmit>
  document.querySelectorAll("select[data-autosubmit]").forEach(function (select) {
    select.addEventListener("change", function () { select.form.submit(); });
  });

  // License form: show fields relevant to the chosen type and key mode.
  var form = document.getElementById("license-form");
  if (form) {
    var typeSelect = form.querySelector("[name=license_type]");
    var productSelect = form.querySelector("[name=product_id]");
    var customerSelect = form.querySelector("[name=customer_id]");
    var orderSelect = form.querySelector("[name=order_id]");

    var effectiveType = function () {
      if (typeSelect.value) return typeSelect.value;
      var opt = productSelect.options[productSelect.selectedIndex];
      return (opt && opt.getAttribute("data-type")) || "fixed";
    };
    var syncType = function () {
      var type = effectiveType();
      form.querySelectorAll("[data-show-for]").forEach(function (el) {
        var show = el.getAttribute("data-show-for").split(" ").indexOf(type) !== -1;
        el.hidden = !show;
      });
      var term = form.querySelector("[name=term_days]");
      var opt = productSelect.options[productSelect.selectedIndex];
      if (term && opt) term.placeholder = opt.getAttribute("data-term") ? "Product default: " + opt.getAttribute("data-term") : "e.g. 365";
    };
    typeSelect.addEventListener("change", syncType);
    productSelect.addEventListener("change", syncType);
    syncType();

    // Picking an order selects its customer.
    if (orderSelect && customerSelect) {
      orderSelect.addEventListener("change", function () {
        var opt = orderSelect.options[orderSelect.selectedIndex];
        var cust = opt && opt.getAttribute("data-customer");
        if (cust) customerSelect.value = cust;
      });
    }

    var existing = form.querySelector("[data-key-existing]");
    if (existing) {
      var syncKey = function () {
        var checked = form.querySelector("[name=key_mode]:checked");
        existing.hidden = !checked || checked.value !== "existing";
      };
      form.querySelectorAll("[name=key_mode]").forEach(function (r) { r.addEventListener("change", syncKey); });
      syncKey();
    }
  }
})();
