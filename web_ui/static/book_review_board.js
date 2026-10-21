/* Book review board: the recommendations a book review wrote, the evidence
 * behind each, and the dry run of whatever is picked.
 *
 * The server owns every judgement here. It says what is applied, what went
 * stale and what conflicts; it validates and stores the picks (board.json);
 * and it computes the diffs, so what you approve is exactly what
 * `book_review.py apply --from-board --yes` writes. This file only draws the
 * state `/api/project/<id>/book-review` returns, sends the picks and notes
 * back, and redraws the preview from each reply. The cards themselves are
 * drawn once per load, so a save never takes away a note you are typing.
 */
(function () {
    'use strict';

    var board = document.getElementById('br-board');
    if (!board) return;

    var PROJECT = board.dataset.project;
    var S = JSON.parse(board.dataset.strings || '{}');
    var API = '/api/project/' + encodeURIComponent(PROJECT) + '/book-review';
    var SAVE_DELAY = 400;

    var data = null;
    var picked = {};      /* rec id -> {n: true} for the changes picked */
    var notes = {};       /* rec id -> note text */
    var revise = {};      /* rec id -> true: the agent rewrites it from the note */
    var saveTimer = null;
    var inFlight = 0;
    var dirty = false;

    var bar = document.getElementById('br-bar');
    var barCount = document.getElementById('br-bar-count');
    var barSave = document.getElementById('br-bar-save');
    var barLink = document.getElementById('br-bar-link');
    barLink.textContent = S.to_preview;

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

    function recOf(id) {
        var recs = (data && data.recommendations) || [];
        for (var i = 0; i < recs.length; i++) if (recs[i].id === id) return recs[i];
        return null;
    }

    /* --- picks <-> apply's --select tokens -------------------------------- */

    function readSelection(selection) {
        picked = {};
        (selection || []).forEach(function (token) {
            var dot = token.indexOf('.');
            var id = dot < 0 ? token : token.slice(0, dot);
            var rec = recOf(id);
            if (!rec) return;
            picked[id] = picked[id] || {};
            if (dot < 0) {
                rec.patches.forEach(function (p) { picked[id][p.n] = true; });
            } else {
                picked[id][parseInt(token.slice(dot + 1), 10)] = true;
            }
        });
    }

    function selection() {
        var out = [];
        data.recommendations.forEach(function (rec) {
            var mine = picked[rec.id];
            if (!mine) return;
            var ns = rec.patches.filter(function (p) { return mine[p.n]; })
                .map(function (p) { return p.n; });
            if (!ns.length) return;
            if (ns.length === rec.patches.length) out.push(rec.id);
            else ns.forEach(function (n) { out.push(rec.id + '.' + n); });
        });
        return out;
    }

    function changeCount() {
        var n = 0;
        Object.keys(picked).forEach(function (id) {
            n += Object.keys(picked[id]).filter(function (k) { return picked[id][k]; }).length;
        });
        return n;
    }

    function isPicked(id) {
        var mine = picked[id];
        return !!mine && Object.keys(mine).some(function (k) { return mine[k]; });
    }

    /* --- saving ------------------------------------------------------------ */

    function scheduleSave() {
        dirty = true;
        var approve = document.querySelector('.br-gate .br-btn-primary');
        if (approve) approve.disabled = true;
        updateBar();
        barSave.textContent = S.saving;
        clearTimeout(saveTimer);
        saveTimer = setTimeout(save, SAVE_DELAY);
    }

    function save() {
        saveTimer = null;
        inFlight++;
        var body = JSON.stringify({
            selection: selection(), notes: notes,
            revise: Object.keys(revise).filter(function (id) { return revise[id]; })
        });
        fetch(API + '/selection', {
            method: 'POST', headers: { 'Content-Type': 'application/json' }, body: body
        }).then(function (response) {
            return response.json().then(function (json) { return { ok: response.ok, json: json }; });
        }).then(function (reply) {
            inFlight--;
            if (!reply.ok) {
                barSave.textContent = fill(S.save_failed, { error: reply.json.error });
                return;
            }
            if (!saveTimer) dirty = false;
            data = reply.json;
            barSave.textContent = S.saved;
            renderPreview();
        }).catch(function (err) {
            inFlight--;
            barSave.textContent = fill(S.save_failed, { error: String(err) });
        });
    }

    function post(path, payload) {
        return fetch(API + path, {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload)
        }).then(function (response) {
            return response.json().then(function (json) {
                if (!response.ok) throw new Error(json.error || response.statusText);
                return json;
            });
        });
    }

    /* --- the cards ---------------------------------------------------------- */

    function textBlock(label, text, cls) {
        var wrap = el('div', 'br-block ' + (cls || ''));
        wrap.appendChild(el('div', 'br-block-label', label));
        wrap.appendChild(el('div', 'br-block-text', text));
        return wrap;
    }

    /* A before or after passage with the words that change marked. */
    function markedBlock(label, parts, cls) {
        var wrap = el('div', 'br-block ' + cls);
        wrap.appendChild(el('div', 'br-block-label', label));
        var text = el('div', 'br-block-text');
        parts.forEach(function (part) {
            text.appendChild(part[1] ? el('mark', 'br-word', part[0]) : document.createTextNode(part[0]));
        });
        wrap.appendChild(text);
        return wrap;
    }

    function findingView(f) {
        var item = el('li', 'br-finding');
        if (f.missing) {
            item.appendChild(el('span', 'br-muted', fill(S.missing_finding, { id: f.id })));
            return item;
        }
        var head = [f.chapter, f.judge, f.rule].filter(Boolean).join(' · ');
        var meta = el('div', 'br-finding-meta', head);
        if (f.mark) meta.appendChild(el('span', 'br-mark br-mark-' + f.mark,
                                        fill(S.marked, { mark: f.mark.replace(/_/g, ' ') })));
        item.appendChild(meta);
        item.appendChild(el('div', 'br-finding-msg', f.message || ''));
        if (f.excerpt) {
            var quote = el('div', 'br-finding-quote');
            quote.appendChild(el('span', 'br-es', '«' + f.excerpt + '»'));
            if (f.suggestion) {
                quote.appendChild(el('span', 'br-arrow', ' → '));
                quote.appendChild(el('span', 'br-es br-sugg', '«' + f.suggestion + '»'));
            }
            item.appendChild(quote);
        }
        if (f.english) {
            var en = el('div', 'br-finding-en');
            en.appendChild(el('span', 'br-block-label', S.english + ' '));
            en.appendChild(document.createTextNode(f.english));
            item.appendChild(en);
        }
        return item;
    }

    function findingList(findings) {
        var list = el('ul', 'br-findings');
        findings.forEach(function (f) { list.appendChild(findingView(f)); });
        return list;
    }

    function patchDetail(p) {
        var box = el('div', 'br-patch-detail');
        var raw = p.patch || {};
        if (p.current) {
            var c = p.current;
            var line = c.english + ' → ' + c.spanish;
            if (c.alternatives && c.alternatives.length) line += ' (' + c.alternatives.join(', ') + ')';
            if (c.context) line += '\n' + c.context;
            box.appendChild(textBlock(S.current_entry, line, 'br-block-now'));
        }
        if (p.type === 'style.replace_text' || p.type === 'address.replace_global_rules_text') {
            var words = wordDiff(raw.before || '', raw.after || '');
            box.appendChild(markedBlock(S.now, words ? words.del : [[raw.before, false]], 'br-block-now'));
            box.appendChild(markedBlock(S.becomes, words ? words.add : [[raw.after, false]], 'br-block-after'));
        } else if (p.type === 'style.append') {
            box.appendChild(textBlock(S.adds, raw.text, 'br-block-after'));
        } else if (p.type === 'glossary.edit' && raw.context) {
            box.appendChild(textBlock(S.becomes, raw.context, 'br-block-after'));
        } else if (p.type === 'glossary.split_sense' || p.type === 'glossary.add') {
            var entries = raw.entries || [raw];
            box.appendChild(textBlock(S.becomes, entries.map(function (e) {
                return e.english + ' → ' + e.spanish + (e.context ? '\n  ' + e.context : '');
            }).join('\n'), 'br-block-after'));
        } else if (p.type === 'manual') {
            box.appendChild(el('div', 'br-block-text', raw.description || ''));
        } else if (p.type === 'house.rule_proposal') {
            box.appendChild(textBlock(S.adds, raw.rule, 'br-block-after'));
            if (raw.why) box.appendChild(textBlock(S.why, raw.why));
        } else if (p.type === 'rejudge' || p.type === 'retranslate') {
            box.appendChild(textBlock(S.chapters, (raw.chapters || []).join(', ')));
            if (raw.why) box.appendChild(textBlock(S.why, raw.why));
        } else if (p.type === 'address.set_direction' || p.type === 'address.add_pair') {
            if (raw.notes) box.appendChild(textBlock(S.why, raw.notes));
        }
        if (p.findings) {
            var more = el('details', 'br-more');
            more.appendChild(el('summary', null, fill(S.show_findings, { count: p.findings.length })));
            more.appendChild(findingList(p.findings));
            box.appendChild(more);
        }
        return box.childNodes.length ? box : null;
    }

    function evidenceView(rec) {
        var more = el('details', 'br-more br-evidence');
        more.appendChild(el('summary', null, fill(S.evidence, { count: rec.evidence.length })));
        var signals = el('ul', 'br-signals');
        var findings = [];
        rec.evidence.forEach(function (e) {
            if (e.kind === 'signal') {
                var item = el('li', 'br-signal');
                item.appendChild(el('div', 'br-signal-title', e.title));
                if (e.summary) item.appendChild(el('div', 'br-signal-sum', e.summary));
                var bits = [];
                if (e.findings) bits.push(fill(S.signal_findings, { count: e.findings }));
                if (e.chapters && e.chapters.length) {
                    bits.push(e.chapters.slice(0, 12).join(', ') + (e.chapters.length > 12 ? ' …' : ''));
                }
                if (bits.length) item.appendChild(el('div', 'br-muted', bits.join(' · ')));
                signals.appendChild(item);
            } else if (e.kind === 'finding') {
                findings.push(e);
            } else {
                signals.appendChild(el('li', 'br-muted', e.id));
            }
        });
        if (signals.childNodes.length) more.appendChild(signals);
        if (findings.length) more.appendChild(findingList(findings));
        return more;
    }

    function setPicked(rec, on) {
        picked[rec.id] = {};
        if (on) rec.patches.forEach(function (p) { picked[rec.id][p.n] = true; });
    }

    function unpickConflicts(rec, card) {
        var dropped = [];
        (rec.conflicts_with || []).forEach(function (other) {
            if (isPicked(other)) {
                picked[other] = {};
                syncCard(other);
                dropped.push(other);
            }
        });
        var notice = card.querySelector('.br-notice');
        notice.textContent = dropped.length ? fill(S.unpicked, { id: dropped.join(', ') }) : '';
    }

    function blockedOf(rec) {
        return !!rec.applied_at || rec.stale.length > 0 || !!revise[rec.id];
    }

    function syncCard(id) {
        var card = document.getElementById('card-' + id);
        var rec = recOf(id);
        if (!card || !rec) return;
        var on = isPicked(id);
        var blocked = blockedOf(rec);
        card.classList.toggle('br-picked', on);
        card.classList.toggle('br-waiting', !!revise[id]);
        var main = card.querySelector('.br-pick input');
        var count = rec.patches.filter(function (p) { return picked[id] && picked[id][p.n]; }).length;
        main.checked = on;
        main.disabled = blocked;
        main.indeterminate = on && count < rec.patches.length;
        card.querySelectorAll('.br-patch-pick input').forEach(function (box) {
            box.checked = !!(picked[id] && picked[id][box.dataset.n]);
            box.disabled = blocked;
        });
        var ask = card.querySelector('.br-revise input');
        if (ask) {
            ask.checked = !!revise[id];
            ask.disabled = !!rec.applied_at || !(notes[id] || '').trim();
        }
    }

    function patchList(patches, pickable, rec, node) {
        var list = el('ol', 'br-patches');
        patches.forEach(function (p) {
            var item = el('li', 'br-patch');
            var line = el('div', 'br-patch-line');
            if (pickable && patches.length > 1) {
                var sub = el('label', 'br-patch-pick');
                var subBox = document.createElement('input');
                subBox.type = 'checkbox';
                subBox.dataset.n = p.n;
                subBox.setAttribute('aria-label', fill(S.change_n, { n: p.n }));
                subBox.addEventListener('change', function () {
                    picked[rec.id] = picked[rec.id] || {};
                    picked[rec.id][p.n] = subBox.checked;
                    if (subBox.checked) unpickConflicts(rec, node);
                    syncCard(rec.id);
                    scheduleSave();
                });
                sub.appendChild(subBox);
                line.appendChild(sub);
            }
            line.appendChild(el('span', 'br-patch-sum', p.summary));
            item.appendChild(line);
            var detail = patchDetail(p);
            if (detail) item.appendChild(detail);
            list.appendChild(item);
        });
        return list;
    }

    function revisionView(rec) {
        var r = rec.revision;
        var box = el('div', 'br-revision');
        box.appendChild(el('p', 'br-revision-head', fill(S.revised, { n: r.n, date: day(r.at) })));
        if (r.note) box.appendChild(textBlock(S.your_note, r.note));
        box.appendChild(textBlock(S.what_changed, r.why));
        var more = el('details', 'br-more');
        more.appendChild(el('summary', null, fill(S.original_changes, { count: r.original.length })));
        if (r.original_rationale && r.original_rationale !== rec.rationale) {
            more.appendChild(el('p', 'br-muted', r.original_rationale));
        }
        more.appendChild(patchList(r.original, false, rec, null));
        box.appendChild(more);
        return box;
    }

    function card(rec) {
        var node = el('article', 'br-card');
        node.id = 'card-' + rec.id;
        if (rec.applied_at) node.classList.add('br-applied');
        if (rec.stale.length) node.classList.add('br-stale');

        var head = el('header', 'br-card-head');
        var pick = el('label', 'br-pick');
        var box = document.createElement('input');
        box.type = 'checkbox';
        box.addEventListener('change', function () {
            setPicked(rec, box.checked);
            if (box.checked) unpickConflicts(rec, node);
            syncCard(rec.id);
            scheduleSave();
        });
        pick.appendChild(box);
        pick.appendChild(el('span', 'br-title', rec.title));
        head.appendChild(pick);

        var tags = el('div', 'br-tags');
        tags.appendChild(el('span', 'br-tag br-id', rec.id));
        tags.appendChild(el('span', 'br-tag br-theme-' + rec.theme, (S.themes || {})[rec.theme] || rec.theme));
        tags.appendChild(el('span', 'br-tag br-conf-' + rec.confidence,
                            (S.confidence || {})[rec.confidence] || rec.confidence));
        if (rec.applied_at) tags.appendChild(el('span', 'br-tag br-tag-applied',
                                                fill(S.applied, { date: day(rec.applied_at) })));
        head.appendChild(tags);
        node.appendChild(head);

        if (rec.stale.length) {
            node.appendChild(el('p', 'br-warn', fill(S.stale, { reasons: rec.stale.join('; ') })));
        }
        if (rec.revision) node.appendChild(revisionView(rec));
        if (rec.conflicts_with.length) {
            node.appendChild(el('p', 'br-conflict', fill(S.conflicts, { ids: rec.conflicts_with.join(', ') })));
        }
        node.appendChild(el('p', 'br-notice'));
        node.appendChild(el('p', 'br-rationale', rec.rationale || ''));
        if (rec.problems && rec.problems.length) {
            node.appendChild(el('p', 'br-muted', fill(S.checks, { list: rec.problems.join('; ') })));
        }

        var changes = el('section', 'br-changes');
        changes.appendChild(el('h4', null, S.changes));
        changes.appendChild(patchList(rec.patches, true, rec, node));
        node.appendChild(changes);

        if (rec.follow_ups.length) {
            var then = el('section', 'br-changes br-then');
            then.appendChild(el('h4', null, S.then));
            var ul = el('ul', 'br-patches');
            rec.follow_ups.forEach(function (f) {
                var li = el('li', 'br-patch');
                li.appendChild(el('div', 'br-patch-sum', f.summary));
                var detail = patchDetail(f);
                if (detail) li.appendChild(detail);
                ul.appendChild(li);
            });
            then.appendChild(ul);
            node.appendChild(then);
        }

        if (rec.evidence.length) node.appendChild(evidenceView(rec));

        var noteWrap = el('label', 'br-note');
        noteWrap.appendChild(el('span', 'br-block-label', S.note_label));
        var note = document.createElement('textarea');
        note.rows = 2;
        note.placeholder = S.note_placeholder;
        note.value = notes[rec.id] || '';
        note.addEventListener('input', function () {
            notes[rec.id] = note.value;
            if (!note.value.trim()) revise[rec.id] = false;
            syncCard(rec.id);
            scheduleSave();
        });
        noteWrap.appendChild(note);
        node.appendChild(noteWrap);

        var ask = el('label', 'br-revise');
        var askBox = document.createElement('input');
        askBox.type = 'checkbox';
        askBox.addEventListener('change', function () {
            revise[rec.id] = askBox.checked;
            if (askBox.checked) setPicked(rec, false);
            syncCard(rec.id);
            scheduleSave();
        });
        ask.appendChild(askBox);
        ask.appendChild(el('span', null, S.revise_label));
        node.appendChild(ask);
        node.appendChild(el('p', 'br-waiting-note', S.revise_waiting));
        return node;
    }

    /* --- the preview -------------------------------------------------------- */

    /* Which words of `a` and `b` are shared, by longest common subsequence.
       A style-guide paragraph or the address map's global rules is one line,
       so without this a one-word change shows as two whole paragraphs. */
    var WORD_DIFF_MAX = 1500;

    function wordDiff(a, b) {
        var x = a.split(/(\s+)/), y = b.split(/(\s+)/);
        if (x.length > WORD_DIFF_MAX || y.length > WORD_DIFF_MAX) return null;
        var n = x.length, m = y.length, i, j;
        var table = [];
        for (i = 0; i <= n; i++) table.push(new Uint16Array(m + 1));
        for (i = n - 1; i >= 0; i--) {
            for (j = m - 1; j >= 0; j--) {
                table[i][j] = x[i] === y[j] ? table[i + 1][j + 1] + 1
                                            : Math.max(table[i + 1][j], table[i][j + 1]);
            }
        }
        var del = [], add = [];
        i = 0; j = 0;
        while (i < n || j < m) {
            if (i < n && j < m && x[i] === y[j]) {
                del.push([x[i], false]); add.push([y[j], false]); i++; j++;
            } else if (j < m && (i >= n || table[i][j + 1] >= table[i + 1][j])) {
                add.push([y[j], true]); j++;
            } else {
                del.push([x[i], true]); i++;
            }
        }
        return { del: del, add: add };
    }

    function diffLine(cls, sign, parts) {
        var line = el('span', 'br-dl ' + cls);
        line.appendChild(document.createTextNode(sign));
        parts.forEach(function (part) {
            line.appendChild(part[1] ? el('mark', 'br-word', part[0]) : document.createTextNode(part[0]));
        });
        line.appendChild(document.createTextNode('\n'));
        return line;
    }

    function diffView(name, text) {
        var wrap = el('div', 'br-diff');
        wrap.appendChild(el('div', 'br-diff-name', name));
        var pre = el('pre', 'br-diff-body');
        var lines = text.split('\n');
        for (var k = 0; k < lines.length; k++) {
            var line = lines[k];
            var next = lines[k + 1];
            var pairs = line.charAt(0) === '-' && !/^---/.test(line) &&
                        next != null && next.charAt(0) === '+' && !/^\+\+\+/.test(next) &&
                        (lines[k + 2] == null || lines[k + 2].charAt(0) !== '+');
            var words = pairs ? wordDiff(line.slice(1), next.slice(1)) : null;
            if (words) {
                pre.appendChild(diffLine('br-dl-del', '-', words.del));
                pre.appendChild(diffLine('br-dl-add', '+', words.add));
                k++;
                continue;
            }
            var cls = 'br-dl';
            if (/^(\+\+\+|---)/.test(line)) cls += ' br-dl-file';
            else if (line.charAt(0) === '+') cls += ' br-dl-add';
            else if (line.charAt(0) === '-') cls += ' br-dl-del';
            else if (line.indexOf('@@') === 0) cls += ' br-dl-hunk';
            pre.appendChild(el('span', cls, line + '\n'));
        }
        wrap.appendChild(pre);
        return wrap;
    }

    function actionSummary(action) {
        var parts = action.id.split('.');
        var rec = recOf(parts[0]);
        if (rec && parts[1] === 'follow_up') {
            var f = rec.follow_ups.filter(function (x) { return x.type === action.type; })[0];
            return f ? f.summary : action.type;
        }
        var p = rec && rec.patches[parseInt(parts[1], 10) - 1];
        return p ? p.summary : action.type;
    }

    function renderPreview() {
        var box = document.getElementById('br-preview');
        if (!box) return;
        box.innerHTML = '';
        box.appendChild(el('h2', null, S.preview_title));
        var preview = data.preview;
        if (!preview || !data.selection.length) {
            box.appendChild(el('p', 'br-muted', S.preview_none));
            updateBar();
            return;
        }
        if (preview.status === 'error') {
            box.appendChild(el('p', 'br-warn', fill(S.preview_error, { error: preview.error })));
            updateBar();
            return;
        }
        var diffs = preview.diffs || {};
        var names = Object.keys(diffs);
        if (!names.length) box.appendChild(el('p', 'br-muted', S.no_artifact_change));
        names.forEach(function (name) { box.appendChild(diffView(name, diffs[name])); });

        var others = (preview.actions || []).filter(function (a) {
            return !/^(glossary|style|address)\./.test(a.type);
        });
        if (others.length) {
            box.appendChild(el('h3', null, S.actions_title));
            var list = el('ul', 'br-actions');
            others.forEach(function (a) {
                var item = el('li', null);
                item.appendChild(el('span', 'br-tag br-id', a.id.replace('.follow_up', '')));
                item.appendChild(document.createTextNode(' ' + actionSummary(a)));
                if (a.status === 'offer' || a.type === 'manual' || a.type === 'retranslate') {
                    item.appendChild(el('span', 'br-muted', ' (' + S.offer + ')'));
                }
                list.appendChild(item);
            });
            box.appendChild(list);
        }

        var gate = el('div', 'br-gate');
        if (data.approved) {
            gate.appendChild(el('p', 'br-approved', fill(S.approved, { at: day(data.approved.at) })));
            var withdraw = el('button', 'br-btn', S.withdraw);
            withdraw.type = 'button';
            withdraw.addEventListener('click', function () {
                post('/approve', { withdraw: true }).then(function (json) {
                    data = json;
                    renderPreview();
                }).catch(function (err) { barSave.textContent = String(err.message || err); });
            });
            gate.appendChild(withdraw);
        } else {
            gate.appendChild(el('p', 'br-muted', S.approve_hint));
            var approve = el('button', 'br-btn br-btn-primary', S.approve);
            approve.type = 'button';
            approve.disabled = dirty || inFlight > 0;
            approve.addEventListener('click', function () {
                approve.disabled = true;
                post('/approve', { selection: data.selection }).then(function (json) {
                    data = json;
                    renderPreview();
                }).catch(function (err) {
                    approve.disabled = false;
                    barSave.textContent = String(err.message || err);
                });
            });
            gate.appendChild(approve);
        }
        box.appendChild(gate);
        updateBar();
    }

    function updateBar() {
        if (!data || !data.recommendations.length) { bar.hidden = true; return; }
        bar.hidden = false;
        var recs = Object.keys(picked).filter(isPicked).length;
        barCount.textContent = recs ? fill(S.count_line, { recs: recs, changes: changeCount() })
                                    : S.nothing_picked;
    }

    /* --- the page ------------------------------------------------------------ */

    function render() {
        board.innerHTML = '';
        if (data.status !== 'ok' || !data.recommendations.length) {
            board.appendChild(el('p', 'br-status', S.empty));
            updateBar();
            return;
        }
        notes = Object.assign({}, data.notes || {});
        revise = {};
        (data.revise || []).forEach(function (id) { revise[id] = true; });
        readSelection(data.selection);

        board.appendChild(el('p', 'br-meta', fill(S.meta, {
            count: data.recommendations.length,
            model: [data.worker_model, data.cli].filter(Boolean).join(', '),
            date: day(data.committed_at)
        })));
        var owed = Object.keys(data.owed || {});
        if (owed.length) {
            board.appendChild(el('p', 'br-warn', fill(S.owed, {
                list: owed.map(function (j) {
                    return fill(S.owed_item, { judge: j, count: data.owed[j] });
                }).join('; ')
            })));
        }
        if (data.omitted && data.omitted.length) {
            board.appendChild(el('p', 'br-muted', fill(S.omitted, {
                list: data.omitted.map(function (o) { return o.signal_id; }).join(', ')
            })));
        }

        [1, 2, 3].forEach(function (priority) {
            var group = data.recommendations.filter(function (r) { return r.priority === priority; });
            if (!group.length) return;
            var section = el('section', 'br-group');
            section.appendChild(el('h2', null, (S.priorities || {})[priority] + ' (' + group.length + ')'));
            group.forEach(function (rec) { section.appendChild(card(rec)); });
            board.appendChild(section);
        });

        if (data.no_action.length) {
            var none = el('details', 'br-more br-group');
            none.appendChild(el('summary', null, fill(S.no_action_title, { count: data.no_action.length })));
            var ul = el('ul', 'br-plain');
            data.no_action.forEach(function (n) {
                var li = el('li');
                li.appendChild(el('code', null, n.signal_id));
                li.appendChild(document.createTextNode(' ' + n.reason));
                ul.appendChild(li);
            });
            none.appendChild(ul);
            board.appendChild(none);
        }
        if (data.rejected.length) {
            var bad = el('details', 'br-more br-group');
            bad.appendChild(el('summary', null, fill(S.rejected_title, { count: data.rejected.length })));
            var ul2 = el('ul', 'br-plain');
            data.rejected.forEach(function (r) {
                ul2.appendChild(el('li', null, (r.title || '?') + ': ' + r.problems.join('; ')));
            });
            bad.appendChild(ul2);
            board.appendChild(bad);
        }

        var preview = el('section', 'br-preview');
        preview.id = 'br-preview';
        board.appendChild(preview);

        data.recommendations.forEach(function (rec) { syncCard(rec.id); });
        renderPreview();
    }

    function load() {
        return fetch(API).then(function (response) { return response.json(); }).then(function (json) {
            data = json;
            dirty = false;
            render();
        }).catch(function (err) {
            board.innerHTML = '';
            board.appendChild(el('p', 'br-warn', String(err)));
        });
    }

    function editing() {
        var active = document.activeElement;
        return dirty || inFlight > 0 || (active && active.tagName === 'TEXTAREA');
    }

    document.getElementById('br-refresh').addEventListener('click', function () {
        if (!editing()) load();
    });
    /* Coming back from the chat after the agent applied something. */
    window.addEventListener('focus', function () { if (data && !editing()) load(); });
    load();
})();
