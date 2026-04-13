/**
 * Chunk editor — full-textarea edit for a single chunk's translated text.
 * Posts to /api/chunk/<project>/<chunk_id>/edit which rewrites the chunk,
 * recombines the chapter, and realigns.
 */
(function () {
    'use strict';

    const app = document.getElementById('chunk-edit-app');
    if (!app) return;

    const projectId = app.dataset.project;
    const chapter = app.dataset.chapter;
    const chunkId = app.dataset.chunkId;
    const expectedMtime = parseFloat(app.dataset.mtime) || 0;
    const overlapStart = parseInt(app.dataset.overlapStart || '0', 10) || 0;
    const overlapEnd = parseInt(app.dataset.overlapEnd || '0', 10) || 0;
    const anchorText = app.dataset.anchorText || '';

    const textarea = document.getElementById('chunk-edit-textarea');
    const backBtn = document.getElementById('chunk-edit-back');
    const saveBtn = document.getElementById('chunk-edit-save');
    const statusEl = document.getElementById('chunk-edit-status');

    const initialValue = textarea.value;
    let dirty = false;

    textarea.addEventListener('input', () => {
        dirty = textarea.value !== initialValue;
    });

    function readerUrl(extraQuery) {
        let url = `/read/${projectId}/${chapter}`;
        if (extraQuery) url += '?' + extraQuery;
        return url;
    }

    function goBack() {
        if (dirty && !window.confirm(app.dataset.dirtyConfirm || 'Discard unsaved changes?')) {
            return;
        }
        const params = new URLSearchParams();
        if (anchorText) params.set('anchor', anchorText);
        window.location.href = readerUrl(params.toString());
    }

    backBtn.addEventListener('click', goBack);

    // Warn on accidental navigation (closing tab, back button) while dirty
    window.addEventListener('beforeunload', (e) => {
        if (dirty) {
            e.preventDefault();
            e.returnValue = '';
        }
    });

    // Position the caret near the tapped sentence on load.
    function positionCaret() {
        if (!anchorText) {
            textarea.focus();
            return;
        }
        const idx = textarea.value.indexOf(anchorText);
        if (idx < 0) {
            textarea.focus();
            return;
        }
        // Don't place caret inside the read-only overlap region.
        const caret = Math.max(idx, overlapStart);
        textarea.focus();
        try {
            textarea.setSelectionRange(caret, caret);
        } catch (_) {}
        // Scroll the textarea so the caret line is near the middle.
        // Approximate: lineHeight * (char_offset / avg_chars_per_line)
        const upToCaret = textarea.value.slice(0, caret);
        const linesBefore = upToCaret.split('\n').length;
        const cs = window.getComputedStyle(textarea);
        const lineHeight = parseFloat(cs.lineHeight) || 24;
        const target = Math.max(0, linesBefore * lineHeight - textarea.clientHeight / 2);
        textarea.scrollTop = target;
    }

    // Defer so the browser has laid out the textarea.
    setTimeout(positionCaret, 0);

    function setStatus(msg, ok) {
        statusEl.textContent = msg || '';
        statusEl.classList.toggle('ok', !!ok);
    }

    function doSave() {
        const newText = textarea.value;
        if (newText === initialValue) {
            goBack();
            return;
        }
        // Client-side overlap guard matches the server's.
        if (overlapStart > 0 && newText.slice(0, overlapStart) !== initialValue.slice(0, overlapStart)) {
            setStatus('Cannot edit the first ' + overlapStart + ' characters (overlap with previous chunk).', false);
            return;
        }
        if (overlapEnd > 0 && newText.slice(-overlapEnd) !== initialValue.slice(-overlapEnd)) {
            setStatus('Cannot edit the last ' + overlapEnd + ' characters (overlap with next chunk).', false);
            return;
        }

        saveBtn.disabled = true;
        const prevLabel = saveBtn.textContent;
        saveBtn.textContent = app.dataset.savingLabel || 'Saving...';
        setStatus('', false);

        fetch(`/api/chunk/${projectId}/${chunkId}/edit`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                translated_text: newText,
                expected_mtime: expectedMtime,
            }),
        })
            .then(r => r.json().then(body => ({ status: r.status, body })))
            .then(({ status, body }) => {
                if (status >= 200 && status < 300 && body.ok) {
                    dirty = false;
                    const params = new URLSearchParams();
                    if (anchorText) params.set('anchor', anchorText);
                    window.location.href = readerUrl(params.toString());
                } else {
                    setStatus(body.error || ('HTTP ' + status), false);
                    saveBtn.disabled = false;
                    saveBtn.textContent = prevLabel;
                }
            })
            .catch(err => {
                setStatus('Network error: ' + err.message, false);
                saveBtn.disabled = false;
                saveBtn.textContent = prevLabel;
            });
    }

    saveBtn.addEventListener('click', doSave);

    // Ctrl/Cmd+S to save
    document.addEventListener('keydown', (e) => {
        if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 's') {
            e.preventDefault();
            doSave();
        }
    });
})();
