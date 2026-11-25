// Small progressive enhancements. Every page works without JavaScript.
document.addEventListener("DOMContentLoaded", function () {
  var form = document.querySelector("[data-followup]");
  if (form) {
    var box = form.querySelector("textarea");
    var editBtn = form.querySelector("[data-edit]");
    var copyBtn = form.querySelector("[data-copy]");

    editBtn.addEventListener("click", function () {
      box.readOnly = false;
      box.classList.add("editing");
      box.focus();
      editBtn.textContent = "Editing…";
      editBtn.disabled = true;
    });

    copyBtn.addEventListener("click", function () {
      var done = function () {
        copyBtn.textContent = "Copied ✓";
        setTimeout(function () { copyBtn.textContent = "Copy message"; }, 2000);
      };
      if (navigator.clipboard && window.isSecureContext) {
        navigator.clipboard.writeText(box.value).then(done, fallback);
      } else {
        fallback();
      }
      function fallback() {
        var wasReadOnly = box.readOnly;
        box.readOnly = false;
        box.select();
        var copied = false;
        try { copied = document.execCommand("copy"); } catch (e) { copied = false; }
        box.readOnly = wasReadOnly;
        // The text stays selected, so a manual copy works if the browser blocked ours.
        if (copied) { done(); } else { copyBtn.textContent = "Press Ctrl+C to copy"; }
      }
    });
  }

  var shortcuts = document.querySelector("[data-due-shortcuts]");
  if (shortcuts) {
    var issue = document.getElementById("issue_date");
    var due = document.getElementById("due_date");
    shortcuts.addEventListener("click", function (e) {
      var btn = e.target.closest("[data-days]");
      if (!btn || !issue.value) return;
      var d = new Date(issue.value + "T00:00:00");
      d.setDate(d.getDate() + parseInt(btn.dataset.days, 10));
      var pad = function (n) { return String(n).padStart(2, "0"); };
      due.value = d.getFullYear() + "-" + pad(d.getMonth() + 1) + "-" + pad(d.getDate());
    });
  }
});
