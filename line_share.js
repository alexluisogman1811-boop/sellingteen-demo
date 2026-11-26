// sellingteen: adds a green "Share on LINE" button next to every Copy button in the apps.
// One tap opens LINE (app on phones, LINE website on computers) with the message already typed.
// Nothing is sent by itself: the owner still picks the chat and presses Send in LINE.
(function () {
  "use strict";
  var LINE_URL = "https://line.me/R/share?text=";

  function textOfToken(token) {
    token = (token || "").trim();
    if (!token) return "";
    var el = null;
    try {
      el = /^[#.\[]/.test(token) ? document.querySelector(token) : document.getElementById(token);
    } catch (e) { el = null; }
    if (!el) return "";
    var v = ("value" in el && typeof el.value === "string") ? el.value : el.textContent;
    return (v || "").trim();
  }

  function textFor(btn) {
    if (btn.hasAttribute("data-copy-text")) return (btn.getAttribute("data-copy-text") || "").trim();
    var ref = btn.getAttribute("data-copy") || btn.getAttribute("data-copy-target");
    if (ref) {
      return ref.split(",").map(textOfToken).filter(Boolean).join("\n\n");
    }
    // Copy buttons without a reference: use the nearest message box.
    var box = btn.closest("form, section, article, .card, .panel, div");
    while (box) {
      var field = box.querySelector("textarea, input[readonly]");
      if (field) return (field.value || "").trim();
      box = box.parentElement ? box.parentElement.closest("form, section, article, .card, .panel, div") : null;
    }
    return "";
  }

  function isCopyButton(btn) {
    if (btn.hasAttribute("data-copy") || btn.hasAttribute("data-copy-target") || btn.hasAttribute("data-copy-text")) return true;
    return /^\s*copy\b/i.test(btn.textContent || "");
  }

  function addButtons(root) {
    var buttons = (root || document).querySelectorAll("button, a.btn, a.button");
    Array.prototype.forEach.call(buttons, function (btn) {
      if (btn.getAttribute("data-line-added") || btn.classList.contains("st-line-share") || !isCopyButton(btn)) return;
      btn.setAttribute("data-line-added", "1");
      var line = document.createElement("button");
      line.type = "button";
      // Same look as the app's own button, minus any "copy" classes the app's script listens to.
      line.className = (btn.className || "").split(/\s+/).filter(function (c) { return c && !/copy/i.test(c); }).join(" ") + " st-line-share";
      line.textContent = "Share on LINE";
      line.setAttribute("aria-label", "Open LINE with this text ready to send");
      line.style.background = "#06C755";
      line.style.borderColor = "#06C755";
      line.style.color = "#ffffff";
      line.style.marginLeft = "8px";
      line.style.marginTop = "4px";
      line.addEventListener("click", function () {
        var text = textFor(btn);
        if (!text) { line.textContent = "Nothing to share yet"; setTimeout(function () { line.textContent = "Share on LINE"; }, 2000); return; }
        window.open(LINE_URL + encodeURIComponent(text), "_blank", "noopener");
      });
      btn.insertAdjacentElement("afterend", line);
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", function () { addButtons(); });
  } else {
    addButtons();
  }
})();
