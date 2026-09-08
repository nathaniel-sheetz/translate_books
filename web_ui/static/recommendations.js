/* Recommendations: fill each chapter as it nears the viewport, and filter what
 * is shown — by kind, and by what became of it.
 *
 * Two jobs, both presentational. This file decides nothing — every item it
 * renders comes from /api/project/<id>/recommendations/<chapter>, which is
 * `_build_chapter_review` (the reader's own builder) plus the reviewed
 * annotations, so this screen and the reader never disagree about what is live.
 *
 * Nothing here acts on the book: no apply, no reject, no dismiss. The inbox and
 * the reader own those, along with the locks and the staleness checks that
 * writing safely needs.
 *
 * The one exception is the heart, which records that you want an item again
 * later. It changes no prose, so it needs none of that machinery.
 */
(function () {
    'use strict';

    var list = document.getElementById('rec-list');
    if (!list) return;

    var PROJECT = list.dataset.project;
    var KINDS = parseMap(list.dataset.kinds);
    var DETAIL_LABELS = parseMap(list.dataset.detailLabels);
    var STATUSES = parseMap(list.dataset.statuses);

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
        /* A card showing stored text has no live sentence to aim at, and the
           reader's anchor lookup fails silently (`if (!match) return`) — the
           link would look like it worked and do nothing. Open the chapter. */
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
        /* A note carries no unanchored_reason — it never quoted the book, so
           "this quote is not verbatim in the prose" would accuse the reader of
           something only a model can do. What is true of a note with no
           sentence is that the sentence it sat on is no longer there. */
        var why = item.source === 'annotation'
            ? list.dataset.textChanged
            : (item.unanchored_reason === 'obsolete'
                ? list.dataset.unanchoredObsolete
                : list.dataset.unanchoredUnplaceable);
        block.appendChild(el('p', 'rec-orphan-why', why));
        return block;
    }

    /* The prose as it stood when the model wrote about it. Shown only when
       there is no live sentence: either the text moved on (a finding you fixed,
       a note whose sentence was rewritten) or the sentence is gone. Kept
       visually apart from live context so a snapshot is never mistaken for what
       the book says now. */
    function originalBlock(item) {
        var block = el('div', 'rec-original-then');
        block.appendChild(el('p', 'rec-original-label', list.dataset.originalThen));
        block.appendChild(el('p', 'rec-original-text', item.original_text || ''));
        block.appendChild(el('p', 'rec-orphan-why', item.unanchored_reason === 'obsolete'
            ? list.dataset.unanchoredObsolete
            : list.dataset.textChanged));
        return block;
    }

    /* Outline when off, filled when on - one path, and `fill` switched in CSS,
       so the two states cannot drift apart. */
    function heart() {
        var svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
        svg.setAttribute('viewBox', '0 0 24 24');
        svg.setAttribute('width', '15');
        svg.setAttribute('height', '15');
        svg.setAttribute('fill', 'none');
        svg.setAttribute('stroke', 'currentColor');
        svg.setAttribute('stroke-width', '2');
        svg.setAttribute('stroke-linejoin', 'round');
        svg.setAttribute('aria-hidden', 'true');
        var path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
        path.setAttribute('d', 'M12 20.5 3.8 12.3a4.9 4.9 0 0 1 6.9-6.9l1.3 1.3 ' +
                               '1.3-1.3a4.9 4.9 0 0 1 6.9 6.9Z');
        svg.appendChild(path);
        return svg;
    }

    /* aria-pressed is the state; the class on the card is what the filter reads
       and the label is what a screen reader hears. All three move together, so
       this is the only place that sets any of them. */
    function setFavorite(btn, on) {
        var card = btn.closest('.rec-card');
        btn.setAttribute('aria-pressed', on ? 'true' : 'false');
        btn.setAttribute('aria-label',
                         on ? list.dataset.favRemove : list.dataset.favAdd);
        if (card) card.classList.toggle('rec-card-fav', on);
    }

    function favButton(item) {
        var btn = el('button', 'topbar-icon-btn rec-fav');
        btn.type = 'button';
        btn.dataset.favId = item.fav_id;
        btn.appendChild(heart());
        setFavorite(btn, !!item.favorite);
        return btn;
    }

    function card(chapterId, item) {
        var node = el('article', 'rec-card rec-kind-' + item.kind +
                                 ' rec-source-' + item.source +
                                 ' rec-status-' + (item.status || 'open'));
        node.setAttribute('role', 'listitem');
        if (item.favorite) node.classList.add('rec-card-fav');
        if (item.stale) node.classList.add('rec-card-stale');
        if (item.status && item.status !== 'open' && item.status !== 'stale') {
            node.classList.add('rec-card-history');
        }

        var meta = el('div', 'rec-meta');
        meta.appendChild(chip('rec-chip-kind', KINDS[item.kind] || item.kind));
        if (item.severity) {
            meta.appendChild(chip('rec-chip-sev rec-chip-sev-' + item.severity, item.severity));
        }
        if (item.category) meta.appendChild(chip('rec-chip-cat', item.category));
        if (item.confidence) meta.appendChild(chip('rec-chip-conf', item.confidence));
        if (item.status && item.status !== 'open') {
            var label = STATUSES[item.status] || item.status;
            /* The date is the whole point of a status on a read-only page: it
               says when you dealt with this, not merely that you did. */
            if (item.status_at) label += ' \u00b7 ' + item.status_at.slice(0, 10);
            meta.appendChild(chip('rec-chip-status rec-chip-status-' + item.status, label));
        }
        /* Not alongside the status chip: `stale` is set precisely when the
           status is `stale`, so both chips would say "edited since" in a row. */
        if (item.stale && item.status !== 'stale') {
            meta.appendChild(chip('rec-chip-stale', list.dataset.stale));
        }
        /* Not when the card shows stored text: "no sentence to show" above a
           sentence reads as a contradiction, and the block underneath already
           says what this is and why it is here. */
        if (item.unanchored_reason && !item.original_text) {
            meta.appendChild(chip('rec-chip-orphan', list.dataset.unanchored));
        }
        var open = el('a', 'rec-open', list.dataset.original);
        open.href = readerHref(chapterId, item);
        meta.appendChild(open);
        /* After the link, which owns margin-left:auto - so the two of them
           cluster at the right edge instead of the heart being pushed alone.
           No fav_id means the item has no stable identity to save against
           (see favorites.finding_id); no heart beats one that cannot persist. */
        if (item.fav_id) meta.appendChild(favButton(item));
        node.appendChild(meta);

        if (item.context && item.context.current) {
            node.appendChild(contextBlock(item));
        } else if (item.original_text) {
            node.appendChild(originalBlock(item));
        } else {
            node.appendChild(orphanBlock(item));
        }

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
                /* The shell counted records in favorites.jsonl, which can name
                   an item that no longer exists — a favorited judge finding
                   loses its id when that judge re-runs and rewords the message.
                   Now that the chapter has rendered we can count the hearts
                   themselves, so "Favorites only" stops showing a heading with
                   nothing under it. */
                section.dataset.favorites = String(items.filter(function (i) {
                    return i.favorite;
                }).length);
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

    /* Delegated, because no card exists when this binds - the same reason the
       filters delegate. The toggle is optimistic: the heart flips at once and
       flips back if the write fails, because waiting on a round trip to colour
       an icon makes the page feel broken on a slow link. */
    list.addEventListener('click', function (event) {
        var btn = event.target.closest && event.target.closest('.rec-fav');
        if (!btn || btn.disabled) return;

        var on = btn.getAttribute('aria-pressed') !== 'true';
        var section = btn.closest('.rec-chapter');
        btn.disabled = true;
        btn.removeAttribute('title');
        setFavorite(btn, on);

        fetch('/api/project/' + encodeURIComponent(PROJECT) +
              '/recommendations/favorite', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ id: btn.dataset.favId, favorite: on }),
        })
            .then(function (r) { return r.json(); })
            .then(function (data) {
                if (!data || !data.ok) throw new Error(data && data.error);
                btn.disabled = false;
                /* Keep the shell's count true as you toggle, or "favorites
                   only" would hide a chapter you just favorited something in
                   and fetch one you just emptied. */
                if (section) {
                    var n = parseInt(section.dataset.favorites, 10) || 0;
                    section.dataset.favorites = String(Math.max(0, n + (on ? 1 : -1)));
                }
            })
            .catch(function () {
                setFavorite(btn, !on);
                btn.disabled = false;
                btn.title = list.dataset.favFailed;
            });
    });

    /* The third axis. Show-only rather than hide, so it gets its own class
       instead of extending hideClass() - and because they are independent
       classes on one container, it intersects with the other two for free.

       Turning it on has to fetch, which neither other filter does. Chapters
       fill as you reach them, so a favorite in a chapter you never scrolled to
       is not in the DOM and CSS alone would show a page of empty headings. The
       shell's per-chapter count says which chapters to fetch; the rest are
       hidden outright, so this stays a handful of requests rather than the
       whole-book fill the lazy loading exists to avoid. */
    var favCb = document.querySelector('.rec-fav-cb');
    if (favCb) {
        var syncFavorites = function () {
            var on = favCb.checked;
            list.classList.toggle('rec-only-favorites', on);
            if (!on) return;
            sections.forEach(function (section, i) {
                if (started[i]) return;
                if ((parseInt(section.dataset.favorites, 10) || 0) < 1) return;
                started[i] = true;
                if (observer) observer.unobserve(section);
                fill(section);
            });
        };

        favCb.addEventListener('change', syncFavorites);
        syncFavorites();
        /* Same restore the two rows below need: Back from the reader brings the
           ticked box without the class the change handler set. */
        window.addEventListener('pageshow', syncFavorites);
    }

    /* Both filter rows drive the same thing: a `rec-hide-<prefix><value>` class
       on the list, which CSS pairs with the matching class on each card. A
       checkbox says which prefix it belongs to, so neither axis knows the other
       exists and unticking one of each hides the intersection. */
    var filters = Array.prototype.slice.call(
        document.querySelectorAll('#rec-filter, #rec-status-filter'));
    filters.forEach(function (filter) {
        /* The container's hide classes are derived state and the checkboxes are
           the source, so read them rather than assume they start ticked. A
           browser restores form-control state on a history navigation: follow a
           card into the reader and come Back, and the boxes you unticked are
           still unticked while the classes the change handler set are gone with
           the old document. That left every kind you had filtered out on screen
           under a box that said it was hidden, and only a tick-untick fixed it.
           pageshow covers the restore that lands after this script has run, and
           the bfcache case where no script runs at all. */
        var hideClass = function (cb) {
            return 'rec-hide-' + (cb.dataset.prefix || '') + cb.value;
        };

        var syncFilter = function () {
            var boxes = filter.querySelectorAll('.rec-hide-cb');
            Array.prototype.forEach.call(boxes, function (cb) {
                list.classList.toggle(hideClass(cb), !cb.checked);
            });
        };

        filter.addEventListener('change', function (event) {
            var cb = event.target;
            if (!cb.classList.contains('rec-hide-cb')) return;
            list.classList.toggle(hideClass(cb), !cb.checked);
        });

        syncFilter();
        window.addEventListener('pageshow', syncFilter);
    });
})();
