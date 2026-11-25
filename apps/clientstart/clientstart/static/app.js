// ClientStart UI helpers. No inline scripts (CSP: script-src 'self').
document.addEventListener("DOMContentLoaded", () => {
  // Copy-to-clipboard buttons.
  document.querySelectorAll(".copy-btn").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const el = document.getElementById(btn.dataset.copyTarget);
      if (!el) return;
      const original = btn.textContent;
      try {
        await navigator.clipboard.writeText(el.value);
      } catch {
        el.select();
        document.execCommand("copy");
      }
      btn.textContent = "Copied ✓";
      setTimeout(() => (btn.textContent = original), 1500);
    });
  });

  // Status dropdowns submit on change.
  document.querySelectorAll("select.auto-submit").forEach((sel) => {
    sel.addEventListener("change", () => sel.form.submit());
  });

  // Confirmation prompts on destructive buttons.
  document.querySelectorAll(".confirm").forEach((btn) => {
    btn.addEventListener("click", (e) => {
      if (!window.confirm(btn.dataset.confirm || "Are you sure?")) e.preventDefault();
    });
  });

  // Warn about oversized files before uploading (the server enforces the real limit).
  document.querySelectorAll("input[type=file][data-max-bytes]").forEach((input) => {
    input.addEventListener("change", () => {
      const max = Number(input.dataset.maxBytes);
      const file = input.files && input.files[0];
      input.setCustomValidity(file && file.size > max
        ? `This file is ${(file.size / 1048576).toFixed(1)} MB. The limit is ${Math.round(max / 1048576)} MB.`
        : "");
      input.reportValidity();
    });
  });

  // Mobile navigation toggle.
  const toggle = document.querySelector(".nav-toggle");
  const nav = document.getElementById("mainnav");
  if (toggle && nav) {
    toggle.addEventListener("click", () => {
      const open = nav.classList.toggle("open");
      toggle.setAttribute("aria-expanded", String(open));
    });
  }
});
