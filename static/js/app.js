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

  // ── Leaderboard metric switching ─────────────────────────────────
  function initLeaderboardMetrics() {
    var section = document.getElementById('leaderboard');
    if (!section) return;

    var btns = section.querySelectorAll('.metric-btn');
    var tbody = document.getElementById('leaderboard-tbody');
    var podium = document.getElementById('leaderboard-podium');
    var sortLabel = document.getElementById('leaderboard-sort-label');

    function getMetricVal(row, metric) {
      return parseFloat(row.getAttribute('data-' + metric) || '0');
    }

    function sortByMetric(metric) {
      // Update sort label
      var labels = { wins: 'wins', mmr: 'MMR', winrate: 'win rate' };
      if (sortLabel) sortLabel.textContent = labels[metric] || metric;

      // Sort table rows
      if (tbody) {
        var rows = Array.prototype.slice.call(tbody.querySelectorAll('tr[data-entity-type="player"]'));
        rows.sort(function (a, b) {
          return getMetricVal(b, metric) - getMetricVal(a, metric);
        });
        rows.forEach(function (row, i) {
          var rankCell = row.querySelector('.rank-pill');
          if (rankCell) rankCell.textContent = i + 1;
          tbody.appendChild(row);
        });
      }

      // Reorder podium cards
      if (podium) {
        var cards = Array.prototype.slice.call(podium.querySelectorAll('[data-entity-type="player"]'));
        cards.sort(function (a, b) {
          return getMetricVal(b, metric) - getMetricVal(a, metric);
        });
        var ranks = ['podium-rank-1', 'podium-rank-2', 'podium-rank-3'];
        cards.forEach(function (card, i) {
          ranks.forEach(function (cls) { card.classList.remove(cls); });
          if (ranks[i]) card.classList.add(ranks[i]);
          podium.appendChild(card);
        });
      }
    }

    btns.forEach(function (btn) {
      btn.addEventListener('click', function () {
        var metric = btn.getAttribute('data-metric');
        btns.forEach(function (b) { b.classList.remove('is-active'); });
        btn.classList.add('is-active');
        sortByMetric(metric);
      });
    });
  }

  // ── Deck sort switching ──────────────────────────────────────────
  function initDeckSort() {
    var grid = document.getElementById('decks-in-form-grid');
    if (!grid) return;

    var sortBtns = document.querySelectorAll('[data-deck-sort]');

    function sortDecks(metric) {
      var tiles = Array.prototype.slice.call(grid.querySelectorAll('[data-entity-type="deck"]'));
      tiles.sort(function (a, b) {
        return parseFloat(b.getAttribute('data-' + metric) || '0') -
               parseFloat(a.getAttribute('data-' + metric) || '0');
      });
      tiles.forEach(function (tile) { grid.appendChild(tile); });
    }

    sortBtns.forEach(function (btn) {
      btn.addEventListener('click', function () {
        var metric = btn.getAttribute('data-deck-sort');
        sortBtns.forEach(function (b) {
          b.classList.remove('btn-primary');
          b.classList.add('btn-outline-light');
        });
        btn.classList.remove('btn-outline-light');
        btn.classList.add('btn-primary');
        sortDecks(metric);
      });
    });
  }

  // ── Connected entity selection / highlighting ────────────────────
  function initEntityHighlighting() {
    var ALL_ENTITY_TYPES = ['player', 'deck', 'game'];

    function getEntityEls(type, id) {
      return document.querySelectorAll('[data-entity-type="' + type + '"][data-entity-id="' + id + '"]');
    }

    function getAllEntities() {
      return document.querySelectorAll('[data-entity-type]');
    }

    function clearHighlights() {
      getAllEntities().forEach(function (el) {
        el.classList.remove('is-selected', 'is-related', 'is-dimmed');
      });
    }

    // When a player element is selected, dim unrelated decks in recent games
    function highlightPlayer(playerId) {
      var allEntities = getAllEntities();

      // Dim everything first
      allEntities.forEach(function (el) {
        el.classList.add('is-dimmed');
      });

      // Mark selected player rows/cards
      getEntityEls('player', playerId).forEach(function (el) {
        el.classList.remove('is-dimmed');
        el.classList.add('is-selected');
      });

      // Mark player's own deck tiles as related using leaderboard rows data-wins
      // (we can't directly map player->deck here without server data, so we
      //  highlight game rows where the player appears)
      document.querySelectorAll('.player-pill[data-entity-id="' + playerId + '"]').forEach(function (pill) {
        var gameTile = pill.closest('[data-entity-type="game"]');
        if (gameTile) {
          gameTile.classList.remove('is-dimmed');
          gameTile.classList.add('is-related');
        }
      });
    }

    function handleEntityClick(e) {
      var target = e.target.closest('[data-entity-type]');
      if (!target) {
        clearHighlights();
        return;
      }

      // If already selected, deselect
      if (target.classList.contains('is-selected')) {
        clearHighlights();
        return;
      }

      var type = target.getAttribute('data-entity-type');
      var id = target.getAttribute('data-entity-id');

      clearHighlights();

      if (type === 'player') {
        highlightPlayer(id);
        e.preventDefault();
      } else {
        // For deck and game entities, just highlight the group
        getAllEntities().forEach(function (el) {
          el.classList.add('is-dimmed');
        });
        getEntityEls(type, id).forEach(function (el) {
          el.classList.remove('is-dimmed');
          el.classList.add('is-selected');
        });
        // Don't preventDefault on deck/game — let the link navigate
      }
    }

    // Only intercept clicks that are on entity containers, not their child links
    document.addEventListener('click', function (e) {
      var entityEl = e.target.closest('[data-entity-type]');
      if (!entityEl) {
        // Click outside any entity — clear
        if (document.querySelector('.is-selected')) clearHighlights();
        return;
      }
      // Only intercept player entities for selection; deck/game navigate normally
      var type = entityEl.getAttribute('data-entity-type');
      if (type === 'player') {
        // Only if the click was on the container itself, not a child link going to player detail
        var link = e.target.closest('a');
        if (!link || link === entityEl) {
          handleEntityClick(e);
        }
      }
    });

    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape') clearHighlights();
    });
  }

  // ── Init ─────────────────────────────────────────────────────────
  UI.init = function () {
    initMobileMenu();
    initPodSwitcher();
    initFlashToasts();
    initLeaderboardMetrics();
    initDeckSort();
    initEntityHighlighting();
  };

  global.CommandersohnUI = UI;

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', UI.init);
  } else {
    UI.init();
  }
}(window));
