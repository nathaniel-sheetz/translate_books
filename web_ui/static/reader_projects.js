/* Reader home page (`/read/`, mode == "projects").
 *
 * Language + sheet-layout toggles, the new-project modal, the status filter,
 * the review-category picker, and each card's ⋮ menu. Lifted out of the
 * template's inline <script> when the picker landed; strings arrive via
 * window.__i18n_projects, the same way the reader gets window.__i18n.
 *
 * A card's status is derived server-side from its files, so nothing here sets
 * one — the only status the reader chooses is archived, via the ⋮ menu.
 */
(function () {
    'use strict';

    var i = window.__i18n_projects || {};

    /* ── Popups ──
     *
     * The status filter, the category picker and every card menu are the same
     * anchored popup: a button that toggles `hidden` on the panel beside it,
     * closing on an outside click or Escape. One registry so opening any popup
     * closes the rest.
     */

    var popups = [];

    function closeAllPopups(except) {
        popups.forEach(function (p) { if (p !== except) p.close(); });
    }

    function bindPopup(button, panel, container) {
        var api = {
            isOpen: false,
            set: function (open) {
                api.isOpen = open;
                panel.hidden = !open;
                button.setAttribute('aria-expanded', open ? 'true' : 'false');
                button.classList.toggle('popup-open', open);
            },
            close: function () { if (api.isOpen) api.set(false); },
            contains: function (node) { return container.contains(node); },
        };
        button.addEventListener('click', function (e) {
            e.stopPropagation();
            var open = !api.isOpen;
            closeAllPopups(api);
            api.set(open);
        });
        popups.push(api);
        return api;
    }

    document.addEventListener('click', function (e) {
        popups.forEach(function (p) {
            if (p.isOpen && !p.contains(e.target)) p.close();
        });
    });

    document.addEventListener('keydown', function (e) {
        if (e.key === 'Escape') closeAllPopups(null);
    });

    /* ── Settings toggles (language, reader sheet layout) ── */

    document.querySelectorAll('.lang-btn[data-lang]').forEach(function (btn) {
        btn.addEventListener('click', function () {
            fetch('/api/set-lang', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ lang: btn.dataset.lang }),
            }).then(function () { location.reload(); });
        });
    });

    document.querySelectorAll('.ui-version-toggle [data-ui-version]').forEach(function (btn) {
        btn.addEventListener('click', function () {
            if (btn.classList.contains('active')) return;
            fetch('/api/set-ui-version', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ version: btn.getAttribute('data-ui-version') }),
            }).then(function () { location.reload(); })
              .catch(function () { location.reload(); });
        });
    });

    /* ── New project modal ── */
    (function () {
        var overlay = document.getElementById('new-project-modal');
        var titleInput = document.getElementById('new-project-title');
        var errEl = document.getElementById('new-project-error');
        var openBtn = document.getElementById('btn-new-project');
        if (!overlay || !titleInput || !errEl || !openBtn) return;

        openBtn.addEventListener('click', function () {
            titleInput.value = '';
            errEl.style.display = 'none';
            overlay.style.display = 'flex';
            titleInput.focus();
        });

        document.getElementById('btn-cancel-project').addEventListener('click', function () {
            overlay.style.display = 'none';
        });

        overlay.addEventListener('click', function (e) {
            if (e.target === overlay) overlay.style.display = 'none';
        });

        titleInput.addEventListener('keydown', function (e) {
            if (e.key === 'Enter') document.getElementById('btn-create-project').click();
        });

        document.getElementById('btn-create-project').addEventListener('click', function () {
            var title = titleInput.value.trim();
            if (!title) {
                errEl.textContent = i.new_project_error || 'Please enter a title.';
                errEl.style.display = '';
                return;
            }
            var btn = this;
            btn.disabled = true;
            fetch('/api/projects/create', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ title: title })
            })
            .then(function (r) { return r.json(); })
            .then(function (data) {
                if (data.error) {
                    errEl.textContent = data.error;
                    errEl.style.display = '';
                    btn.disabled = false;
                    return;
                }
                window.location.href = data.redirect;
            })
            .catch(function (err) {
                errEl.textContent = err.message;
                errEl.style.display = '';
                btn.disabled = false;
            });
        });
    })();

    /* ── Status filter ──
     *
     * The server already rendered every card's derived status into data-status,
     * so filtering is a local show/hide; the POST only persists the choice
     * (globally, via cookie) for the next load.
     */

    var statusCbs = Array.prototype.slice.call(
        document.querySelectorAll('.status-filter-cb'));

    function selectedStatuses() {
        return statusCbs.filter(function (cb) { return cb.checked; })
                        .map(function (cb) { return cb.value; });
    }

    function showsStatus(status) {
        // Nothing ticked reads as "no filter" — the same convention the
        // category picker uses.
        var chosen = selectedStatuses();
        return chosen.length === 0 || chosen.indexOf(status) !== -1;
    }

    function applyStatusFilter() {
        document.querySelectorAll('.project-card[data-status]').forEach(function (card) {
            card.style.display = showsStatus(card.dataset.status) ? '' : 'none';
        });
    }

    (function () {
        var group = document.getElementById('status-filter-group');
        var btn = document.getElementById('status-filter-btn');
        var popup = document.getElementById('status-filter-popup');
        if (!group || !btn || !popup) return;

        bindPopup(btn, popup, group);

        statusCbs.forEach(function (cb) {
            cb.addEventListener('change', function () {
                applyStatusFilter();
                fetch('/api/set-status-filter', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ statuses: selectedStatuses() })
                }).catch(function () { /* the local filter already applied; retry next change */ });
            });
        });

        applyStatusFilter();
    })();

    /* ── Card ⋮ menu (dashboard + archive toggle) ── */

    document.querySelectorAll('.project-card .card-menu').forEach(function (menu) {
        var btn = menu.querySelector('.card-menu-btn');
        var popup = menu.querySelector('.card-menu-popup');
        var archiveBtn = menu.querySelector('.card-archive-btn');
        if (!btn || !popup || !archiveBtn) return;

        var api = bindPopup(btn, popup, menu);
        var card = menu.closest('.project-card');
        var label = archiveBtn.querySelector('.card-archive-label');

        archiveBtn.addEventListener('click', function () {
            var archived = archiveBtn.dataset.archived !== '1';
            archiveBtn.disabled = true;
            fetch('/api/project/' + archiveBtn.dataset.projectId + '/archived', {
                method: 'PATCH',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ archived: archived })
            })
            .then(function (r) { return r.json(); })
            .then(function (data) {
                archiveBtn.disabled = false;
                if (!data.ok) return;
                archiveBtn.dataset.archived = data.archived ? '1' : '0';
                label.textContent = data.archived ? i.unarchive : i.archive;
                card.classList.toggle('card-archived', data.archived);
                // Unarchiving lands on whichever status the files imply, so
                // take it from the response rather than guessing.
                card.dataset.status = data.status;
                api.close();
                card.style.display = showsStatus(data.status) ? '' : 'none';
            })
            .catch(function () { archiveBtn.disabled = false; });
        });
    });

    /* ── Review-category picker ──
     *
     * The counts for all six categories ship with the page in each card's
     * data-flags, so re-summing on a checkbox change is instant and local; the
     * POST only persists the choice (globally, via cookie) for the next load
     * and for the chapter-list page.
     */
    (function () {
        var group = document.getElementById('review-topbar-group');
        var optionsBtn = document.getElementById('review-options-btn');
        var popup = document.getElementById('review-types-popup');
        if (!group || !optionsBtn || !popup) return;

        var typeCbs = Array.prototype.slice.call(popup.querySelectorAll('.review-type-cb'));

        bindPopup(optionsBtn, popup, group);

        function selectedTypes() {
            return typeCbs.filter(function (cb) { return cb.checked; })
                          .map(function (cb) { return cb.value; });
        }

        function resum() {
            // Nothing ticked reads as "no category filter" — the same convention
            // the status filter uses, and what the server falls back to.
            var chosen = selectedTypes();
            if (!chosen.length) chosen = typeCbs.map(function (cb) { return cb.value; });

            document.querySelectorAll('.project-card[data-flags]').forEach(function (card) {
                var chip = card.querySelector('.project-chip-flags');
                if (!chip) return;   // nothing translated yet: no work chips at all
                var counts = {};
                try { counts = JSON.parse(card.dataset.flags) || {}; } catch (e) { counts = {}; }
                var total = chosen.reduce(function (sum, t) { return sum + (counts[t] || 0); }, 0);
                chip.querySelector('.chip-flag-count').textContent = total;
                chip.querySelector('.chip-flag-label').textContent =
                    total === 1 ? i.flag_one : i.flag_many;
                chip.hidden = total === 0;
                var clean = card.querySelector('.project-chip-clean');
                if (clean) clean.hidden = total !== 0;
            });
        }

        typeCbs.forEach(function (cb) {
            cb.addEventListener('change', function () {
                resum();
                fetch('/api/set-review-types', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ types: selectedTypes() })
                }).catch(function () { /* the re-sum already happened; retry next change */ });
            });
        });
    })();
})();
