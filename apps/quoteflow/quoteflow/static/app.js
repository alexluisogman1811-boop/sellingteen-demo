// Small progressive enhancements. Every page works without this file.
(function () {
  'use strict';

  // ---- Forms: prevent double submits, show a busy label ----
  // <form data-busy-label="Saving…"> disables its submit buttons once submitted.
  function resetForm(form) {
    form.removeAttribute('data-submitting');
    form.querySelectorAll('button[type=submit]').forEach(function (b) {
      b.disabled = false;
      if (b.dataset.label) b.textContent = b.dataset.label;
    });
  }

  document.querySelectorAll('form[data-busy-label]').forEach(function (form) {
    form.addEventListener('submit', function (e) {
      if (form.hasAttribute('data-submitting')) { e.preventDefault(); return; }
      form.setAttribute('data-submitting', '');
      form.querySelectorAll('button[type=submit]').forEach(function (b) {
        b.dataset.label = b.textContent;
        b.textContent = form.dataset.busyLabel;
        // Disable after the browser has captured the submission.
        setTimeout(function () { b.disabled = true; }, 0);
      });
    });
  });

  // ---- Quote generation: loading panel with elapsed time ----
  var genForm = document.getElementById('quote-form');
  var loading = document.getElementById('generating');
  var timer = null;

  function stopLoading() {
    if (timer) clearInterval(timer);
    timer = null;
    if (loading) loading.hidden = true;
    if (genForm) {
      resetForm(genForm);
      genForm.querySelector('textarea').readOnly = false;
    }
  }

  if (genForm && loading) {
    genForm.addEventListener('submit', function () {
      var started = Date.now();
      var elapsed = document.getElementById('generating-elapsed');
      var slow = document.getElementById('generating-slow');
      genForm.querySelector('textarea').readOnly = true;
      // A retry shouldn't show the previous attempt's error while it runs.
      document.querySelectorAll('.flash.error').forEach(function (el) { el.remove(); });
      loading.hidden = false;
      timer = setInterval(function () {
        var s = Math.round((Date.now() - started) / 1000);
        elapsed.textContent = s + 's';
        if (s >= 45) slow.hidden = false;
      }, 1000);
    });
  }

  // Coming back with the Back button restores the page from cache in its
  // "submitting" state; reset it so the owner can generate again.
  window.addEventListener('pageshow', function (e) {
    if (!e.persisted) return;
    stopLoading();
    document.querySelectorAll('form[data-submitting]').forEach(resetForm);
  });

  // ---- Warn before leaving with unsaved edits ----
  document.querySelectorAll('form[data-warn-unsaved]').forEach(function (form) {
    var dirty = false;
    form.addEventListener('input', function () { dirty = true; });
    form.addEventListener('change', function () { dirty = true; });
    form.addEventListener('submit', function () { dirty = false; });
    window.addEventListener('beforeunload', function (e) {
      if (dirty) { e.preventDefault(); e.returnValue = ''; }
    });
  });

  // ---- Copy reply ----
  var copyBtn = document.getElementById('copy-btn');
  if (copyBtn) {
    var box = document.getElementById('reply_text');
    var label = copyBtn.textContent;
    var done = function () {
      copyBtn.textContent = 'Copied ✓';
      setTimeout(function () { copyBtn.textContent = label; }, 2000);
    };
    var fallback = function () {
      box.select();
      try {
        if (document.execCommand('copy')) { done(); return; }
      } catch (err) { /* fall through */ }
      // Text is now selected; let the user finish with their device's own copy command.
      copyBtn.textContent = 'Text selected — copy it manually';
    };
    copyBtn.addEventListener('click', function () {
      if (navigator.clipboard && window.isSecureContext) {
        navigator.clipboard.writeText(box.value).then(done, fallback);
      } else {
        fallback();
      }
    });
  }
})();
