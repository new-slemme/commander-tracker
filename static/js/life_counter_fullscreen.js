/* Fullscreen the document so the landscape game and its menus stay together. */
(() => {
  const root = document.documentElement;
  const button = document.getElementById('fullscreenToggle');
  const label = document.getElementById('fullscreenLabel');
  const help = document.getElementById('fullscreenHelp');
  const landscapeModal = document.getElementById('landscapeModal');
  const landscapeStart = document.getElementById('landscapeStart');
  const landscapeHelp = document.getElementById('landscapeHelp');
  const board = document.querySelector('.life-wrapper');
  const portraitPhone = window.matchMedia('(orientation: portrait) and (pointer: coarse)');
  const standalone = window.matchMedia('(display-mode: standalone)');
  const installedFullscreen = window.matchMedia('(display-mode: fullscreen)');
  const fullscreenElement = () => document.fullscreenElement || document.webkitFullscreenElement;
  const canFullscreen = () => Boolean(
    (root.requestFullscreen && document.fullscreenEnabled !== false) ||
    (root.webkitRequestFullscreen && document.webkitFullscreenEnabled !== false)
  );
  const isInstalled = () => standalone.matches || installedFullscreen.matches || navigator.standalone === true;
  const isAppleMobile = /iPhone|iPad|iPod/.test(navigator.userAgent) ||
    (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1);
  let busy = false;

  function updateOrientationPrompt() {
    const portrait = portraitPhone.matches;
    board.inert = portrait;
    const modal = bootstrap.Modal.getOrCreateInstance(landscapeModal, {backdrop: 'static', keyboard: false});
    if (portrait) {
      const quickMenu = document.getElementById('quickMenuModal');
      if (quickMenu.classList.contains('show')) {
        quickMenu.addEventListener('hidden.bs.modal', updateOrientationPrompt, {once: true});
        bootstrap.Modal.getInstance(quickMenu)?.hide();
        return;
      }
      window.closePlayerSaltMenu();
      modal.show();
    } else {
      modal.hide();
    }
    landscapeStart.textContent = fullscreenElement() ? 'Try landscape again' : 'Start landscape fullscreen';
  }

  function syncFullscreen() {
    const active = Boolean(fullscreenElement());
    button.hidden = !active && !canFullscreen() && isInstalled();
    button.setAttribute('aria-pressed', String(active));
    label.textContent = active ? 'Exit Fullscreen' : canFullscreen() ? 'Enter Fullscreen' : 'Fullscreen Help';
    updateOrientationPrompt();
  }

  function showHelp(message) {
    help.textContent = message;
    help.hidden = false;
    landscapeHelp.textContent = message;
  }

  async function lockLandscape() {
    try {
      if (!screen.orientation?.lock) throw new Error('Orientation locking unavailable');
      await screen.orientation.lock('landscape');
    } catch (error) {
      landscapeHelp.textContent = 'Turn your phone sideways to play. If it stays upright, enable Auto-rotate in your phone settings.';
    }
    updateOrientationPrompt();
  }

  function releaseOrientation() {
    try { screen.orientation?.unlock?.(); } catch (error) {}
  }

  async function changeFullscreen(exit = false) {
    if (busy) return;
    if (!exit && !fullscreenElement() && !canFullscreen()) {
      showHelp(isAppleMobile
        ? 'Open this site from Safari’s Share → Add to Home Screen, then turn your phone sideways.'
        : 'Turn your phone sideways to play. Fullscreen is unavailable in this browser; you can also try opening the game in Chrome.');
      return;
    }
    busy = true;
    button.disabled = landscapeStart.disabled = true;
    help.hidden = true;
    try {
      if (exit) {
        if (document.exitFullscreen) await document.exitFullscreen();
        else await document.webkitExitFullscreen();
      } else {
        // Fullscreen needs the tap; request it before any asynchronous work.
        if (!fullscreenElement()) {
          if (root.requestFullscreen) await root.requestFullscreen({navigationUI: 'hide'});
          else await root.webkitRequestFullscreen();
        }
        // Android browsers can reject rotation until fullscreen is active.
        await lockLandscape();
      }
      bootstrap.Modal.getInstance(document.getElementById('quickMenuModal'))?.hide();
    } catch (error) {
      showHelp('Fullscreen was unavailable. Turn your phone sideways to play, or tap again to retry fullscreen.');
    } finally {
      busy = false;
      button.disabled = landscapeStart.disabled = false;
      syncFullscreen();
    }
  }

  button.addEventListener('click', () => changeFullscreen(Boolean(fullscreenElement())));
  landscapeStart.addEventListener('click', () => changeFullscreen());
  landscapeModal.addEventListener('shown.bs.modal', () => window.pauseTimers('landscape'));
  landscapeModal.addEventListener('hidden.bs.modal', () => window.resumeTimers('landscape'));
  function handleFullscreenChange() {
    syncFullscreen();
    if (fullscreenElement()) {
      if (!busy) lockLandscape();
    } else {
      releaseOrientation();
    }
  }
  document.addEventListener('fullscreenchange', handleFullscreenChange);
  document.addEventListener('webkitfullscreenchange', handleFullscreenChange);
  window.addEventListener('pagehide', releaseOrientation);
  portraitPhone.addEventListener('change', updateOrientationPrompt);
  standalone.addEventListener('change', syncFullscreen);
  installedFullscreen.addEventListener('change', syncFullscreen);
  syncFullscreen();
  // Installed apps may permit rotation without fullscreen. Browser tabs use
  // the visible entry button instead of silently failing on page load.
  if (isInstalled()) lockLandscape();
})();
