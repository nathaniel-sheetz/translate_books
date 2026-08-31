/* Review inbox: tick resolutions, apply them per book, report what landed.
 *
 * The server is the source of truth for every outcome. This file never decides
 * that something was applied — it relays `applied` / `already_applied` /
 * `stale` / `unknown_ids` straight back from `review.apply`, because "stale"
 * (the note was edited in the reader since the review) is the case a hopeful
 * client-side assumption would quietly hide.
 */
(function () {
    'use strict';

    function itemsOf(form) {
        return Array.prototype.slice.call(form.querySelectorAll('.inbox-cb'));
    }

    function selectedKeys(form) {
        return itemsOf(form).filter(function (cb) { return cb.checked; })
                            .map(function (cb) { return cb.value; });
    }

    function setStatus(form, message, isError) {
        var el = form.querySelector('.inbox-status');
        if (!el) return;
        el.textContent = message;
        el.classList.toggle('inbox-error', !!isError);
    }

    /* An applied row is done: drop the checkbox so it cannot be re-submitted,
       and leave the text on the page so you can see what you just did. */
    function retire(form, keys, label) {
        keys.forEach(function (key) {
            var cb = form.querySelector('.inbox-cb[value="' + CSS.escape(key) + '"]');
            if (!cb) return;
            var row = cb.closest('.inbox-item');
            cb.checked = false;
            cb.disabled = true;
            if (row) {
                row.classList.add('inbox-item-done');
                row.style.opacity = '0.55';
                var meta = row.querySelector('.inbox-item-meta');
                if (meta && !meta.querySelector('.inbox-done-tag')) {
                    var tag = document.createElement('span');
                    tag.className = 'inbox-flag inbox-done-tag';
                    tag.textContent = label;
                    meta.appendChild(tag);
                }
            }
        });
    }

    function apply(form) {
        var projectId = form.dataset.project;
        var keys = selectedKeys(form);
        if (!keys.length) {
            setStatus(form, 'Nothing selected.', true);
            return;
        }

        var button = form.querySelector('.inbox-apply');
        if (button) button.disabled = true;
        setStatus(form, 'Applying ' + keys.length + '…', false);

        fetch('/api/review-inbox/apply', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ project_id: projectId, keys: keys })
        }).then(function (response) {
            return response.json().then(function (data) {
                return { ok: response.ok, data: data };
            });
        }).then(function (result) {
            if (button) button.disabled = false;
            if (!result.ok) {
                setStatus(form, result.data.error || 'Apply failed.', true);
                return;
            }
            var data = result.data;
            retire(form, data.applied || [], 'applied');
            retire(form, data.already_applied || [], 'already applied');

            var parts = [(data.applied || []).length + ' applied'];
            if ((data.already_applied || []).length) {
                parts.push((data.already_applied || []).length + ' already applied');
            }
            /* Stale is the one outcome worth spelling out: the note changed in
               the reader after the review, so this resolution describes text
               that is no longer there. Re-review the book to refresh it. */
            if ((data.stale || []).length) {
                parts.push((data.stale || []).length + ' stale (edited since the review — re-run the review)');
            }
            if ((data.unknown_ids || []).length) {
                parts.push((data.unknown_ids || []).length + ' unknown');
            }
            setStatus(form, parts.join(' · '), (data.stale || []).length > 0);

            if (data.needs_epub_rebuild) {
                var panel = form.querySelector('.inbox-epub');
                if (panel) panel.hidden = false;
            }
        }).catch(function (err) {
            if (button) button.disabled = false;
            setStatus(form, 'Apply failed: ' + err, true);
        });
    }

    function rebuild(button) {
        var projectId = button.dataset.project;
        var form = button.closest('.inbox-form');
        button.disabled = true;
        setStatus(form, 'Rebuilding EPUB…', false);
        /* Empty body on purpose: the build route falls back to project.json for
           title, author and metadata, which is exactly the state the reader
           already curated on the dashboard. */
        fetch('/api/project/' + encodeURIComponent(projectId) + '/build-epub', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: '{}'
        }).then(function (response) {
            return response.json().then(function (data) {
                return { ok: response.ok, data: data };
            });
        }).then(function (result) {
            button.disabled = false;
            if (!result.ok) {
                setStatus(form, result.data.error || 'Rebuild failed.', true);
                return;
            }
            setStatus(form, 'Rebuilt ' + result.data.filename + '.', false);
        }).catch(function (err) {
            button.disabled = false;
            setStatus(form, 'Rebuild failed: ' + err, true);
        });
    }

    document.addEventListener('submit', function (event) {
        var form = event.target.closest('.inbox-form');
        if (!form) return;
        event.preventDefault();
        apply(form);
    });

    document.addEventListener('click', function (event) {
        var all = event.target.closest('.inbox-select-all');
        if (all) {
            var form = all.closest('.inbox-form');
            var boxes = itemsOf(form).filter(function (cb) { return !cb.disabled; });
            var turnOn = boxes.some(function (cb) { return !cb.checked; });
            boxes.forEach(function (cb) { cb.checked = turnOn; });
            return;
        }
        var rebuildBtn = event.target.closest('.inbox-rebuild');
        if (rebuildBtn) rebuild(rebuildBtn);
    });
})();
