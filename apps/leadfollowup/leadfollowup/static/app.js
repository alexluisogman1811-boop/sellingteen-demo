/* LeadFollowUp AI - small progressive enhancements. No framework needed. */
(function () {
  "use strict";

  var toastTimer = null;

  function showToast(message, kind) {
    var el = document.getElementById("toast");
    if (!el) return;
    el.textContent = message;
    el.className = "toast toast-" + (kind || "success");
    el.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(function () { el.hidden = true; }, 2800);
  }

  function fallbackCopy(text) {
    var ta = document.createElement("textarea");
    ta.value = text;
    ta.setAttribute("readonly", "");
    ta.style.position = "fixed";
    ta.style.top = "-1000px";
    document.body.appendChild(ta);
    ta.select();
    var ok = false;
    try { ok = document.execCommand("copy"); } catch (e) { ok = false; }
    document.body.removeChild(ta);
    return ok;
  }

  /** Copy text to the clipboard. Resolves true on success, false otherwise. */
  function copyText(text) {
    if (!text) return Promise.resolve(false);
    if (navigator.clipboard && window.isSecureContext) {
      return navigator.clipboard.writeText(text).then(
        function () { return true; },
        function () { return fallbackCopy(text); }
      );
    }
    return Promise.resolve(fallbackCopy(text));
  }

  // After copying a follow-up, fill in the "record contact" form so saving is one click.
  var SOURCE_METHOD = { LINE: "LINE", WhatsApp: "WhatsApp", Facebook: "Facebook", Website: "Email", Phone: "SMS" };

  function prefillContact() {
    var body = document.getElementById("act-body");
    var method = document.getElementById("act-method");
    var card = document.querySelector("[data-source]");
    var hint = document.getElementById("after-copy");
    if (!body || !card) return;
    if (!body.value.trim()) {
      body.value = "Sent a follow-up message.";
      var m = SOURCE_METHOD[card.dataset.source];
      if (m && method) {
        method.value = m;
        method.dispatchEvent(new Event("change"));
      }
    }
    if (hint) hint.hidden = false;
  }

  // Links like "Record contact" jump to a section and put the cursor where it's needed.
  function initFocusLinks() {
    document.addEventListener("click", function (ev) {
      var link = ev.target.closest("a[data-focus]");
      if (!link) return;
      var section = document.querySelector(link.getAttribute("href"));
      var field = document.getElementById(link.dataset.focus);
      if (!section || !field) return;
      ev.preventDefault();
      section.scrollIntoView({ block: "start" });
      field.focus({ preventScroll: true });
    });
  }

  function csrfToken() {
    var m = document.querySelector('meta[name="csrf-token"]');
    return m ? m.getAttribute("content") : "";
  }

  function setLoading(btn, loading, text) {
    if (!btn) return;
    if (loading) {
      btn.dataset.originalText = btn.textContent;
      btn.textContent = text || "Loading…";
      btn.disabled = true;
      btn.setAttribute("aria-busy", "true");
    } else {
      btn.textContent = btn.dataset.originalText || btn.textContent;
      btn.disabled = false;
      btn.removeAttribute("aria-busy");
    }
  }

  function initGenerate() {
    var card = document.querySelector("[data-generate-url]");
    if (!card) return;
    var btn = document.getElementById("generate-btn");
    var out = document.getElementById("ai-output");
    var msg = document.getElementById("ai-message");
    var err = document.getElementById("ai-error");
    var notice = document.getElementById("ai-notice");

    var lastGenerated = "";

    btn.addEventListener("click", function () {
      if (msg.value.trim() && msg.value !== lastGenerated &&
          !window.confirm("Replace your edited message with a new one?")) {
        return;
      }
      var picked = card.querySelector('input[name="style"]:checked');
      var style = picked ? picked.value : "friendly";
      err.hidden = true;
      notice.hidden = true;
      setLoading(btn, true, "Generating…");
      fetch(card.dataset.generateUrl, {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json", "Accept": "application/json", "X-CSRF-Token": csrfToken() },
        body: JSON.stringify({ style: style })
      })
        .then(function (res) {
          if (res.status === 401) throw new Error("Your session has expired. Please reload the page and log in again.");
          return res.json().catch(function () { return {}; }).then(function (data) {
            if (!res.ok) throw new Error(data.error || "Couldn't generate a message. Please try again.");
            return data;
          });
        })
        .then(function (data) {
          msg.value = data.message;
          lastGenerated = data.message;
          out.hidden = false;
          if (data.notice) { notice.textContent = data.notice; notice.hidden = false; }
          btn.dataset.originalText = "Regenerate";
          msg.focus();
        })
        .catch(function (e) {
          err.textContent = (e && e.message && e.message !== "Failed to fetch")
            ? e.message : "Couldn't reach the server. Check your connection and try again.";
          err.hidden = false;
        })
        .finally(function () { setLoading(btn, false); });
    });

    // "Generate follow-up" in the action bar: stay on the page, no reload.
    document.querySelectorAll("[data-compose]").forEach(function (link) {
      link.addEventListener("click", function (ev) {
        ev.preventDefault();
        card.scrollIntoView({ block: "start" });
        btn.click();
      });
    });

    // Arriving from a dashboard "Write message" button: generate straight away.
    if (new URLSearchParams(window.location.search).get("compose") === "1") {
      card.scrollIntoView({ block: "start" });
      btn.click();
    }
  }

  function initCopy() {
    document.addEventListener("click", function (ev) {
      var btn = ev.target.closest("[data-copy-target]");
      if (!btn) return;
      var target = document.getElementById(btn.dataset.copyTarget);
      var text = target ? (target.value || target.textContent || "").trim() : "";
      if (!text) { showToast("Nothing to copy yet.", "error"); return; }
      copyText(text).then(function (ok) {
        if (ok) {
          showToast("Message copied to clipboard");
          if (btn.dataset.copyTarget === "ai-message") prefillContact();
          var original = btn.textContent;
          btn.textContent = "Copied ✓";
          setTimeout(function () { btn.textContent = original; }, 1800);
        } else {
          showToast("Couldn't copy automatically. Select the text and copy it manually.", "error");
        }
      });
    });
  }

  // After a redirect to an anchor (e.g. #history) the flash message at the top
  // is scrolled out of view, so repeat it as a toast.
  function surfaceFlash() {
    var flash = document.querySelector(".flashes .alert");
    if (flash && window.location.hash) {
      showToast(flash.textContent.trim(), flash.classList.contains("alert-error") ? "error" : "success");
    }
  }

  // "Note only" entries never reschedule, so disable the date field for them.
  function initActivityForm() {
    var method = document.getElementById("act-method");
    var next = document.getElementById("act-next");
    if (!method || !next) return;
    var sync = function () { next.disabled = method.value === "Note"; };
    method.addEventListener("change", sync);
    sync();
  }

  function focusFirstError() {
    var bad = document.querySelector('[aria-invalid="true"]');
    if (bad) bad.focus();
  }

  function initForms() {
    // Loading state on submit, and prevent double submits.
    document.addEventListener("submit", function (ev) {
      var btn = ev.submitter;
      if (btn && btn.dataset.loadingText) {
        setTimeout(function () { setLoading(btn, true, btn.dataset.loadingText); }, 0);
      }
    });
    // Filters apply as soon as a dropdown changes.
    document.querySelectorAll("form[data-autosubmit] select").forEach(function (sel) {
      sel.addEventListener("change", function () { sel.form.requestSubmit ? sel.form.requestSubmit() : sel.form.submit(); });
    });
  }

  window.LeadFollowUp = { copyText: copyText, showToast: showToast };

  document.addEventListener("DOMContentLoaded", function () {
    initGenerate();
    initCopy();
    initForms();
    focusFirstError();
    surfaceFlash();
    initActivityForm();
    initFocusLinks();
  });
})();
