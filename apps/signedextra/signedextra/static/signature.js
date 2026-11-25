/* Signature pad: draws with mouse, finger or stylus and stores a PNG data URL
   in a hidden field when the form is submitted. */
(function () {
  "use strict";
  var form = document.querySelector("form[data-signature-form]");
  if (!form) return;
  var canvas = form.querySelector("canvas[data-sigpad]");
  var output = form.querySelector("[data-sig-output]");
  var clearBtn = form.querySelector("[data-sig-clear]");
  var ctx = canvas.getContext("2d");
  var drawing = false, hasInk = false, last = null;

  function resize() {
    var ratio = Math.max(window.devicePixelRatio || 1, 1);
    var rect = canvas.getBoundingClientRect();
    var data = hasInk ? canvas.toDataURL() : null;
    canvas.width = Math.round(rect.width * ratio);
    canvas.height = Math.round(rect.height * ratio);
    ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
    ctx.lineWidth = 2.5; ctx.lineCap = "round"; ctx.lineJoin = "round"; ctx.strokeStyle = "#111";
    if (data) {
      var img = new Image();
      img.onload = function () { ctx.drawImage(img, 0, 0, rect.width, rect.height); };
      img.src = data;
    }
  }
  function point(e) {
    var rect = canvas.getBoundingClientRect();
    return { x: e.clientX - rect.left, y: e.clientY - rect.top };
  }
  canvas.addEventListener("pointerdown", function (e) {
    drawing = true; last = point(e); canvas.setPointerCapture(e.pointerId);
    ctx.beginPath(); ctx.arc(last.x, last.y, 1, 0, Math.PI * 2); ctx.fill();
    hasInk = true; e.preventDefault();
  });
  canvas.addEventListener("pointermove", function (e) {
    if (!drawing) return;
    var p = point(e);
    ctx.beginPath(); ctx.moveTo(last.x, last.y); ctx.lineTo(p.x, p.y); ctx.stroke();
    last = p; e.preventDefault();
  });
  ["pointerup", "pointercancel", "pointerleave"].forEach(function (t) {
    canvas.addEventListener(t, function () { drawing = false; });
  });
  clearBtn.addEventListener("click", function () {
    ctx.clearRect(0, 0, canvas.width, canvas.height); hasInk = false; output.value = "";
  });
  form.addEventListener("submit", function () {
    output.value = hasInk ? canvas.toDataURL("image/png") : "";
  });
  window.addEventListener("resize", resize);
  resize();
})();
