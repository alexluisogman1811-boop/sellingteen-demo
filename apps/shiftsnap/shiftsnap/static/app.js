// Progressive enhancement only: every feature works without JavaScript.
(function () {
  // Confirmation dialogs for destructive forms.
  document.addEventListener("submit", function (e) {
    var msg = e.target.getAttribute("data-confirm");
    if (msg && !window.confirm(msg)) e.preventDefault();
  });

  // Pre-fill a shift's role from the chosen employee when the role is empty.
  var sel = document.getElementById("employee-select");
  var role = document.getElementById("role-input");
  if (sel && role) {
    sel.addEventListener("change", function () {
      var opt = sel.options[sel.selectedIndex];
      var r = opt && opt.getAttribute("data-role");
      if (r && (!role.value || role.dataset.auto === "1")) {
        role.value = r;
        role.dataset.auto = "1";
      }
    });
    role.addEventListener("input", function () { role.dataset.auto = "0"; });
  }

  // Show/hide per-day availability inputs.
  var track = document.getElementById("track-availability");
  var days = document.getElementById("avail-days");
  if (track && days) {
    var sync = function () { days.hidden = !track.checked; };
    track.addEventListener("change", sync);
    sync();
  }

  var printBtn = document.getElementById("print-btn");
  if (printBtn) printBtn.addEventListener("click", function () { window.print(); });
})();
