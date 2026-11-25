// Copy buttons: <button data-copy="#selector">
document.addEventListener("click", async (event) => {
  const button = event.target.closest("[data-copy]");
  if (!button) return;
  const source = document.querySelector(button.dataset.copy);
  if (!source) return;
  const original = button.textContent;
  let copied = false;
  try {
    await navigator.clipboard.writeText(source.value);
    copied = true;
  } catch (err) {
    // Clipboard API can be unavailable (e.g. non-HTTPS); fall back to selecting the text.
    source.focus();
    source.select();
    try { copied = document.execCommand("copy"); } catch (e) { copied = false; }
  }
  button.textContent = copied ? "Copied!" : "Text selected: press Ctrl+C (or tap Copy)";
  const note = button.dataset.copyNote && document.querySelector(button.dataset.copyNote);
  if (note && copied) note.hidden = false;
  setTimeout(() => { button.textContent = original; }, 2500);
});

// Confirmation for destructive forms: <form data-confirm="...">
document.addEventListener("submit", (event) => {
  const message = event.target.dataset && event.target.dataset.confirm;
  if (message && !window.confirm(message)) event.preventDefault();
});
