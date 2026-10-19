// SPDX-License-Identifier: AGPL-3.0-or-later
// Copy-to-clipboard for <button data-copy="element-id">. The only script on the site.
document.addEventListener("click", (event) => {
  const button = event.target.closest("button[data-copy]");
  if (!button) return;
  const source = document.getElementById(button.dataset.copy);
  if (!source || !navigator.clipboard) return;
  navigator.clipboard.writeText(source.textContent.trim()).then(() => {
    const label = button.textContent;
    button.textContent = "Copied";
    setTimeout(() => { button.textContent = label; }, 1500);
  });
});
