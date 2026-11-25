// Small progressive enhancements. Everything works without JavaScript except
// the confirmation prompts and bar widths.
document.addEventListener("DOMContentLoaded", function () {
  document.querySelectorAll("form[data-confirm]").forEach(function (form) {
    form.addEventListener("submit", function (e) {
      if (!window.confirm(form.getAttribute("data-confirm"))) e.preventDefault();
    });
  });

  document.querySelectorAll("select[data-autosubmit]").forEach(function (sel) {
    sel.addEventListener("change", function () { sel.form.submit(); });
  });

  document.querySelectorAll("[data-select-on-focus]").forEach(function (el) {
    el.addEventListener("focus", function () { el.select(); });
  });

  document.querySelectorAll(".bar-fill[data-pct]").forEach(function (el) {
    var pct = parseInt(el.getAttribute("data-pct"), 10);
    if (!isNaN(pct)) el.style.width = Math.max(0, Math.min(100, pct)) + "%";
  });

  document.querySelectorAll("[data-copy-target]").forEach(function (btn) {
    btn.addEventListener("click", function () {
      var el = document.getElementById(btn.getAttribute("data-copy-target"));
      if (!el) return;
      el.select();
      var done = function () { btn.textContent = "Copied ✓"; };
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(el.value).then(done, function () { document.execCommand("copy"); done(); });
      } else {
        document.execCommand("copy");
        done();
      }
    });
  });

  var toggle = document.querySelector(".nav-toggle");
  var nav = document.getElementById("mainnav");
  if (toggle && nav) {
    toggle.addEventListener("click", function () {
      var open = nav.classList.toggle("open");
      toggle.setAttribute("aria-expanded", open ? "true" : "false");
    });
  }
});
