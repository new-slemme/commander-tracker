(() => {
  const input = document.getElementById('profile-picture-file');
  if (!input) return;
  const editor = input.closest('.picture-editor');
  const preview = editor.querySelector('[data-picture-preview]');
  const original = preview.innerHTML;
  const status = editor.querySelector('[data-picture-status]');
  let previewUrl;
  input.addEventListener('change', () => {
    if (previewUrl) URL.revokeObjectURL(previewUrl);
    preview.innerHTML = original;
    input.setCustomValidity('');
    status.textContent = '';
    const file = input.files[0];
    if (!file) return;
    if (file.size >= 15 * 1024 * 1024) {
      input.setCustomValidity('Choose an image smaller than 15 MB.');
      status.textContent = input.validationMessage;
      input.reportValidity();
      return;
    }
    previewUrl = URL.createObjectURL(file);
    const img = document.createElement('img');
    img.src = previewUrl;
    img.alt = 'Preview of your new profile picture';
    img.className = 'player-avatar player-avatar--large';
    preview.replaceChildren(img);
    status.textContent = 'Preview only. Save your uploaded picture to apply it.';
  });
  window.addEventListener('pagehide', () => {
    if (previewUrl) URL.revokeObjectURL(previewUrl);
  });
})();
