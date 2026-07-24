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
        sortBtns.forEach(function (b) { b.classList.remove('is-active'); });
        btn.classList.add('is-active');
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

  // ── Expandable game activity ─────────────────────────────────────
  function initGameExpand() {
    var openRegionId = null;

    document.addEventListener('click', function (e) {
      var btn = e.target.closest('.game-expand-btn');
      if (!btn) return;
      e.preventDefault();
      e.stopPropagation();

      var controlsId = btn.getAttribute('aria-controls');
      var region = controlsId ? document.getElementById(controlsId) : null;
      if (!region) return;

      var isOpen = btn.getAttribute('aria-expanded') === 'true';

      // Close previously open region
      if (openRegionId && openRegionId !== controlsId) {
        var prev = document.getElementById(openRegionId);
        var prevBtn = document.querySelector('[aria-controls="' + openRegionId + '"]');
        if (prev) { prev.setAttribute('aria-hidden', 'true'); prev.classList.remove('is-open'); }
        if (prevBtn) { prevBtn.setAttribute('aria-expanded', 'false'); prevBtn.textContent = 'Details'; }
      }

      if (isOpen) {
        region.setAttribute('aria-hidden', 'true');
        region.classList.remove('is-open');
        btn.setAttribute('aria-expanded', 'false');
        btn.textContent = 'Details';
        openRegionId = null;
      } else {
        region.setAttribute('aria-hidden', 'false');
        region.classList.add('is-open');
        btn.setAttribute('aria-expanded', 'true');
        btn.textContent = 'Close';
        openRegionId = controlsId;
      }
    });
  }

  // ── Compare tray ─────────────────────────────────────────────────
  function initCompareTray() {
    var tray = document.getElementById('compare-tray');
    var slots = document.getElementById('compare-tray-slots');
    var goLink = document.getElementById('compare-tray-link');
    var clearBtn = document.getElementById('compare-tray-clear');
    if (!tray || !slots || !goLink) return;

    var KEY = 'cmp_players';
    var MAX = 2;

    function load() {
      try { return JSON.parse(sessionStorage.getItem(KEY) || '[]'); } catch (_) { return []; }
    }

    function save(players) {
      sessionStorage.setItem(KEY, JSON.stringify(players.slice(0, MAX)));
    }

    function render() {
      var players = load();
      slots.innerHTML = '';

      if (players.length === 0) {
        var empty = document.createElement('span');
        empty.className = 'compare-tray__empty';
        empty.textContent = 'Add players to compare';
        slots.appendChild(empty);
        goLink.setAttribute('aria-disabled', 'true');
        goLink.href = '/compare';
      } else {
        players.forEach(function (pl) {
          var chip = document.createElement('span');
          chip.className = 'compare-chip';
          chip.textContent = pl.name;
          var rm = document.createElement('button');
          rm.type = 'button';
          rm.className = 'compare-chip__remove';
          rm.setAttribute('aria-label', 'Remove ' + pl.name);
          rm.textContent = '×';
          rm.addEventListener('click', function () {
            save(load().filter(function (p) { return p.id !== pl.id; }));
            render();
          });
          chip.appendChild(rm);
          slots.appendChild(chip);
        });

        if (players.length === MAX) {
          goLink.removeAttribute('aria-disabled');
          goLink.href = '/compare?a=' + players[0].id + '&b=' + players[1].id;
        } else {
          goLink.setAttribute('aria-disabled', 'true');
          goLink.href = '/compare';
        }
      }

      if (players.length > 0) {
        tray.setAttribute('aria-hidden', 'false');
        tray.classList.add('is-visible');
      } else {
        tray.setAttribute('aria-hidden', 'true');
        tray.classList.remove('is-visible');
      }
    }

    document.addEventListener('click', function (e) {
      var btn = e.target.closest('[data-compare-player]');
      if (!btn) return;
      e.preventDefault();
      var id = parseInt(btn.getAttribute('data-compare-player'), 10);
      var name = btn.getAttribute('data-compare-name') || ('Player ' + id);
      var players = load();
      if (players.some(function (p) { return p.id === id; })) return;
      if (players.length >= MAX) {
        players = players.slice(1);
      }
      players.push({ id: id, name: name });
      save(players);
      render();
    });

    if (clearBtn) {
      clearBtn.addEventListener('click', function () {
        save([]);
        render();
      });
    }

    // Prevent disabled link navigation
    goLink.addEventListener('click', function (e) {
      if (goLink.getAttribute('aria-disabled') === 'true') e.preventDefault();
    });

    // Hide tray when virtual keyboard opens so it doesn't overlap the active field
    document.addEventListener('focusin', function (e) {
      if (e.target.matches('input, textarea, [contenteditable]')) {
        document.body.classList.add('is-keyboard-active');
      }
    });
    document.addEventListener('focusout', function () {
      setTimeout(function () {
        if (!document.activeElement ||
            !document.activeElement.matches('input, textarea, [contenteditable]')) {
          document.body.classList.remove('is-keyboard-active');
        }
      }, 0);
    });

    render();
  }

  // ── Entity detail drawer ─────────────────────────────────────────
  function initEntityDrawer() {
    var drawer = document.getElementById('entity-drawer');
    var backdrop = document.getElementById('entity-drawer-backdrop');
    var closeBtn = document.getElementById('entity-drawer-close');
    var title = document.getElementById('entity-drawer-title');
    var fullLink = document.getElementById('entity-drawer-full-link');
    var loading = document.getElementById('entity-drawer-loading');
    var errorEl = document.getElementById('entity-drawer-error');
    var body = document.getElementById('entity-drawer-body');
    if (!drawer || !closeBtn || !body) return;

    var _previousFocus = null;
    var _currentFetch = null;

    var API_PATHS = {
      player: '/api/players/',
      deck: '/api/decks/',
      game: '/api/games/',
    };

    function openDrawer() {
      _previousFocus = document.activeElement;
      drawer.setAttribute('aria-hidden', 'false');
      drawer.classList.add('is-open');
      document.body.style.overflow = 'hidden';
      closeBtn.focus();
    }

    function closeDrawer() {
      drawer.setAttribute('aria-hidden', 'true');
      drawer.classList.remove('is-open');
      document.body.style.overflow = '';
      if (_currentFetch) { _currentFetch = null; }
      if (_previousFocus) { _previousFocus.focus(); _previousFocus = null; }
      if (window.history && window.history.state && window.history.state.__drawerOpen) {
        window.history.back();
      }
    }

    function setLoading(on) {
      loading.setAttribute('aria-hidden', on ? 'false' : 'true');
      body.setAttribute('aria-hidden', on ? 'true' : 'false');
      errorEl.setAttribute('aria-hidden', 'true');
    }

    function setError(msg) {
      loading.setAttribute('aria-hidden', 'true');
      body.setAttribute('aria-hidden', 'true');
      errorEl.setAttribute('aria-hidden', 'false');
      errorEl.textContent = msg || 'Could not load details.';
    }

    function esc(str) {
      var d = document.createElement('div');
      d.textContent = str != null ? String(str) : '';
      return d.innerHTML;
    }

    function renderPlayer(data) {
      title.textContent = data.name || 'Player';
      if (fullLink) {
        fullLink.href = data.full_page_url || ('#');
      }

      var winrateClass = data.winrate >= 60 ? 'good' : (data.winrate >= 45 ? 'mid' : 'bad');
      var html = '';

      html += '<div class="drawer-hero" style="--player-accent:' + esc(data.accent) + '">';
      html += '<div class="drawer-avatar">' + esc((data.name || '?')[0].toUpperCase()) + '</div>';
      html += '<div><div class="drawer-name">' + esc(data.name) + '</div>';
      html += '<div class="drawer-stats">';
      html += '<span><b>' + esc(data.games_won) + '</b> wins</span>';
      html += '<span><b>' + esc(data.games_played) + '</b> games</span>';
      html += '<span class="win-badge ' + winrateClass + '">' + esc(data.winrate) + '%</span>';
      html += '</div></div></div>';

      if (data.decks && data.decks.length) {
        html += '<div class="drawer-section-label">Decks</div>';
        html += '<div class="drawer-list">';
        data.decks.forEach(function (d) {
          if (d.retired || d.planned) return;
          var wr = d.winrate >= 60 ? 'good' : (d.winrate >= 45 ? 'mid' : 'bad');
          html += '<a class="drawer-row" href="/deck/' + esc(d.id) + '" data-entity-type="deck" data-entity-id="' + esc(d.id) + '">';
          if (d.art_url) {
            html += '<div class="drawer-row-art" style="background-image:url(\'' + esc(d.art_url) + '\')"></div>';
          }
          html += '<div class="drawer-row-body"><div class="drawer-row-name">' + esc(d.name) + '</div>';
          html += '<div class="drawer-row-meta">' + esc(d.commander) + '</div></div>';
          html += '<span class="win-badge ' + wr + '">' + esc(d.winrate) + '%</span>';
          html += '</a>';
        });
        html += '</div>';
      }

      if (data.recent_games && data.recent_games.length) {
        html += '<div class="drawer-section-label">Recent Games</div>';
        html += '<div class="drawer-list">';
        data.recent_games.slice(0, 5).forEach(function (g) {
          html += '<a class="drawer-row" href="/games/' + esc(g.game_id) + '" data-entity-type="game" data-entity-id="' + esc(g.game_id) + '">';
          html += '<div class="drawer-row-body">';
          html += '<div class="drawer-row-name">' + esc(g.deck_name) + '</div>';
          html += '<div class="drawer-row-meta">' + esc((g.date || '').substring(0, 10)) + '</div>';
          html += '</div>';
          if (g.won) {
            html += '<span class="win-badge good">W</span>';
          } else {
            html += '<span class="win-badge bad">L</span>';
          }
          html += '</a>';
        });
        html += '</div>';
      }

      setBodyHtml(html);
    }

    function renderDeck(data) {
      title.textContent = data.name || 'Deck';
      if (fullLink) fullLink.href = data.full_page_url || '#';

      var winrateClass = data.winrate >= 60 ? 'good' : (data.winrate >= 45 ? 'mid' : 'bad');
      var html = '';

      html += '<div class="drawer-hero" style="--player-accent:' + esc(data.owner_accent) + '">';
      if (data.art_url) {
        html += '<div class="drawer-deck-art" style="background-image:url(\'' + esc(data.art_url) + '\')"></div>';
      }
      html += '<div><div class="drawer-name">' + esc(data.name) + '</div>';
      html += '<div class="drawer-row-meta">' + esc(data.commander) + '</div>';
      html += '<div class="drawer-stats">';
      html += '<span><b>' + esc(data.wins) + '</b> wins</span>';
      html += '<span><b>' + esc(data.uses) + '</b> games</span>';
      html += '<span class="win-badge ' + winrateClass + '">' + esc(data.winrate) + '%</span>';
      html += '<span><b>' + esc(data.mmr) + '</b> MMR</span>';
      html += '</div>';
      html += '<div class="drawer-row-meta">Owner: <a href="/player/' + esc(data.player_id) + '">' + esc(data.player_name) + '</a></div>';
      html += '</div></div>';

      if (data.recent_games && data.recent_games.length) {
        html += '<div class="drawer-section-label">Recent Games</div>';
        html += '<div class="drawer-list">';
        data.recent_games.slice(0, 5).forEach(function (g) {
          html += '<a class="drawer-row" href="/games/' + esc(g.game_id) + '" data-entity-type="game" data-entity-id="' + esc(g.game_id) + '">';
          html += '<div class="drawer-row-body">';
          html += '<div class="drawer-row-name">' + esc((g.date || '').substring(0, 10)) + '</div>';
          html += '</div>';
          if (g.won) {
            html += '<span class="win-badge good">W</span>';
          } else {
            html += '<span class="win-badge bad">L</span>';
          }
          html += '</a>';
        });
        html += '</div>';
      }

      setBodyHtml(html);
    }

    function renderGame(data) {
      var dateStr = (data.date || '').substring(0, 10);
      title.textContent = 'Game · ' + dateStr;
      if (fullLink) fullLink.href = data.full_page_url || '#';

      var html = '';
      html += '<div class="drawer-section-label">Winner</div>';
      html += '<div class="drawer-hero">';
      html += '<a href="/player/' + esc(data.winner.id) + '" class="drawer-name">' + esc(data.winner.name) + '</a>';
      html += '<span class="win-badge good ms-2">W</span>';
      if (data.ending_turn != null) {
        html += '<span class="drawer-row-meta ms-2">Turn ' + esc(data.ending_turn) + '</span>';
      }
      html += '</div>';

      if (data.participants && data.participants.length) {
        html += '<div class="drawer-section-label">Participants</div>';
        html += '<div class="drawer-list">';
        data.participants.forEach(function (gp) {
          var isWinner = gp.won;
          html += '<div class="drawer-row" data-entity-type="player" data-entity-id="' + esc(gp.player_id) + '"';
          html += ' style="--player-accent:' + esc(gp.player_accent) + '">';
          html += '<div class="drawer-avatar drawer-avatar--sm">' + esc((gp.player_name || '?')[0]) + '</div>';
          html += '<div class="drawer-row-body">';
          html += '<div class="drawer-row-name"><a href="' + esc(gp.player_url) + '">' + esc(gp.player_name) + '</a></div>';
          if (gp.art_url) {
            html += '<div class="drawer-row-meta"><a href="' + esc(gp.deck_url || '#') + '">' + esc(gp.deck_name) + '</a></div>';
          } else {
            html += '<div class="drawer-row-meta"><a href="' + esc(gp.deck_url || '#') + '">' + esc(gp.deck_name) + '</a></div>';
          }
          html += '</div>';
          if (gp.mmr_delta != null && gp.mmr_delta !== 0) {
            var sign = gp.mmr_delta > 0 ? '+' : '';
            var dClass = gp.mmr_delta > 0 ? 'good' : 'bad';
            html += '<span class="win-badge ' + dClass + '">' + sign + esc(gp.mmr_delta) + '</span>';
          }
          if (isWinner) {
            html += '<span class="win-badge good">W</span>';
          }
          html += '</div>';
        });
        html += '</div>';
      }

      setBodyHtml(html);
    }

    function setBodyHtml(html) {
      body.innerHTML = html;
      loading.setAttribute('aria-hidden', 'true');
      body.setAttribute('aria-hidden', 'false');
      errorEl.setAttribute('aria-hidden', 'true');
    }

    function fetchAndRender(type, id) {
      var apiBase = API_PATHS[type];
      if (!apiBase) return;
      setLoading(true);
      openDrawer();

      var token = _currentFetch = {};
      fetch(apiBase + id, {
        headers: { 'X-Requested-With': 'XMLHttpRequest' },
        credentials: 'same-origin',
      }).then(function (resp) {
        if (token !== _currentFetch) return;
        if (!resp.ok) {
          setError('Could not load ' + type + ' details (HTTP ' + resp.status + ').');
          return;
        }
        return resp.json().then(function (data) {
          if (token !== _currentFetch) return;
          if (type === 'player') renderPlayer(data);
          else if (type === 'deck') renderDeck(data);
          else if (type === 'game') renderGame(data);
          if (window.history && window.history.pushState) {
            window.history.pushState({ __drawerOpen: true, type: type, id: id }, '');
          }
        });
      }).catch(function () {
        if (token !== _currentFetch) return;
        setError('Network error loading details.');
      });
    }

    // Intercept entity link clicks — open drawer for primary clicks,
    // allow Ctrl/Cmd/middle/shift to pass through to the full page.
    document.addEventListener('click', function (e) {
      if (e.ctrlKey || e.metaKey || e.shiftKey || e.button !== 0) return;
      var trigger = e.target.closest('[data-entity-type][data-entity-id][href]');
      if (!trigger) return;
      var type = trigger.getAttribute('data-entity-type');
      var id = trigger.getAttribute('data-entity-id');
      if (!API_PATHS[type] || !id) return;
      // Only intercept if it's a real navigation link (not a table row or non-anchor)
      if (trigger.tagName !== 'A') return;
      e.preventDefault();
      fetchAndRender(type, id);
    }, true);

    closeBtn.addEventListener('click', closeDrawer);
    if (backdrop) backdrop.addEventListener('click', closeDrawer);

    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape' && drawer.classList.contains('is-open')) {
        e.preventDefault();
        closeDrawer();
      }
      // Basic focus trap inside open drawer
      if (!drawer.classList.contains('is-open')) return;
      if (e.key !== 'Tab') return;
      var focusable = Array.prototype.slice.call(
        drawer.querySelectorAll('a[href], button:not([disabled]), [tabindex="0"]')
      );
      if (!focusable.length) return;
      var first = focusable[0];
      var last = focusable[focusable.length - 1];
      if (e.shiftKey) {
        if (document.activeElement === first) { e.preventDefault(); last.focus(); }
      } else {
        if (document.activeElement === last) { e.preventDefault(); first.focus(); }
      }
    });

    window.addEventListener('popstate', function (e) {
      if (drawer.classList.contains('is-open')) {
        drawer.setAttribute('aria-hidden', 'true');
        drawer.classList.remove('is-open');
        document.body.style.overflow = '';
        if (_previousFocus) { _previousFocus.focus(); _previousFocus = null; }
      }
    });
  }

  // ── Command Palette ───────────────────────────────────────────────
  function initCommandPalette() {
    var palette = document.getElementById('command-palette');
    if (!palette) return;

    var input     = document.getElementById('palette-input');
    var results   = document.getElementById('palette-results');
    var backdrop  = document.getElementById('palette-backdrop');
    var trigger       = document.getElementById('palette-trigger');
    var mobileTrigger = document.getElementById('palette-trigger-mobile');

    var _open        = false;
    var _debounce    = null;
    var _activeXhr   = null;
    var _activeIdx   = -1;
    var DEBOUNCE_MS  = 180;

    function openPalette() {
      if (_open) return;
      _open = true;
      palette.setAttribute('aria-hidden', 'false');
      palette.classList.add('is-open');
      document.body.style.overflow = 'hidden';
      input.value = '';
      results.innerHTML = '';
      _activeIdx = -1;
      input.focus();
    }

    function closePalette() {
      if (!_open) return;
      _open = false;
      palette.setAttribute('aria-hidden', 'true');
      palette.classList.remove('is-open');
      document.body.style.overflow = '';
      if (_debounce) { clearTimeout(_debounce); _debounce = null; }
      if (_activeXhr) { _activeXhr.abort(); _activeXhr = null; }
      var focusTarget = (trigger && trigger.offsetParent !== null)
        ? trigger
        : (mobileTrigger || null);
      if (focusTarget) focusTarget.focus();
    }

    function esc(s) {
      var d = document.createElement('div');
      d.appendChild(document.createTextNode(String(s)));
      return d.innerHTML;
    }

    function renderSection(label, items, makeRow) {
      if (!items || !items.length) return '';
      var rows = items.map(makeRow).join('');
      return '<div class="palette-section"><div class="palette-section__label">' + esc(label) + '</div>' + rows + '</div>';
    }

    function renderResults(data) {
      var html = '';
      html += renderSection('Players', data.players, function (p) {
        return '<a class="palette-row" role="option" href="' + esc(p.url) + '"'
             + ' data-entity-type="player" data-entity-id="' + esc(p.id) + '"'
             + ' style="--player-accent:' + esc(p.accent) + '">'
             + '<span class="palette-row__avatar" aria-hidden="true">' + esc(p.name[0] || '?') + '</span>'
             + '<span class="palette-row__name">' + esc(p.name) + '</span>'
             + '</a>';
      });
      html += renderSection('Decks', data.decks, function (d) {
        var badge = d.retired ? ' <span class="palette-row__badge">Retired</span>' : (d.planned ? ' <span class="palette-row__badge">Planned</span>' : '');
        var cmdrLabel = d.commander_match
          ? '<span class="palette-row__cmdr-match">Cmdr: ' + esc(d.commander) + '</span>'
          : '<span class="palette-row__meta">' + esc(d.commander || '') + '</span>';
        return '<a class="palette-row" role="option" href="' + esc(d.url) + '"'
             + ' data-entity-type="deck" data-entity-id="' + esc(d.id) + '">'
             + '<span class="palette-row__name">' + esc(d.name) + badge + '</span>'
             + cmdrLabel
             + '</a>';
      });
      html += renderSection('Actions', data.actions, function (a) {
        return '<a class="palette-row palette-row--action" role="option" href="' + esc(a.url) + '">'
             + '<span class="palette-row__name">' + esc(a.label) + '</span>'
             + '</a>';
      });
      results.innerHTML = html || '<div class="palette-empty">No results</div>';
      _activeIdx = -1;
    }

    function fetchResults(q) {
      if (_activeXhr) { _activeXhr.abort(); _activeXhr = null; }
      var xhr = new XMLHttpRequest();
      _activeXhr = xhr;
      xhr.open('GET', '/api/search?q=' + encodeURIComponent(q));
      xhr.onload = function () {
        if (xhr.status === 200) {
          try {
            renderResults(JSON.parse(xhr.responseText));
          } catch (_) {}
        }
        _activeXhr = null;
      };
      xhr.onerror = function () { _activeXhr = null; };
      xhr.send();
    }

    function navigateList(dir) {
      var rows = results.querySelectorAll('.palette-row');
      if (!rows.length) return;
      _activeIdx = (_activeIdx + dir + rows.length) % rows.length;
      rows.forEach(function (r, i) {
        r.classList.toggle('is-active', i === _activeIdx);
        if (i === _activeIdx) r.scrollIntoView({ block: 'nearest' });
      });
    }

    function confirmActive() {
      var rows = results.querySelectorAll('.palette-row');
      if (_activeIdx >= 0 && rows[_activeIdx]) {
        rows[_activeIdx].click();
      }
    }

    if (trigger) trigger.addEventListener('click', openPalette);
    if (mobileTrigger) mobileTrigger.addEventListener('click', openPalette);

    if (backdrop) {
      backdrop.addEventListener('click', closePalette);
    }

    document.addEventListener('keydown', function (e) {
      if ((e.ctrlKey || e.metaKey) && e.key === 'k') {
        e.preventDefault();
        _open ? closePalette() : openPalette();
        return;
      }
      if (!_open) return;
      if (e.key === 'Escape') { e.preventDefault(); closePalette(); return; }
      if (e.key === 'ArrowDown') { e.preventDefault(); navigateList(1); return; }
      if (e.key === 'ArrowUp') { e.preventDefault(); navigateList(-1); return; }
      if (e.key === 'Enter') { e.preventDefault(); confirmActive(); return; }
    });

    input.addEventListener('input', function () {
      var q = input.value.trim();
      if (_debounce) clearTimeout(_debounce);
      _debounce = setTimeout(function () {
        fetchResults(q);
      }, DEBOUNCE_MS);
    });

    results.addEventListener('click', function (e) {
      var row = e.target.closest('.palette-row');
      if (row) closePalette();
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
    initGameExpand();
    initCompareTray();
    initEntityDrawer();
    initCommandPalette();
  };

  global.CommandersohnUI = UI;

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', UI.init);
  } else {
    UI.init();
  }
}(window));
