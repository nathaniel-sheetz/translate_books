/* Recommendations: fill each chapter as it nears the viewport, and filter what
 * is shown by kind.
 *
 * Two jobs, both presentational. This file decides nothing — every item it
 * renders comes from /api/project/<id>/recommendations/<chapter>, which is
 * `_build_chapter_review` (the reader's own builder) plus the reviewed
 * annotations, so this screen and the reader never disagree about what is live.
 *
 * There is no action here on purpose: no apply, no reject, no dismiss. The
 * inbox and the reader own those, along with the locks and the staleness checks
 * that writing safely needs.
 */
(function () {
    'use strict';

    var list = document.getElementById('rec-list');
    if (!list) return;

    var PROJECT = list.dataset.project;
    var KINDS = parseMap(list.dataset.kinds);
    var DETAIL_LABELS = parseMap(list.dataset.detailLabels);

    function parseMap(raw) {
        try {
            return JSON.parse(raw || '{}');
        } catch (e) {
            return {};
        }
    }

    function el(tag, className, text) {
        var node = document.createElement(tag);
        if (className) node.className = className;
        if (text != null) node.textContent = text;
        return node;
    }

    function chip(className, text) {
        return el('span', 'rec-chip ' + className, text);
    }

    /* The reader's deep link: ?anchor=<text prefix> keyed by text rather than
       index, because realign renumbers sentences and a stale es_idx would land
       on the wrong prose. &esi disambiguates when several sentences share the
       prefix; &hl=1 asks the reader to flash what it landed on. */
    function readerHref(chapterId, item) {
        var href = '/read/' + encodeURIComponent(PROJECT) + '/' +
                   encodeURIComponent(chapterId);
        var current = item.context && item.context.current;
        if (!current || !current.es) return href;
        return href + '?anchor=' + encodeURIComponent(current.es.slice(0, 60)) +
               '&esi=' + encodeURIComponent(String(current.es_idx)) + '&hl=1';
    }

    /* The one sentence the item is about, with the offending span marked.
       A judge reports an excerpt and no offsets, so match_start is null and the
       whole sentence is tinted instead — the same fallback the reader paints. */
    function currentSentence(item) {
        var current = item.context && item.context.current;
        var node = el('p', 'rec-ctx rec-ctx-current');
        var text = (current && current.es) || '';
        var start = item.match_start;
        var end = item.match_end;
        if (typeof start === 'number' && typeof end === 'number' &&
            start >= 0 && end > start && end <= text.length) {
            node.appendChild(document.createTextNode(text.slice(0, start)));
            node.appendChild(el('mark', null, text.slice(start, end)));
            node.appendChild(document.createTextNode(text.slice(end)));
        } else {
            node.textContent = text;
            node.classList.add('rec-ctx-tinted');
        }
        return node;
    }

    function contextBlock(item) {
        var ctx = item.context || {};
        var block = el('div', 'rec-context');
        if (ctx.before) block.appendChild(el('p', 'rec-ctx rec-ctx-before', ctx.before.es));
        block.appendChild(currentSentence(item));
        if (ctx.after) block.appendChild(el('p', 'rec-ctx rec-ctx-after', ctx.after.es));
        if (ctx.current && ctx.current.en) {
            var en = el('p', 'rec-en');
            en.appendChild(el('span', 'rec-en-label', list.dataset.sourceLine));
            en.appendChild(document.createTextNode(ctx.current.en));
            block.appendChild(en);
        }
        return block;
    }

    /* No sentence to show: the excerpt on its own, plus why it could not be
       placed. Never attached to a nearby sentence — a misplaced highlight is
       worse than no highlight. */
    function orphanBlock(item) {
        var block = el('div', 'rec-orphan');
        block.appendChild(el('p', 'rec-orphan-excerpt', item.excerpt || ''));
        var why = item.unanchored_reason === 'obsolete'
            ? list.dataset.unanchoredObsolete
            : list.dataset.unanchoredUnplaceable;
        block.appendChild(el('p', 'rec-orphan-why', why));
        return block;
    }

    function card(chapterId, item) {
        var node = el('article', 'rec-card rec-kind-' + item.kind +
                                 ' rec-source-' + item.source);
        node.setAttribute('role', 'listitem');
        if (item.stale) node.classList.add('rec-card-stale');

        var meta = el('div', 'rec-meta');
        meta.appendChild(chip('rec-chip-kind', KINDS[item.kind] || item.kind));
        if (item.severity) {
            meta.appendChild(chip('rec-chip-sev rec-chip-sev-' + item.severity, item.severity));
        }
        if (item.category) meta.appendChild(chip('rec-chip-cat', item.category));
        if (item.confidence) meta.appendChild(chip('rec-chip-conf', item.confidence));
        if (item.stale) meta.appendChild(chip('rec-chip-stale', list.dataset.stale));
        if (item.unanchored_reason) {
            meta.appendChild(chip('rec-chip-orphan', list.dataset.unanchored));
        }
        var open = el('a', 'rec-open', list.dataset.original);
        open.href = readerHref(chapterId, item);
        meta.appendChild(open);
        node.appendChild(meta);

        node.appendChild(
            (item.context && item.context.current) ? contextBlock(item) : orphanBlock(item)
        );

        if (item.suggestion) {
            var sugg = el('div', 'rec-suggestion');
            sugg.appendChild(el('span', 'rec-label', list.dataset.suggestion));
            sugg.appendChild(document.createTextNode(item.suggestion));
            node.appendChild(sugg);
        }
        if (item.explanation) {
            node.appendChild(el('p', 'rec-explanation', item.explanation));
        }
        if (item.detail && item.detail.length) {
            var detail = el('div', 'rec-detail');
            item.detail.forEach(function (entry) {
                var line = el('p', 'rec-detail-text');
                line.appendChild(el('span', 'rec-detail-label',
                                    DETAIL_LABELS[entry.label] || entry.label));
                line.appendChild(document.createTextNode(' ' + entry.text));
                detail.appendChild(line);
            });
            node.appendChild(detail);
        }
        return node;
    }

    function fill(section) {
        var chapterId = section.dataset.chapter;
        var status = section.querySelector('.rec-chapter-status');
        var body = section.querySelector('.rec-items');

        fetch('/api/project/' + encodeURIComponent(PROJECT) +
              '/recommendations/' + encodeURIComponent(chapterId))
            .then(function (r) { return r.json(); })
            .then(function (data) {
                if (!data || !data.ok) throw new Error(data && data.error);
                var items = data.items || [];
                items.forEach(function (item) {
                    body.appendChild(card(chapterId, item));
                });
                /* A chapter can be counted and still fill empty: the shell's
                   counts come from load_chapter_type_counts, which can see a
                   finding in a chunk that has no usable alignment rows for the
                   builder to anchor against. Say so rather than leaving a
                   heading with nothing under it. */
                status.textContent = items.length ? '' : list.dataset.chapterEmpty;
                if (items.length) status.hidden = true;
            })
            .catch(function () {
                status.textContent = list.dataset.failed;
            });
    }

    /* rootMargin starts the fetch a screen and a half early, so a chapter has
       normally landed by the time you scroll onto it. Each section is filled at
       most once and unobserved when it is: this page never refetches, because a
       chapter changing under you as you read it is the one thing a reading
       surface must not do.

       Reaching a section fills every section *before* it, not just that one.
       An observer only reports what is intersecting now, and you do not have to
       scroll through a page to get down it — End, or dragging the scrollbar
       thumb, teleports past whole runs of sections, which then never intersect
       anything and sit on "Loading…" for ever. Jumping to chapter 60 on an
       80-chapter book left 18 of them stuck exactly that way. Filling backwards
       costs a few background fetches you may not read; not filling costs the
       chapters you scroll back up to. */
    var sections = Array.prototype.slice.call(list.querySelectorAll('.rec-chapter'));
    var started = [];

    function fillThrough(last) {
        for (var i = 0; i <= last; i++) {
            if (started[i]) continue;
            started[i] = true;
            if (observer) observer.unobserve(sections[i]);
            fill(sections[i]);
        }
    }

    var observer = null;
    if ('IntersectionObserver' in window) {
        observer = new IntersectionObserver(function (entries) {
            var last = -1;
            entries.forEach(function (entry) {
                if (!entry.isIntersecting) return;
                var index = sections.indexOf(entry.target);
                if (index > last) last = index;
            });
            if (last >= 0) fillThrough(last);
        }, { rootMargin: '150% 0px' });
        sections.forEach(function (section) { observer.observe(section); });
    } else {
        fillThrough(sections.length - 1);
    }

    var filter = document.getElementById('rec-filter');
    if (filter) {
        filter.addEventListener('change', function (event) {
            var cb = event.target;
            if (!cb.classList.contains('rec-kind-cb')) return;
            list.classList.toggle('rec-hide-' + cb.value, !cb.checked);
        });
    }
})();
