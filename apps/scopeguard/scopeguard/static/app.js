// Small progressive enhancements; every page works without JavaScript.
document.addEventListener("click", function (event) {
  var copy = event.target.closest("[data-copy]");
  if (copy) {
    var target = document.getElementById(copy.getAttribute("data-copy"));
    if (!target) return;
    var text = target.value !== undefined ? target.value : target.textContent;
    var done = function () {
      var old = copy.textContent;
      copy.textContent = "Copied";
      setTimeout(function () { copy.textContent = old; }, 1500);
    };
    if (navigator.clipboard) {
      navigator.clipboard.writeText(text).then(done, function () { target.select && target.select(); });
    } else if (target.select) {
      target.select();
      document.execCommand("copy");
      done();
    }
  }
  if (event.target.matches("[data-print]")) window.print();
});

document.addEventListener("submit", function (event) {
  var msg = event.target.getAttribute("data-confirm");
  if (msg && !window.confirm(msg)) event.preventDefault();
});

// Show the client-evidence fields only when recording a client decision on a change request.
document.addEventListener("change", function (event) {
  if (event.target.matches("[data-toggle]")) {
    var box = document.getElementById(event.target.getAttribute("data-toggle"));
    if (box) box.hidden = !event.target.checked;
  }
});
document.addEventListener("DOMContentLoaded", function () {
  document.querySelectorAll("[data-toggle]").forEach(function (el) {
    var box = document.getElementById(el.getAttribute("data-toggle"));
    if (box) box.hidden = !el.checked;
  });
  // Client review page: after a rejected submission, bring the form with the error into view.
  var scrollTo = document.querySelector("[data-scroll-to]");
  if (scrollTo) {
    var target = document.getElementById(scrollTo.getAttribute("data-scroll-to"));
    if (target) target.scrollIntoView({ block: "start" });
  }
  // Compose page: re-render when a different template or change request is chosen.
  document.querySelectorAll("select[data-autosubmit]").forEach(function (el) {
    el.addEventListener("change", function () { el.form.submit(); });
  });
});
