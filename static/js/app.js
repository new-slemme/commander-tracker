/* CommandersohnUI — global interaction layer */
(function (global) {
  'use strict';

  var UI = {};

  // ── CSRF helper ───────────────────────────────────────────────────
  function csrfToken() {
    var meta = document.querySelector('meta[name="csrf-token"]');
    return meta ? meta.content : '';
  }

  function postJson(url) {
    return fetch(url, {
      method: 'POST',
      headers: {
        'X-Requested-With': 'XMLHttpRequest',
        'X-CSRFToken': csrfToken(),
      },
    });
  }

  // ── Toast helper ─────────────────────────────────────────────────
  function showToast(message) {
    var container = document.querySelector('.app-toast-container');
    if (!container) {
      container = document.createElement('div');
      container.className = 'toast-container position-fixed top-0 end-0 p-3 app-toast-container';
      document.body.appendChild(container);
    }
    var toast = document.createElement('div');
    toast.className = 'toast align-items-center text-bg-dark border-0 app-toast';
    toast.setAttribute('role', 'status');
    toast.setAttribute('aria-live', 'polite');
    toast.setAttribute('aria-atomic', 'true');
    toast.setAttribute('data-bs-delay', '2400');
    var inner = document.createElement('div');
    inner.className = 'd-flex';
    var body = document.createElement('div');
    body.className = 'toast-body';
    body.textContent = message;
    var btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'btn-close btn-close-white me-2 m-auto';
    btn.setAttribute('data-bs-dismiss', 'toast');
    btn.setAttribute('aria-label', 'Close');
    inner.appendChild(body);
    inner.appendChild(btn);
    toast.appendChild(inner);
    container.appendChild(toast);
    var instance = global.bootstrap && bootstrap.Toast.getOrCreateInstance(toast);
    if (instance) {
      toast.addEventListener('hidden.bs.toast', function () { toast.remove(); });
      instance.show();
    }
  }

  // ── Mobile menu ──────────────────────────────────────────────────
  function initMobileMenu() {
    var btn = document.getElementById('menuBtn');
    var closeBtn = document.getElementById('menuClose');
    var drawer = document.getElementById('menuDrawer');
    var overlay = document.getElementById('menuOverlay');
    if (!btn || !drawer || !overlay || !closeBtn) return;

    function openMenu() {
      drawer.classList.add('open');
      overlay.classList.add('open');
      overlay.setAttribute('aria-hidden', 'false');
      btn.setAttribute('aria-expanded', 'true');
      document.body.style.overflow = 'hidden';
    }

    function closeMenu() {
      drawer.classList.remove('open');
      overlay.classList.remove('open');
      overlay.setAttribute('aria-hidden', 'true');
      btn.setAttribute('aria-expanded', 'false');
      document.body.style.overflow = '';
    }

    btn.addEventListener('click', function (e) {
      e.preventDefault();
      if (drawer.classList.contains('open')) closeMenu(); else openMenu();
    });
    closeBtn.addEventListener('click', function (e) { e.preventDefault(); closeMenu(); });
    overlay.addEventListener('click', closeMenu);
    drawer.addEventListener('click', function (e) {
      if (e.target.closest('a')) closeMenu();
    });
    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape') closeMenu();
    });
  }

  // ── Pod context switcher ─────────────────────────────────────────
  function initPodSwitcher() {
    var switcher = document.getElementById('nav-pod-switcher');
    var toggle = document.getElementById('nav-pod-toggle');
    var menu = document.getElementById('nav-pod-menu');
    if (!switcher || !toggle || !menu) return;

    toggle.addEventListener('click', function () {
      var open = switcher.classList.toggle('is-open');
      toggle.setAttribute('aria-expanded', open ? 'true' : 'false');
    });

    document.addEventListener('click', function (event) {
      if (!switcher.contains(event.target)) {
        switcher.classList.remove('is-open');
        toggle.setAttribute('aria-expanded', 'false');
      }
    });

    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape' && switcher.classList.contains('is-open')) {
        switcher.classList.remove('is-open');
        toggle.setAttribute('aria-expanded', 'false');
        toggle.focus();
      }
    });

    menu.querySelectorAll('[data-pod-id]').forEach(function (btn) {
      btn.addEventListener('click', function () {
        var podId = btn.getAttribute('data-pod-id');
        postJson('/pods/switch/' + podId)
          .then(function (res) {
            if (!res.ok) return;
            return res.json().then(function (data) {
              if (data.message) showToast(data.message);
              setTimeout(function () { window.location.reload(); }, 400);
            });
          })
          .catch(function () {});
      });
    });
  }

  // ── Bootstrap toasts (flash messages) ───────────────────────────
  function initFlashToasts() {
    document.querySelectorAll('.app-toast').forEach(function (el) {
      if (global.bootstrap) bootstrap.Toast.getOrCreateInstance(el).show();
    });
  }

  // ── Init ─────────────────────────────────────────────────────────
  UI.init = function () {
    initMobileMenu();
    initPodSwitcher();
    initFlashToasts();
  };

  global.CommandersohnUI = UI;

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', UI.init);
  } else {
    UI.init();
  }
}(window));
