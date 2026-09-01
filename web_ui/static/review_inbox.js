/* Review inbox: tick resolutions and apply them per book, or reject them one at
 * a time; report what landed either way.
 *
 * The server is the source of truth for every outcome. This file never decides
 * that something was applied — it relays `applied` / `rejected` /
 * `already_applied` / `stale` / `unknown_ids` straight back from `review.apply`,
 * because "stale" (the note was edited in the reader since the review) is the
 * case a hopeful client-side assumption would quietly hide.
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

    /* One row, one POST. There is deliberately no bulk reject: applying is a
       batch decision (you read a book's suggestions and take the good ones),
       declining is a judgement about one suggestion, and a "reject all" button
       is exactly the gesture that empties a queue nobody read. */
    function reject(button) {
        var row = button.closest('.inbox-item-row');
        var form = button.closest('.inbox-form');
        if (!row || !form) return;

        button.disabled = true;
        setStatus(form, 'Rejecting…', false);

        fetch('/api/review-inbox/reject', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                project_id: form.dataset.project,
                key: button.dataset.key
            })
        }).then(function (response) {
            return response.json().then(function (data) {
                return { ok: response.ok, data: data };
            });
        }).then(function (result) {
            var data = result.data;
            if (!result.ok) {
                button.disabled = false;
                setStatus(form, data.error || 'Reject failed.', true);
                return;
            }
            /* The server decides, not this file. An empty `rejected` means the
               note moved under us — stale, or already settled — and saying so is
               the whole point of relaying its buckets rather than assuming. */
            if (!(data.rejected || []).length) {
                button.disabled = false;
                if ((data.stale || []).length) {
                    setStatus(form, 'Edited since the review — re-run the review.', true);
                } else if ((data.already_applied || []).length) {
                    setStatus(form, 'Already settled.', false);
                    showRejected(row, true);
                } else {
                    setStatus(form, 'Nothing to reject.', true);
                }
                return;
            }
            showRejected(row, false);
            setStatus(form, 'Rejected. It will not come back.', false);
        }).catch(function (err) {
            button.disabled = false;
            setStatus(form, 'Reject failed: ' + err, true);
        });
    }

    /* Grey the row and offer the undo. The row stays on the page until you
       reload — a rejection is durable on disk from the moment it is written, so
       this is a view of what you just did, not a pending state. */
    function showRejected(row, settledElsewhere) {
        var cb = row.querySelector('.inbox-cb');
        var rejectBtn = row.querySelector('.inbox-reject');
        var undoBtn = row.querySelector('.inbox-unreject');
        var tag = row.querySelector('.inbox-rejected-tag');

        row.classList.add('inbox-item-rejected');
        if (cb) { cb.checked = false; cb.disabled = true; }
        if (rejectBtn) rejectBtn.hidden = true;
        if (tag) tag.hidden = false;
        /* Nothing to undo when the record was already settled by something else
           — a prior reject, or the night's own apply. */
        if (undoBtn) undoBtn.hidden = !!settledElsewhere;
    }

    function unreject(button) {
        var row = button.closest('.inbox-item-row');
        var form = button.closest('.inbox-form');
        if (!row || !form) return;

        button.disabled = true;
        setStatus(form, 'Undoing…', false);

        fetch('/api/review-inbox/unreject', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                project_id: form.dataset.project,
                key: button.dataset.key
            })
        }).then(function (response) {
            return response.json().then(function (data) {
                return { ok: response.ok, data: data };
            });
        }).then(function (result) {
            button.disabled = false;
            if (!result.ok) {
                setStatus(form, result.data.error || 'Undo failed.', true);
                return;
            }
            var cb = row.querySelector('.inbox-cb');
            var rejectBtn = row.querySelector('.inbox-reject');
            var tag = row.querySelector('.inbox-rejected-tag');
            row.classList.remove('inbox-item-rejected');
            if (cb) cb.disabled = false;
            if (rejectBtn) { rejectBtn.hidden = false; rejectBtn.disabled = false; }
            if (tag) tag.hidden = true;
            button.hidden = true;
            setStatus(form, 'Rejection lifted — it is back in the queue.', false);
        }).catch(function (err) {
            button.disabled = false;
            setStatus(form, 'Undo failed: ' + err, true);
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
        var rejectBtn = event.target.closest('.inbox-reject');
        if (rejectBtn) {
            reject(rejectBtn);
            return;
        }
        var undoBtn = event.target.closest('.inbox-unreject');
        if (undoBtn) {
            unreject(undoBtn);
            return;
        }
        var rebuildBtn = event.target.closest('.inbox-rebuild');
        if (rebuildBtn) rebuild(rebuildBtn);
    });
})();
