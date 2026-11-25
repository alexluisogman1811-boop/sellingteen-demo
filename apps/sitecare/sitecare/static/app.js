// SiteCare Desk - small progressive enhancements. Every page works without JavaScript.
(function () {
  "use strict";

  // Mobile navigation toggle
  var toggle = document.querySelector(".nav-toggle");
  var nav = document.getElementById("main-nav");
  if (toggle && nav) {
    toggle.addEventListener("click", function () {
      var open = nav.classList.toggle("open");
      toggle.setAttribute("aria-expanded", open ? "true" : "false");
    });
  }

  // Confirm destructive actions: <form data-confirm="...">
  document.querySelectorAll("form[data-confirm]").forEach(function (form) {
    form.addEventListener("submit", function (event) {
      if (!window.confirm(form.getAttribute("data-confirm"))) {
        event.preventDefault();
      }
    });
  });

  // Prevent double submission (which used to create duplicate records)
  var postForms = document.querySelectorAll('form[method="post"]');
  postForms.forEach(function (form) {
    form.addEventListener("submit", function (event) {
      if (event.defaultPrevented) return;
      if (form.dataset.submitting === "1") { event.preventDefault(); return; }
      form.dataset.submitting = "1";
      // Disable after the browser has captured the submitter's value.
      setTimeout(function () {
        form.querySelectorAll('button[type="submit"]').forEach(function (b) { b.disabled = true; });
      }, 0);
    });
  });
  window.addEventListener("pageshow", function () {  // re-enable after Back navigation
    postForms.forEach(function (form) {
      form.dataset.submitting = "";
      form.querySelectorAll('button[type="submit"]').forEach(function (b) { b.disabled = false; });
    });
  });

  // Show a field only when another select has a given value: data-show-when="name=value1,value2"
  document.querySelectorAll("[data-show-when]").forEach(function (field) {
    var rule = field.getAttribute("data-show-when").split("=");
    var source = document.querySelector('[name="' + rule[0] + '"]');
    var values = rule[1].split(",");
    if (!source) return;
    var update = function () {
      field.classList.toggle("is-hidden", values.indexOf(source.value) === -1);
    };
    source.addEventListener("change", update);
    update();
  });

  // Pre-fill an empty task title from the chosen category
  var category = document.querySelector('select[name="category"]');
  var title = document.querySelector('input[name="title"]');
  if (category && title && document.querySelector('[name="frequency"]')) {
    var lastAuto = title.value;
    category.addEventListener("change", function () {
      var label = category.options[category.selectedIndex].text;
      if ((title.value === "" || title.value === lastAuto) && category.value && category.value !== "other") {
        title.value = label;
        lastAuto = label;
      }
    });
  }

  // Copy report summary to the clipboard
  document.querySelectorAll("[data-copy-target]").forEach(function (button) {
    button.addEventListener("click", function () {
      var source = document.querySelector(button.getAttribute("data-copy-target"));
      var status = document.querySelector(".copy-status");
      if (!source) return;
      var text = source.value || source.textContent;
      var done = function (ok) {
        if (status) status.textContent = ok ? "Summary copied to clipboard." : "Copy failed - select the text and copy manually.";
      };
      if (navigator.clipboard && window.isSecureContext) {
        navigator.clipboard.writeText(text).then(function () { done(true); }, function () { fallback(); });
      } else {
        fallback();
      }
      function fallback() {
        source.focus();
        source.select();
        var ok = false;
        try { ok = document.execCommand("copy"); } catch (e) { ok = false; }
        done(ok);
      }
    });
  });

  // Print button on the printable report
  document.querySelectorAll("[data-print]").forEach(function (button) {
    button.addEventListener("click", function () { window.print(); });
  });
})();
