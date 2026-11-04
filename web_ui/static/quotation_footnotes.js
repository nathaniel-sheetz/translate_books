/* Reference footnotes: one endnote per quotation inside « », holding its
 * reference. The server places every note and says what a write would add and
 * retire; this page shows each one where its marker would land, sends a tick
 * when you change it, and approves the plan. Nothing is written from here:
 * `quotation_pass.py footnotes` writes an approved plan.
 */
(function () {
    'use strict';

    var board = document.getElementById('qf-board');
    if (!board) return;

    var S = JSON.parse(board.dataset.strings || '{}');
    var API = '/api/project/' + encodeURIComponent(board.dataset.project) + '/quotations/footnotes';
    var FILTERS = ['ticked', 'unticked', 'unplaced', 'ref_written', 'repeat', 'replaces'];

    var data = null;
    var show = { ticked: true, unticked: true, unplaced: true, ref_written: true, repeat: true, replaces: true };
    var saving = 0;

    var bar = document.getElementById('qf-bar');
    var barCount = document.getElementById('qf-bar-count');
    var barSave = document.getElementById('qf-bar-save');
    var barGate = document.getElementById('qf-bar-gate');
    var approveBtn = document.getElementById('qf-approve');

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

    /* The flags an item answers to; the tick ones are mutually exclusive. */
    function flags(item) {
        var out = [item.unplaced ? 'unplaced' : item.ticked ? 'ticked' : 'unticked'];
        if (item.ref_written) out.push('ref_written');
        if (item.repeat) out.push('repeat');
        if ((item.replaces || []).length) out.push('replaces');
        return out;
    }

    /* Shown when its tick state is shown and none of its other flags is hidden. */
    function shown(item) {
        var f = flags(item);
        if (!show[f[0]]) return false;
        return f.slice(1).every(function (k) { return show[k]; });
    }

    function allItems() {
        var out = [];
        ((data && data.chapters) || []).forEach(function (ch) { ch.items.forEach(function (i) { out.push(i); }); });
        return out;
    }

    function renderItem(item) {
        var node = el('article', 'qb-card qf-item' + (item.ticked ? '' : ' qf-off'));
        node.dataset.key = item.key;
        var head = el('div', 'qb-head');
        head.appendChild(el('span', 'qb-ref', item.note));
        if (item.ref && item.ref !== item.note) head.appendChild(el('span', 'qb-badge', item.ref));
        if (item.written && item.ticked) head.appendChild(el('span', 'qb-badge qb-badge-done', S.written));
        node.appendChild(head);

        if (item.unplaced) {
            node.appendChild(el('p', 'br-warn qb-small', fill(S.unplaced, { reason: item.unplaced })));
            return node;
        }
        /* Where the marker lands, the note's number standing in as a superscript. */
        var line = el('p', 'qf-preview');
        line.appendChild(el('span', null, (item.head.length >= 140 ? '…' : '') + item.head));
        line.appendChild(el('sup', 'qf-marker', item.note));
        line.appendChild(el('span', null, item.tail));
        node.appendChild(line);

        if (item.ref_written) node.appendChild(el('p', 'br-muted', S.ref_written));
        if (item.repeat) node.appendChild(el('p', 'br-muted', S.repeat));
        if (item.by_tail) node.appendChild(el('p', 'br-warn qb-small', S.by_tail));
        if (!item.nbla) node.appendChild(el('p', 'br-muted', S.not_nbla));
        (item.replaces || []).forEach(function (r) {
            node.appendChild(el('p', 'br-warn qb-small', fill(S.replaces, { content: r.content })));
        });

        var row = el('label', 'qb-check');
        var box = document.createElement('input');
        box.type = 'checkbox';
        box.checked = !!item.ticked;
        box.addEventListener('change', function () {
            var change = {};
            change[item.key] = box.checked;
            save(change);
        });
        row.appendChild(box);
        row.appendChild(el('span', null, S.tick));
        node.appendChild(row);
        return node;
    }

    function chip(key, count) {
        var row = el('label', 'qb-chip');
        var box = document.createElement('input');
        box.type = 'checkbox';
        box.checked = !!show[key];
        box.addEventListener('change', function () { show[key] = box.checked; render(); });
        row.appendChild(box);
        row.appendChild(el('span', null, (S.filters || {})[key] || key));
        row.appendChild(el('span', 'qb-chip-count', String(count)));
        return row;
    }

    function filterPanel() {
        var counts = {};
        allItems().forEach(function (i) { flags(i).forEach(function (f) { counts[f] = (counts[f] || 0) + 1; }); });
        var panel = el('div', 'qb-filters');
        var line = el('div', 'qb-axis');
        line.appendChild(el('span', 'qb-axis-label', S.filter_label));
        FILTERS.forEach(function (f) { if (counts[f]) line.appendChild(chip(f, counts[f])); });
        panel.appendChild(line);
        return panel;
    }

    function render() {
        var y = window.scrollY;
        board.textContent = '';
        if (!data || data.status !== 'ok' || !allItems().length) {
            board.appendChild(el('p', 'br-status', S.empty));
            bar.hidden = true;
            return;
        }
        board.appendChild(el('p', 'br-meta', fill(S.meta, data.counts)));
        board.appendChild(filterPanel());
        var any = false;
        data.chapters.forEach(function (ch) {
            var items = ch.items.filter(shown);
            if (!items.length) return;
            any = true;
            var section = el('section', 'qb-chapter');
            section.appendChild(el('h2', 'qb-chapter-title', ch.chapter_id.replace('chapter_', '') + ' · ' + items.length));
            items.forEach(function (i) { section.appendChild(renderItem(i)); });
            board.appendChild(section);
        });
        if (!any) board.appendChild(el('p', 'br-status', S.none_shown));
        updateBar();
        window.scrollTo(0, y);
    }

    function updateBar() {
        bar.hidden = false;
        barCount.textContent = fill(S.bar, data.counts);
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
        approveBtn.disabled = saving > 0 ||
            approveBtn.dataset.mode === 'approve' && !(data.counts.to_add || data.counts.to_retire);
    }

    function post(path, body) {
        return fetch(API + path, {
            method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
            keepalive: true
        }).then(function (response) {
            return response.json().then(function (json) { return { ok: response.ok, json: json }; });
        });
    }

    function save(choices) {
        saving++;
        barSave.textContent = S.saving;
        approveBtn.disabled = true;
        post('/choices', { choices: choices }).then(function (reply) {
            saving--;
            if (!reply.ok) {
                barSave.textContent = fill(S.save_failed, { error: reply.json.error || '?' });
                load();
                return;
            }
            data = reply.json;
            barSave.textContent = saving ? S.saving : S.saved;
            render();
        }).catch(function (err) {
            saving--;
            barSave.textContent = fill(S.save_failed, { error: String(err) });
            if (data) updateBar();
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
        }).catch(function (err) {
            barSave.textContent = fill(S.save_failed, { error: String(err) });
            updateBar();
        });
    });

    function load() {
        fetch(API).then(function (r) { return r.json(); }).then(function (json) {
            data = json;
            render();
        }).catch(function (err) {
            board.textContent = '';
            board.appendChild(el('p', 'br-status', String(err)));
        });
    }

    load();
})();
