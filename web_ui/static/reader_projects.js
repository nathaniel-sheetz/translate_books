/* Reader home page (`/read/`, mode == "projects").
 *
 * Language + sheet-layout toggles, the new-project modal, the status filter,
 * the status dropdown, and the review-category picker. Lifted out of the
 * template's inline <script> when the picker landed; strings arrive via
 * window.__i18n_projects, the same way the reader gets window.__i18n.
 */
(function () {
    'use strict';

    var i = window.__i18n_projects || {};

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

    /* ── Status filter ── */

    var filterBar = document.getElementById('filter-bar');

    function activeStatuses() {
        if (!filterBar) return [];
        var active = [];
        filterBar.querySelectorAll('.filter-btn[data-status]').forEach(function (b) {
            if (b.dataset.status !== 'all' && b.classList.contains('active')) {
                active.push(b.dataset.status);
            }
        });
        return active;
    }

    (function () {
        if (!filterBar) return;
        var buttons = filterBar.querySelectorAll('.filter-btn[data-status]');
        var allBtn = filterBar.querySelector('[data-status="all"]');
        var cards = document.querySelectorAll('.project-card[data-status]');

        function applyFilter() {
            var active = activeStatuses();
            allBtn.classList.toggle('active', active.length === 4);
            var noneActive = active.length === 0;
            cards.forEach(function (card) {
                card.style.display =
                    (noneActive || active.indexOf(card.dataset.status) !== -1) ? '' : 'none';
            });
        }

        buttons.forEach(function (btn) {
            btn.addEventListener('click', function () {
                if (btn.dataset.status === 'all') {
                    var turnOn = !btn.classList.contains('active');
                    buttons.forEach(function (b) { b.classList.toggle('active', turnOn); });
                } else {
                    btn.classList.toggle('active');
                    allBtn.classList.toggle('active', activeStatuses().length === 4);
                }
                applyFilter();
            });
        });

        applyFilter();
    })();

    /* ── Status dropdown change ── */

    document.querySelectorAll('.status-select').forEach(function (sel) {
        sel.addEventListener('change', function () {
            var newStatus = sel.value;
            var card = sel.closest('.project-card');
            fetch('/api/project/' + sel.dataset.projectId + '/status', {
                method: 'PATCH',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ status: newStatus })
            })
            .then(function (r) { return r.json(); })
            .then(function (data) {
                if (!data.ok) return;
                card.dataset.status = newStatus;
                sel.className = 'status-select status-' + newStatus;
                card.classList.toggle('card-archived', newStatus === 'archived');
                var active = activeStatuses();
                card.style.display =
                    (active.length === 0 || active.indexOf(newStatus) !== -1) ? '' : 'none';
            });
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
        var popupOpen = false;

        function setPopupOpen(open) {
            popupOpen = open;
            popup.hidden = !open;
            optionsBtn.setAttribute('aria-expanded', open ? 'true' : 'false');
            optionsBtn.classList.toggle('popup-open', open);
        }

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

        optionsBtn.addEventListener('click', function (e) {
            e.stopPropagation();
            setPopupOpen(!popupOpen);
        });

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

        document.addEventListener('click', function (e) {
            if (popupOpen && !group.contains(e.target)) setPopupOpen(false);
        });

        document.addEventListener('keydown', function (e) {
            if (e.key === 'Escape' && popupOpen) setPopupOpen(false);
        });
    })();
})();
