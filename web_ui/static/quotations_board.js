/* Quotation board: every Bible quotation the quotation pass found, the NBLA
 * wording it proposes, and the diff of whatever is ticked.
 *
 * The server owns every judgement. It builds the cards (mapped.json), stores
 * the choices (board.json), recommends a decision for each card, and computes
 * each edit on an in-memory copy of the current chunk, so what you approve is
 * exactly what `quotation_pass.py apply` writes. This file draws
 * /api/project/<id>/quotations, sends a card's choice back when you change it,
 * and redraws that card from the reply. Nothing is written to the book from here.
 *
 * Each card has a state (apply / not yet / reject) that says what the final
 * approve does with it; the ticks say what the change is. Filtering is
 * presentational: the chips re-draw from the data already loaded.
 */
(function () {
    'use strict';

    var board = document.getElementById('qb-board');
    if (!board) return;

    var PROJECT = board.dataset.project;
    var S = JSON.parse(board.dataset.strings || '{}');
    var API = '/api/project/' + encodeURIComponent(PROJECT) + '/quotations';
    var SAVE_DELAY = 400;
    var PHASES = ['pending', 'approve', 'reject', 'applied'];
    var VERDICTS = ['approve', 'unsure', 'decline'];
    var NO_SCRIPTURE = ['not_scripture', 'unresolved'];

    var data = null;
    var filters = {
        phase: { pending: true, approve: true, reject: true, applied: false },
        rec: { approve: true, unsure: true, decline: true },
        kind: {},
        onlyChanges: true,
        onlyAsked: false
    };
    var focusKey = null;  /* the card the editor sent us back to: always shown */
    var pending = {};     /* card key -> choice waiting to be saved */
    var saveTimer = null;
    var latest = 0;       /* only the newest save's reply is drawn */

    var bar = document.getElementById('qb-bar');
    var barCount = document.getElementById('qb-bar-count');
    var barSave = document.getElementById('qb-bar-save');
    var barGate = document.getElementById('qb-bar-gate');
    var approveBtn = document.getElementById('qb-approve');

    function fill(text, values) {
        return String(text || '').replace(/\{(\w+)\}/g, function (whole, key) {
            return key in values ? (values[key] == null ? '' : values[key]) : whole;
        });
    }

    function el(tag, cls, text) {
        var node = document.createElement(tag);
        if (cls) node.className = cls;
        if (text != null) node.textContent = text;
        return node;
    }

    function day(stamp) {
        return stamp ? String(stamp).replace('T', ' ').slice(0, 16) : '';
    }

    /* --- a word diff, for reading what a change does ----------------------- */

    function tokens(text) {
        return String(text || '').match(/\s+|[^\s]+/g) || [];
    }

    function diffNodes(before, after) {
        var a = tokens(before), b = tokens(after);
        var n = a.length, m = b.length, i, j;
        if (n * m > 250000) return { del: [el('span', null, before)], ins: [el('span', null, after)] };
        var lcs = [];
        for (i = 0; i <= n; i++) { lcs.push(new Array(m + 1).fill(0)); }
        for (i = n - 1; i >= 0; i--) {
            for (j = m - 1; j >= 0; j--) {
                lcs[i][j] = a[i] === b[j] ? lcs[i + 1][j + 1] + 1 : Math.max(lcs[i + 1][j], lcs[i][j + 1]);
            }
        }
        var del = [], ins = [];
        i = 0; j = 0;
        while (i < n || j < m) {
            if (i < n && j < m && a[i] === b[j]) {
                del.push(el('span', null, a[i])); ins.push(el('span', null, b[j])); i++; j++;
            } else if (j < m && (i >= n || lcs[i][j + 1] >= lcs[i + 1][j])) {
                ins.push(el('ins', 'qb-ins', b[j])); j++;
            } else {
                del.push(el('del', 'qb-del', a[i])); i++;
            }
        }
        return { del: del, ins: ins };
    }

    /* The change shown in place: the server sends a few sentences of the
       chapter around it (`context`); without them, the bare span. */
    function changeBlock(item) {
        var ctx = item.context;
        return ctx ? diffBlock(ctx.before, ctx.after) : diffBlock(item.old, item.new);
    }

    function diffBlock(before, after, labels) {
        labels = labels || [S.now, S.becomes];
        var wrap = el('div', 'qb-diff');
        var parts = diffNodes(before, after);
        var now = el('div', 'qb-diff-row');
        now.appendChild(el('span', 'qb-diff-label', labels[0]));
        var nowText = el('span', 'qb-diff-text');
        parts.del.forEach(function (n) { nowText.appendChild(n); });
        now.appendChild(nowText);
        var then = el('div', 'qb-diff-row');
        then.appendChild(el('span', 'qb-diff-label', labels[1]));
        var thenText = el('span', 'qb-diff-text');
        parts.ins.forEach(function (n) { thenText.appendChild(n); });
        then.appendChild(thenText);
        wrap.appendChild(now);
        wrap.appendChild(then);
        return wrap;
    }

    /* --- which cards show ---------------------------------------------------- */

    function choiceOf(card) {
        return pending[card.key] || card.choice;
    }

    /* Mirrors the server's `phase`, on the choice as it stands on this page. */
    function phaseOf(card) {
        var c = choiceOf(card);
        var h = card.history || {};
        if (h.action === 'applied' && !(c.hand && !h.hand)) return 'applied';
        return c.state;
    }

    function changes(card) {
        if (card.ref_edit) return true;
        if ((card.echo || []).length) return true;
        return (card.quote_edits || []).some(function (e) { return e.old !== e.new; });
    }

    function shown(card) {
        if (card.key === focusKey) return true;
        var c = choiceOf(card);
        var ph = phaseOf(card);
        if (!filters.phase[ph]) return false;
        if (filters.rec[card.recommendation.verdict] === false) return false;
        if (filters.kind[card.kind] === false) return false;
        var asked = !!(c.ask || card.revised);
        if (filters.onlyAsked && !asked) return false;
        if (filters.onlyChanges && ph !== 'applied' &&
            !(changes(card) || card.status === 'not_scripture' || asked || c.hand)) return false;
        return true;
    }

    /* --- one card ------------------------------------------------------------ */

    function setChoice(card, change) {
        var c = JSON.parse(JSON.stringify(choiceOf(card)));
        Object.keys(change).forEach(function (k) { c[k] = change[k]; });
        /* Applying a card with nothing ticked would do nothing: tick its change. */
        if (change.state === 'approve' && !(c.ref || c.nbla || (c.echo || []).length || c.paste || c.hand)) {
            if ((card.quote_edits || []).length) c.nbla = true;
            if (card.ref_edit) c.ref = true;
        }
        if (c.ask) c.state = 'pending';
        pending[card.key] = c;
        scheduleSave();
    }

    function checkbox(label, checked, disabled, onChange) {
        var row = el('label', 'qb-check');
        var box = document.createElement('input');
        box.type = 'checkbox';
        box.checked = !!checked;
        box.disabled = !!disabled;
        box.addEventListener('change', function () { onChange(box.checked); });
        row.appendChild(box);
        row.appendChild(el('span', null, label));
        return row;
    }

    function landingWarning(card, what, n) {
        if (choiceOf(card).hand) return null;
        var edits = card.edits || [];
        var miss = edits.some(function (e) {
            return e.what === what && !e.lands && (n == null || e.n === n);
        });
        return miss ? el('p', 'br-warn qb-small', S.not_landing) : null;
    }

    function stateControl(card, choice) {
        var group = el('div', 'qb-state');
        group.setAttribute('role', 'group');
        group.setAttribute('aria-label', S.state_label);
        group.appendChild(el('span', 'qb-label', S.state_label));
        ['approve', 'pending', 'reject'].forEach(function (state) {
            var btn = el('button', 'qb-state-btn qb-state-' + state, (S.states || {})[state] || state);
            btn.type = 'button';
            var on = choice.state === state;
            btn.setAttribute('aria-pressed', on ? 'true' : 'false');
            /* An open ask holds the card until the agent revises it. */
            btn.disabled = !!choice.ask && state !== 'pending';
            btn.addEventListener('click', function () {
                if (!on) setChoice(card, { state: state });
            });
            group.appendChild(btn);
        });
        return group;
    }

    function renderCard(card) {
        var choice = choiceOf(card);
        var ph = phaseOf(card);
        var past = card.history || {};
        var locked = !!card.applied;
        var verdict = card.recommendation || {};
        var node = el('article', 'qb-card qb-status-' + card.status + ' qb-phase-' + ph +
            (card.applied ? ' qb-applied' : ''));
        node.dataset.key = card.key;
        node.id = 'card-' + encodeURIComponent(card.key);

        var head = el('div', 'qb-head');
        head.appendChild(el('span', 'qb-kind', (S.kinds || {})[card.kind] || card.kind));
        if (card.ref) head.appendChild(el('span', 'qb-ref', card.ref));
        head.appendChild(el('span', 'qb-badge', (S.statuses || {})[card.status] || card.status));
        head.appendChild(el('span', 'qb-badge qb-rec qb-rec-' + verdict.verdict,
            fill(S.recommend, { verdict: (S.verdicts || {})[verdict.verdict] || verdict.verdict })));
        if (card.confidence === 'low') head.appendChild(el('span', 'qb-badge qb-badge-low', card.confidence));
        if (choice.ask) head.appendChild(el('span', 'qb-badge qb-badge-asked', S.asked));
        if (card.revised) head.appendChild(el('span', 'qb-badge qb-badge-asked', S.revised));
        if (past.action === 'applied') {
            head.appendChild(el('span', 'qb-badge qb-badge-done',
                fill(past.hand ? S.applied_hand : S.applied, { date: day(past.ts) })));
        } else if (past.action === 'declined') {
            head.appendChild(el('span', 'qb-badge', fill(S.declined, { date: day(past.ts) })));
        }
        node.appendChild(head);

        node.appendChild(el('p', 'qb-reason',
            fill((S.reasons || {})[verdict.rule] || verdict.rule, { detail: verdict.detail || '' })));

        var en = el('p', 'qb-en');
        en.appendChild(el('span', 'qb-label', S.english + ': '));
        en.appendChild(el('span', null, card.en_span));
        node.appendChild(en);

        if (card.revised) node.appendChild(el('p', 'br-muted', fill(S.revised_note, { ask: card.revised.ask || '' })));
        if (card.adapted) node.appendChild(el('p', 'br-warn qb-small', fill(S.adapted, { why: card.why || '' })));
        if (card.drift) node.appendChild(el('p', 'br-warn qb-small', fill(S.drift, { drift: card.drift })));
        if (card.note) node.appendChild(el('p', 'br-muted', fill(S.note, { note: card.note })));
        if (card.status === 'unresolved') node.appendChild(el('p', 'br-warn qb-small', fill(S.unresolved, { reason: card.unresolved_reason || '' })));
        if ((card.refusals || []).length) {
            node.appendChild(el('p', 'br-muted', fill(S.refused, {
                list: card.refusals.map(function (r) { return r.what + ' ' + r.reason; }).join('; ')
            })));
        }

        if (ph !== 'applied') node.appendChild(stateControl(card, choice));

        /* What went in, from the ledger: the card's own proposal may be older. */
        if (card.applied) {
            (past.edits || []).forEach(function (e) {
                if (e.old !== e.new) node.appendChild(diffBlock(e.old, e.new, [S.was, S.now]));
            });
        }

        if (card.ref_edit && !locked) {
            node.appendChild(checkbox(S.fix_ref + ': ' + card.ref_edit.old + ' → ' + card.ref_edit.new,
                choice.ref, choice.hand, function (on) { setChoice(card, { ref: on }); }));
            if (card.ref_edit.context) node.appendChild(changeBlock(card.ref_edit));
            var w = landingWarning(card, 'ref');
            if (w) node.appendChild(w);
        }

        var quotes = card.quote_edits || [];
        var changing = quotes.filter(function (e) { return e.old !== e.new; });
        if (quotes.length && !locked) {
            node.appendChild(checkbox(S.use_nbla, choice.nbla, choice.hand || !changing.length && card.status !== 'matches',
                function (on) { setChoice(card, { nbla: on }); }));
            changing.forEach(function (e) { node.appendChild(changeBlock(e)); });
            var wq = landingWarning(card, 'quote');
            if (wq) node.appendChild(wq);
        }

        var echo = card.echo || [];
        if (echo.length && !locked) {
            var box = el('div', 'qb-echo');
            box.appendChild(el('p', 'qb-echo-title', S.echo_title));
            echo.forEach(function (e, i) {
                var on = (choice.echo || []).indexOf(i) >= 0;
                box.appendChild(checkbox(fill(S.echo_evidence, { evidence: e.en_evidence }), on, choice.hand, function (checked) {
                    var list = (choiceOf(card).echo || []).filter(function (x) { return x !== i; });
                    if (checked) list.push(i);
                    setChoice(card, { echo: list.sort() });
                }));
                box.appendChild(changeBlock(e));
                var we = landingWarning(card, 'echo', i);
                if (we) box.appendChild(we);
            });
            node.appendChild(box);
        }

        if (NO_SCRIPTURE.indexOf(card.status) >= 0 && card.es_span && !locked) {
            var label = el('label', 'qb-paste');
            label.appendChild(el('span', 'qb-label', S.paste_label));
            label.appendChild(el('span', 'qb-current', card.es_span));
            var area = document.createElement('textarea');
            area.rows = 2;
            area.value = choice.paste || '';
            area.addEventListener('input', function () { setChoice(card, { paste: area.value }); });
            label.appendChild(area);
            node.appendChild(label);
        }

        /* Two ways to a wording other than the proposed one: edit the sentence in
           the book and say so, or ask the agent for a revision. */
        var tools = el('div', 'qb-tools');
        if (card.reader_url) {
            var link = el('a', 'qb-edit-link', S.edit_link);
            link.href = card.reader_url;
            /* The reader has no way back; the hash brings Back to this card. */
            link.addEventListener('click', function () {
                window.history.replaceState(null, '', '#card-' + encodeURIComponent(card.key));
            });
            tools.appendChild(link);
        }
        if (NO_SCRIPTURE.indexOf(card.status) < 0) {
            tools.appendChild(checkbox(S.hand, choice.hand, false, function (on) {
                setChoice(card, on ? { hand: true, state: 'approve' } : { hand: false });
            }));
        }
        node.appendChild(tools);
        if (choice.hand) node.appendChild(el('p', 'br-muted', S.hand_note));

        if (!locked && NO_SCRIPTURE.indexOf(card.status) < 0) {
            var ask = el('label', 'qb-paste qb-ask');
            ask.appendChild(el('span', 'qb-label', S.ask_label));
            var askArea = document.createElement('textarea');
            askArea.rows = 2;
            askArea.value = choice.ask || '';
            askArea.addEventListener('input', function () { setChoice(card, { ask: askArea.value }); });
            ask.appendChild(askArea);
            node.appendChild(ask);
        }
        return node;
    }

    /* --- the filters ----------------------------------------------------------- */

    function chip(label, count, checked, onChange) {
        var row = el('label', 'qb-chip');
        var box = document.createElement('input');
        box.type = 'checkbox';
        box.checked = !!checked;
        box.addEventListener('change', function () { onChange(box.checked); });
        row.appendChild(box);
        row.appendChild(el('span', null, label));
        if (count != null) row.appendChild(el('span', 'qb-chip-count', String(count)));
        return row;
    }

    function axis(title, values, labels, counts, store) {
        var line = el('div', 'qb-axis');
        line.appendChild(el('span', 'qb-axis-label', title));
        values.forEach(function (v) {
            if (!counts[v]) return;
            line.appendChild(chip(labels[v] || v, counts[v], store[v] !== false, function (on) {
                store[v] = on;
                render();
            }));
        });
        return line;
    }

    function tally(cards, fn) {
        var out = {};
        cards.forEach(function (c) { var k = fn(c); out[k] = (out[k] || 0) + 1; });
        return out;
    }

    function filterPanel() {
        var cards = allCards();
        var panel = el('div', 'qb-filters');
        panel.appendChild(axis(S.phase_label, PHASES, S.phases || {}, tally(cards, phaseOf), filters.phase));
        panel.appendChild(axis(S.rec_label, VERDICTS, S.verdicts || {},
            tally(cards, function (c) { return c.recommendation.verdict; }), filters.rec));
        var kinds = tally(cards, function (c) { return c.kind; });
        panel.appendChild(axis(S.kind_label, Object.keys(kinds).sort(), S.kinds || {}, kinds, filters.kind));
        var more = el('div', 'qb-axis');
        more.appendChild(chip(S.only_changes, null, filters.onlyChanges, function (on) {
            filters.onlyChanges = on; render();
        }));
        more.appendChild(chip(S.only_asked, null, filters.onlyAsked, function (on) {
            filters.onlyAsked = on; render();
        }));
        panel.appendChild(more);
        return panel;
    }

    /* --- the board ----------------------------------------------------------- */

    function allCards() {
        var out = [];
        ((data && data.chapters) || []).forEach(function (ch) { ch.cards.forEach(function (c) { out.push(c); }); });
        return out;
    }

    function render() {
        board.textContent = '';
        if (!data || data.status !== 'ok' || !allCards().length) {
            board.appendChild(el('p', 'br-status', S.empty));
            bar.hidden = true;
            return;
        }
        board.appendChild(el('p', 'br-meta', fill(S.meta, {
            cards: data.counts.cards, model: data.worker_model || '?',
            date: day(data.committed_at), applied: data.counts.applied
        })));
        board.appendChild(filterPanel());

        var any = false;
        data.chapters.forEach(function (ch) {
            var cards = ch.cards.filter(shown);
            if (!cards.length) return;
            any = true;
            var section = el('section', 'qb-chapter');
            section.appendChild(el('h2', 'qb-chapter-title', ch.chapter_id.replace('chapter_', '') + ' · ' + cards.length));
            cards.forEach(function (card) { section.appendChild(renderCard(card)); });
            board.appendChild(section);
        });
        if (!any) board.appendChild(el('p', 'br-status', S.none_shown));
        updateBar();
    }

    function updateBar() {
        bar.hidden = false;
        var by = tally(allCards(), phaseOf);
        barCount.textContent = fill(S.bar, {
            approve: by.approve || 0, reject: by.reject || 0, pending: by.pending || 0
        });
        var approved = data.approved;
        if (approved && !approved.stale) {
            barGate.textContent = fill(S.approved, { date: day(approved.at) });
            approveBtn.textContent = S.withdraw;
            approveBtn.dataset.mode = 'withdraw';
        } else {
            barGate.textContent = approved && approved.stale ? S.approved_stale : '';
            approveBtn.textContent = S.approve;
            approveBtn.dataset.mode = 'approve';
        }
        approveBtn.disabled = Object.keys(pending).length > 0 ||
            approveBtn.dataset.mode === 'approve' && !(data.counts && data.counts.to_record);
    }

    /* --- talking to the server ---------------------------------------------- */

    function post(path, body) {
        return fetch(API + path, {
            method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body)
        }).then(function (response) {
            return response.json().then(function (json) { return { ok: response.ok, json: json }; });
        });
    }

    function redrawCards() {
        /* Redraw each visible card in place; a textarea being typed in keeps its card. */
        var active = document.activeElement;
        var typing = active && active.tagName === 'TEXTAREA' ? active : null;
        allCards().forEach(function (card) {
            var old = board.querySelector('[data-key="' + CSS.escape(card.key) + '"]');
            if (!old || (typing && old.contains(typing))) return;
            old.replaceWith(renderCard(card));
        });
        updateBar();
    }

    function scheduleSave() {
        approveBtn.disabled = true;
        barSave.textContent = S.saving;
        clearTimeout(saveTimer);
        saveTimer = setTimeout(save, SAVE_DELAY);
        redrawCards();
    }

    function save() {
        var sending = pending;
        pending = {};
        var mine = ++latest;
        post('/choices', { choices: sending }).then(function (reply) {
            if (mine !== latest) return;
            if (!reply.ok) {
                barSave.textContent = fill(S.save_failed, { error: reply.json.error || '?' });
                return;
            }
            data = reply.json;
            barSave.textContent = S.saved;
            redrawCards();
        }).catch(function (err) {
            barSave.textContent = fill(S.save_failed, { error: String(err) });
        });
    }

    approveBtn.addEventListener('click', function () {
        var body = approveBtn.dataset.mode === 'withdraw' ? { withdraw: true } : {};
        approveBtn.disabled = true;
        post('/approve', body).then(function (reply) {
            if (!reply.ok) {
                barSave.textContent = fill(S.save_failed, { error: reply.json.error || '?' });
                updateBar();
                return;
            }
            data = reply.json;
            updateBar();
        });
    });

    /* Back from the reader: show that card, wherever the filters are. */
    function focusFromHash() {
        var hash = window.location.hash || '';
        if (hash.indexOf('#card-') !== 0) return;
        try { focusKey = decodeURIComponent(hash.slice(6)); } catch (e) { focusKey = null; }
    }

    function scrollToFocus() {
        if (!focusKey) return;
        var node = board.querySelector('[data-key="' + CSS.escape(focusKey) + '"]');
        if (node) node.scrollIntoView({ block: 'center' });
    }

    function load() {
        fetch(API).then(function (r) { return r.json(); }).then(function (json) {
            var y = window.scrollY;
            data = json;
            render();
            if (focusKey) scrollToFocus();
            else window.scrollTo(0, y);
        }).catch(function (err) {
            board.textContent = '';
            board.appendChild(el('p', 'br-status', String(err)));
        });
    }

    document.getElementById('qb-refresh').addEventListener('click', load);
    document.addEventListener('visibilitychange', function () {
        if (document.visibilityState === 'visible' && !Object.keys(pending).length) load();
    });
    window.addEventListener('pageshow', function (e) {
        if (!e.persisted) return;
        focusFromHash();
        scrollToFocus();
    });
    focusFromHash();
    load();
})();
