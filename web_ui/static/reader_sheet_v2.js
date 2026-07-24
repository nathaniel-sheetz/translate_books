/**
 * Reader bottom sheet — opt-in v2 skin (Annotate / Edit / Issues).
 *
 * This is a presentation layer only. reader.js remains the working controller:
 * it loads the data, drives the (hidden) classic sheet + shared modals, and
 * hands each opened sentence here via window.ReaderSheetV2.onOpen. Every action
 * routes back through window.ReaderCore so there is a single source of truth for
 * persistence, the retranslate/remove modals, and re-render.
 *
 * Data model note: the backend stores exactly ONE annotation (type + content)
 * per sentence, so the Annotate tab shows a single card (edit/delete) or the
 * Add row — not the multi-note list from the static mockup.
 */

(function () {
    'use strict';

    if (window.READER_UI_VERSION !== 'v2') return;

    const sheet = document.getElementById('reader-sheet-v2');
    const overlay = document.getElementById('rv2-overlay');
    if (!sheet || !overlay) return;

    const I = window.__i18n || {};
    const V = I.v2 || {};
    const T = (k, d) => (V[k] != null ? V[k] : d);
    const el = (id) => document.getElementById(id);
    const core = () => window.ReaderCore || {};
    const TOPBAR_PX = 56;   // fixed 48px reader topbar + 8px breathing room

    const els = {
        src: el('rv2-src'),
        srcTxt: el('rv2-src-txt'),
        srcToggle: el('rv2-src-toggle'),
        srcKicker: el('rv2-src-kicker'),
        close: el('rv2-close'),
        grip: el('rv2-grip'),
        tabs: sheet.querySelectorAll('.rv2-tab'),
        cardList: el('rv2-card-list'),
        addRow: el('rv2-add-row'),
        addIcons: el('rv2-add-icons'),
        editTa: el('rv2-edit-ta'),
        editSave: el('rv2-edit-save'),
        expandBtn: el('rv2-expand-btn'),
        chipRe: el('rv2-chip-retranslate'),
        chipBound: el('rv2-chip-boundaries'),
        chipRemove: el('rv2-chip-remove'),
        issueList: el('rv2-issue-list'),
        annCount: el('rv2-ann-count'),
        issueCount: el('rv2-issue-count'),
        bubble: el('rv2-bubble'),
    };
    const panels = {
        annotate: el('rv2-panel-annotate'),
        edit: el('rv2-panel-edit'),
        issues: el('rv2-panel-issues'),
    };

    // ── Type metadata ──────────────────────────────────────────────────────────
    const ANN_NAMES = {
        word_choice: I.ann_word_choice || 'Word choice',
        inconsistency: I.ann_inconsistency || 'Inconsistency',
        footnote: I.ann_footnote || 'Footnote',
        flag: I.ann_flag || 'Other',
    };
    const ICON = {
        word_choice: '<svg viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.5"><circle cx="10" cy="10" r="8.5"/><path d="M7.5 7.5a2.5 2.5 0 0 1 4.5 1.5c0 1.5-2 2-2 3.5"/><circle cx="10" cy="15" r=".5" fill="currentColor"/></svg>',
        inconsistency: '<svg viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.5"><polyline points="2,14 5,6 8,14 11,6 14,14 17,6"/></svg>',
        flag: '<svg viewBox="0 0 20 20" fill="currentColor"><circle cx="5" cy="10" r="1.5"/><circle cx="10" cy="10" r="1.5"/><circle cx="15" cy="10" r="1.5"/></svg>',
    };
    const PENCIL = '<svg viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.6"><path d="M13.5 3.5l3 3L7 16l-4 1 1-4z"/></svg>';
    const TRASH = '<svg viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.6"><path d="M4 6h12M8 6V4h4v2M6 6l1 10h6l1-10"/></svg>';
    const DISK = '<svg viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linejoin="round"><path d="M4.5 3.5h8L16 7v9.5H4.5z"/><path d="M7 3.5v3.5h5V3.5"/><rect x="7" y="10.5" width="6" height="4.5"/></svg>';

    const DOT = { address: 'addr', grammar: 'gram', dictionary: 'gloss', consistency: 'cons', register: 'reg' };
    const SEVCLASS = { error: 'error', info: 'info' };
    const TYPELABELS = I.review_types || {};
    const FB = [
        ['false_positive', I.review_fb_false_positive || 'False positive'],
        ['bad_message', I.review_fb_bad_message || 'Bad message'],
        ['missing_context_gap', I.review_fb_missing_context_gap || 'Missing context'],
        ['resolved', I.review_fb_resolved || 'Resolved'],
    ];

    function esc(s) {
        return String(s == null ? '' : s)
            .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;');
    }
    // Never interpolate raw ann.type into HTML/class names — API accepts freeform strings.
    const ANN_TYPES = { word_choice: 1, inconsistency: 1, footnote: 1, flag: 1 };
    function safeAnnType(type) {
        return ANN_TYPES[type] ? type : 'flag';
    }
    function typeGlyph(type) {
        type = safeAnnType(type);
        return type === 'footnote' ? '<span class="a">a</span>' : (ICON[type] || '');
    }
    function paintTps(scope) {
        scope.querySelectorAll('.rv2-tp').forEach(function (x) {
            if (!x.dataset.painted) { x.innerHTML = typeGlyph(x.dataset.type); x.dataset.painted = '1'; }
        });
    }

    // ── State ──────────────────────────────────────────────────────────────────
    let cur = null;   // { esIdx, en, es, ann, findings, reviewOn, defaultErrors }

    // ── Open / close ───────────────────────────────────────────────────────────
    function onOpen(data) {
        cur = data;
        renderSource(data.en);
        renderAnnotate();
        renderEdit(data.es);
        renderIssues();
        updateCounts();
        setTab(data.defaultErrors ? 'issues' : 'annotate');
        show();
    }
    function onClose() { hide(); }

    function show() {
        overlay.hidden = false;
        sheet.hidden = false;
        sheet.classList.remove('rv2-collapsed');
        requestAnimationFrame(function () { refreshSrcToggle(); anchorSheet(); });
    }
    function hide() {
        overlay.hidden = true;
        sheet.hidden = true;
        sheet.classList.remove('editing', 'editfull', 'rv2-kb');
        sheet.style.top = '';
        sheet.style.bottom = '';
        sheet.style.height = '';
        sheet.style.maxHeight = '';
        endCompose();
    }

    // ── Tabs ───────────────────────────────────────────────────────────────────
    function setTab(name) {
        els.tabs.forEach((t) => t.classList.toggle('active', t.dataset.tab === name));
        Object.keys(panels).forEach((k) => panels[k].classList.toggle('active', k === name));
        const editing = name === 'edit';
        sheet.classList.toggle('editing', editing);
        els.srcKicker.textContent = editing
            ? T('source_compare', 'English source · compare')
            : T('source_label', 'English source');
        if (!editing) setEditFull(false);
        refreshSrcToggle();
        anchorSheet();
    }
    els.tabs.forEach((t) => t.addEventListener('click', () => setTab(t.dataset.tab)));

    // ── Source (clamp + more/less) ─────────────────────────────────────────────
    function renderSource(en) {
        els.srcTxt.textContent = en || '';
        els.src.classList.remove('min');
        els.src.classList.add('clamp');
        els.srcToggle.textContent = T('more', 'more');
    }
    function refreshSrcToggle() {
        const box = els.src, txt = els.srcTxt, btn = els.srcToggle;
        // Keyboard up: source is pinned to one line, always offer "more" (it
        // doubles as the dismiss-keyboard affordance).
        if (sheet.classList.contains('rv2-kb')) { btn.style.display = ''; return; }
        if (!box.classList.contains('clamp')) { btn.style.display = ''; return; }
        const truncated = (txt.scrollHeight - txt.clientHeight) > 2;
        btn.style.display = truncated ? '' : 'none';
    }
    els.srcToggle.addEventListener('click', function () {
        const box = els.src;
        // Keyboard up: the source is clamped to one line to make room for the
        // editor. Tapping "more" dismisses the keyboard and reveals it in full.
        if (sheet.classList.contains('rv2-kb')) {
            endCompose();                       // blur → keyboard hides → focusout clears rv2-kb
            box.classList.remove('min', 'clamp');
            els.srcToggle.textContent = T('less', 'less');
            return;
        }
        if (box.classList.contains('min')) {
            box.classList.remove('min', 'clamp');
            els.srcToggle.textContent = T('less', 'less');
            setEditFull(false, true);
            refreshSrcToggle();
            return;
        }
        box.classList.toggle('clamp');
        els.srcToggle.textContent = box.classList.contains('clamp') ? T('more', 'more') : T('less', 'less');
        refreshSrcToggle();
    });

    // ── Annotate (single annotation per sentence) ──────────────────────────────
    function renderAnnotate() {
        els.cardList.innerHTML = '';
        els.addRow.classList.remove('rv2-hide');
        if (cur && cur.ann) {
            els.cardList.appendChild(existingCard(cur.ann));
            els.addRow.classList.add('rv2-hide');
        }
    }
    function composerHtml(type, value, withDelete) {
        const tps = ['word_choice', 'inconsistency', 'footnote', 'flag']
            .map((tp) => '<button class="rv2-tp" type="button" data-type="' + tp + '" aria-label="' + esc(ANN_NAMES[tp]) + '"></button>')
            .join('');
        const cancelLbl = esc(T('cancel', 'Cancel'));
        const saveLbl = esc(T('save', 'Save'));
        const trailing = withDelete
            ? '<button class="rv2-icon-del" type="button" aria-label="' + esc(T('aria_delete_note', 'Delete note')) + '">' + TRASH + '</button>'
            : '<button class="rv2-icon-del rv2-cancel" type="button" aria-label="' + cancelLbl + '" title="' + cancelLbl + '">' + TRASH + '</button>';
        return '<div class="rv2-composer">'
            + '<textarea placeholder="' + esc(T('note_placeholder', 'Note text…')) + '">' + esc(value) + '</textarea>'
            + '<div class="rv2-fn-hint' + (type === 'footnote' ? ' show' : '') + '">' + esc(T('fn_hint', '')) + '</div>'
            + '<div class="rv2-composer-bar"><div class="rv2-type-row">' + tps + '</div>'
            + trailing
            + '<button class="rv2-btn rv2-primary rv2-iconbtn rv2-save" type="button" aria-label="' + saveLbl + '" title="' + saveLbl + '">' + DISK + '</button>'
            + '</div></div>';
    }
    function existingCard(ann) {
        const type = safeAnnType(ann.type);
        const d = document.createElement('div');
        d.className = 'rv2-card';
        d.dataset.type = type;
        d.innerHTML =
            '<div class="rv2-card-head">'
            + '<span class="rv2-badge ' + type + '">' + typeGlyph(type) + '</span>'
            + '<span class="rv2-card-txt"><span class="rv2-card-type">' + esc(ANN_NAMES[type] || type) + '</span>' + esc(ann.content || '') + '</span>'
            + '<button class="rv2-card-edit" type="button" aria-label="' + esc(T('aria_edit_note', 'Edit note')) + '">' + PENCIL + '</button>'
            + '</div>'
            + '<div class="rv2-card-body">' + composerHtml(type, ann.content || '', true) + '</div>';
        paintTps(d);
        selectType(d, type);
        d.querySelector('.rv2-card-edit').addEventListener('click', function () {
            d.classList.add('open');
            focusCompose(d.querySelector('textarea'));
        });
        return d;
    }
    function selectType(card, type) {
        type = safeAnnType(type);
        card.dataset.type = type;
        card.querySelectorAll('.rv2-type-row .rv2-tp').forEach((x) => x.classList.toggle('sel', x.dataset.type === type));
        const hint = card.querySelector('.rv2-fn-hint');
        if (hint) hint.classList.toggle('show', type === 'footnote');
        const badge = card.querySelector('.rv2-badge');
        if (badge) { badge.className = 'rv2-badge ' + type; badge.innerHTML = typeGlyph(type); }
    }
    function openAdd(type) {
        closeAdd();
        els.addRow.classList.add('rv2-hide');
        const wrap = document.createElement('div');
        wrap.className = 'rv2-card open';
        wrap.id = 'rv2-add-card';
        wrap.dataset.type = type;
        wrap.innerHTML = '<div class="rv2-card-body">' + composerHtml(type, '', false) + '</div>';
        els.cardList.appendChild(wrap);
        paintTps(wrap);
        selectType(wrap, type);
        focusCompose(wrap.querySelector('textarea'));
    }
    function closeAdd() {
        const w = el('rv2-add-card');
        if (w) w.remove();
        if (!cur || !cur.ann) els.addRow.classList.remove('rv2-hide');
    }
    els.addIcons.addEventListener('click', function (e) {
        const tp = e.target.closest('.rv2-tp');
        if (tp) openAdd(tp.dataset.type);
    });
    panels.annotate.addEventListener('click', function (e) {
        const tp = e.target.closest('.rv2-type-row .rv2-tp');
        if (tp) { selectType(tp.closest('.rv2-card'), tp.dataset.type); return; }
        if (e.target.closest('.rv2-cancel')) { closeAdd(); endCompose(); return; }
        if (e.target.closest('.rv2-icon-del')) { core().deleteAnnotation && core().deleteAnnotation(); return; }
        if (e.target.closest('.rv2-save')) {
            const card = e.target.closest('.rv2-card');
            const type = card.dataset.type;
            const val = (card.querySelector('textarea').value || '').trim();
            if (core().saveAnnotation) core().saveAnnotation(type, val);
            return;
        }
    });

    // ── Edit ───────────────────────────────────────────────────────────────────
    function renderEdit(es) {
        els.editTa.value = es || '';
        setEditFull(false);
    }
    function setEditFull(full, keepSrc) {
        sheet.classList.toggle('editfull', full);
        els.editTa.style.minHeight = full ? '40vh' : '120px';
        els.expandBtn.textContent = full ? T('collapse', '⤡ Collapse') : T('expand', '⤢ Expand');
        const src = els.src;
        if (full) {
            src.classList.add('clamp', 'min');
            els.srcToggle.textContent = T('more', 'more');
        } else if (!keepSrc) {
            src.classList.remove('min');
            src.classList.add('clamp');
            els.srcToggle.textContent = T('more', 'more');
        } else {
            src.classList.remove('min');
        }
        refreshSrcToggle();
    }
    els.expandBtn.addEventListener('click', () => setEditFull(!sheet.classList.contains('editfull')));
    els.editSave.addEventListener('click', () => core().saveCorrection && core().saveCorrection(els.editTa.value));
    els.chipRe.addEventListener('click', () => core().retranslate && core().retranslate());
    els.chipBound.addEventListener('click', () => core().editBoundaries && core().editBoundaries());
    els.chipRemove.addEventListener('click', () => core().removeText && core().removeText());

    // ── Issues ─────────────────────────────────────────────────────────────────
    function renderIssues() {
        els.issueList.innerHTML = '';
        const findings = (cur && cur.findings) || [];
        if (!findings.length) {
            const e = document.createElement('div');
            e.className = 'rv2-empty';
            e.textContent = T('no_findings', I.review_no_errors || 'No findings on this sentence.');
            els.issueList.appendChild(e);
            return;
        }
        findings.forEach((f) => els.issueList.appendChild(issueEl(f)));
    }
    function sevLabel(sev) {
        const key = 'review_sev_' + (sev === 'warning' ? 'warning' : sev);
        return I[key] || sev;
    }
    function issueEl(f) {
        const type = f.eval_name;
        const sev = f.severity || 'info';
        const wrap = document.createElement('div');
        wrap.className = 'rv2-issue';

        const body = document.createElement('div');
        body.className = 'rv2-issue-body';
        const line = document.createElement('div');
        line.className = 'rv2-iline';
        line.innerHTML =
            '<span class="rv2-dot ' + (DOT[type] || '') + '"></span>'
            + '<span class="rv2-sev ' + (SEVCLASS[sev] || '') + '">' + esc(sevLabel(sev)) + '</span>'
            + '<span class="rv2-idesc"><b>' + esc(TYPELABELS[type] || type) + '</b>' + (f.message ? ' — ' + esc(f.message) : '') + '</span>'
            + '<span class="rv2-chev">›</span>';
        body.appendChild(line);
        if (f.suggestion) {
            const fix = document.createElement('div');
            fix.className = 'rv2-ifix';
            fix.innerHTML = '<span class="arrow">→</span> ' + esc(f.suggestion);
            body.appendChild(fix);
        }
        wrap.appendChild(body);

        if (f.excerpt) {
            const det = document.createElement('div');
            det.className = 'rv2-issue-detail';
            const ctx = document.createElement('div');
            ctx.className = 'rv2-ctx';
            ctx.textContent = f.excerpt;
            det.appendChild(ctx);
            wrap.appendChild(det);
            body.addEventListener('click', () => wrap.classList.toggle('open'));
        } else {
            wrap.querySelector('.rv2-chev').style.visibility = 'hidden';
        }

        const acts = document.createElement('div');
        acts.className = 'rv2-issue-actions';
        // Primary "Apply" is the resolved-feedback action, promoted to first
        // position and styled primary. It records the resolved signal and drops
        // the issue, then sends the user to the Edit tab with the full
        // translation intact so they can apply the fix by hand — it must NOT
        // overwrite the translation with the raw suggestion.
        const applyResolved = document.createElement('button');
        applyResolved.type = 'button';
        applyResolved.className = 'rv2-btn rv2-primary rv2-sm';
        applyResolved.textContent = T('apply', 'Apply');
        applyResolved.addEventListener('click', function (e) {
            e.stopPropagation();
            acts.querySelectorAll('button').forEach((x) => { x.disabled = true; });
            if (core().submitFeedback) core().submitFeedback(cur.esIdx, f, 'resolved');
            dropIssue(wrap, f);
            setTab('edit');
        });
        acts.appendChild(applyResolved);
        FB.filter((pair) => pair[0] !== 'resolved').forEach(function (pair) {
            const b = document.createElement('button');
            b.type = 'button';
            b.className = 'rv2-fb-btn';
            b.textContent = pair[1];
            b.addEventListener('click', function (e) {
                e.stopPropagation();
                acts.querySelectorAll('button').forEach((x) => { x.disabled = true; });
                if (core().submitFeedback) core().submitFeedback(cur.esIdx, f, pair[0]);
                dropIssue(wrap, f);
            });
            acts.appendChild(b);
        });
        wrap.appendChild(acts);
        return wrap;
    }
    function dropIssue(wrap, finding) {
        wrap.remove();
        if (cur && cur.findings) {
            cur.findings = cur.findings.filter((x) => !(
                x.eval_name === finding.eval_name &&
                x.issue_index === finding.issue_index &&
                x.chunk_id === finding.chunk_id
            ));
        }
        updateCounts();
        if (cur && (!cur.findings || !cur.findings.length)) {
            renderIssues();
            if (panels.issues.classList.contains('active')) setTab('annotate');
        }
    }

    // ── Counts ─────────────────────────────────────────────────────────────────
    function countEl(node, n) {
        if (!node) return;
        node.textContent = String(n);
        node.hidden = n === 0;
    }
    function updateCounts() {
        countEl(els.annCount, cur && cur.ann ? 1 : 0);
        countEl(els.issueCount, cur && cur.findings ? cur.findings.length : 0);
    }

    // ── Compose focus ──────────────────────────────────────────────────────────
    function focusCompose(ta) {
        if (!ta) return;
        setTimeout(function () {
            ta.focus();
            (ta.closest('.rv2-card') || ta).scrollIntoView({ block: 'start' });
        }, 30);
    }
    function endCompose() {
        const a = document.activeElement;
        if (a && a.blur && sheet.contains(a)) a.blur();
    }

    // ── Chrome: close / overlay / grip ─────────────────────────────────────────
    els.close.addEventListener('click', () => core().close && core().close());
    overlay.addEventListener('click', () => core().close && core().close());
    els.grip.addEventListener('click', () => sheet.classList.toggle('rv2-collapsed'));

    // ── Keyboard-aware state (focus-driven + visual-viewport anchoring) ────────
    // interactive-widget=resizes-content (v2 viewport meta) lets the soft
    // keyboard shrink the layout viewport so the sheet rides above it. We flip an
    // `rv2-kb` class on focus (CSS grows the Edit sheet to full height and pins
    // the source to one line). For that full-height Edit case we also anchor the
    // sheet precisely to the *visible* region so it clears both the keyboard and
    // a bottom URL bar that stays up while typing. We use only vv.offsetTop /
    // vv.height (never window.innerHeight, which jumps as the URL bar toggles and
    // caused the earlier flicker), so it stays stable across URL-bar changes.
    const vv = window.visualViewport;
    // Measure a viewport-percentage unit (svh/lvh) in px via a throwaway probe.
    function vp(unit) {
        const p = document.createElement('div');
        p.style.cssText = 'position:fixed;left:-9999px;top:0;width:1px;visibility:hidden;height:100' + unit;
        document.body.appendChild(p);
        const h = p.getBoundingClientRect().height;
        p.remove();
        return h;
    }
    // Tallest large-viewport height seen (keyboard down) = our reference for
    // detecting when the keyboard is up. Seeded now, while the keyboard is down.
    let maxLvh = 0;
    try { maxLvh = vp('lvh'); } catch (e) { /* svh/lvh unsupported */ }
    // Samsung Internet's bottom toolbar RESERVES space when the keyboard is down
    // (the sheet already sits above it) but OVERLAYS the viewport when the
    // keyboard is up. Only in that overlay case do we pad the action row up by
    // the toolbar height (lvh − svh); otherwise reserving would leave a blank
    // strip. The sheet itself always fills to vv.bottom, so nothing bleeds.
    function urlbarReserve() {
        let svh, lvh;
        try { svh = vp('svh'); lvh = vp('lvh'); } catch (e) { return 0; }
        if (lvh > maxLvh) maxLvh = lvh;
        const keyboardUp = lvh < maxLvh - 100;
        return keyboardUp ? Math.max(0, Math.round(lvh - svh)) : 0;
    }
    // Full-height edit sheet: fill from just below the topbar down to the bottom
    // of the *visual* viewport, then reserve the toolbar strip on the action row.
    function anchorSheet() {
        if (!vv || sheet.hidden) return;
        if (sheet.classList.contains('rv2-kb') && sheet.classList.contains('editing')) {
            sheet.style.top = (vv.offsetTop + TOPBAR_PX) + 'px';
            sheet.style.height = Math.max(160, vv.height - TOPBAR_PX) + 'px';
            sheet.style.bottom = 'auto';
            sheet.style.setProperty('--rv2-urlbar', urlbarReserve() + 'px');
        } else {
            sheet.style.top = '';
            sheet.style.height = '';
            sheet.style.bottom = '';
            sheet.style.setProperty('--rv2-urlbar', '0px');
        }
    }
    function isField(t) { return !!t && (t.tagName === 'TEXTAREA' || t.tagName === 'INPUT'); }
    sheet.addEventListener('focusin', function (e) {
        if (!isField(e.target) || sheet.classList.contains('rv2-kb')) return;
        sheet.classList.add('rv2-kb');
        els.srcToggle.textContent = T('more', 'more');
        refreshSrcToggle();
        anchorSheet();
    });
    sheet.addEventListener('focusout', function () {
        // Defer so moving focus between fields inside the sheet doesn't flicker.
        setTimeout(function () {
            if (sheet.hidden) return;
            if (isField(document.activeElement) && sheet.contains(document.activeElement)) return;
            sheet.classList.remove('rv2-kb');
            refreshSrcToggle();
            anchorSheet();
        }, 60);
    });
    if (vv) {
        vv.addEventListener('resize', anchorSheet);
        vv.addEventListener('scroll', anchorSheet);
    }
    window.addEventListener('resize', refreshSrcToggle);

    // ── Long-press label bubble (type icons + chunk-action chips) ──────────────
    const bubble = els.bubble;
    let bTimer = null;
    function showBubble(elm) {
        const label = elm.getAttribute('aria-label') || '';
        if (!label) return;
        const r = elm.getBoundingClientRect();
        bubble.textContent = label;
        bubble.style.left = (r.left + r.width / 2) + 'px';
        bubble.style.top = (r.top - 6) + 'px';
        bubble.classList.add('show');
    }
    function hideBubble() { bubble.classList.remove('show'); }
    sheet.addEventListener('pointerdown', function (e) {
        const t = e.target.closest('.rv2-tp, .rv2-chip');
        if (!t) return;
        bTimer = setTimeout(() => showBubble(t), 450);
    });
    ['pointerup', 'pointercancel'].forEach((ev) =>
        sheet.addEventListener(ev, function () { clearTimeout(bTimer); hideBubble(); }));
    sheet.addEventListener('pointermove', function () { clearTimeout(bTimer); });

    // Paint the static Add-row icons once.
    paintTps(els.addIcons);

    window.ReaderSheetV2 = { onOpen, onClose };
})();
