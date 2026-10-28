/* Reader home page (`/read/`, mode == "projects").
 *
 * The header's language menu and theme button, the new-project modal, the status filter, the
 * review-category picker, the posters/cards toggle, badge fitting, and each
 * card's ⋮ menu. Lifted out of the template's
 * inline <script> when the picker landed; strings arrive via
 * window.__i18n_projects, the same way the reader gets window.__i18n.
 *
 * A card's status is derived server-side from its files, so nothing here sets
 * one — the only status the reader chooses is archived, via the ⋮ menu.
 */
(function () {
    'use strict';

    var i = window.__i18n_projects || {};

    /* ── Badge fitting ──
     *
     * The server renders every badge a card has, most important first. How many
     * show depends on the view and the room: a poster keeps one row of at most
     * two, a card up to two rows of all of them. When a row would overflow, the
     * last badge steps down — icon + number, then icon only, then gone — before
     * the one ahead of it is touched. The first badge is never dropped; it
     * shortens to icon + number only as a last resort. A flags badge at zero
     * (`data-zero`, set by the category picker) is out of the running.
     */

    var grid = document.getElementById('project-grid');

    function fitLimits() {
        return grid && grid.classList.contains('library-cards')
            ? { rows: 2, max: Infinity }
            : { rows: 1, max: 2 };
    }

    function fitChips(row) {
        var lim = fitLimits();
        var live = [];
        Array.prototype.forEach.call(row.querySelectorAll('.chip'), function (c) {
            c.classList.remove('short', 'icon');
            var on = !c.hasAttribute('data-zero') && live.length < lim.max;
            c.hidden = !on;
            if (on) live.push(c);
        });
        row.hidden = live.length === 0;
        if (!live.length) return;

        function fits() {
            if (lim.rows === 1) return row.scrollWidth <= row.clientWidth + 0.5;
            var tops = {};
            live.forEach(function (c) { if (!c.hidden) tops[c.offsetTop] = true; });
            return Object.keys(tops).length <= lim.rows;
        }

        if (fits()) return;
        for (var k = live.length - 1; k >= 1; k--) {
            var c = live[k];
            if (c.dataset.short) {
                c.classList.add('short');
                if (fits()) return;
                c.classList.remove('short');
            }
            c.classList.add('icon');
            if (fits()) return;
            c.hidden = true;
            if (fits()) return;
        }
        if (live[0].dataset.short) live[0].classList.add('short');
    }

    function fitAll() {
        document.querySelectorAll('.project-card .chips').forEach(fitChips);
    }

    // A row's width changes with the viewport, the view, and a card coming back
    // from the status filter's display:none, so watch the rows themselves.
    if (window.ResizeObserver) {
        var ro = new ResizeObserver(function (entries) {
            entries.forEach(function (e) { fitChips(e.target); });
        });
        document.querySelectorAll('.project-card .chips').forEach(function (row) { ro.observe(row); });
    }
    fitAll();
    if (document.fonts && document.fonts.ready) document.fonts.ready.then(fitAll);

    // Badges sit above the tile's link so a shortened one still shows its full
    // text on hover, so they navigate themselves: to the row's data-href (the
    // tile's link, or the dashboard for a pending book's setup badges), in a
    // new tab on Ctrl/Cmd/Shift-click or middle-click, as a real link would.
    document.querySelectorAll('.project-card .chips[data-href]').forEach(function (row) {
        function go(e) {
            var newTab = e.button === 1 || e.ctrlKey || e.metaKey || e.shiftKey;
            if (e.button > 1) return;
            e.preventDefault();
            if (newTab) window.open(row.dataset.href, '_blank', 'noopener');
            else window.location.href = row.dataset.href;
        }
        row.addEventListener('click', go);
        row.addEventListener('auxclick', go);
    });

    /* ── View toggle (posters / cards) ── */
    (function () {
        var btns = Array.prototype.slice.call(document.querySelectorAll('.view-btn[data-view]'));
        if (!grid || !btns.length) return;
        btns.forEach(function (btn) {
            btn.addEventListener('click', function () {
                var view = btn.dataset.view;
                if (btn.getAttribute('aria-pressed') === 'true') return;
                grid.classList.remove('library-posters', 'library-cards');
                grid.classList.add('library-' + view);
                btns.forEach(function (b) {
                    b.setAttribute('aria-pressed', b === btn ? 'true' : 'false');
                });
                fitAll();
                fetch('/api/set-library-view', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ view: view })
                }).catch(function () { /* the view already switched; only the cookie is lost, so the next load opens the old view */ });
            });
        });
    })();

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

    /* ── UI language menu ── */
    (function () {
        var container = document.getElementById('lang-menu');
        var button = document.getElementById('lang-menu-btn');
        var panel = document.getElementById('lang-menu-popup');
        if (!container || !button || !panel) return;
        bindPopup(button, panel, container);

        panel.querySelectorAll('.lang-option[data-lang]').forEach(function (opt) {
            opt.addEventListener('click', function () {
                if (opt.classList.contains('active')) {
                    closeAllPopups(null);
                    return;
                }
                fetch('/api/set-lang', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ lang: opt.dataset.lang }),
                }).then(function () { location.reload(); })
                  .catch(function () { location.reload(); });
            });
        });
    })();

    /* ── Light/dark theme ──
     *
     * Written to localStorage.reader_theme, which the head script of every
     * reader template (the book page, the reader, the boards) applies before
     * first paint. The button shows a moon in light mode and a sun in dark;
     * CSS picks which from data-theme.
     */
    (function () {
        var btn = document.getElementById('theme-btn');
        if (!btn) return;
        function sync() {
            btn.setAttribute('aria-pressed',
                document.documentElement.dataset.theme === 'dark' ? 'true' : 'false');
        }
        sync();
        btn.addEventListener('click', function () {
            var next = document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark';
            document.documentElement.dataset.theme = next;
            try { localStorage.setItem('reader_theme', next); } catch (e) { /* this page only */ }
            sync();
        });
    })();

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
            .then(function (r) {
                return r.json().then(function (data) {
                    return { httpOk: r.ok, data: data };
                }, function () {
                    return { httpOk: r.ok, data: null };
                });
            })
            .then(function (result) {
                archiveBtn.disabled = false;
                if (!result.httpOk || !result.data || !result.data.ok) {
                    window.alert(i.archive_error || 'Could not update archive status.');
                    return;
                }
                var data = result.data;
                archiveBtn.dataset.archived = data.archived ? '1' : '0';
                label.textContent = data.archived ? i.unarchive : i.archive;
                card.classList.toggle('card-archived', data.archived);
                // Unarchiving lands on whichever status the files imply, so
                // take it from the response rather than guessing. (An archived
                // card hides its badges in CSS; unarchived ones return on reload.)
                card.classList.remove('st-' + card.dataset.status);
                card.classList.add('st-' + data.status);
                var pill = card.querySelector('.status-pill');
                if (pill && i.statuses && i.statuses[data.status]) pill.textContent = i.statuses[data.status];
                card.dataset.status = data.status;
                api.close();
                card.style.display = showsStatus(data.status) ? '' : 'none';
            })
            .catch(function () {
                archiveBtn.disabled = false;
                window.alert(i.archive_error || 'Could not update archive status.');
            });
        });
    });

    /* ── Review-category picker ──
     *
     * The counts for every category ship with the page in each card's
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

        var eyeOff = optionsBtn.querySelector('.filter-eyeoff');

        function resum() {
            // Nothing ticked means nothing counted: the ticked types are the
            // whole review setting, here, on the book page and in the reader.
            var chosen = selectedTypes();
            // An <svg> has no `hidden` property; toggle the attribute the CSS keys on.
            if (eyeOff) eyeOff.toggleAttribute('hidden', chosen.length === typeCbs.length);

            document.querySelectorAll('.project-card[data-flags]').forEach(function (card) {
                var chip = card.querySelector('.project-chip-flags');
                if (!chip) return;   // no flags badge for this status at all
                var counts = {};
                try { counts = JSON.parse(card.dataset.flags) || {}; } catch (e) { counts = {}; }
                var total = chosen.reduce(function (sum, t) { return sum + (counts[t] || 0); }, 0);
                var label = total === 1 ? i.flag_one : i.flag_many;
                chip.querySelector('.chip-count').textContent = total;
                chip.querySelector('.chip-label').textContent = label;
                chip.title = total + ' ' + label;
                if (total) chip.removeAttribute('data-zero');
                else chip.setAttribute('data-zero', '');
                fitChips(chip.parentNode);
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
