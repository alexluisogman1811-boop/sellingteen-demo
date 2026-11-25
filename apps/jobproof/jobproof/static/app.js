// Small progressive enhancements. Every feature also works without JavaScript.
(function () {
  "use strict";

  // Confirm destructive actions: <form data-confirm="...">
  document.querySelectorAll("form[data-confirm]").forEach(function (form) {
    form.addEventListener("submit", function (e) {
      if (!window.confirm(form.getAttribute("data-confirm"))) e.preventDefault();
    });
  });

  // Prevent double submits on slow actions: <form data-busy="Generating…">
  document.querySelectorAll("form[data-busy]").forEach(function (form) {
    form.addEventListener("submit", function () {
      var btn = form.querySelector("button[type=submit]");
      if (!btn) return;
      if (!btn.hasAttribute("data-label")) btn.setAttribute("data-label", btn.textContent);
      setTimeout(function () {
        btn.disabled = true;
        btn.setAttribute("aria-busy", "true");
        btn.textContent = form.getAttribute("data-busy");
      }, 0);
    });
  });

  // Coming back with the Back button can restore a page with disabled "busy" buttons; re-enable them.
  window.addEventListener("pageshow", function (e) {
    if (!e.persisted) return;
    document.querySelectorAll("button[aria-busy=true]").forEach(function (btn) {
      btn.disabled = false;
      btn.removeAttribute("aria-busy");
      btn.textContent = btn.getAttribute("data-label") || btn.textContent;
    });
  });

  // Auto-submit photo stage selects
  document.querySelectorAll("select[data-autosubmit]").forEach(function (sel) {
    sel.addEventListener("change", function () { sel.form.submit(); });
  });

  // Copy share link
  document.querySelectorAll("[data-copy]").forEach(function (btn) {
    btn.addEventListener("click", function () {
      var input = document.querySelector(btn.getAttribute("data-copy"));
      if (!input) return;
      var done = function () {
        var old = btn.textContent;
        btn.textContent = "Copied!";
        setTimeout(function () { btn.textContent = old; }, 1800);
      };
      if (navigator.clipboard && window.isSecureContext) {
        navigator.clipboard.writeText(input.value).then(done, function () { input.select(); document.execCommand("copy"); done(); });
      } else {
        input.select();
        document.execCommand("copy");
        done();
      }
    });
  });

  // Preview photos before upload + client-side size/type hints (server re-validates everything)
  var input = document.getElementById("photo-input");
  var preview = document.getElementById("upload-preview");
  if (input && preview) {
    var MAX = 10 * 1024 * 1024;
    var OK = ["image/jpeg", "image/png", "image/webp"];
    input.addEventListener("change", function () {
      preview.innerHTML = "";
      Array.prototype.forEach.call(input.files, function (file) {
        var fig = document.createElement("figure");
        var cap = document.createElement("figcaption");
        cap.textContent = file.name;
        if (OK.indexOf(file.type) === -1) {
          cap.textContent = file.name + " – not a supported image";
          cap.className = "bad";
        } else if (file.size > MAX) {
          cap.textContent = file.name + " – over 10 MB";
          cap.className = "bad";
        } else {
          var img = document.createElement("img");
          img.alt = "";
          img.src = URL.createObjectURL(file);
          fig.appendChild(img);
        }
        fig.appendChild(cap);
        preview.appendChild(fig);
      });
    });
  }
})();
