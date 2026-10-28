/* Per-book chapter list (`/read/<project_id>`, mode == "chapters").
 *
 * The book's ⋮ menu, the Capítulos | Proceso tabs, the place you came back
 * to, and the flag chip with its type picker. The project id and strings
 * arrive via window.__reader_chapters, the same way the home page gets
 * window.__i18n_projects. The theme toggle lives on the home page now.
 */
(function () {
    'use strict';

    var cfg = window.__reader_chapters || {};

    /* ── Book ⋮ menu (recommendations, images, dashboard) ──
     *
     * The home page's card menu, one per page here. reader_projects.js's
     * bindPopup is not loaded on this page, so the open/close is spelled out
     * the same way the flag picker below does it.
     */
    (function () {
        var menu = document.getElementById('chapter-menu');
        var btn = document.getElementById('chapter-menu-btn');
        var popup = document.getElementById('chapter-menu-popup');
        if (!menu || !btn || !popup) return;

        var open = false;

        function setOpen(next) {
            open = next;
            popup.hidden = !next;
            btn.setAttribute('aria-expanded', next ? 'true' : 'false');
            btn.classList.toggle('popup-open', next);
        }

        btn.addEventListener('click', function () {
            setOpen(!open);
        });

        // Capture: the flag chip stops propagation, and a click on it should
        // still close this menu.
        document.addEventListener('click', function (e) {
            if (open && !menu.contains(e.target)) setOpen(false);
        }, true);

        document.addEventListener('keydown', function (e) {
            if (e.key === 'Escape' && open) setOpen(false);
        });
    })();

    /* ── Tabs: Capítulos | Proceso ──
     *
     * Switched in place, no reload, and never remembered: the chapters always
     * come first. Proceso's triage row arrives without its numbers when they
     * are not cached yet (they take seconds to work out), so the first visit to
     * the tab asks the server for them and swaps in the strip and the panel.
     */
    (function () {
        var tabs = Array.prototype.slice.call(document.querySelectorAll('.book-tab'));
        var resume = document.querySelector('.book-resume');
        if (!tabs.length) return;
        var triageAsked = false;

        function loadTriage() {
            if (triageAsked || !document.querySelector('.phase-row.ph-unknown')) return;
            triageAsked = true;
            fetch('/api/book-phases/' + encodeURIComponent(cfg.projectId || '') + '/triage')
                .then(function (r) { return r.ok ? r.json() : null; })
                .then(function (data) {
                    if (!data) { triageAsked = false; return; }
                    var wrap = document.getElementById('phase-strip-wrap');
                    var panel = document.getElementById('tab-process');
                    if (wrap) {
                        wrap.innerHTML = data.strip;
                        bindStrip();
                    }
                    if (panel) panel.innerHTML = data.panel;
                    var count = document.getElementById('process-count');
                    if (count) {
                        count.textContent = data.missing;
                        count.hidden = !data.missing;
                    }
                })
                // The row keeps saying it is checking; the next tab open retries.
                .catch(function () { triageAsked = false; });
        }

        function select(name, focus) {
            tabs.forEach(function (tab) {
                var on = tab.dataset.tab === name;
                tab.setAttribute('aria-selected', on ? 'true' : 'false');
                tab.tabIndex = on ? 0 : -1;
                var panel = document.getElementById(tab.getAttribute('aria-controls'));
                if (panel) panel.hidden = !on;
                if (on && focus) tab.focus();
            });
            // Resuming is a chapter-list action; it would read as a phase link.
            if (resume) resume.hidden = name !== 'chapters';
            if (name === 'process') loadTriage();
        }

        tabs.forEach(function (tab, idx) {
            tab.addEventListener('click', function () { select(tab.dataset.tab); });
            tab.addEventListener('keydown', function (e) {
                var step = e.key === 'ArrowRight' ? 1 : e.key === 'ArrowLeft' ? -1 : 0;
                if (!step) return;
                e.preventDefault();
                select(tabs[(idx + step + tabs.length) % tabs.length].dataset.tab, true);
            });
        });

        // The strip is the header's summary of the Proceso tab: a click opens it.
        function bindStrip() {
            var strip = document.getElementById('phase-strip');
            if (!strip) return;
            strip.addEventListener('click', function () {
                select('process');
                var bar = document.querySelector('.book-tabs');
                if (bar && bar.getBoundingClientRect().top < 0) bar.scrollIntoView();
            });
        }
        bindStrip();
    })();

    /* ── Coming back from a chapter ──
     *
     * The reader's back link carries ?from=<chapter>; the server has already
     * marked that row. Put it mid-screen (no smooth scroll on load), then drop
     * the parameter so a reload opens at the top like any other visit.
     */
    (function () {
        var row = document.querySelector('.nav-list li.last-opened');
        if (row) row.scrollIntoView({ block: 'center' });
        if (/[?&]from=/.test(location.search) && window.history && history.replaceState) {
            var params = new URLSearchParams(location.search);
            params.delete('from');
            var qs = params.toString();
            history.replaceState(history.state, '',
                location.pathname + (qs ? '?' + qs : '') + location.hash);
        }
    })();

    /* ── Flag chip and type picker ──
     *
     * The ticked types are the whole review setting: counted here and on the
     * home page, highlighted in the reader. The selection is global and lives
     * in a cookie, so the checkboxes render checked from the server and POST on
     * change. None ticked means none: no counts, no highlights.
     *
     * The counts for every type ship with the page (each row's data-flags, the
     * chip's data-totals), so redrawing on a change is instant and local — the
     * POST only persists the choice for the next load and the other pages.
     */
    (function () {
        var wrap = document.getElementById('flag-filter');
        var chip = document.getElementById('flag-chip-btn');
        var popup = document.getElementById('review-types-popup');
        var icons = document.getElementById('flag-chip-icons');
        if (!wrap || !chip || !popup) return;

        var typeCbs = Array.prototype.slice.call(popup.querySelectorAll('.review-type-cb'));
        var totals = {};
        try { totals = JSON.parse(chip.dataset.totals) || {}; } catch (e) { totals = {}; }
        var popupOpen = false;

        function setPopupOpen(open) {
            popupOpen = open;
            popup.hidden = !open;
            chip.setAttribute('aria-expanded', open ? 'true' : 'false');
            chip.classList.toggle('popup-open', open);
        }

        function selectedTypes() {
            return typeCbs.filter(function (cb) { return cb.checked; })
                          .map(function (cb) { return cb.value; });
        }

        function typeLabel(cb) {
            var span = cb.parentNode.querySelector('span');
            return span ? span.textContent : cb.value;
        }

        function icon(name) {
            var svg = icons && icons.content.querySelector('.flag-chip-icon-' + name);
            return svg ? svg.cloneNode(true) : document.createTextNode('');
        }

        function text(s) {
            var span = document.createElement('span');
            span.className = 'flag-chip-text';
            span.textContent = s;
            return span;
        }

        // The same five states app._flag_chip renders: a count, a count with
        // some types hidden, nothing ticked, a book with no flags at all, and a
        // zero that only exists because the flagged types are hidden.
        function drawChip(chosen) {
            var off = typeCbs.filter(function (cb) { return !cb.checked; });
            var count = chosen.reduce(function (sum, t) { return sum + (totals[t] || 0); }, 0);
            var any = Object.keys(totals).some(function (t) { return totals[t]; });
            var state = !chosen.length ? 'hidden' : !any ? 'clean' : count ? 'count' : 'zero';

            ['is-count', 'is-zero', 'is-clean', 'is-hidden'].forEach(function (c) {
                chip.classList.toggle(c, c === 'is-' + state);
            });
            chip.title = off.length
                ? cfg.flags_hidden_types.replace('{types}', off.map(typeLabel).join(', '))
                : cfg.flags_all_types;
            while (chip.firstChild) chip.removeChild(chip.firstChild);
            if (state === 'hidden') {
                chip.appendChild(icon('eyeoff'));
                chip.appendChild(text(cfg.flags_hidden));
            } else if (state === 'clean') {
                chip.appendChild(icon('check'));
                chip.appendChild(text(cfg.flags_none));
            } else {
                chip.appendChild(icon('flag'));
                chip.appendChild(text(count + ' ' + (count === 1 ? cfg.flag_one : cfg.flag_many)));
                if (off.length) chip.appendChild(icon('eyeoff'));
            }
            chip.appendChild(icon('caret'));
        }

        function persistTypes() {
            fetch('/api/set-review-types', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ types: selectedTypes() })
            }).catch(function () { /* keeps the current page's selection either way */ });
        }

        function resum() {
            var chosen = selectedTypes();
            drawChip(chosen);

            document.querySelectorAll('.nav-list li[data-flags]').forEach(function (row) {
                var flags = row.querySelector('.chapter-chip-flags');
                if (!flags) return;
                var counts = {};
                try { counts = JSON.parse(row.dataset.flags) || {}; } catch (e) { counts = {}; }
                var total = chosen.reduce(function (sum, t) { return sum + (counts[t] || 0); }, 0);
                flags.querySelector('.chip-flag-count').textContent = total;
                flags.querySelector('.chip-flag-label').textContent =
                    total === 1 ? cfg.flag_one : cfg.flag_many;
                flags.hidden = total === 0;
                // "Clean" is a claim about flags; with no type ticked there is
                // nothing it could be a claim about.
                var clean = row.querySelector('.chapter-chip-clean');
                if (clean) clean.hidden = total !== 0 || !chosen.length;
            });
        }

        function changed() {
            resum();
            persistTypes();
        }

        chip.addEventListener('click', function (e) {
            e.stopPropagation();
            setPopupOpen(!popupOpen);
        });

        typeCbs.forEach(function (cb) {
            cb.addEventListener('change', changed);
        });

        popup.querySelectorAll('[data-all]').forEach(function (btn) {
            btn.addEventListener('click', function () {
                var on = btn.dataset.all === '1';
                typeCbs.forEach(function (cb) { cb.checked = on; });
                changed();
            });
        });

        document.addEventListener('click', function (e) {
            if (popupOpen && !wrap.contains(e.target)) setPopupOpen(false);
        });

        document.addEventListener('keydown', function (e) {
            if (e.key === 'Escape' && popupOpen) {
                setPopupOpen(false);
                chip.focus();
            }
        });
    })();
})();
