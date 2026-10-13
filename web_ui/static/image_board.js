/* Image board: every image a book references, at whatever stage the image pass
 * has it in, with the inputs that carry what you want into the next step.
 *
 * The server is the source of truth for everything shown. This file never
 * decides what stage an image is in or whether something you set has been
 * picked up - it draws the rows `/api/project/<id>/image-pass` returns, and
 * after each save it redraws the saved image from the row the server sends
 * back. What it does own is not losing your typing: a save that comes back
 * updates the tags and the "not yet picked up" line around a card, never the
 * fields inside it, and the page only re-fetches itself when nothing is being
 * edited or waiting to be saved.
 */
(function () {
    'use strict';

    var board = document.getElementById('ib-board');
    if (!board) return;

    var PROJECT = board.dataset.project;
    var S = JSON.parse(board.dataset.strings || '{}');
    var API = '/api/project/' + encodeURIComponent(PROJECT) + '/image-pass';
    /* Stages whose images need nothing from you yet: a thumbnail is enough
       until you open one. */
    var SMALL = { untriaged: true, leave: true, missing: true };
    var VERDICTS = ['translate', 'restore', 'replace', 'cover', 'leave'];
    /* The two halves of the page: images the book came with, and made ones. */
    var TRACKS = ['existing', 'new'];
    var SAVE_DELAY = 600;
    var ZOOMS = [1, 2, 4, 8];

    var state = { images: [], counts: {}, stages: [], open: {}, filter: readHash() };
    var timers = {};      /* "<image>|<section>" -> debounce timer */
    var wanted = {};      /* image -> the pick just made (or null), until saved */
    var designWanted = {};  /* image -> the cover design just chosen, until saved */
    var autoTrack = null;
    var inFlight = 0;
    var lastPayload = '';

    function fill(text, values) {
        return String(text || '').replace(/\{(\w+)\}/g, function (whole, key) {
            if (!(key in values)) return whole;
            return values[key] == null ? '' : values[key];
        });
    }

    function el(tag, cls, text) {
        var node = document.createElement(tag);
        if (cls) node.className = cls;
        if (text != null) node.textContent = text;
        return node;
    }

    function button(cls, text) {
        var node = el('button', cls, text);
        node.type = 'button';
        return node;
    }

    function rowOf(image) {
        for (var i = 0; i < state.images.length; i++) {
            if (state.images[i].image === image) return state.images[i];
        }
        return null;
    }

    function cardOf(image) {
        return board.querySelector('.ib-card[data-image="' + CSS.escape(image) + '"]');
    }

    /* ---- filters --------------------------------------------------------- */

    function readHash() {
        /* `type` is the tab; empty until you choose one. */
        var filter = { type: '', stage: 'all', lettering: false, input: false, flagged: false, q: '' };
        location.hash.replace(/^#/, '').split('&').forEach(function (part) {
            var pair = part.split('=');
            var value;
            // A hand-edited or truncated link (`#q=100%`) must not stop the board loading.
            try { value = decodeURIComponent(pair[1] || ''); } catch (e) { return; }
            if (pair[0] === 'type') { if (TRACKS.indexOf(value) >= 0) filter.type = value; }
            else if (pair[0] === 'stage') { if (value) filter.stage = value; }
            else if (pair[0] === 'q') filter.q = value;
            else if (pair[0] in filter) filter[pair[0]] = value === '1';
        });
        return filter;
    }

    function writeHash() {
        var f = state.filter;
        var parts = [];
        if (f.type) parts.push('type=' + f.type);
        if (f.stage !== 'all') parts.push('stage=' + encodeURIComponent(f.stage));
        ['lettering', 'input', 'flagged'].forEach(function (key) {
            if (f[key]) parts.push(key + '=1');
        });
        if (f.q) parts.push('q=' + encodeURIComponent(f.q));
        history.replaceState(null, '', parts.length ? '#' + parts.join('&') : location.pathname + location.search);
    }

    function tally(name) {
        return (state.counts.tracks || {})[name] || {};
    }

    /* The tab that is showing: the one you chose, and until you choose, the
       one with something waiting for you. Settled once, on the first load, so
       the page does not change tabs under you when a run finishes. */
    function track() {
        if (state.filter.type) return state.filter.type;
        if (!autoTrack && state.counts.tracks) {
            var existing = tally('existing');
            var made = tally('new');
            autoTrack = !existing.images || (made.waiting && !existing.waiting) ? 'new' : 'existing';
        }
        return autoTrack || 'existing';
    }

    function renderTabs() {
        var tabs = document.getElementById('ib-tabs');
        var now = track();
        tabs.textContent = '';
        TRACKS.forEach(function (name) {
            var counts = tally(name);
            var node = button('ib-tab');
            node.setAttribute('role', 'tab');
            node.setAttribute('aria-selected', name === now ? 'true' : 'false');
            node.appendChild(el('span', null, S.tracks[name] || name));
            node.appendChild(el('span', 'ib-chip-count', String(counts.images || 0)));
            if (counts.waiting) {
                var dot = el('span', 'ib-dot');
                dot.title = S.tab_waiting;
                dot.setAttribute('role', 'img');
                dot.setAttribute('aria-label', S.tab_waiting);
                node.appendChild(dot);
            }
            node.addEventListener('click', function () {
                state.filter.type = name;
                refilter();
            });
            tabs.appendChild(node);
        });
        /* A made image is a workspace of its own: there is nothing to filter. */
        document.getElementById('ib-filters').hidden = now === 'new';
    }

    function matches(row) {
        var f = state.filter;
        if ((row.track || 'existing') !== track()) return false;
        if (f.stage !== 'all' && row.stage !== f.stage) return false;
        if (f.lettering && !row.lettering) return false;
        if (f.input && !(row.request || row.pick)) return false;
        if (f.flagged && !row.flagged.length) return false;
        if (f.q) {
            var hay = [row.image, row.alt, row.triage && row.triage.finding,
                       row.request && row.request.note].join(' ').toLowerCase();
            if (hay.indexOf(f.q.toLowerCase()) < 0) return false;
        }
        return true;
    }

    function chip(label, count, pressed, onClick) {
        var node = button('ib-chip');
        node.setAttribute('aria-pressed', pressed ? 'true' : 'false');
        node.appendChild(el('span', null, label));
        node.appendChild(el('span', 'ib-chip-count', String(count)));
        node.addEventListener('click', onClick);
        return node;
    }

    function renderFilters() {
        var stages = document.getElementById('ib-stage-chips');
        var toggles = document.getElementById('ib-toggle-chips');
        var f = state.filter;
        /* The chips count the tab that is showing, not the whole book. */
        var counts = state.counts.tracks ? tally(track()) : state.counts;
        renderTabs();
        stages.textContent = '';
        toggles.textContent = '';

        function pickStage(stage) {
            return function () { f.stage = stage; refilter(); };
        }
        stages.appendChild(chip(S.all, counts.images || 0, f.stage === 'all', pickStage('all')));
        state.stages.forEach(function (stage) {
            /* An empty stage is not a choice - except the one you are on, or
               the chip you pressed would vanish under your finger. */
            if (!counts[stage] && f.stage !== stage) return;
            var node = chip(S.stages[stage] || stage, counts[stage] || 0,
                            f.stage === stage, pickStage(stage));
            node.classList.add('ib-chip-' + stage);
            stages.appendChild(node);
        });

        [['lettering', S.lettering, 'lettering'],
         ['input', S.my_input, 'with_input'],
         ['flagged', S.flagged, 'flagged']].forEach(function (spec) {
            toggles.appendChild(chip(spec[1], counts[spec[2]] || 0, f[spec[0]], function () {
                f[spec[0]] = !f[spec[0]];
                refilter();
            }));
        });
    }

    function refilter() {
        writeHash();
        renderFilters();
        renderBoard();
    }

    /* ---- cards ----------------------------------------------------------- */

    /* "20261005_182844", the folder an earlier prompt's candidates went to. */
    function stampDay(stamp) {
        var s = String(stamp || '');
        return s.length >= 8 ? s.slice(0, 4) + '-' + s.slice(4, 6) + '-' + s.slice(6, 8) : s;
    }

    function pictureCaption(row, pic) {
        if (pic.kind === 'candidate') return fill(S.candidate, { n: pic.candidate });
        if (pic.kind === 'reference') return fill(S.reference, { name: pic.key });
        if (pic.kind === 'earlier') {
            return fill(S.earlier_candidate, { n: pic.candidate, when: stampDay(pic.stamp) });
        }
        if (pic.kind === 'draft') return fill(S.draft, { n: pic.draft });
        if (pic.kind === 'art') return S.art;
        if (pic.kind === 'proof') return S.proof;
        if (pic.kind === 'sheet') return pic.name;
        return pic.kind === 'current' ? S.current : S.original;
    }

    function valueText(value) {
        if (value == null) return S.unset;
        if (Array.isArray(value)) {
            return value.every(function (item) { return typeof item === 'string'; })
                ? value.join(' | ') : JSON.stringify(value);
        }
        return typeof value === 'object' ? JSON.stringify(value) : String(value);
    }

    /* What a draft is, in a line: the settings tried, or what changed since
       the proof before it. */
    function draftLine(pic) {
        if (pic.draft_kind === 'variant') return pic.label || '';
        if (pic.draft_kind === 'swatch') return '';
        if (pic.changes == null) return S.first_draft;
        if (!pic.changes.length) return S.same_settings;
        return pic.changes.map(function (change) {
            return change.key + ' ' + valueText(change.from) + ' → ' + valueText(change.to);
        }).join(' · ');
    }

    function colorChip(color) {
        var chipNode = el('span', 'ib-color');
        chipNode.style.background = color;
        return chipNode;
    }

    /* A composite is two pictures: say which, and how much of it is new. */
    function compositeLine(made) {
        function name(side) {
            return fill(S.composite_sources[side.kind] || side.kind, { n: side.candidate });
        }
        var text = fill(S.composite, { from: name(made.from), base: name(made.base) });
        if (typeof made.changed_share === 'number') {
            text += ' ' + fill(S.composite_changed,
                               { pct: (made.changed_share * 100).toFixed(1) });
        }
        return text;
    }

    /* Every picture of a card, in the order the lightbox walks them. */
    function allPictures(row) {
        return row.pictures.concat(row.candidates, row.earlier || []);
    }

    /* `(pic) -> click handler` that opens the lightbox on `pic` among `list`. */
    function opener(row, list) {
        return function (pic) {
            return function () {
                var at = list.indexOf(pic);
                /* The big proof is also one of the drafts: open it there. */
                for (var i = 0; at < 0 && i < list.length; i++) {
                    if (list[i].url === pic.url) at = i;
                }
                openLightbox(row, at < 0 ? [pic] : list, Math.max(at, 0));
            };
        };
    }

    function strip(row, pictures, open) {
        var wrap = el('div', 'ib-strip');
        pictures.forEach(function (pic) { wrap.appendChild(figure(row, pic, open(pic))); });
        return wrap;
    }

    /* A folded-away run of pictures: earlier attempts, other sheets. */
    function moreBlock(row, name, pictures, open) {
        var details = el('details', 'ib-more');
        details.appendChild(el('summary', null,
            (S.more[name] || name) + ' (' + pictures.length + ')'));
        details.appendChild(strip(row, pictures, open));
        return details;
    }

    /* `plain` leaves the buttons off: the big proof is one of the drafts below,
       and that is where it is chosen. */
    function figure(row, pic, onOpen, plain) {
        var fig = el('figure', 'ib-fig ib-fig-' + pic.kind);
        var open = button('ib-fig-btn');
        var img = el('img');
        var caption = pictureCaption(row, pic);
        img.src = pic.url;
        img.alt = caption;
        img.loading = 'lazy';
        /* The real size, so the card is laid out before the file arrives and a
           re-fetch of the board does not make the page jump. */
        if (pic.width) { img.width = pic.width; img.height = pic.height; }
        open.appendChild(img);
        open.addEventListener('click', onOpen);
        fig.appendChild(open);

        var cap = el('figcaption');
        if (pic.color) {
            cap.appendChild(colorChip(pic.color));
            cap.appendChild(el('span', 'ib-fig-name', pic.color));
            cap.appendChild(el('span', 'ib-fig-size', caption));
        } else {
            cap.appendChild(el('span', 'ib-fig-name', caption));
        }
        if (pic.kind === 'draft') {
            if (pic.ts) cap.appendChild(el('span', 'ib-fig-size', String(pic.ts).slice(0, 10)));
            (pic.tags || []).forEach(function (tag) {
                cap.appendChild(el('span', 'ib-tag ib-tag-' + tag, S.draft_tags[tag] || tag));
            });
        } else if (pic.width) {
            cap.appendChild(el('span', 'ib-fig-size', pic.width + '×' + pic.height));
        }
        /* Two models can fill one job's candidates: say which made this one. */
        if (pic.model) cap.appendChild(el('span', 'ib-fig-model', pic.model));
        (pic.flags || []).forEach(function (flag) {
            cap.appendChild(el('span', 'ib-warn', flag));
        });
        fig.appendChild(cap);
        if (pic.composite) fig.appendChild(el('p', 'ib-composite', compositeLine(pic.composite)));

        if (pic.check) {
            var check = el('p', 'ib-check ' + (pic.check.ok ? 'ib-check-ok' : 'ib-check-bad'));
            check.appendChild(el('strong', null, pic.check.ok ? S.check_ok : S.check_bad));
            if (pic.check.finding) check.appendChild(document.createTextNode(' ' + pic.check.finding));
            fig.appendChild(check);
        }
        if (plain) return fig;
        if (pic.kind === 'candidate') {
            var accept = button('ib-pick-btn ib-pick-accept', S.accept);
            accept.dataset.pick = 'accept';
            accept.dataset.candidate = pic.candidate;
            fig.appendChild(accept);
        }
        if (pic.kind === 'draft') {
            var line = draftLine(pic);
            if (line) fig.appendChild(el('p', 'ib-draft-line', line));
            var use = button('ib-pick-btn ib-pick-accept', S.use_draft);
            use.dataset.design = 'draft';
            use.dataset.draft = pic.draft;
            fig.appendChild(use);
        }
        return fig;
    }

    /* The part of a card that follows the server after every save. */
    function renderHead(row, head) {
        head.textContent = '';
        var title = el('h3', 'ib-name');
        title.appendChild(el('span', null, row.image));
        if (row.verdict) {
            title.appendChild(el('span', 'ib-tag ib-tag-' + row.verdict,
                                 S.verdicts[row.verdict] || row.verdict));
        }
        row.flagged.forEach(function (flag) {
            title.appendChild(el('span', 'ib-tag ib-tag-flag', S.flags[flag] || flag));
        });
        var ws = row.workspace;
        if (ws) {
            title.appendChild(el('span', 'ib-ws-status ib-ws-status-' + ws.status,
                                 S.ws_status[ws.status] || ws.status));
        }
        head.appendChild(title);

        var meta = el('p', 'ib-meta');
        if (row.chapter) {
            var link = el('a', null, row.chapter.replace(/_/g, ' '));
            link.href = '/read/' + encodeURIComponent(PROJECT) + '/' + encodeURIComponent(row.chapter);
            meta.appendChild(link);
        }
        if (row.alt) meta.appendChild(el('span', null, row.alt));
        if (meta.childNodes.length) head.appendChild(meta);

        /* A workspace says what was decided step by step, where it happened. */
        if (row.decision && !row.workspace) {
            var said = fill(S.decisions[row.decision.action] || row.decision.action,
                            { n: row.decision.candidate });
            var when = String(row.decision.ts || '').slice(0, 10);
            head.appendChild(el('p', 'ib-decision',
                said + (when ? ' · ' + when : '') +
                (row.decision.note ? ' — ' + row.decision.note : '')));
        }
        if (row.drift.length) {
            var drift = el('p', 'ib-drift');
            drift.appendChild(el('strong', null, S.waiting));
            drift.appendChild(document.createTextNode(' ' + row.drift.map(function (reason) {
                return S.drift[reason] || reason;
            }).join(', ')));
            head.appendChild(drift);
        }
        /* The same line for what you chose about the cover's design. */
        var design = ws && ws.design;
        if (design && design.stale) {
            head.appendChild(el('p', 'ib-drift ib-warn', S.design_stale));
        } else if (design && design.waiting.length) {
            var waiting = el('p', 'ib-drift');
            waiting.appendChild(el('strong', null, S.waiting));
            waiting.appendChild(document.createTextNode(' ' + design.waiting.map(function (reason) {
                return S.design_waiting[reason] || reason;
            }).join(', ')));
            head.appendChild(waiting);
        }
    }

    function labelRow(source, target) {
        var tr = el('tr');
        [source, target].forEach(function (value, i) {
            var td = el('td');
            var input = el('input', i ? 'ib-label-target' : 'ib-label-source');
            input.type = 'text';
            input.value = value;
            input.setAttribute('aria-label', i ? S.label_target : S.label_source);
            td.appendChild(input);
            tr.appendChild(td);
        });
        var td = el('td');
        var remove = button('ib-label-remove', '×');
        remove.title = S.remove_label;
        remove.setAttribute('aria-label', S.remove_label);
        td.appendChild(remove);
        tr.appendChild(td);
        return tr;
    }

    function renderLabels(row, wrap) {
        var edited = !!(row.request && row.request.labels != null);
        wrap.textContent = '';
        wrap.dataset.edited = edited ? '1' : '';

        /* The marker and the undo link are always there and shown by CSS off
           `data-edited`, so the first keystroke in the table brings them up
           without redrawing the table under the cursor. */
        var heading = el('div', 'ib-field-label', S.labels);
        heading.appendChild(el('span', 'ib-edited', S.labels_edited));
        wrap.appendChild(heading);

        /* `labels` travels as [source, target] pairs, not an object: the order
           the lettering was listed in is how you check it against the picture,
           and neither JSON objects nor the server's encoder promise to keep it. */
        if (row.labels.length) {
            var table = el('table', 'ib-labels');
            var thead = el('tr');
            thead.appendChild(el('th', null, S.label_source));
            thead.appendChild(el('th', null, S.label_target));
            thead.appendChild(el('th'));
            table.appendChild(thead);
            row.labels.forEach(function (pair) {
                table.appendChild(labelRow(pair[0], pair[1]));
            });
            wrap.appendChild(table);
        }
        var actions = el('div', 'ib-label-actions');
        actions.appendChild(button('ib-link ib-label-add', S.add_label));
        actions.appendChild(button('ib-link ib-label-reset', S.reset_labels));
        wrap.appendChild(actions);
    }

    function field(labelText, control) {
        var wrap = el('label', 'ib-field');
        wrap.appendChild(el('span', 'ib-field-label', labelText));
        wrap.appendChild(control);
        return wrap;
    }

    function requestForm(row) {
        var form = el('div', 'ib-form');
        var request = row.request || {};

        var verdict = el('select', 'ib-verdict');
        var asIs = row.job ? fill(S.as_prepared, { verdict: S.verdicts[row.job.mode] || row.job.mode })
            : row.triage ? fill(S.as_triaged, { verdict: S.verdicts[row.triage.verdict] || row.triage.verdict })
            : S.no_verdict;
        verdict.appendChild(new Option(asIs, ''));
        VERDICTS.forEach(function (value) {
            /* Only the cover slot can take a cover job. */
            if (value === 'cover' && row.role !== 'cover') return;
            verdict.appendChild(new Option(S.verdicts[value] || value, value));
        });
        verdict.value = request.verdict || '';

        var count = el('select', 'ib-count');
        count.appendChild(new Option(S.candidates_default, ''));
        [1, 2, 3, 4].forEach(function (n) { count.appendChild(new Option(String(n), String(n))); });
        count.value = request.candidates ? String(request.candidates) : '';

        var note = el('textarea', 'ib-note');
        note.rows = 2;
        note.placeholder = S.note_placeholder;
        note.value = request.note || '';

        var choices = el('div', 'ib-form-row');
        choices.appendChild(field(S.what_to_do, verdict));
        choices.appendChild(field(S.candidates, count));
        form.appendChild(choices);
        form.appendChild(field(S.note, note));

        var labels = el('div', 'ib-labels-wrap');
        renderLabels(row, labels);
        form.appendChild(labels);
        return form;
    }

    function collectRequest(card) {
        var labels = null;
        var wrap = card.querySelector('.ib-labels-wrap');
        if (wrap.dataset.edited) {
            labels = [];
            Array.prototype.forEach.call(wrap.querySelectorAll('tr'), function (tr) {
                var source = tr.querySelector('.ib-label-source');
                var target = tr.querySelector('.ib-label-target');
                /* A half-typed row is not a label yet; it is kept on screen
                   and sent once both sides say something. */
                if (source && source.value.trim() && target.value.trim()) {
                    labels.push([source.value.trim(), target.value.trim()]);
                }
            });
        }
        var count = card.querySelector('.ib-count').value;
        return {
            verdict: card.querySelector('.ib-verdict').value || null,
            candidates: count ? parseInt(count, 10) : null,
            note: card.querySelector('.ib-note').value,
            labels: labels
        };
    }

    function pickBlock(row) {
        var wrap = el('div', 'ib-pick');
        var redo = button('ib-pick-btn', S.redo);
        redo.dataset.pick = 'redo';
        var skip = button('ib-pick-btn', S.skip);
        skip.dataset.pick = 'skip';
        var note = el('textarea', 'ib-pick-note');
        note.rows = 1;
        note.placeholder = S.pick_note_placeholder;
        /* A pick that has been applied is history, shown in the head. The
           controls start clean for whatever comes next. */
        note.value = row.pick && row.pick.pending ? row.pick.note || '' : '';
        var buttons = el('div', 'ib-pick-buttons');
        buttons.appendChild(redo);
        buttons.appendChild(skip);
        wrap.appendChild(buttons);
        wrap.appendChild(note);
        wrap.appendChild(el('span', 'ib-pick-status'));
        return wrap;
    }

    /* What the pick controls should show: the pick you just made, until the
       server has it, and after that whatever the server says is still waiting
       to be applied. Without the first half, a label save answering in the gap
       between your click and its own save would lift the button again - and
       the save would then send "no pick". */
    function shownPick(row) {
        if (row.image in wanted) return wanted[row.image];
        return row.pick && row.pick.pending ? row.pick : null;
    }

    /* Which pick button is down, and what the line beside them says. */
    function syncPick(card, row) {
        var pick = shownPick(row);
        Array.prototype.forEach.call(card.querySelectorAll('[data-pick]'), function (node) {
            var on = !!pick && node.dataset.pick === pick.verdict &&
                (pick.verdict !== 'accept' || String(pick.candidate) === node.dataset.candidate);
            node.setAttribute('aria-pressed', on ? 'true' : 'false');
            if (node.dataset.pick === 'accept') node.textContent = on ? S.accepted : S.accept;
        });
        var status = card.querySelector('.ib-pick-status');
        if (!status) return;
        status.textContent = !pick ? '' : pick.stale ? S.pick_stale : S.pick_pending;
        status.classList.toggle('ib-warn', !!pick && !!pick.stale);
    }

    function tile(row) {
        var card = el('article', 'ib-card ib-small ib-stage-' + row.stage);
        card.dataset.image = row.image;
        var open = button('ib-tile');
        open.title = (row.triage && row.triage.finding) || row.alt || row.image;
        open.setAttribute('aria-label', S.expand + ': ' + row.image);
        if (row.pictures.length) {
            var img = el('img');
            img.src = row.pictures[0].url;
            img.alt = row.alt || row.image;
            img.loading = 'lazy';
            open.appendChild(img);
        } else {
            open.appendChild(el('span', 'ib-tile-empty', S.no_picture));
        }
        var name = el('span', 'ib-tile-name', row.image);
        if (row.request || row.pick) name.appendChild(el('span', 'ib-dot'));
        open.appendChild(name);
        if (row.flagged.length) {
            open.appendChild(el('span', 'ib-tag ib-tag-flag', S.flags[row.flagged[0]] || row.flagged[0]));
        }
        open.addEventListener('click', function () {
            state.open[row.image] = true;
            card.replaceWith(buildCard(row));
        });
        card.appendChild(open);
        return card;
    }

    function fullCard(row) {
        var card = el('article', 'ib-card ib-stage-' + row.stage);
        card.dataset.image = row.image;

        if (SMALL[row.stage]) {
            var close = button('ib-link ib-collapse', S.collapse);
            close.addEventListener('click', function () {
                delete state.open[row.image];
                card.replaceWith(buildCard(rowOf(row.image) || row));
            });
            card.appendChild(close);
        }
        var head = el('div', 'ib-head');
        renderHead(row, head);
        card.appendChild(head);

        var open = opener(row, allPictures(row));
        var pics = el('div', 'ib-pics');
        if (!row.pictures.length) pics.appendChild(el('p', 'ib-tile-empty', S.no_picture));
        row.pictures.concat(row.candidates).forEach(function (pic) {
            pics.appendChild(figure(row, pic, open(pic)));
        });
        card.appendChild(pics);
        if (row.candidates.length) card.appendChild(pickBlock(row));
        if ((row.earlier || []).length) {
            card.appendChild(moreBlock(row, 'earlier', row.earlier, open));
        }

        if (row.triage && row.triage.finding) {
            var finding = el('p', 'ib-finding');
            finding.appendChild(el('strong', null, S.finding));
            finding.appendChild(document.createTextNode(' ' + row.triage.finding));
            card.appendChild(finding);
        }
        if (row.job && row.job.instruction) {
            var details = el('details', 'ib-instruction');
            details.appendChild(el('summary', null, S.instruction));
            details.appendChild(el('pre', null, row.job.instruction));
            card.appendChild(details);
        }
        card.appendChild(requestForm(row));
        card.appendChild(el('span', 'ib-save'));
        syncPick(card, row);
        return card;
    }

    /* ---- workspaces: an image that is made, step by step ------------------ */

    function workspacePictures(ws) {
        var list = [];
        ws.steps.forEach(function (step) {
            list = list.concat(step.pictures);
            step.more.forEach(function (group) { list = list.concat(group.pictures); });
        });
        return list;
    }

    /* What you chose about the design: the click just made, until the server
       has it, and after that what the server says. */
    function designOf(row) {
        if (row.image in designWanted) return designWanted[row.image];
        var said = (row.workspace && row.workspace.design) || null;
        /* An approval that is in the book says nothing about the proof made
           since: the buttons start over, and the note stays. */
        if (said && said.spent) return { draft: null, approve: false, note: said.note };
        return said;
    }

    function syncDesign(card, row) {
        var design = designOf(row);
        Array.prototype.forEach.call(card.querySelectorAll('[data-design]'), function (node) {
            var on;
            if (node.dataset.design === 'approve') {
                on = !!design && !!design.approve;
                node.textContent = on ? S.approved : S.approve;
            } else {
                on = !!design && String(design.draft) === node.dataset.draft;
                node.textContent = on ? S.using_draft : S.use_draft;
            }
            node.setAttribute('aria-pressed', on ? 'true' : 'false');
        });
    }

    function stepBlock(row, step, number, open) {
        var ws = row.workspace;
        var block = el('section', 'ib-step' + (step.done ? ' ib-step-done' : ''));
        block.dataset.step = step.id;

        var title = el('h4', 'ib-step-title');
        title.appendChild(el('span', 'ib-step-n', String(number)));
        title.appendChild(el('span', 'ib-step-name', S.steps[step.id] || step.id));
        var where = el('span', 'ib-step-state');
        if (step.values && step.values.color) where.appendChild(colorChip(step.values.color));
        var said = fill(S.step_states[step.id + '_' + step.state] || step.state, step.values || {});
        if (step.values && step.values.backup) said += ' · ' + S.original_kept;
        where.appendChild(document.createTextNode(said));
        title.appendChild(where);
        block.appendChild(title);

        if (step.pictures.length) block.appendChild(strip(row, step.pictures, open));
        step.more.forEach(function (group) {
            block.appendChild(moreBlock(row, group.id, group.pictures, open));
        });

        if (step.id === 'art') {
            if (row.candidates.length) block.appendChild(pickBlock(row));
            if (row.job && row.job.instruction) {
                var sent = el('details', 'ib-instruction');
                sent.appendChild(el('summary', null, S.instruction));
                sent.appendChild(el('pre', null, row.job.instruction));
                block.appendChild(sent);
            }
            /* The same request an existing image takes: a note, how many. */
            var ask = el('details', 'ib-more');
            ask.open = !!row.request;
            ask.appendChild(el('summary', null, S.ask_for_art));
            ask.appendChild(requestForm(row));
            block.appendChild(ask);
        } else if (step.id === 'layout') {
            var note = el('textarea', 'ib-design-note');
            note.rows = 2;
            note.placeholder = S.design_note_placeholder;
            note.value = (ws.design && ws.design.note) || '';
            var wrap = el('div', 'ib-form');
            wrap.appendChild(field(S.design_note, note));
            block.appendChild(wrap);
        } else if (step.id === 'book') {
            var approve = el('div', 'ib-pick');
            if (ws.proof_draft) {
                var yes = button('ib-pick-btn', S.approve);
                yes.dataset.design = 'approve';
                approve.appendChild(yes);
                approve.appendChild(el('span', 'ib-hint', S.approve_hint));
            } else if (ws.hero && ws.hero.kind === 'proof') {
                /* A proof from before drafts were kept has no number to approve. */
                approve.appendChild(el('span', 'ib-hint', S.approve_needs_draft));
            }
            if (approve.childNodes.length) block.appendChild(approve);
        }
        return block;
    }

    function workspace(row) {
        var ws = row.workspace;
        var card = el('article', 'ib-card ib-workspace ib-stage-' + row.stage);
        card.dataset.image = row.image;
        var head = el('div', 'ib-head');
        renderHead(row, head);
        card.appendChild(head);

        var open = opener(row, workspacePictures(ws));
        var body = el('div', 'ib-ws');
        var side = el('div', 'ib-ws-side');
        if (ws.hero) side.appendChild(figure(row, ws.hero, open(ws.hero), true));
        else side.appendChild(el('p', 'ib-tile-empty', S.no_picture));
        body.appendChild(side);

        var steps = el('div', 'ib-ws-steps');
        ws.steps.forEach(function (step, index) {
            steps.appendChild(stepBlock(row, step, index + 1, open));
        });
        body.appendChild(steps);
        card.appendChild(body);
        card.appendChild(el('span', 'ib-save'));
        syncPick(card, row);
        syncDesign(card, row);
        return card;
    }

    function buildCard(row) {
        if (row.workspace) return workspace(row);
        return SMALL[row.stage] && !state.open[row.image] ? tile(row) : fullCard(row);
    }

    function renderBoard() {
        var top = window.scrollY;
        board.textContent = '';
        if (!state.images.length) {
            board.appendChild(el('p', 'empty-state', S.empty));
            return;
        }
        if (track() === 'new') {
            /* No stages to group by: each made image is a workspace of its own. */
            state.images.forEach(function (row) {
                if (row.track === 'new') board.appendChild(buildCard(row));
            });
            window.scrollTo(0, top);
            return;
        }
        var shown = 0;
        state.stages.forEach(function (stage) {
            var rows = state.images.filter(function (row) {
                return row.stage === stage && matches(row);
            });
            if (!rows.length) return;
            shown += rows.length;
            var group = el('section', 'ib-group');
            var heading = el('h2', 'ib-group-title', S.stages[stage] || stage);
            heading.appendChild(el('span', 'ib-chip-count', String(rows.length)));
            group.appendChild(heading);
            var grid = el('div', 'ib-grid');
            rows.forEach(function (row) { grid.appendChild(buildCard(row)); });
            group.appendChild(grid);
            board.appendChild(group);
        });
        if (!shown) board.appendChild(el('p', 'empty-state', S.none_match));
        window.scrollTo(0, top);
    }

    /* ---- saving ---------------------------------------------------------- */

    function setSaveStatus(image, text, failed) {
        var card = cardOf(image);
        var status = card && card.querySelector('.ib-save');
        if (!status) return;
        status.textContent = text;
        status.classList.toggle('ib-warn', !!failed);
    }

    function busy() {
        return inFlight > 0 || Object.keys(timers).length > 0;
    }

    /* A save came back: follow the server everywhere except inside the fields,
       where the user may already be typing the next thing. */
    function applyRow(row, counts) {
        for (var i = 0; i < state.images.length; i++) {
            if (state.images[i].image === row.image) state.images[i] = row;
        }
        state.counts = counts;
        lastPayload = '';
        renderFilters();
        var card = cardOf(row.image);
        if (!card || card.classList.contains('ib-small')) return;
        renderHead(row, card.querySelector('.ib-head'));
        syncPick(card, row);
        syncDesign(card, row);
    }

    function send(image, payload, after) {
        payload.image = image;
        inFlight++;
        fetch(API + '/feedback', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload)
        }).then(function (response) {
            return response.json().then(function (data) {
                if (!response.ok) throw new Error(data.error || response.statusText);
                return data;
            });
        }).then(function (data) {
            inFlight--;
            applyRow(data.image, data.counts);
            setSaveStatus(image, S.saved, false);
            if (after) after(data.image);
        }).catch(function (error) {
            inFlight--;
            setSaveStatus(image, S.save_failed + ': ' + error.message, true);
            /* A pick that did not land must not go on looking as if it had. */
            if ('pick' in payload && !timers[image + '|pick']) {
                delete wanted[image];
                var card = cardOf(image);
                if (card && !card.classList.contains('ib-small')) syncPick(card, rowOf(image));
            }
            if ('design' in payload && !timers[image + '|design']) {
                delete designWanted[image];
                var mine = cardOf(image);
                if (mine) syncDesign(mine, rowOf(image));
            }
        });
    }

    function queue(image, section, build, after) {
        var key = image + '|' + section;
        clearTimeout(timers[key]);
        setSaveStatus(image, S.saving, false);
        timers[key] = setTimeout(function () {
            delete timers[key];
            var card = cardOf(image);
            if (card) send(image, build(card), after);
        }, SAVE_DELAY);
    }

    function saveRequest(card, after) {
        queue(card.dataset.image, 'request', function (now) {
            return { request: collectRequest(now) };
        }, after);
    }

    /* `pick` is {verdict, candidate} or null to take it back. The note is read
       when the save fires, so typing after the click rides along. */
    function savePick(card, pick) {
        var image = card.dataset.image;
        wanted[image] = pick;
        syncPick(card, rowOf(image));
        queue(image, 'pick', function (now) {
            var mine = wanted[image];
            return { pick: mine && {
                verdict: mine.verdict,
                candidate: mine.candidate,
                note: now.querySelector('.ib-pick-note').value
            } };
        }, function (row) {
            /* Unless you clicked again while this one was on its way. */
            if (timers[image + '|pick']) return;
            delete wanted[image];
            var now = cardOf(image);
            if (now) syncPick(now, row);
        });
    }

    /* `design` is {draft, approve}. As with a pick, the note is read when the
       save fires. Nothing chosen and no note clears the section. */
    function saveDesign(card, design) {
        var image = card.dataset.image;
        designWanted[image] = design;
        syncDesign(card, rowOf(image));
        queue(image, 'design', function (now) {
            var mine = designWanted[image] || {};
            var note = now.querySelector('.ib-design-note');
            return { design: {
                draft: mine.draft == null ? null : mine.draft,
                approve: !!mine.approve,
                note: note ? note.value : ''
            } };
        }, function (row) {
            if (timers[image + '|design']) return;
            delete designWanted[image];
            var now = cardOf(image);
            if (now) syncDesign(now, row);
        });
    }

    board.addEventListener('input', function (event) {
        var card = event.target.closest('.ib-card');
        if (!card) return;
        if (event.target.classList.contains('ib-design-note')) {
            var chosen = designOf(rowOf(card.dataset.image)) || {};
            saveDesign(card, { draft: chosen.draft, approve: chosen.approve });
            return;
        }
        if (event.target.classList.contains('ib-pick-note')) {
            /* A note with no verdict has nothing to attach to yet. */
            var standing = shownPick(rowOf(card.dataset.image));
            if (standing) {
                savePick(card, { verdict: standing.verdict, candidate: standing.candidate });
            }
            return;
        }
        if (event.target.closest('.ib-labels-wrap')) {
            card.querySelector('.ib-labels-wrap').dataset.edited = '1';
        }
        saveRequest(card);
    });

    board.addEventListener('click', function (event) {
        var card = event.target.closest('.ib-card');
        if (!card) return;
        var target = event.target;
        var wrap = card.querySelector('.ib-labels-wrap');

        if (target.dataset.design) {
            var row = rowOf(card.dataset.image);
            var now = designOf(row) || {};
            if (target.dataset.design === 'draft') {
                /* Pressing the one that is down takes the choice back. A new
                   choice is not yet an approval of it. */
                var n = parseInt(target.dataset.draft, 10);
                saveDesign(card, { draft: now.draft === n ? null : n, approve: false });
            } else {
                /* Approving is of the draft you chose, or of the proof as it stands. */
                saveDesign(card, {
                    draft: now.draft != null ? now.draft : row.workspace.proof_draft,
                    approve: !now.approve
                });
            }
        } else if (target.dataset.pick) {
            /* Pressing the one that is down takes the pick back. */
            savePick(card, target.getAttribute('aria-pressed') === 'true' ? null : {
                verdict: target.dataset.pick,
                candidate: target.dataset.pick === 'accept'
                    ? parseInt(target.dataset.candidate, 10) : null
            });
        } else if (target.classList.contains('ib-label-add')) {
            var table = wrap.querySelector('table');
            if (!table) {
                table = el('table', 'ib-labels');
                wrap.insertBefore(table, wrap.querySelector('.ib-label-actions'));
            }
            var added = labelRow('', '');
            table.appendChild(added);
            added.querySelector('input').focus();
        } else if (target.classList.contains('ib-label-remove')) {
            target.closest('tr').remove();
            wrap.dataset.edited = '1';
            saveRequest(card);
        } else if (target.classList.contains('ib-label-reset')) {
            wrap.dataset.edited = '';
            /* Back to the job's or triage's map, which only the server knows. */
            saveRequest(card, function (row) {
                var now = cardOf(row.image);
                if (now) renderLabels(row, now.querySelector('.ib-labels-wrap'));
            });
        }
    });

    /* ---- loading --------------------------------------------------------- */

    function load() {
        return fetch(API).then(function (response) {
            if (!response.ok) throw new Error(response.statusText);
            return response.text();
        }).then(function (text) {
            /* Unchanged since the last look: leave the page exactly as it is. */
            if (text === lastPayload) return;
            var data = JSON.parse(text);
            lastPayload = text;
            state.images = data.images;
            state.counts = data.counts;
            state.stages = data.stages;
            renderFilters();
            renderBoard();
        }).catch(function () {
            if (!state.images.length) {
                board.textContent = '';
                board.appendChild(el('p', 'empty-state', S.load_failed));
            }
        });
    }

    function editing() {
        var active = document.activeElement;
        return !!active && board.contains(active) && /^(INPUT|TEXTAREA|SELECT)$/.test(active.tagName);
    }

    /* Coming back from the run is when the board has changed: candidates have
       landed, a job was prepared, a pick was applied. */
    window.addEventListener('focus', function () {
        if (!busy() && !editing() && lightbox.hidden) load();
    });
    document.getElementById('ib-refresh').addEventListener('click', function () {
        if (!busy()) load();
    });

    var search = document.getElementById('ib-search');
    search.value = state.filter.q;
    search.addEventListener('input', function () {
        state.filter.q = search.value.trim();
        writeHash();
        renderBoard();
    });
    /* A link to a filtered board, followed while the board is already open. */
    window.addEventListener('hashchange', function () {
        state.filter = readHash();
        search.value = state.filter.q;
        renderFilters();
        renderBoard();
    });

    /* ---- lightbox -------------------------------------------------------- */

    var lightbox = document.getElementById('ib-lightbox');
    var lbStage = document.getElementById('ib-lightbox-stage');
    var lbImg = document.getElementById('ib-lightbox-img');
    var lbCaption = document.getElementById('ib-lightbox-caption');
    var lbPic = document.getElementById('ib-lightbox-pic');
    var lb = { pics: [], at: 0, zoom: 0, outline: true };
    var SVG = 'http://www.w3.org/2000/svg';

    /* The outline is in the picture's own pixels, so one viewBox carries it
       through every zoom. */
    function drawOutline(pic) {
        var old = lbPic.querySelector('.ib-outline');
        if (old) lbPic.removeChild(old);
        if (!lb.outline || !pic.composite || !pic.width) return;
        var svg = document.createElementNS(SVG, 'svg');
        svg.setAttribute('class', 'ib-outline');
        svg.setAttribute('viewBox', '0 0 ' + pic.width + ' ' + pic.height);
        svg.setAttribute('preserveAspectRatio', 'none');
        pic.composite.regions.forEach(function (region) {
            var polygon = document.createElementNS(SVG, 'polygon');
            polygon.setAttribute('points', region.map(function (point) {
                return point[0] + ',' + point[1];
            }).join(' '));
            svg.appendChild(polygon);
        });
        lbPic.appendChild(svg);
    }

    /* Every picture of a card is drawn at the same fitted size, and swapping
       keeps the scroll position, so original and candidate alternate in place:
       a moved coastline or a clipped letter shows as movement. */
    function showLightbox() {
        var pic = lb.pics[lb.at];
        var roomW = lbStage.clientWidth - 32;
        var roomH = lbStage.clientHeight - 32;
        var fit = pic.width ? Math.min(roomW, roomH * pic.width / pic.height) : roomW;
        lbImg.src = pic.url;
        lbImg.alt = pic.caption;
        lbImg.style.width = Math.round(fit * ZOOMS[lb.zoom]) + 'px';
        lbCaption.textContent = pic.caption +
            (pic.width ? ' · ' + pic.width + '×' + pic.height : '') +
            ' · ' + (lb.at + 1) + '/' + lb.pics.length +
            (lb.zoom ? ' · ' + ZOOMS[lb.zoom] + '×' : '');
        drawOutline(pic);
    }

    function openLightbox(row, pictures, index) {
        lb.pics = pictures.map(function (pic) {
            var caption = pictureCaption(row, pic);
            if (pic.color) caption += ' · ' + pic.color;
            else if (pic.kind === 'draft' && pic.label) caption += ' · ' + pic.label;
            return { url: pic.url, width: pic.width, height: pic.height,
                     composite: pic.composite,
                     caption: row.image + ' — ' + caption };
        });
        lb.at = index;
        lb.zoom = 0;
        lightbox.hidden = false;
        showLightbox();
    }

    function closeLightbox() {
        lightbox.hidden = true;
        lbImg.removeAttribute('src');
    }

    function zoomBy(step) {
        var before = ZOOMS[lb.zoom];
        lb.zoom = Math.max(0, Math.min(ZOOMS.length - 1, lb.zoom + step));
        /* Keep the middle of the view where it was. */
        var ratio = ZOOMS[lb.zoom] / before;
        var midX = lbStage.scrollLeft + lbStage.clientWidth / 2;
        var midY = lbStage.scrollTop + lbStage.clientHeight / 2;
        showLightbox();
        lbStage.scrollLeft = midX * ratio - lbStage.clientWidth / 2;
        lbStage.scrollTop = midY * ratio - lbStage.clientHeight / 2;
    }

    function swap(step) {
        lb.at = (lb.at + step + lb.pics.length) % lb.pics.length;
        showLightbox();
    }

    document.getElementById('ib-lightbox-close').addEventListener('click', closeLightbox);
    lbStage.addEventListener('click', function (event) {
        if (event.target === lbStage) closeLightbox();
    });
    lbImg.addEventListener('click', function () {
        if (lb.zoom === ZOOMS.length - 1) { lb.zoom = 0; showLightbox(); } else zoomBy(1);
    });
    document.addEventListener('keydown', function (event) {
        if (lightbox.hidden) return;
        if (event.key === 'Escape') closeLightbox();
        else if (event.key === 'ArrowRight' || event.key === ' ') swap(1);
        else if (event.key === 'ArrowLeft') swap(-1);
        else if (event.key === '+' || event.key === '=') zoomBy(1);
        else if (event.key === '-') zoomBy(-1);
        else if (event.key === 'o' || event.key === 'O') { lb.outline = !lb.outline; showLightbox(); }
        else return;
        event.preventDefault();
    });

    load();
})();
