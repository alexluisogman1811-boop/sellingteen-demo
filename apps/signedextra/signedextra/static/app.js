/* SignedExtra page behaviors. Everything here is progressive enhancement:
   pages still work (without these conveniences) if JavaScript fails. */
(function () {
  "use strict";

  function $(sel, root) { return (root || document).querySelector(sel); }
  function $all(sel, root) { return Array.prototype.slice.call((root || document).querySelectorAll(sel)); }

  function storage() {
    try { return window.localStorage; } catch (e) { return null; }
  }

  // Confirm before submitting destructive forms.
  $all("form[data-confirm]").forEach(function (form) {
    form.addEventListener("submit", function (e) {
      if (!window.confirm(form.getAttribute("data-confirm"))) e.preventDefault();
    });
  });

  // Submit a form when a control changes (status filter, photo picker).
  $all("[data-autosubmit]").forEach(function (el) {
    el.addEventListener("change", function () {
      if (el.form && el.form.hasAttribute("data-resize-images")) return; // handled below
      if (el.form) el.form.submit();
    });
  });

  $all("[data-print]").forEach(function (btn) {
    btn.addEventListener("click", function () { window.print(); });
  });

  // Copy and share helpers.
  function textOf(sel) {
    var el = $(sel);
    if (!el) return "";
    return el.value !== undefined && el.tagName === "INPUT" ? el.value : el.textContent;
  }
  $all("[data-copy]").forEach(function (btn) {
    btn.addEventListener("click", function () {
      var text = textOf(btn.getAttribute("data-copy"));
      var done = function () { btn.textContent = "Copied ✓"; };
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(text).then(done, function () { fallbackCopy(text); done(); });
      } else { fallbackCopy(text); done(); }
    });
  });
  function fallbackCopy(text) {
    var ta = document.createElement("textarea");
    ta.value = text; document.body.appendChild(ta); ta.select();
    try { document.execCommand("copy"); } catch (e) { /* ignore */ }
    document.body.removeChild(ta);
  }
  $all("[data-share]").forEach(function (btn) {
    if (!navigator.share) { btn.hidden = true; return; }
    btn.addEventListener("click", function () {
      navigator.share({ title: btn.getAttribute("data-share-title") || document.title,
                        text: btn.getAttribute("data-share-title") || "",
                        url: textOf(btn.getAttribute("data-share")) }).catch(function () {});
    });
  });

  // Ticket form: line rows.
  var lines = $("#lines");
  var tpl = $("#line-template");
  function syncKind(row) {
    var select = $("[data-kind-select]", row);
    row.setAttribute("data-kind", select.value);
  }
  function wireRow(row) {
    var select = $("[data-kind-select]", row);
    select.addEventListener("change", function () { syncKind(row); });
    $("[data-remove-line]", row).addEventListener("click", function () {
      if ($all(".line-row", lines).length > 1) row.parentNode.removeChild(row);
      else $all("input[type=text]", row).forEach(function (i) { i.value = ""; });
      saveDraft();
    });
    syncKind(row);
  }
  if (lines && tpl) {
    $all(".line-row", lines).forEach(wireRow);
    $all("[data-add-line]").forEach(function (btn) {
      btn.addEventListener("click", function () {
        var row = tpl.content.firstElementChild.cloneNode(true);
        $("[data-kind-select]", row).value = btn.getAttribute("data-add-line");
        lines.appendChild(row);
        wireRow(row);
        var first = $(btn.getAttribute("data-add-line") === "labor" ? "select[name=line_labor]" : "input[name=line_desc]", row);
        if (first) first.focus();
      });
    });
  }

  // Ticket form: autosave a draft on this device so a dropped signal or a
  // closed tab doesn't lose what the foreman typed. Photos are not saved.
  var store = storage();
  var draftForm = $("form[data-autosave]");
  var submittedKey = null;
  try { submittedKey = window.sessionStorage.getItem("autosave-submitted"); } catch (e) { /* ignore */ }
  if (store && submittedKey && !draftForm) {
    // The form was submitted and the server accepted it: forget the draft.
    store.removeItem("draft:" + submittedKey);
    try { window.sessionStorage.removeItem("autosave-submitted"); } catch (e) { /* ignore */ }
  }
  function serialize(form) {
    var data = [];
    $all("input, select, textarea", form).forEach(function (el) {
      if (!el.name || el.name === "_csrf" || el.type === "hidden" || el.type === "file") return;
      data.push([el.name, el.value]);
    });
    return data;
  }
  function saveDraft() {
    if (!store || !draftForm) return;
    try {
      store.setItem("draft:" + draftForm.getAttribute("data-autosave"),
                    JSON.stringify({ at: Date.now(), fields: serialize(draftForm) }));
      var status = $("[data-autosave-status]");
      if (status) status.textContent = "Draft saved on this device.";
    } catch (e) { /* storage full or blocked */ }
  }
  if (store && draftForm) {
    var key = "draft:" + draftForm.getAttribute("data-autosave");
    var hasErrors = !!$(".flash-error", document);
    var saved = null;
    try { saved = JSON.parse(store.getItem(key) || "null"); } catch (e) { saved = null; }
    if (saved && !hasErrors && saved.fields && saved.fields.length) {
      var bar = document.createElement("div");
      bar.className = "flash flash-info no-print";
      bar.innerHTML = "You have an unsaved draft on this device. ";
      var restore = document.createElement("button");
      restore.type = "button"; restore.className = "btn btn-sm btn-secondary"; restore.textContent = "Restore draft";
      var discard = document.createElement("button");
      discard.type = "button"; discard.className = "btn btn-sm btn-secondary"; discard.textContent = "Discard";
      bar.appendChild(restore); bar.appendChild(document.createTextNode(" ")); bar.appendChild(discard);
      draftForm.parentNode.insertBefore(bar, draftForm);
      restore.addEventListener("click", function () { restoreDraft(saved.fields); bar.remove(); });
      discard.addEventListener("click", function () { store.removeItem(key); bar.remove(); });
    }
    draftForm.addEventListener("input", saveDraft);
    draftForm.addEventListener("change", saveDraft);
    draftForm.addEventListener("submit", function () {
      try { window.sessionStorage.setItem("autosave-submitted", draftForm.getAttribute("data-autosave")); } catch (e) { /* ignore */ }
    });
  }
  function restoreDraft(fields) {
    if (!lines || !tpl) return;
    var lineCount = fields.filter(function (f) { return f[0] === "line_kind"; }).length;
    var rows = $all(".line-row", lines);
    while (rows.length < lineCount) {
      var row = tpl.content.firstElementChild.cloneNode(true);
      lines.appendChild(row); wireRow(row); rows.push(row);
    }
    var counters = {};
    fields.forEach(function (f) {
      var name = f[0], value = f[1];
      if (name.indexOf("line_") === 0) {
        var i = counters[name] || 0; counters[name] = i + 1;
        var el = rows[i] && $("[name=" + name + "]", rows[i]);
        if (el) el.value = value;
      } else {
        var input = draftForm.elements[name];
        if (input && input.value !== undefined) input.value = value;
      }
    });
    rows.forEach(syncKind);
  }

  // Photo upload: shrink large photos on the phone before sending.
  $all("form[data-resize-images]").forEach(function (form) {
    var input = $("input[type=file]", form);
    var status = $("[data-upload-status]", form);
    input.addEventListener("change", function () {
      var files = Array.prototype.slice.call(input.files || []);
      if (!files.length) return;
      if (!window.DataTransfer || !window.createImageBitmap) { form.submit(); return; }
      if (status) status.textContent = "Preparing photos…";
      Promise.all(files.map(shrink)).then(function (out) {
        var dt = new DataTransfer();
        out.forEach(function (f) { dt.items.add(f); });
        input.files = dt.files;
        if (status) status.textContent = "Uploading…";
        form.submit();
      }).catch(function () { form.submit(); });
    });
  });
  function shrink(file) {
    var MAX = 1800;
    if (!/^image\/(jpeg|png|webp)$/.test(file.type) || file.size < 700 * 1024) return Promise.resolve(file);
    return createImageBitmap(file).then(function (bmp) {
      var scale = Math.min(1, MAX / Math.max(bmp.width, bmp.height));
      var canvas = document.createElement("canvas");
      canvas.width = Math.round(bmp.width * scale); canvas.height = Math.round(bmp.height * scale);
      canvas.getContext("2d").drawImage(bmp, 0, 0, canvas.width, canvas.height);
      return new Promise(function (resolve) {
        canvas.toBlob(function (blob) {
          if (!blob || blob.size >= file.size) { resolve(file); return; }
          resolve(new File([blob], file.name.replace(/\.\w+$/, "") + ".jpg", { type: "image/jpeg" }));
        }, "image/jpeg", 0.82);
      });
    });
  }
})();
