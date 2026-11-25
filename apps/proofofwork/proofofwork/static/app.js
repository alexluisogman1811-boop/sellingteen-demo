// Progressive enhancements only; every feature works without JavaScript.
(function () {
  "use strict";

  // After creating a share link the page is the POST response; point the address bar back at the job
  // so refreshing doesn't resubmit (and create another link).
  var replaceTarget = document.querySelector("[data-replace-url]");
  if (replaceTarget && window.history && history.replaceState) {
    history.replaceState(null, "", replaceTarget.getAttribute("data-replace-url"));
  }

  // ---- confirmations, printing, copying -------------------------------------------------
  document.addEventListener("submit", function (event) {
    var form = event.target;
    var message = form.getAttribute("data-confirm");
    if (message && !window.confirm(message)) {
      event.preventDefault();
    }
  });

  document.addEventListener("click", function (event) {
    if (event.target.closest("[data-print]")) {
      window.print();
      return;
    }
    var chip = event.target.closest("[data-fill-label]");
    if (chip) {
      var labelInput = chip.closest("form").querySelector("input[name=label]");
      labelInput.value = chip.getAttribute("data-fill-label");
      return;
    }
    var copyBtn = event.target.closest("[data-copy]");
    if (copyBtn) {
      var input = document.getElementById(copyBtn.getAttribute("data-copy"));
      if (!input) return;
      input.select();
      var done = function () { copyBtn.textContent = "Copied"; };
      if (navigator.clipboard) {
        navigator.clipboard.writeText(input.value).then(done, function () { document.execCommand("copy"); done(); });
      } else {
        document.execCommand("copy");
        done();
      }
    }
  });

  // ---- unsaved-changes warning (checklist and notes) ----------------------------------
  var dirtyForms = new Set();
  document.querySelectorAll("form[data-dirty-warn]").forEach(function (form) {
    form.addEventListener("input", function () { dirtyForms.add(form); });
    form.addEventListener("change", function () { dirtyForms.add(form); });
    form.addEventListener("submit", function () { dirtyForms.clear(); });
  });
  window.addEventListener("beforeunload", function (event) {
    if (dirtyForms.size) {
      event.preventDefault();
      event.returnValue = "";
    }
  });

  // ---- checklist: show the reason field when "Not completed" is chosen ---------------
  document.addEventListener("change", function (event) {
    var radio = event.target;
    if (radio.type === "radio" && radio.name.indexOf("state_") === 0) {
      var item = radio.closest(".item");
      if (!item) return;
      item.setAttribute("data-state", radio.value);
      if (radio.value === "incomplete") {
        var reason = item.querySelector(".reason-field input");
        if (reason && !reason.value) reason.focus();
      }
    }
  });

  // ---- photos: preview, and shrink oversized phone photos before upload ---------------
  function shrinkImage(file, maxDim) {
    if (!window.createImageBitmap || !window.HTMLCanvasElement) return Promise.resolve(file);
    return createImageBitmap(file, { imageOrientation: "from-image" }).then(function (bitmap) {
      var scale = Math.min(1, maxDim / Math.max(bitmap.width, bitmap.height));
      var canvas = document.createElement("canvas");
      canvas.width = Math.round(bitmap.width * scale);
      canvas.height = Math.round(bitmap.height * scale);
      canvas.getContext("2d").drawImage(bitmap, 0, 0, canvas.width, canvas.height);
      return new Promise(function (resolve) {
        canvas.toBlob(function (blob) {
          if (!blob) { resolve(file); return; }
          var name = file.name.replace(/\.[^.]+$/, "") + ".jpg";
          resolve(new File([blob], name, { type: "image/jpeg" }));
        }, "image/jpeg", 0.85);
      });
    }).catch(function () { return file; }); // the server still validates whatever is sent
  }

  document.querySelectorAll("form[data-photo-upload]").forEach(function (form) {
    var input = form.querySelector("input[type=file]");
    var previews = form.querySelector("[data-previews]");
    var button = form.querySelector("[data-upload-button]");
    var maxBytes = parseInt(form.getAttribute("data-max-bytes"), 10);
    var maxFiles = parseInt(form.getAttribute("data-max-files"), 10);
    var shrinkOver = Math.min(maxBytes, 3 * 1024 * 1024);

    input.addEventListener("change", function () {
      previews.textContent = "";
      var files = Array.prototype.slice.call(input.files);
      files.slice(0, maxFiles).forEach(function (file) {
        var img = document.createElement("img");
        img.alt = "Selected photo preview";
        img.src = URL.createObjectURL(file);
        img.onload = function () { URL.revokeObjectURL(img.src); };
        previews.appendChild(img);
      });
      var note = document.createElement("p");
      note.className = "preview-note";
      note.textContent = files.length > maxFiles
        ? "Too many photos selected: choose at most " + maxFiles + "."
        : files.length + " photo" + (files.length === 1 ? "" : "s") + " selected";
      previews.appendChild(note);
      button.textContent = files.length > 1 ? "Upload " + files.length + " photos" : "Upload photo";
    });

    form.addEventListener("submit", function (event) {
      var files = Array.prototype.slice.call(input.files || []);
      var needsShrink = files.some(function (f) { return f.size > shrinkOver; });
      if (!needsShrink || !window.DataTransfer || form.dataset.prepared) return;
      event.preventDefault();
      button.disabled = true;
      button.textContent = "Preparing photos…";
      Promise.all(files.map(function (f) { return f.size > shrinkOver ? shrinkImage(f, 2400) : f; }))
        .then(function (prepared) {
          var transfer = new DataTransfer();
          prepared.forEach(function (f) { transfer.items.add(f); });
          input.files = transfer.files;
        })
        .catch(function () { /* fall back to the original files */ })
        .then(function () {
          form.dataset.prepared = "1";
          button.textContent = "Uploading…";
          form.submit();
        });
    });
  });

  // ---- job form: only show properties belonging to the chosen customer ----------------
  function filterProperties(select) {
    var target = document.getElementById(select.getAttribute("data-filters"));
    if (!target) return;
    var customer = select.value;
    Array.prototype.forEach.call(target.options, function (opt) {
      var owner = opt.getAttribute("data-customer");
      var visible = !owner || !customer || owner === customer;
      opt.hidden = !visible;
      opt.disabled = !visible;
      if (!visible && opt.selected) target.value = "";
    });
  }
  document.querySelectorAll("select[data-filters]").forEach(function (select) {
    filterProperties(select);
    select.addEventListener("change", function () { filterProperties(select); });
  });
})();
