/**
 * Reader "Find in book" concordance — full-screen search surface.
 *
 * Read-only folded-substring search across one book (GET /api/search). Results
 * are source+translation pairs (navigable) or display-only KWIC snippets for
 * untranslated chapters. State persists in sessionStorage (per project) so the
 * list + scroll survive a jump-out-and-back. See design 20260603 (T3/T5/T6,
 * D-T1..D-T4, DR1/DR2/DR3/DR5/DR6/DR8/DR9).
 */
(function () {
    'use strict';

    const app = document.getElementById('reader-app');
    const surface = document.getElementById('search-surface');
    if (!app || !surface) return;

    const projectId = app.dataset.project;
    const chapter = app.dataset.chapter;
    const i = window.__i18n || {};

    const btnOpen = document.getElementById('btn-search');
    const btnClose = document.getElementById('search-close');
    const input = document.getElementById('search-input');
    const sideEs = document.getElementById('search-side-es');
    const sideEn = document.getElementById('search-side-en');
    const resumeBtn = document.getElementById('search-resume');
    const statusEl = document.getElementById('search-status');
    const resultsEl = document.getElementById('search-results');

    const MIN_QUERY = 2;
    const SESSION_KEY = 'concordance:' + projectId;

    let side = 'translation';   // 'translation' (ES) | 'source' (EN)
    let resume = null;          // { chapter, anchor, label }

    // ---- small helpers ----

    function fmt(str, vars) {
        return (str || '').replace(/\{(\w+)\}/g, (m, k) =>
            (vars && k in vars) ? vars[k] : m);
    }

    function escapeHtml(s) {
        const div = document.createElement('div');
        div.textContent = s == null ? '' : String(s);
        return div.innerHTML;
    }

    // Wrap [start, end) of `text` in <mark>, escaping all segments. Offsets
    // come from the server, mapped into the ORIGINAL string (D5).
    function highlight(text, start, end, doHighlight) {
        text = text || '';
        if (!doHighlight || start == null || end == null ||
            start < 0 || end > text.length || start >= end) {
            return escapeHtml(text);
        }
        return escapeHtml(text.slice(0, start)) +
            '<mark>' + escapeHtml(text.slice(start, end)) + '</mark>' +
            escapeHtml(text.slice(end));
    }

    function setStatus(html) {
        statusEl.innerHTML = html || '';
        statusEl.style.display = html ? '' : 'none';
    }

    // ---- session persistence (DR3: per-project, survives nav) ----

    function loadSession() {
        try { return JSON.parse(sessionStorage.getItem(SESSION_KEY)) || null; }
        catch { return null; }
    }

    function saveSession(patch) {
        const cur = loadSession() || {};
        const next = Object.assign(cur, patch);
        try { sessionStorage.setItem(SESSION_KEY, JSON.stringify(next)); }
        catch { /* private mode / quota — search still works, just no resume */ }
    }

    // ---- resume reading (DR6) ----

    function chapterLabel() {
        const el = document.querySelector('.reader-title');
        return el ? el.textContent.trim() : chapter;
    }

    // The es text of the topmost sentence currently visible under the topbar.
    function topmostVisibleEs() {
        const spans = document.querySelectorAll('#reader-content .sentence[data-es-idx]');
        for (const s of spans) {
            if (s.getBoundingClientRect().bottom > 56) {
                return s.textContent.trim();
            }
        }
        return null;
    }

    function showResume() {
        if (!resume || !resume.anchor) { resumeBtn.hidden = true; return; }
        resumeBtn.hidden = false;
        resumeBtn.textContent = fmt(i.search_resume || 'Resume reading · {label}',
            { label: resume.label || '' });
    }

    function goResume() {
        if (!resume || !resume.anchor) return;
        // Save list state first so reopening still restores the results.
        saveSession({ listScroll: resultsEl.scrollTop });
        window.location.href = '/read/' + encodeURIComponent(projectId) + '/' +
            encodeURIComponent(resume.chapter) +
            '?anchor=' + encodeURIComponent(resume.anchor);
    }

    // ---- open / close ----

    function openSurface() {
        const sess = loadSession();
        surface.hidden = false;
        document.body.style.overflow = 'hidden';

        // Reuse the stored spot only when it belongs to the chapter we're in;
        // a stored resume from an earlier chapter is stale after cross-chapter
        // nav (DR6). Otherwise re-capture the current reading spot. Never
        // persist a null resume — if content hasn't rendered yet, leave any
        // prior resume intact and try again on the next open.
        if (sess && sess.resume && sess.resume.chapter === chapter) {
            resume = sess.resume;
        } else {
            const es = topmostVisibleEs();
            if (es) {
                resume = { chapter, anchor: es.slice(0, 80), label: chapterLabel() };
                saveSession({ resume });
            } else {
                resume = (sess && sess.resume) || null;
            }
        }
        showResume();

        if (sess && sess.data) {
            // Rehydrate last search (DR3).
            side = sess.side || 'translation';
            applySideButtons();
            input.value = sess.query || '';
            renderData(sess.data);
            if (typeof sess.listScroll === 'number') {
                resultsEl.scrollTop = sess.listScroll;
            }
        } else {
            renderIdle();
        }
        // Focus into the input (DR8); keep the value selected for quick replace.
        setTimeout(() => { input.focus(); input.select(); }, 0);
    }

    function closeSurface() {
        saveSession({ listScroll: resultsEl.scrollTop, side: side });
        surface.hidden = true;
        document.body.style.overflow = '';
        // Return focus to the trigger so keyboard / AT users aren't stranded
        // at the top of the document after the modal closes.
        if (btnOpen) btnOpen.focus();
    }

    // Keep Tab focus inside the modal dialog (aria-modal=true) instead of
    // leaking to the reader page behind it.
    function trapFocus(e) {
        if (e.key !== 'Tab') return;
        const focusable = surface.querySelectorAll(
            'button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])');
        const visible = Array.prototype.filter.call(focusable,
            el => !el.hidden && el.offsetParent !== null);
        if (!visible.length) return;
        const first = visible[0];
        const last = visible[visible.length - 1];
        if (e.shiftKey && document.activeElement === first) {
            e.preventDefault();
            last.focus();
        } else if (!e.shiftKey && document.activeElement === last) {
            e.preventDefault();
            first.focus();
        }
    }

    // ---- side toggle ----

    function applySideButtons() {
        const isEs = side === 'translation';
        sideEs.classList.toggle('active', isEs);
        sideEn.classList.toggle('active', !isEs);
        sideEs.setAttribute('aria-pressed', String(isEs));
        sideEn.setAttribute('aria-pressed', String(!isEs));
    }

    function setSide(next) {
        if (next === side) return;
        side = next;
        applySideButtons();
        saveSession({ side: side });
        if (input.value.trim().length >= MIN_QUERY) runSearch();
    }

    // ---- search ----

    function renderIdle() {
        resultsEl.innerHTML = '';
        setStatus(escapeHtml(i.search_idle ||
            'Search the translation or source across the whole book.'));
    }

    let searchAbort = null;

    function runSearch() {
        const q = input.value.trim();
        if (q.length < MIN_QUERY) { renderIdle(); return; }

        setStatus(escapeHtml(i.search_loading || 'Searching…'));
        resultsEl.innerHTML = '';

        // Cancel any in-flight search so a slow earlier response can't clobber
        // newer results (e.g. rapid ES/EN toggling or Enter spam).
        if (searchAbort) searchAbort.abort();
        searchAbort = new AbortController();
        const reqSide = side;

        fetch('/api/search/' + encodeURIComponent(projectId) +
            '?q=' + encodeURIComponent(q) + '&side=' + encodeURIComponent(side),
            { signal: searchAbort.signal })
            .then(r => {
                if (!r.ok) throw new Error('http ' + r.status);
                return r.json();
            })
            .then(data => {
                saveSession({ query: q, side: reqSide, data: data, listScroll: 0 });
                renderData(data);
            })
            .catch((err) => {
                if (err && err.name === 'AbortError') return;  // superseded
                renderError();
            });
    }

    function renderError() {
        resultsEl.innerHTML = '';
        const msg = escapeHtml(i.search_error || "Couldn't complete the search.");
        setStatus(msg);
        const btn = document.createElement('button');
        btn.className = 'search-retry';
        btn.textContent = i.search_retry || 'Retry';
        btn.addEventListener('click', runSearch);
        statusEl.appendChild(btn);
    }

    function renderData(data) {
        const results = (data && data.results) || [];
        const q = (data && data.query) || input.value.trim();

        if (!results.length) {
            resultsEl.innerHTML = '';
            const hint = '<span class="search-status-hint">' +
                escapeHtml(i.search_empty_hint || 'Try a shorter fragment, or switch ES/EN.') +
                '</span>';
            setStatus(escapeHtml(fmt(i.search_empty || 'No matches for «{q}».', { q })) + hint);
            return;
        }

        // Only-untranslated state: every hit is a display-only source snippet.
        const allUntranslated = results.every(r => r.translated === false);
        setStatus(allUntranslated
            ? escapeHtml(i.search_only_untranslated || '')
            : '');

        const frag = document.createDocumentFragment();

        const count = document.createElement('div');
        count.className = 'search-count';
        count.textContent = fmt(i.search_count || '{n} results · {m} chapters',
            { n: data.n_results, m: data.n_chapters });
        frag.appendChild(count);

        let curChapter = null;
        for (const row of results) {
            if (row.chapter !== curChapter) {
                curChapter = row.chapter;
                frag.appendChild(buildDivider(row));
            }
            frag.appendChild(row.translated ? buildPairRow(row) : buildSourceRow(row));
        }

        resultsEl.innerHTML = '';
        resultsEl.appendChild(frag);
        resultsEl.scrollTop = 0;
    }

    function buildDivider(row) {
        const div = document.createElement('div');
        div.className = 'search-chapter-divider';
        div.textContent = row.chapter_label || row.chapter;
        if (row.translated === false) {
            const note = document.createElement('span');
            note.className = 'search-divider-note';
            note.textContent = ' · ' + (i.search_not_translated || 'not translated');
            div.appendChild(note);
        }
        return div;
    }

    // Navigable translated pair: full ES (primary) + EN (muted reference).
    function buildPairRow(row) {
        const btn = document.createElement('button');
        btn.type = 'button';
        btn.className = 'search-row';

        const body = document.createElement('div');
        body.className = 'search-row-body';

        const es = document.createElement('div');
        es.className = 'search-row-es';
        es.innerHTML = highlight(row.es, row.match_start, row.match_end,
            row.match_field === 'es');
        body.appendChild(es);

        if (row.en) {
            const en = document.createElement('div');
            en.className = 'search-row-en';
            en.innerHTML = highlight(row.en, row.match_start, row.match_end,
                row.match_field === 'en');
            body.appendChild(en);
        }
        btn.appendChild(body);

        const chev = document.createElement('span');
        chev.className = 'search-row-chevron';
        chev.setAttribute('aria-hidden', 'true');
        chev.textContent = '›';
        btn.appendChild(chev);

        btn.addEventListener('click', () => navigateTo(row));
        return btn;
    }

    // Display-only untranslated source hit (KWIC snippet, no jump — D4).
    function buildSourceRow(row) {
        const div = document.createElement('div');
        div.className = 'search-row untranslated';

        const body = document.createElement('div');
        body.className = 'search-row-body';
        const kwic = document.createElement('div');
        kwic.className = 'search-row-kwic';
        kwic.innerHTML = highlight(row.snippet, row.match_start, row.match_end, true);
        body.appendChild(kwic);
        div.appendChild(body);

        // Stroke-SVG "no jump" marker (no emoji — DR7).
        div.insertAdjacentHTML('beforeend',
            '<svg class="search-row-chevron" viewBox="0 0 24 24" width="16" height="16" ' +
            'fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true">' +
            '<circle cx="12" cy="12" r="9"/><line x1="6" y1="6" x2="18" y2="18"/></svg>');
        return div;
    }

    function navigateTo(row) {
        // Persist list + scroll so reopening restores results (DR3).
        saveSession({ listScroll: resultsEl.scrollTop });
        // Pass es_idx so reader.js lands on the exact sentence when several
        // share the anchor prefix (falls back to prefix-match if renumbered).
        const esi = (row.es_idx != null)
            ? '&esi=' + encodeURIComponent(row.es_idx) : '';
        window.location.href = '/read/' + encodeURIComponent(projectId) + '/' +
            encodeURIComponent(row.chapter) +
            '?anchor=' + encodeURIComponent(row.anchor) + '&hl=1' + esi;
    }

    // ---- wiring ----

    if (btnOpen) btnOpen.addEventListener('click', openSurface);
    btnClose.addEventListener('click', closeSurface);
    sideEs.addEventListener('click', () => setSide('translation'));
    sideEn.addEventListener('click', () => setSide('source'));
    resumeBtn.addEventListener('click', goResume);

    input.addEventListener('keydown', (e) => {
        if (e.key === 'Enter') { e.preventDefault(); runSearch(); }
    });

    surface.addEventListener('keydown', trapFocus);

    document.addEventListener('keydown', (e) => {
        if (e.key === 'Escape' && !surface.hidden) {
            e.preventDefault();
            closeSurface();
        }
    });
})();
