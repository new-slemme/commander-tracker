/* Avatar controls share the existing player selects and their change handlers. */
(() => {
  const dialog = document.getElementById('playerPickerDialog');
  if (!dialog || typeof dialog.showModal !== 'function') return;
  const pickers = Array.from(document.querySelectorAll('[data-player-picker]'));
  let activePicker = null;

  function selectFor(picker) {
    return document.getElementById('player' + picker.dataset.playerPicker);
  }

  function syncChoices(container, select) {
    for (const button of container.querySelectorAll('[data-player-id]')) {
      const option = Array.from(select.options).find(opt => opt.value === button.dataset.playerId);
      button.disabled = !option || option.disabled;
      button.setAttribute('aria-pressed', String(select.value === button.dataset.playerId));
      button.title = option ? option.text + (option.disabled ? ' — already selected in another seat' : '') : '';
    }
  }

  function sync() {
    for (const picker of pickers) {
      const select = selectFor(picker);
      const choices = Array.from(picker.querySelectorAll('[data-player-id]'));
      const visibleIds = choices.slice(0, 4).map(button => button.dataset.playerId);
      if (select.value && !visibleIds.includes(select.value)) {
        visibleIds[visibleIds.length - 1] = select.value;
      }
      for (const button of choices) button.hidden = !visibleIds.includes(button.dataset.playerId);
      syncChoices(picker, select);
      picker.querySelector('.player-picker__selection').textContent = select.value
        ? select.options[select.selectedIndex].text : 'No player selected';
    }
    if (dialog.open && activePicker) {
      if (activePicker.closest('.player-panel').classList.contains('hidden')) dialog.close();
      else syncChoices(dialog, selectFor(activePicker));
    }
  }

  function choose(picker, id) {
    const select = selectFor(picker);
    const option = Array.from(select.options).find(opt => opt.value === id);
    if (!option || option.disabled || select.value === id) return;
    select.value = id;
    select.dispatchEvent(new Event('change', {bubbles: true}));
  }

  function positionDialog() {
    if (!dialog.open || !activePicker) return;
    const anchor = activePicker.querySelector('.player-picker__more').getBoundingClientRect();
    const bounds = dialog.getBoundingClientRect();
    const gap = 10;
    const left = Math.max(12, Math.min(anchor.right - bounds.width, window.innerWidth - bounds.width - 12));
    const below = anchor.bottom + gap;
    const top = below + bounds.height <= window.innerHeight - 12
      ? below : Math.max(12, anchor.top - bounds.height - gap);
    dialog.style.left = left + 'px';
    dialog.style.top = top + 'px';
  }

  for (const picker of pickers) {
    const select = selectFor(picker);
    select.classList.add('d-none');
    picker.hidden = false;
    picker.addEventListener('click', event => {
      const choice = event.target.closest('[data-player-id]');
      if (choice && !choice.disabled) choose(picker, choice.dataset.playerId);
    });
    picker.querySelector('.player-picker__more').addEventListener('click', () => {
      activePicker = picker;
      const seat = picker.closest('.player-panel').querySelector('.pos-text').textContent;
      document.getElementById('playerPickerTitle').textContent = 'Select player · ' + seat;
      syncChoices(dialog, select);
      picker.querySelector('.player-picker__more').setAttribute('aria-expanded', 'true');
      dialog.showModal();
      positionDialog();
      const selected = dialog.querySelector('[aria-pressed="true"]:not(:disabled)');
      (selected || dialog.querySelector('[data-player-id]:not(:disabled)') || dialog.querySelector('button')).focus();
    });
  }

  dialog.addEventListener('click', event => {
    const choice = event.target.closest('[data-player-id]');
    if (choice && !choice.disabled) {
      choose(activePicker, choice.dataset.playerId);
      dialog.close();
    } else if (event.target === dialog) {
      const bounds = dialog.getBoundingClientRect();
      if (event.clientX < bounds.left || event.clientX > bounds.right ||
          event.clientY < bounds.top || event.clientY > bounds.bottom) dialog.close();
    }
  });
  dialog.querySelector('.player-picker-dialog__close').addEventListener('click', () => dialog.close());
  dialog.querySelector('.player-picker-dialog__clear').addEventListener('click', () => {
    choose(activePicker, '');
    dialog.close();
  });
  dialog.addEventListener('close', () => {
    const trigger = activePicker?.querySelector('.player-picker__more');
    trigger?.setAttribute('aria-expanded', 'false');
    if (trigger && !activePicker.closest('.player-panel').classList.contains('hidden')) trigger.focus();
    activePicker = null;
  });
  window.addEventListener('resize', positionDialog);
  window.addEventListener('scroll', positionDialog, {passive: true});
  document.addEventListener('playerselectionchange', sync);
  // Reconcile browser back/forward form restoration as well as rematch prefills.
  window.addEventListener('pageshow', () => updatePlayerOptionDisabling());
  sync();
})();
