/* Per-book chapter list (`/read/<project_id>`, mode == "chapters").
 *
 * Theme toggle plus the review-mode switch and its category picker. Lifted out
 * of the template's inline <script> when the chapter rows grew a flags badge;
 * the project id arrives via window.__reader_chapters, the same way the home
 * page gets window.__i18n_projects.
 */
(function () {
    'use strict';

    var cfg = window.__reader_chapters || {};

    /* ── Theme toggle ── */
    (function () {
        var btn = document.getElementById('theme-toggle');
        if (!btn) return;
        var dark = document.documentElement.dataset.theme === 'dark';
        btn.textContent = dark ? '☀️' : '🌙';
        btn.addEventListener('click', function () {
            dark = !dark;
            document.documentElement.dataset.theme = dark ? 'dark' : 'light';
            localStorage.setItem('reader_theme', dark ? 'dark' : 'light');
            btn.textContent = dark ? '☀️' : '🌙';
        });
    })();

    /* ── Review mode selection ──
     *
     * The on/off switch is a per-book reading preference and stays in localStorage;
     * the *category* selection is global and lives in a cookie, so the home page,
     * every other book, and the reader all agree on it. The checkboxes therefore
     * render checked from the server and POST on change.
     *
     * The counts for every category ship with the page in each row's
     * data-flags, so re-summing on a checkbox change is instant and local — the
     * POST only persists the choice for the next load and for the other pages.
     */
    (function () {
        var KEY = 'reader_review:' + (cfg.projectId || '');
        var group = document.getElementById('review-topbar-group');
        var toggleBtn = document.getElementById('review-mode-toggle');
        var optionsBtn = document.getElementById('review-options-btn');
        var popup = document.getElementById('review-types-popup');
        if (!group || !toggleBtn || !optionsBtn || !popup) return;

        var typeCbs = Array.prototype.slice.call(popup.querySelectorAll('.review-type-cb'));
        var reviewOn = false;
        var popupOpen = false;

        function setPopupOpen(open) {
            popupOpen = open;
            popup.hidden = !open;
            toggleBtn.setAttribute('aria-expanded', open ? 'true' : 'false');
            optionsBtn.setAttribute('aria-expanded', open ? 'true' : 'false');
            optionsBtn.classList.toggle('popup-open', open);
        }

        function setReviewOn(on, opts) {
            opts = opts || {};
            reviewOn = on;
            toggleBtn.classList.toggle('review-on', on);
            toggleBtn.setAttribute('aria-pressed', on ? 'true' : 'false');
            optionsBtn.hidden = !on;
            if (!on) setPopupOpen(false);
            else if (opts.showPopup) setPopupOpen(true);
        }

        function selectedTypes() {
            return typeCbs.filter(function (cb) { return cb.checked; })
                          .map(function (cb) { return cb.value; });
        }

        function persist() {
            localStorage.setItem(KEY, JSON.stringify({ on: reviewOn }));
        }

        function persistTypes() {
            fetch('/api/set-review-types', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ types: selectedTypes() })
            }).catch(function () { /* keeps the current page's selection either way */ });
        }

        function resum() {
            // Nothing ticked reads as "no category filter" — the same convention
            // the home page uses, and what the server falls back to.
            var chosen = selectedTypes();
            if (!chosen.length) chosen = typeCbs.map(function (cb) { return cb.value; });

            document.querySelectorAll('.nav-list li[data-flags]').forEach(function (row) {
                var chip = row.querySelector('.chapter-chip-flags');
                if (!chip) return;
                var counts = {};
                try { counts = JSON.parse(row.dataset.flags) || {}; } catch (e) { counts = {}; }
                var total = chosen.reduce(function (sum, t) { return sum + (counts[t] || 0); }, 0);
                chip.querySelector('.chip-flag-count').textContent = total;
                chip.querySelector('.chip-flag-label').textContent =
                    total === 1 ? cfg.flag_one : cfg.flag_many;
                chip.hidden = total === 0;
                var clean = row.querySelector('.chapter-chip-clean');
                if (clean) clean.hidden = total !== 0;
            });
        }

        function loadSaved() {
            var saved = {};
            try { saved = JSON.parse(localStorage.getItem(KEY)) || {}; } catch (e) { saved = {}; }
            setReviewOn(!!saved.on, { showPopup: false });
        }

        toggleBtn.addEventListener('click', function (e) {
            e.stopPropagation();
            if (reviewOn) {
                setReviewOn(false);
            } else {
                setReviewOn(true, { showPopup: true });
            }
            persist();
        });

        optionsBtn.addEventListener('click', function (e) {
            e.stopPropagation();
            setPopupOpen(!popupOpen);
        });

        typeCbs.forEach(function (cb) {
            cb.addEventListener('change', function () {
                resum();
                persistTypes();
            });
        });

        document.addEventListener('click', function (e) {
            if (!popupOpen) return;
            if (!group.contains(e.target)) setPopupOpen(false);
        });

        document.addEventListener('keydown', function (e) {
            if (e.key === 'Escape' && popupOpen) setPopupOpen(false);
        });

        loadSaved();
        persist();
    })();
})();
