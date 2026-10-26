/* Silent commander art: fetch/play only when visible, active, and motion is allowed. */
(() => {
  const reduced = window.matchMedia('(prefers-reduced-motion: reduce)');
  const videos = [...document.querySelectorAll('video[data-commander-video]')];
  const visible = new WeakMap();
  function update(video) {
    const card = video.closest('.life-card');
    const enabled = !document.hidden && !reduced.matches && visible.get(video) &&
      (!card || card.classList.contains('active-turn')) && !video.dataset.failed;
    if (!enabled) {
      video.pause();
      return;
    }
    if (!video.hasAttribute('src')) video.src = video.dataset.commanderVideo;
    video.muted = true;
    if (video.paused) video.play().catch(() => {
      // Autoplay can be blocked by device policy; the poster remains visible.
      video.classList.remove('is-playing');
      card?.classList.remove('motion-playing');
    });
  }
  function refresh() { videos.forEach(update); }
  const observer = 'IntersectionObserver' in window ? new IntersectionObserver(entries => {
    entries.forEach(entry => { visible.set(entry.target, entry.isIntersecting); update(entry.target); });
  }) : null;
  videos.forEach(video => {
    const card = video.closest('.life-card');
    video.addEventListener('playing', () => {
      // A play() promise may complete after a turn/visibility change.
      if (document.hidden || reduced.matches || !visible.get(video) ||
          (card && !card.classList.contains('active-turn'))) { video.pause(); return; }
      video.classList.add('is-playing');
      card?.classList.add('motion-playing');
    });
    video.addEventListener('pause', () => {
      video.classList.remove('is-playing');
      card?.classList.remove('motion-playing');
    });
    video.addEventListener('error', () => {
      video.dataset.failed = '1';
      video.classList.remove('is-playing');
      card?.classList.remove('motion-playing');
    });
    if (card) new MutationObserver(() => update(video)).observe(card, { attributes: true, attributeFilter: ['class'] });
    if (observer) observer.observe(video);
    else { visible.set(video, true); update(video); }
  });
  document.addEventListener('visibilitychange', refresh);
  window.addEventListener('pagehide', () => videos.forEach(video => video.pause()));
  window.addEventListener('pageshow', refresh);
  document.addEventListener('pointerdown', refresh, { passive: true });
  reduced.addEventListener('change', refresh);
})();
