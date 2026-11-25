// ContentQueue front-end helpers. No external libraries, no network calls.
(function () {
  "use strict";

  // Mobile navigation toggle
  var toggle = document.querySelector(".nav-toggle");
  var nav = document.getElementById("mainnav");
  if (toggle && nav) {
    toggle.addEventListener("click", function () {
      var open = nav.classList.toggle("open");
      toggle.setAttribute("aria-expanded", open ? "true" : "false");
    });
  }

  // Copy to clipboard: data-copy="id1,id2" joins the text of those elements.
  function fallbackCopy(text) {
    var ta = document.createElement("textarea");
    ta.value = text;
    ta.setAttribute("readonly", "");
    ta.className = "visually-hidden";
    document.body.appendChild(ta);
    ta.select();
    var ok = false;
    try { ok = document.execCommand("copy"); } catch (e) { ok = false; }
    document.body.removeChild(ta);
    return ok ? Promise.resolve() : Promise.reject(new Error("copy failed"));
  }

  function copyText(text) {
    if (navigator.clipboard && window.isSecureContext) {
      return navigator.clipboard.writeText(text).catch(function () { return fallbackCopy(text); });
    }
    return fallbackCopy(text);
  }

  document.querySelectorAll("[data-copy]").forEach(function (btn) {
    btn.addEventListener("click", function () {
      var parts = btn.getAttribute("data-copy").split(",").map(function (id) {
        var el = document.getElementById(id.trim());
        return el ? (el.value !== undefined && el.tagName === "TEXTAREA" ? el.value : el.textContent).trim() : "";
      }).filter(Boolean);
      var label = btn.textContent;
      if (!parts.length) {
        btn.textContent = "Nothing to copy";
        setTimeout(function () { btn.textContent = label; }, 1400);
        return;
      }
      copyText(parts.join("\n\n")).then(function () {
        btn.textContent = "Copied ✓";
        btn.classList.add("copied");
        setTimeout(function () { btn.textContent = label; btn.classList.remove("copied"); }, 1600);
      }, function () {
        // Clipboard blocked (e.g. plain-http access from another device): select the
        // text instead so the user can press Ctrl+C / Cmd+C themselves.
        var first = document.getElementById(btn.getAttribute("data-copy").split(",")[0].trim());
        if (first && window.getSelection) {
          var range = document.createRange();
          range.selectNodeContents(first);
          var sel = window.getSelection();
          sel.removeAllRanges();
          sel.addRange(range);
        }
        btn.textContent = /Mac/.test(navigator.platform) ? "Selected — press ⌘C" : "Selected — press Ctrl+C";
        setTimeout(function () { btn.textContent = label; }, 4000);
      });
    });
  });

  // Caption character counter tied to the selected platform's limit.
  var caption = document.getElementById("caption");
  var platform = document.getElementById("platform");
  if (caption && caption.dataset.counter) {
    var out = document.getElementById(caption.dataset.counter);
    var update = function () {
      var len = Array.from(caption.value).length;
      var opt = platform ? platform.options[platform.selectedIndex] : null;
      var limit = opt && opt.dataset.limit ? parseInt(opt.dataset.limit, 10) : null;
      if (limit) {
        out.textContent = len + " / " + limit + " characters for " + opt.textContent.trim();
        out.classList.toggle("over", len > limit);
        out.classList.toggle("near", len <= limit && len > limit * 0.9);
      } else {
        out.textContent = len + " characters";
        out.classList.remove("over", "near");
      }
    };
    caption.addEventListener("input", update);
    if (platform) platform.addEventListener("change", update);
    update();
  }

  // Confirmation prompts for destructive forms
  document.querySelectorAll("form[data-confirm]").forEach(function (form) {
    form.addEventListener("submit", function (e) {
      if (!window.confirm(form.getAttribute("data-confirm"))) e.preventDefault();
    });
  });

  // Client-side checks on the post form (the server re-validates everything).
  var postForm = document.getElementById("post-form");
  if (postForm) {
    var summary = document.getElementById("form-summary");
    var imageInput = document.getElementById("image");
    var imageError = document.getElementById("image-client-error");
    var allowed = /\.(png|jpe?g|gif|webp)$/i;

    var checkImage = function () {
      if (!imageInput || !imageInput.files || !imageInput.files.length) return null;
      var file = imageInput.files[0];
      var max = parseInt(imageInput.dataset.maxBytes || "0", 10);
      if (!allowed.test(file.name)) {
        return /\.hei[cf]$/i.test(file.name)
          ? "HEIC photos aren't supported — export or share the photo as JPG first."
          : "Only PNG, JPG, GIF or WebP images are allowed.";
      }
      if (max && file.size > max) return "Image is too large (max " + Math.round(max / 1048576) + " MB).";
      return null;
    };

    if (imageInput && imageError) {
      imageInput.addEventListener("change", function () {
        var msg = checkImage();
        imageError.textContent = msg || "";
        imageError.hidden = !msg;
        if (msg) imageInput.value = "";
      });
    }

    postForm.addEventListener("submit", function (e) {
      var problems = [];
      var val = function (id) { var el = document.getElementById(id); return el ? el.value.trim() : ""; };
      if (!val("title")) problems.push("Give the post a working title.");
      var status = val("status");
      var date = val("planned_date");
      if (val("planned_time") && !date) problems.push("Add a date to go with the time.");
      if (status === "scheduled" && !date) problems.push("Scheduled posts need a planned date.");
      var cap = document.getElementById("caption");
      var plat = document.getElementById("platform");
      var opt = plat ? plat.options[plat.selectedIndex] : null;
      var limit = opt && opt.dataset.limit ? parseInt(opt.dataset.limit, 10) : null;
      if (status === "scheduled" && limit && cap && Array.from(cap.value.trim()).length > limit) {
        problems.push("Caption is longer than " + opt.textContent.trim() + " allows (" + limit + "). Shorten it before scheduling.");
      }
      var imgMsg = checkImage();
      if (imgMsg) problems.push(imgMsg);
      if (problems.length) {
        e.preventDefault();
        summary.innerHTML = "";
        var strong = document.createElement("strong");
        strong.textContent = "Please fix the following:";
        var ul = document.createElement("ul");
        problems.forEach(function (p) { var li = document.createElement("li"); li.textContent = p; ul.appendChild(li); });
        summary.appendChild(strong);
        summary.appendChild(ul);
        summary.hidden = false;
        summary.scrollIntoView({ behavior: "smooth", block: "center" });
      }
    });
  }

  // Warn before leaving the post form with unsaved changes.
  if (postForm) {
    var dirty = false;
    var markDirty = function () { dirty = true; };
    postForm.addEventListener("input", markDirty);
    postForm.addEventListener("change", markDirty);
    postForm.addEventListener("submit", function (e) { if (!e.defaultPrevented) dirty = false; });
    window.addEventListener("beforeunload", function (e) {
      if (dirty) { e.preventDefault(); e.returnValue = ""; }
    });
  }

  // Local draft autosave: protects typing against an expired session, a crashed tab or
  // a closed browser. Stored only in this browser; cleared after the server confirms
  // the save (body[data-clear-draft]) and on sign-out.
  var DRAFT_PREFIX = "cq-draft:";
  var DRAFT_FIELDS = ["title", "caption", "hashtags", "notes", "planned_date", "planned_time", "platform", "status"];
  var store = (function () {
    try { var k = "__cq_test"; localStorage.setItem(k, "1"); localStorage.removeItem(k); return localStorage; }
    catch (e) { return null; }
  })();

  var clearKey = document.body.getAttribute("data-clear-draft");
  if (store && clearKey) store.removeItem(DRAFT_PREFIX + clearKey);

  document.querySelectorAll("form.nav-logout").forEach(function (f) {
    f.addEventListener("submit", function () {
      if (!store) return;
      Object.keys(store).forEach(function (k) { if (k.indexOf(DRAFT_PREFIX) === 0) store.removeItem(k); });
    });
  });

  if (store && postForm && postForm.dataset.draftKey) {
    var draftKey = DRAFT_PREFIX + postForm.dataset.draftKey;
    var field = function (name) { return postForm.querySelector("[name='" + name + "']:not([type=hidden])") || postForm.querySelector("[name='" + name + "']"); };
    var snapshot = function () {
      var data = {};
      DRAFT_FIELDS.forEach(function (n) { var el = field(n); if (el) data[n] = el.value; });
      return data;
    };
    var same = function (a, b) { return DRAFT_FIELDS.every(function (n) { return (a[n] || "") === (b[n] || ""); }); };
    var initial = snapshot();

    var saved = null;
    try { saved = JSON.parse(store.getItem(draftKey) || "null"); } catch (e) { saved = null; }
    var banner = document.getElementById("draft-restore");
    if (saved && saved.fields && !same(saved.fields, initial) && banner) {
      var when = saved.savedAt ? new Date(saved.savedAt).toLocaleString() : "earlier";
      document.getElementById("draft-restore-text").textContent =
        "Unsaved text from " + when + " was found in this browser.";
      banner.hidden = false;
      document.getElementById("draft-restore-yes").addEventListener("click", function () {
        DRAFT_FIELDS.forEach(function (n) {
          var el = field(n);
          if (el && !el.readOnly && !el.disabled && saved.fields[n] !== undefined) el.value = saved.fields[n];
        });
        banner.hidden = true;
        postForm.dispatchEvent(new Event("input", { bubbles: true }));
        var cap = document.getElementById("caption");
        if (cap) cap.dispatchEvent(new Event("input", { bubbles: true }));
      });
      document.getElementById("draft-restore-no").addEventListener("click", function () {
        store.removeItem(draftKey);
        banner.hidden = true;
      });
    }

    var timer = null;
    var persist = function () {
      var current = snapshot();
      if (same(current, initial)) return;
      try { store.setItem(draftKey, JSON.stringify({ fields: current, savedAt: Date.now() })); } catch (e) { /* quota */ }
    };
    postForm.addEventListener("input", function () { clearTimeout(timer); timer = setTimeout(persist, 400); });
    postForm.addEventListener("change", persist);
  }

  // Prevent double submissions (double clicks create duplicate records).
  // Runs after per-form handlers (bubbling to document), so cancelled submits are ignored.
  document.addEventListener("submit", function (e) {
    var form = e.target;
    if (e.defaultPrevented || (form.method || "").toLowerCase() !== "post") return;
    if (form.dataset.submitting === "1") { e.preventDefault(); return; }
    form.dataset.submitting = "1";
    form.querySelectorAll("button[type=submit], button:not([type])").forEach(function (b) {
      b.disabled = true;
      b.classList.add("is-busy");
    });
    // Re-enable if the page is restored from the back/forward cache.
    window.addEventListener("pageshow", function () {
      form.dataset.submitting = "";
      form.querySelectorAll("button.is-busy").forEach(function (b) { b.disabled = false; b.classList.remove("is-busy"); });
    }, { once: true });
  });

  // Print buttons
  document.querySelectorAll("[data-print]").forEach(function (btn) {
    btn.addEventListener("click", function () { window.print(); });
  });

  // Calendar drag & drop: drop a post chip on a day to move it (regular POST + CSRF token).
  var cal = document.querySelector(".calendar[data-move-csrf]");
  if (cal) {
    var dragged = null;
    cal.addEventListener("dragstart", function (e) {
      var chip = e.target.closest ? e.target.closest(".chip[data-move-url]") : null;
      if (!chip) return;
      dragged = chip;
      e.dataTransfer.effectAllowed = "move";
      e.dataTransfer.setData("text/plain", chip.getAttribute("data-move-url"));
    });
    cal.addEventListener("dragend", function () {
      dragged = null;
      cal.querySelectorAll(".drop-target").forEach(function (d) { d.classList.remove("drop-target"); });
    });
    cal.addEventListener("dragover", function (e) {
      var day = e.target.closest(".cal-day");
      if (!day || !dragged) return;
      e.preventDefault();
      cal.querySelectorAll(".drop-target").forEach(function (d) { if (d !== day) d.classList.remove("drop-target"); });
      day.classList.add("drop-target");
    });
    cal.addEventListener("drop", function (e) {
      var day = e.target.closest(".cal-day");
      if (!day || !dragged) return;
      e.preventDefault();
      if (dragged.closest(".cal-day") === day) return;
      var form = document.createElement("form");
      form.method = "post";
      form.action = dragged.getAttribute("data-move-url");
      [["csrf_token", cal.getAttribute("data-move-csrf")],
       ["new_date", day.getAttribute("data-date")],
       ["next", window.location.pathname + window.location.search]].forEach(function (pair) {
        var input = document.createElement("input");
        input.type = "hidden";
        input.name = pair[0];
        input.value = pair[1];
        form.appendChild(input);
      });
      document.body.appendChild(form);
      form.submit();
    });
  }
})();
