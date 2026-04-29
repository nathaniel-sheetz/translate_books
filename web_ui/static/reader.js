/**
 * Reader Mode — tablet-optimized reading view with tap-to-reveal,
 * inline correction, and sentence-level annotations.
 */

(function () {
    'use strict';

    const app = document.getElementById('reader-app');
    if (!app) return;

    const projectId = app.dataset.project;
    const chapter = app.dataset.chapter;

    // i18n strings injected by the template
    const i = window.__i18n || {};

    // On desktop (mouse/trackpad), auto-expand the sheet on tap — no keyboard popup concern
    const isDesktop = window.matchMedia('(hover: hover) and (pointer: fine)').matches;

    // --- Offline retry queue ---
    const QUEUE_KEY = 'reader_save_queue';

    function getQueue() {
        try { return JSON.parse(localStorage.getItem(QUEUE_KEY)) || []; }
        catch { return []; }
    }

    function enqueue(url, method, payload) {
        const q = getQueue();
        q.push({ url, method, payload, ts: Date.now() });
        localStorage.setItem(QUEUE_KEY, JSON.stringify(q));
    }

    function flushQueue() {
        const q = getQueue();
        if (!q.length) return;
        localStorage.removeItem(QUEUE_KEY);
        let remaining = [];
        q.forEach(item => {
            fetch(item.url, {
                method: item.method,
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(item.payload),
            }).catch(() => { remaining.push(item); })
              .finally(() => {
                  if (remaining.length) {
                      const prev = getQueue();
                      localStorage.setItem(QUEUE_KEY, JSON.stringify(prev.concat(remaining)));
                  }
              });
        });
    }

    flushQueue();

    const content = document.getElementById('reader-content');
    const bottomSheet = document.getElementById('bottom-sheet');
    const sheetOverlay = document.getElementById('sheet-overlay');
    const sheetEn = document.getElementById('sheet-en');
    const sheetTextarea = document.getElementById('sheet-textarea');
    const btnSave = document.getElementById('btn-save');
    const sheetClose = document.getElementById('sheet-close');
    const sheetEditChunk = document.getElementById('sheet-edit-chunk');
    const sheetHandle = document.getElementById('sheet-handle');
    const readerStats = document.getElementById('reader-stats');

    // Annotation elements
    const annTypeButtons = document.querySelectorAll('.ann-type-btn');
    const annRemoveBtn = document.getElementById('ann-remove');
    const annNoteRow = document.getElementById('ann-note-row');
    const annNoteInput = document.getElementById('ann-note');
    const btnAnnSave = document.getElementById('btn-ann-save');
    const annTypeLabel = document.getElementById('ann-type-label');
    const annExisting = document.getElementById('ann-existing');

    let alignmentData = null;
    let annotationsMap = {};   // es_idx -> annotation record
    let activeIdx = null;
    let selectedAnnType = null;

    // Load alignment data and annotations in parallel.
    // Exposed as a function so the removal flow can re-bootstrap after a
    // synchronous recombine + realign on the server.
    function loadAndRender(scrollPrefix) {
        return Promise.all([
            fetch(`/api/alignment/${projectId}/${chapter}`).then(r => {
                if (!r.ok) throw new Error(i.error_alignment || 'Alignment not found');
                return r.json();
            }),
            fetch(`/api/annotations/${projectId}/${chapter}`).then(r => r.json()),
        ])
            .then(([data, annData]) => {
                alignmentData = data;

                // Build annotations map
                annotationsMap = {};
                for (const ann of (annData.annotations || [])) {
                    annotationsMap[ann.es_idx] = ann;
                }

                renderSentences(data.alignments);
                addReviewButton();
                updateStats();
                if (scrollPrefix) {
                    scrollToPrefix(scrollPrefix);
                } else {
                    scrollToAnchorParam();
                }
            })
            .catch(err => {
                content.innerHTML = `<p class="empty-state">${i.error_prefix || 'Error: '}${err.message}</p>`;
            });
    }

    function scrollToPrefix(prefix) {
        if (!prefix || !alignmentData) return;
        const trimmed = prefix.trim().slice(0, 30);
        if (!trimmed) return;
        for (const a of alignmentData.alignments) {
            if (a && typeof a.es === 'string' && a.es.startsWith(trimmed)) {
                const el = content.querySelector(`[data-es-idx="${a.es_idx}"]`);
                if (!el) return;
                setTimeout(() => {
                    const top = el.getBoundingClientRect().top + window.scrollY - 60;
                    window.scrollTo({ top, behavior: 'instant' });
                }, 0);
                return;
            }
        }
    }

    loadAndRender();

    function renderSentences(alignments) {
        content.innerHTML = '';

        for (const a of alignments) {
            // Render image records
            if (a.type === 'image') {
                const div = document.createElement('div');
                div.className = 'reader-image';
                const img = document.createElement('img');
                img.src = a.src;
                img.alt = a.alt || '';
                img.loading = 'lazy';
                div.appendChild(img);
                content.appendChild(div);
                continue;
            }

            // Insert paragraph break when alignment record is tagged
            if (a.para_start) {
                const br = document.createElement('span');
                br.className = 'para-break';
                content.appendChild(br);
            }

            const span = document.createElement('span');
            span.className = 'sentence';
            span.textContent = a.es + ' ';
            span.dataset.esIdx = a.es_idx;

            if (a.confidence === 'low') {
                span.classList.add('low-confidence');
            }
            if (a.corrected) {
                span.classList.add('corrected');
            }

            // Apply annotation highlight
            const ann = annotationsMap[a.es_idx];
            if (ann) {
                span.classList.add('ann-' + ann.type);
            }

            span.addEventListener('click', () => onSentenceTap(a));

            content.appendChild(span);
        }
    }

    function onSentenceTap(alignment) {
        // Deactivate previous
        const prev = content.querySelector('.sentence.active');
        if (prev) prev.classList.remove('active');

        // Activate this sentence
        const el = content.querySelector(`[data-es-idx="${alignment.es_idx}"]`);
        if (el) el.classList.add('active');

        activeIdx = alignment.es_idx;

        // Populate bottom sheet
        sheetEn.textContent = alignment.en;
        sheetTextarea.value = alignment.es;

        // Reset annotation UI
        resetAnnotationUI();

        // Show existing annotation if present
        const ann = annotationsMap[alignment.es_idx];
        if (ann) {
            // Highlight the matching type button
            const matchBtn = document.querySelector(`.ann-type-btn[data-type="${ann.type}"]`);
            if (matchBtn) matchBtn.classList.add('selected');
            selectedAnnType = ann.type;
            annTypeLabel.textContent = ANN_TYPE_NAMES[ann.type] || ann.type;
            annTypeLabel.style.display = 'block';

            // Show existing note
            if (ann.content) {
                annExisting.textContent = ann.content;
                annExisting.style.display = 'block';
            }

            // Show remove button
            annRemoveBtn.classList.add('has-annotation');

            // Pre-fill note input
            annNoteInput.value = ann.content || '';
        }

        // Show sheet — auto-expand on desktop, collapsed on mobile (avoids keyboard popup)
        bottomSheet.classList.add('visible');
        sheetOverlay.classList.add('visible');
        if (isDesktop) {
            expandSheet();
        } else {
            bottomSheet.classList.remove('expanded');
        }
    }

    function resetAnnotationUI() {
        selectedAnnType = null;
        annTypeButtons.forEach(btn => btn.classList.remove('selected'));
        annTypeLabel.style.display = 'none';
        annTypeLabel.textContent = '';
        annNoteRow.style.display = 'none';
        annNoteInput.value = '';
        annExisting.style.display = 'none';
        annExisting.textContent = '';
        annRemoveBtn.classList.remove('has-annotation');
    }

    function closeSheet(scrollToIdx) {
        // Remember which sentence to scroll to before closing
        const targetIdx = scrollToIdx !== undefined ? scrollToIdx : activeIdx;
        const targetEl = targetIdx !== null
            ? content.querySelector(`[data-es-idx="${targetIdx}"]`)
            : null;

        bottomSheet.classList.remove('visible', 'expanded');
        sheetOverlay.classList.remove('visible');

        const prev = content.querySelector('.sentence.active');
        if (prev) prev.classList.remove('active');

        activeIdx = null;
        resetAnnotationUI();

        // Scroll the sentence to the top of the viewport so the reader
        // can continue from where they left off.
        // Wait for the sheet close transition (250ms) to finish first.
        if (targetEl) {
            setTimeout(() => {
                const top = targetEl.getBoundingClientRect().top + window.scrollY - 60;
                window.scrollTo({ top, behavior: 'instant' });
            }, 280);
        }
    }

    function expandSheet() {
        bottomSheet.classList.add('expanded');
        sheetTextarea.focus();
    }

    // After the initial load (or after returning from the chunk editor),
    // scroll to the alignment whose es starts with ?anchor=<prefix>. This is
    // keyed by text instead of es_idx because realign can renumber sentences.
    function scrollToAnchorParam() {
        const params = new URLSearchParams(window.location.search);
        const anchor = params.get('anchor');
        if (!anchor || !alignmentData) return;
        const prefix = anchor.trim();
        if (!prefix) return;

        let match = null;
        for (const a of alignmentData.alignments) {
            if (a && typeof a.es === 'string' && a.es.startsWith(prefix)) {
                match = a;
                break;
            }
        }
        if (!match) return;
        const el = content.querySelector(`[data-es-idx="${match.es_idx}"]`);
        if (!el) return;
        // Strip the anchor param from the URL so refreshes don't keep jumping
        params.delete('anchor');
        const newSearch = params.toString();
        const newUrl = window.location.pathname + (newSearch ? '?' + newSearch : '');
        window.history.replaceState({}, '', newUrl);
        // Defer to give the browser a frame to lay out the content
        setTimeout(() => {
            const top = el.getBoundingClientRect().top + window.scrollY - 60;
            window.scrollTo({ top, behavior: 'instant' });
        }, 0);
    }

    // --- Annotation type button handling ---

    const ANN_TYPE_NAMES = {
        word_choice: i.ann_word_choice || 'Word choice',
        inconsistency: i.ann_inconsistency || 'Inconsistency',
        footnote: i.ann_footnote || 'Footnote',
        flag: i.ann_flag || 'Other',
    };

    annTypeButtons.forEach(btn => {
        btn.addEventListener('click', () => {
            const type = btn.dataset.type;

            if (selectedAnnType === type) {
                // Deselect
                btn.classList.remove('selected');
                selectedAnnType = null;
                annTypeLabel.style.display = 'none';
                annNoteRow.style.display = 'none';
            } else {
                // Select this type
                annTypeButtons.forEach(b => b.classList.remove('selected'));
                btn.classList.add('selected');
                selectedAnnType = type;
                annTypeLabel.textContent = ANN_TYPE_NAMES[type] || type;
                annTypeLabel.style.display = 'block';
                annNoteRow.style.display = 'flex';
                annNoteInput.focus();
            }
        });
    });

    // Save annotation
    btnAnnSave.addEventListener('click', () => {
        if (activeIdx === null || !selectedAnnType) return;

        const payload = {
            project_id: projectId,
            chapter_id: chapter,
            es_idx: activeIdx,
            type: selectedAnnType,
            content: annNoteInput.value.trim(),
        };

        btnAnnSave.disabled = true;
        btnAnnSave.textContent = '...';

        fetch('/api/annotation', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload),
        })
            .then(r => r.json())
            .then(result => {
                if (result.saved) {
                    annotationsMap[activeIdx] = payload;
                    const el = content.querySelector(`[data-es-idx="${activeIdx}"]`);
                    if (el) {
                        el.className = el.className.replace(/\bann-\w+/g, '');
                        el.classList.add('ann-' + selectedAnnType);
                    }
                    updateStats();
                    closeSheet();
                }
            })
            .catch(() => {
                enqueue('/api/annotation', 'POST', payload);
                // Still update UI optimistically
                annotationsMap[activeIdx] = payload;
                const el = content.querySelector(`[data-es-idx="${activeIdx}"]`);
                if (el) {
                    el.className = el.className.replace(/\bann-\w+/g, '');
                    el.classList.add('ann-' + selectedAnnType);
                }
                updateStats();
                closeSheet();
            })
            .finally(() => {
                btnAnnSave.disabled = false;
                btnAnnSave.textContent = i.save || 'Save';
            });
    });

    // Allow Enter key in note input to save
    annNoteInput.addEventListener('keydown', e => {
        if (e.key === 'Enter') {
            e.preventDefault();
            btnAnnSave.click();
        }
    });

    // Remove annotation
    annRemoveBtn.addEventListener('click', () => {
        if (activeIdx === null) return;

        const deletePayload = {
            project_id: projectId,
            chapter_id: chapter,
            es_idx: activeIdx,
        };
        const idxToRemove = activeIdx;

        function applyRemoveUI() {
            delete annotationsMap[idxToRemove];
            const el = content.querySelector(`[data-es-idx="${idxToRemove}"]`);
            if (el) el.className = el.className.replace(/\bann-\w+/g, '');
            updateStats();
            closeSheet();
        }

        fetch('/api/annotation', {
            method: 'DELETE',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(deletePayload),
        })
            .then(r => r.json())
            .then(result => { if (result.removed) applyRemoveUI(); })
            .catch(() => {
                enqueue('/api/annotation', 'DELETE', deletePayload);
                applyRemoveUI();
            });
    });

    const STICKY_NOTE_SVG = '<svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.2" stroke-linecap="round" stroke-linejoin="round" style="vertical-align:-2px;margin-right:3px"><path d="M2 2h12v8l-4 4H2z"/><path d="M10 10v4"/></svg>';

    let annotatedIndices = [];
    let annCyclePos = -1;

    function updateStats() {
        if (!readerStats) return;
        const annCount = Object.keys(annotationsMap).length;
        readerStats.innerHTML = annCount > 0 ? STICKY_NOTE_SVG + annCount : '';
        annotatedIndices = Object.keys(annotationsMap).map(Number).sort((a, b) => a - b);
        annCyclePos = -1;
    }

    if (readerStats) {
        readerStats.addEventListener('click', () => {
            if (annotatedIndices.length === 0) return;
            annCyclePos = (annCyclePos + 1) % annotatedIndices.length;
            const idx = annotatedIndices[annCyclePos];
            const el = content.querySelector(`[data-es-idx="${idx}"]`);
            if (!el) return;
            el.scrollIntoView({ behavior: 'smooth', block: 'start' });
        });
    }

    // Tap overlay to close
    sheetOverlay.addEventListener('click', closeSheet);
    sheetClose.addEventListener('click', closeSheet);

    // Open the full chunk editor for the tapped sentence's chunk.
    if (sheetEditChunk) {
        sheetEditChunk.addEventListener('click', () => {
            if (activeIdx === null || !alignmentData) return;
            const a = alignmentData.alignments.find(x => x.es_idx === activeIdx);
            if (!a || !a.chunk_id) return;
            const anchor = (a.es || '').slice(0, 30);
            const params = new URLSearchParams({
                anchor_idx: String(activeIdx),
                anchor: anchor,
            });
            window.location.href =
                `/read/${projectId}/${chapter}/chunk/${a.chunk_id}/edit?` + params.toString();
        });
    }

    // Tap handle or swipe up to expand
    sheetHandle.addEventListener('click', expandSheet);

    // Touch gesture: swipe up on sheet to expand
    let touchStartY = 0;
    bottomSheet.addEventListener('touchstart', e => {
        touchStartY = e.touches[0].clientY;
    }, { passive: true });

    bottomSheet.addEventListener('touchend', e => {
        const touchEndY = e.changedTouches[0].clientY;
        const diff = touchStartY - touchEndY;
        if (diff > 30 && !bottomSheet.classList.contains('expanded')) {
            expandSheet();
        } else if (diff < -50 && bottomSheet.classList.contains('expanded')) {
            bottomSheet.classList.remove('expanded');
        } else if (diff < -50) {
            closeSheet();
        }
    }, { passive: true });

    // Save correction
    btnSave.addEventListener('click', () => {
        if (activeIdx === null || !alignmentData) return;

        const alignment = alignmentData.alignments.find(a => a.es_idx === activeIdx);
        if (!alignment) return;

        const correctedEs = sheetTextarea.value.trim();
        if (!correctedEs || correctedEs === alignment.es) {
            closeSheet();
            return;
        }

        btnSave.disabled = true;
        btnSave.textContent = i.saving || 'Saving...';

        const payload = {
            project_id: projectId,
            chapter_id: chapter,
            es_idx: activeIdx,
            original_es: alignment.es,
            corrected_es: correctedEs,
            en_reference: alignment.en,
        };

        fetch('/api/correction', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload),
        })
            .then(r => r.json())
            .then(result => {
                if (result.saved) {
                    alignment.es = correctedEs;
                    alignment.corrected = true;

                    const el = content.querySelector(`[data-es-idx="${activeIdx}"]`);
                    if (el) {
                        el.textContent = correctedEs + ' ';
                        el.classList.add('corrected');
                    }

                    closeSheet();
                } else {
                    alert((i.error_saving || 'Error saving: ') + (result.error || 'Unknown error'));
                }
            })
            .catch(() => {
                enqueue('/api/correction', 'POST', payload);
                // Optimistic UI update
                alignment.es = correctedEs;
                alignment.corrected = true;
                const el = content.querySelector(`[data-es-idx="${activeIdx}"]`);
                if (el) {
                    el.textContent = correctedEs + ' ';
                    el.classList.add('corrected');
                }
                closeSheet();
            })
            .finally(() => {
                btnSave.disabled = false;
                btnSave.textContent = i.save || 'Save';
            });
    });

    // Keyboard shortcut: Escape to close sheet
    document.addEventListener('keydown', e => {
        if (e.key === 'Escape') closeSheet();
    });

    // --- Remove-text modal ---

    const removeModal = document.getElementById('remove-modal');
    const removeBtn = document.getElementById('sheet-remove-text');
    const removeEsPane = document.getElementById('remove-es');
    const removeEnPane = document.getElementById('remove-en');
    const removeEsStatus = document.getElementById('remove-es-status');
    const removeEnStatus = document.getElementById('remove-en-status');
    const removeEsReset = document.getElementById('remove-es-reset');
    const removeEnReset = document.getElementById('remove-en-reset');
    const removeEsClear = document.getElementById('remove-es-clear');
    const removeEnClear = document.getElementById('remove-en-clear');
    const removeEsApply = document.getElementById('remove-es-apply');
    const removeEnApply = document.getElementById('remove-en-apply');
    const removeEsUnhi = document.getElementById('remove-es-unhighlight');
    const removeEnUnhi = document.getElementById('remove-en-unhighlight');
    const removeError = document.getElementById('remove-error');
    const removeConfirm = document.getElementById('remove-confirm');
    const removeCancel = document.getElementById('remove-cancel');
    const removeHelpBtn = document.getElementById('remove-help-btn');
    const removeHelp = document.getElementById('remove-help');
    const REMOVE_BTN_LABEL = removeConfirm ? removeConfirm.textContent : 'Remove';
    const removeConfirmOverlay = document.getElementById('remove-confirm-overlay');
    const removeConfirmYes = document.getElementById('remove-confirm-yes');
    const removeConfirmNo = document.getElementById('remove-confirm-no');

    // Populate confirmation dialog text from i18n
    if (removeConfirmOverlay) {
        const cTitle = document.getElementById('remove-confirm-title');
        const cWarn = document.getElementById('remove-confirm-warning');
        if (cTitle) cTitle.textContent = i.remove_confirm_title || 'Are you sure?';
        if (cWarn) cWarn.textContent = i.remove_confirm_warning || 'This action cannot be undone.';
        if (removeConfirmYes) removeConfirmYes.textContent = i.remove_confirm_yes || 'Yes, remove';
        if (removeConfirmNo) removeConfirmNo.textContent = i.remove_confirm_no || 'Go back';
    }

    if (removeHelpBtn && removeHelp) {
        removeHelpBtn.addEventListener('click', () => {
            const open = removeHelp.style.display !== 'none';
            removeHelp.style.display = open ? 'none' : '';
            removeHelpBtn.setAttribute('aria-expanded', open ? 'false' : 'true');
        });
    }

    let removalCtx = null;
    let esSel = null;
    let enSel = null;
    let esSugg = null;
    let enSugg = null;

    function escapeHtml(s) {
        return (s || '').replace(/[&<>"']/g, c => ({
            '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
        }[c]));
    }

    function rangesIntersect(aStart, aEnd, ranges) {
        for (const r of ranges) {
            const bStart = r[0], bEnd = r[1];
            if (aStart < bEnd && bStart < aEnd) return [bStart, bEnd];
        }
        return null;
    }

    function renderPane(paneEl, fullText, sel) {
        if (!sel || sel.start >= sel.end) {
            paneEl.innerHTML = escapeHtml(fullText);
            return;
        }
        const start = Math.max(0, Math.min(sel.start, fullText.length));
        const end = Math.max(start, Math.min(sel.end, fullText.length));
        paneEl.innerHTML =
            escapeHtml(fullText.slice(0, start)) +
            '<span class="hi">' + escapeHtml(fullText.slice(start, end)) + '</span>' +
            escapeHtml(fullText.slice(end));
    }

    function scrollHighlightIntoView(paneEl) {
        const hi = paneEl.querySelector('.hi');
        if (!hi) return;
        const paneRect = paneEl.getBoundingClientRect();
        const hiRect = hi.getBoundingClientRect();
        const offsetWithinPane = hiRect.top - paneRect.top + paneEl.scrollTop;
        paneEl.scrollTop = Math.max(0, offsetWithinPane - 30);
    }

    function statusForSel(sel) {
        if (!sel || sel.start >= sel.end) return '';
        const n = sel.end - sel.start;
        return (i.remove_chars || '{n} char selected').replace('{n}', n);
    }

    function updateRemoveButtons() {
        removeEsStatus.textContent = statusForSel(esSel);
        removeEnStatus.textContent = statusForSel(enSel);
        const valid = !!removalCtx && (
            (esSel && esSel.end > esSel.start) ||
            (enSel && enSel.end > enSel.start)
        );
        removeConfirm.disabled = !valid;
    }

    function showRemoveError(msg) {
        if (!msg) {
            removeError.textContent = '';
            removeError.style.display = 'none';
        } else {
            removeError.textContent = msg;
            removeError.style.display = '';
        }
    }

    function paneCharOffset(paneEl, node, offset) {
        let total = 0;
        const walker = document.createTreeWalker(paneEl, NodeFilter.SHOW_TEXT);
        let n;
        while ((n = walker.nextNode())) {
            if (n === node) return total + offset;
            total += n.nodeValue.length;
        }
        return total;
    }


    function openRemoveModal() {
        if (activeIdx === null || !alignmentData) return;
        const a = alignmentData.alignments.find(x => x.es_idx === activeIdx);
        if (!a) return;
        if (a.type === 'image') {
            alert(i.remove_image_record || "Image records can't be removed here.");
            return;
        }

        showRemoveError('');
        removalCtx = null;
        esSel = enSel = esSugg = enSugg = null;
        removeEsPane.textContent = i.remove_loading || 'Loading…';
        removeEnPane.textContent = '';
        removeEsStatus.textContent = '';
        removeEnStatus.textContent = '';
        removeConfirm.disabled = true;
        removeConfirm.textContent = REMOVE_BTN_LABEL;
        removeModal.style.display = 'flex';
        updateActionButtons();

        fetch(`/api/removal-context/${projectId}/${chapter}/${activeIdx}`)
            .then(r => r.json().then(d => ({ status: r.status, body: d })))
            .then(({ status, body }) => {
                if (status !== 200 || body.error) {
                    showRemoveError(body.error || `HTTP ${status}`);
                    removeEsPane.textContent = '';
                    return;
                }
                removalCtx = body;
                esSugg = body.es_suggested ? { ...body.es_suggested } : null;
                enSugg = body.en_suggested ? { ...body.en_suggested } : null;
                esSel = esSugg ? { ...esSugg } : null;
                enSel = enSugg ? { ...enSugg } : null;
                renderPane(removeEsPane, body.es_full, esSel);
                renderPane(removeEnPane, body.en_full, enSel);
                if (!esSugg && !enSugg) {
                    showRemoveError(i.remove_no_match || "Couldn't seed the highlight — paint your selection.");
                }
                scrollHighlightIntoView(removeEsPane);
                scrollHighlightIntoView(removeEnPane);
                updateRemoveButtons();
            })
            .catch(err => {
                showRemoveError((i.error_prefix || 'Error: ') + err.message);
            });
    }

    function closeRemoveModal() {
        removeModal.style.display = 'none';
        if (removeConfirmOverlay) removeConfirmOverlay.style.display = 'none';
        removalCtx = null;
        esSel = enSel = esSugg = enSugg = null;
        showRemoveError('');
    }

    if (removeBtn) removeBtn.addEventListener('click', openRemoveModal);

    function paneHasSelection(paneEl) {
        const browserSel = window.getSelection();
        if (!browserSel || browserSel.isCollapsed) return false;
        return paneEl.contains(browserSel.anchorNode) && paneEl.contains(browserSel.focusNode);
    }

    function updateActionButtons() {
        const esSelected = paneHasSelection(removeEsPane);
        const enSelected = paneHasSelection(removeEnPane);
        if (removeEsApply) removeEsApply.hidden = !esSelected;
        if (removeEsUnhi) removeEsUnhi.hidden = !esSelected;
        if (removeEsReset) removeEsReset.hidden = esSelected;
        if (removeEsClear) removeEsClear.hidden = esSelected;
        if (removeEnApply) removeEnApply.hidden = !enSelected;
        if (removeEnUnhi) removeEnUnhi.hidden = !enSelected;
        if (removeEnReset) removeEnReset.hidden = enSelected;
        if (removeEnClear) removeEnClear.hidden = enSelected;
    }

    document.addEventListener('selectionchange', () => {
        if (removeModal.style.display !== 'flex') return;
        updateActionButtons();
    });

    function applySelectionAsHighlight(paneEl, lang) {
        if (!removalCtx) return;
        const browserSel = window.getSelection();
        if (!browserSel || browserSel.isCollapsed) return;
        if (!paneEl.contains(browserSel.anchorNode) || !paneEl.contains(browserSel.focusNode)) return;

        const aOffset = paneCharOffset(paneEl, browserSel.anchorNode, browserSel.anchorOffset);
        const fOffset = paneCharOffset(paneEl, browserSel.focusNode, browserSel.focusOffset);
        const start = Math.min(aOffset, fOffset);
        const end = Math.max(aOffset, fOffset);
        if (end <= start) return;

        const fullText = lang === 'es' ? removalCtx.es_full : removalCtx.en_full;
        const ranges = (lang === 'es'
            ? removalCtx.image_token_ranges_es
            : removalCtx.image_token_ranges_en) || [];
        if (rangesIntersect(start, end, ranges)) {
            showRemoveError(i.remove_image_overlap || 'Selection overlaps an image token.');
            browserSel.removeAllRanges();
            updateActionButtons();
            return;
        }
        showRemoveError('');

        if (lang === 'es') esSel = { start, end };
        else enSel = { start, end };
        renderPane(paneEl, fullText, lang === 'es' ? esSel : enSel);
        browserSel.removeAllRanges();
        updateActionButtons();
        updateRemoveButtons();
    }

    if (removeEsApply) removeEsApply.addEventListener('mousedown', e => e.preventDefault());
    if (removeEnApply) removeEnApply.addEventListener('mousedown', e => e.preventDefault());
    if (removeEsUnhi) removeEsUnhi.addEventListener('mousedown', e => e.preventDefault());
    if (removeEnUnhi) removeEnUnhi.addEventListener('mousedown', e => e.preventDefault());

    if (removeEsApply) removeEsApply.addEventListener('click', () => applySelectionAsHighlight(removeEsPane, 'es'));
    if (removeEnApply) removeEnApply.addEventListener('click', () => applySelectionAsHighlight(removeEnPane, 'en'));
    if (removeEsUnhi) removeEsUnhi.addEventListener('click', () => {
        if (!removalCtx) return;
        esSel = null;
        renderPane(removeEsPane, removalCtx.es_full, null);
        const s = window.getSelection(); if (s) s.removeAllRanges();
        updateActionButtons();
        updateRemoveButtons();
        showRemoveError('');
    });
    if (removeEnUnhi) removeEnUnhi.addEventListener('click', () => {
        if (!removalCtx) return;
        enSel = null;
        renderPane(removeEnPane, removalCtx.en_full, null);
        const s = window.getSelection(); if (s) s.removeAllRanges();
        updateActionButtons();
        updateRemoveButtons();
        showRemoveError('');
    });

    removeEsReset.addEventListener('click', () => {
        if (!removalCtx) return;
        esSel = esSugg ? { ...esSugg } : null;
        renderPane(removeEsPane, removalCtx.es_full, esSel);
        scrollHighlightIntoView(removeEsPane);
        updateRemoveButtons();
        showRemoveError('');
    });
    removeEnReset.addEventListener('click', () => {
        if (!removalCtx) return;
        enSel = enSugg ? { ...enSugg } : null;
        renderPane(removeEnPane, removalCtx.en_full, enSel);
        scrollHighlightIntoView(removeEnPane);
        updateRemoveButtons();
        showRemoveError('');
    });
    if (removeEsClear) removeEsClear.addEventListener('click', () => {
        if (!removalCtx) return;
        esSel = null;
        renderPane(removeEsPane, removalCtx.es_full, null);
        updateRemoveButtons();
        showRemoveError('');
    });
    if (removeEnClear) removeEnClear.addEventListener('click', () => {
        if (!removalCtx) return;
        enSel = null;
        renderPane(removeEnPane, removalCtx.en_full, null);
        updateRemoveButtons();
        showRemoveError('');
    });

    removeCancel.addEventListener('click', closeRemoveModal);
    removeModal.addEventListener('click', e => {
        if (e.target === removeModal) closeRemoveModal();
    });

    document.addEventListener('keydown', e => {
        if (e.key === 'Escape' && removeModal.style.display === 'flex') {
            closeRemoveModal();
        }
    });

    // Show confirmation overlay when user clicks "Remove"
    removeConfirm.addEventListener('click', () => {
        if (!removalCtx) return;
        const esCheck = esSel && esSel.end > esSel.start
            ? removalCtx.es_full.slice(esSel.start, esSel.end) : '';
        const enCheck = enSel && enSel.end > enSel.start
            ? removalCtx.en_full.slice(enSel.start, enSel.end) : '';
        if (!esCheck && !enCheck) return;
        if (removeConfirmOverlay) removeConfirmOverlay.style.display = 'flex';
    });

    if (removeConfirmNo) {
        removeConfirmNo.addEventListener('click', () => {
            if (removeConfirmOverlay) removeConfirmOverlay.style.display = 'none';
        });
    }

    // Actually perform the removal only after explicit confirmation
    (removeConfirmYes || removeConfirm).addEventListener('click', function onConfirmedRemove() {
        if (!removeConfirmYes) return;  // guard: only runs on the yes btn
        if (!removalCtx) return;
        if (removeConfirmOverlay) removeConfirmOverlay.style.display = 'none';
        const esSubstr = esSel && esSel.end > esSel.start
            ? removalCtx.es_full.slice(esSel.start, esSel.end) : '';
        const enSubstr = enSel && enSel.end > enSel.start
            ? removalCtx.en_full.slice(enSel.start, enSel.end) : '';
        if (!esSubstr && !enSubstr) return;

        // Capture a prefix of the previous Spanish sentence so we can
        // scroll back to where the eye was after re-render.
        let scrollAnchor = null;
        if (alignmentData) {
            const prev = alignmentData.alignments
                .filter(a => a.type !== 'image' && typeof a.es_idx === 'number' && a.es_idx < activeIdx)
                .pop();
            if (prev && typeof prev.es === 'string') {
                scrollAnchor = prev.es.slice(0, 30);
            }
        }

        showRemoveError('');
        removeConfirm.disabled = true;
        removeConfirm.textContent = i.remove_working || 'Removing…';

        fetch('/api/remove-text', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                project_id: projectId,
                chapter_id: chapter,
                chunk_id: removalCtx.chunk_id,
                es_remove: esSubstr,
                en_remove: enSubstr,
                es_remove_start: esSubstr ? esSel.start : null,
                en_remove_start: enSubstr ? enSel.start : null,
                expected_chunk_mtime: removalCtx.chunk_mtime,
            }),
        })
            .then(r => r.json().then(d => ({ status: r.status, body: d })))
            .then(({ status, body }) => {
                if (status !== 200 || !body.ok) {
                    showRemoveError(body.error || `HTTP ${status}`);
                    removeConfirm.disabled = false;
                    removeConfirm.textContent = REMOVE_BTN_LABEL;
                    return;
                }
                const orphans = body.orphaned_annotations || 0;
                closeRemoveModal();
                closeSheet();
                loadAndRender(scrollAnchor).then(() => {
                    if (orphans > 0 && i.remove_orphans) {
                        // Lightweight surfacing — alert is acceptable here
                        // because orphaned annotations are rare and worth
                        // explicit attention.
                        alert(i.remove_orphans.replace('{n}', orphans));
                    }
                });
            })
            .catch(err => {
                showRemoveError((i.network_error || 'Network error: ') + err.message);
                removeConfirm.disabled = false;
                removeConfirm.textContent = REMOVE_BTN_LABEL;
            });
    });

    // --- Mark as reviewed button ---

    function addReviewButton() {
        const marker = document.createElement('div');
        marker.className = 'review-marker';

        const btn = document.createElement('button');
        btn.className = 'btn-reviewed';

        // Check current status
        fetch(`/api/reviewed/${projectId}/${chapter}`)
            .then(r => r.json())
            .then(data => {
                if (data.reviewed) {
                    btn.classList.add('is-reviewed');
                    btn.textContent = i.reviewed_check || 'Reviewed \u2713';
                } else {
                    btn.textContent = i.mark_reviewed || 'Mark as reviewed';
                }
            })
            .catch(() => {
                btn.textContent = i.mark_reviewed || 'Mark as reviewed';
            });

        btn.addEventListener('click', () => {
            const isReviewed = btn.classList.contains('is-reviewed');
            fetch(`/api/reviewed/${projectId}/${chapter}`, {
                method: isReviewed ? 'DELETE' : 'POST',
            })
                .then(r => r.json())
                .then(() => {
                    if (isReviewed) {
                        btn.classList.remove('is-reviewed');
                        btn.textContent = i.mark_reviewed || 'Mark as reviewed';
                    } else {
                        btn.classList.add('is-reviewed');
                        btn.textContent = i.reviewed_check || 'Reviewed \u2713';
                    }
                })
                .catch(err => alert((i.error_prefix || 'Error: ') + err.message));
        });

        marker.appendChild(btn);
        content.appendChild(marker);
    }

    // ========================================================================
    // Retranslate flow (Phase 2)
    // ========================================================================

    const retransBtn = document.getElementById('sheet-retranslate');
    const retransModal = document.getElementById('retranslate-modal');
    const retransAlign = document.getElementById('retranslate-alignment');
    const retransSource = document.getElementById('retranslate-source');
    const retransCurrent = document.getElementById('retranslate-current');
    const retransModelSel = document.getElementById('retranslate-model');
    const retransRun = document.getElementById('retranslate-run');
    const retransStatus = document.getElementById('retranslate-status');
    const retransNewRow = document.getElementById('retranslate-new-row');
    const retransNew = document.getElementById('retranslate-new');
    const retransCost = document.getElementById('retranslate-cost');
    const retransReset = document.getElementById('retranslate-reset');
    const retransError = document.getElementById('retranslate-error');
    const retransDiscard = document.getElementById('retranslate-discard');
    const retransReplace = document.getElementById('retranslate-replace');
    const retransConfirmOverlay = document.getElementById('retranslate-confirm-overlay');
    const retransConfirmTitle = document.getElementById('retranslate-confirm-title');
    const retransConfirmWarn = document.getElementById('retranslate-confirm-warning');
    const retransConfirmYes = document.getElementById('retranslate-confirm-yes');
    const retransConfirmNo = document.getElementById('retranslate-confirm-no');
    const retransExpandPanel = document.getElementById('retranslate-expand-panel');
    const retransExpandBeforeRow = document.getElementById('retranslate-expand-before-row');
    const retransExpandAfterRow = document.getElementById('retranslate-expand-after-row');
    const retransExpandBefore = document.getElementById('retranslate-expand-before');
    const retransExpandAfter = document.getElementById('retranslate-expand-after');
    const retransExpandBeforePreview = document.getElementById('retranslate-expand-before-preview');
    const retransExpandAfterPreview = document.getElementById('retranslate-expand-after-preview');
    const retransContextCount = document.getElementById('retranslate-context-count');

    let retransCtx = null;     // {row, llmOutput, originalCurrent, originalSource,
                               //  beforeRow, afterRow, beforeIncluded, afterIncluded,
                               //  panelOpen, userEditedSource}
    let modelsLoaded = false;

    function loadModelsOnce() {
        if (modelsLoaded || !retransModelSel) return Promise.resolve();
        return fetch('/api/llm/models')
            .then(r => r.json())
            .then(data => {
                retransModelSel.innerHTML = '';
                const stored = (window.localStorage && localStorage.getItem('retranslate.preferred_model')) || '';
                const seenStored = (data.models || []).some(m => m.id === stored);
                (data.models || []).forEach(m => {
                    const opt = document.createElement('option');
                    opt.value = m.id;
                    opt.textContent = m.name + (m.is_default ? ' (default)' : '');
                    retransModelSel.appendChild(opt);
                });
                if (stored && seenStored) {
                    retransModelSel.value = stored;
                } else if (data.default_model) {
                    retransModelSel.value = data.default_model;
                }
                modelsLoaded = true;
            })
            .catch(() => { /* leave empty; user will see no options */ });
    }

    function findRowByEsIdx(idx) {
        if (!alignmentData) return null;
        return (alignmentData.alignments || []).find(a =>
            a.type !== 'image' && typeof a.es_idx === 'number' && a.es_idx === idx
        );
    }

    // Walk the alignment array by position (skipping image rows) to find the
    // nearest non-empty English-bearing neighbors. Required because intermediate
    // es_idx values inside an N:1 group are not exposed as separate rows.
    function findArrayNeighbors(targetRow) {
        const arr = (alignmentData && alignmentData.alignments) || [];
        const i = arr.indexOf(targetRow);
        if (i < 0) return { before: null, after: null };
        const walk = (start, step) => {
            for (let j = start; j >= 0 && j < arr.length; j += step) {
                const r = arr[j];
                if (r && r.type !== 'image' && r.en) return r;
            }
            return null;
        };
        return { before: walk(i - 1, -1), after: walk(i + 1, +1) };
    }

    function rebuildSourceFromExpansion() {
        if (!retransCtx) return;
        const parts = [];
        if (retransCtx.beforeIncluded && retransCtx.beforeRow && retransCtx.beforeRow.en) {
            parts.push(retransCtx.beforeRow.en);
        }
        parts.push(retransCtx.originalSource);
        if (retransCtx.afterIncluded && retransCtx.afterRow && retransCtx.afterRow.en) {
            parts.push(retransCtx.afterRow.en);
        }
        retransSource.value = parts.join(' ');
        retransCtx.userEditedSource = false;
    }

    function buildContextText() {
        if (!retransContextCount || !retransCtx || !retransCtx.row) return '';
        const raw = parseInt(retransContextCount.value, 10);
        const n = Math.max(0, Math.min(5, isNaN(raw) ? 0 : raw));
        if (!n) return '';
        const arr = (alignmentData && alignmentData.alignments) || [];

        const sourceRows = new Set([retransCtx.row]);
        if (retransCtx.beforeIncluded && retransCtx.beforeRow) sourceRows.add(retransCtx.beforeRow);
        if (retransCtx.afterIncluded && retransCtx.afterRow) sourceRows.add(retransCtx.afterRow);

        const positions = [];
        sourceRows.forEach(r => {
            const p = arr.indexOf(r);
            if (p >= 0) positions.push(p);
        });
        if (!positions.length) return '';
        const minPos = Math.min.apply(null, positions);
        const maxPos = Math.max.apply(null, positions);

        const collect = (start, step, count) => {
            const out = [];
            let pos = start;
            while (out.length < count && pos >= 0 && pos < arr.length) {
                const r = arr[pos];
                if (r && r.type !== 'image' && r.en) out.push(r.en);
                pos += step;
            }
            return out;
        };
        const before = collect(minPos - 1, -1, n).reverse();
        const after = collect(maxPos + 1, +1, n);

        const sections = [];
        if (before.length) sections.push('Before:\n' + before.join(' '));
        if (after.length) sections.push('After:\n' + after.join(' '));
        return sections.join('\n\n');
    }

    function previewSnippet(text) {
        const s = (text || '').trim().replace(/\s+/g, ' ');
        return s.length > 80 ? s.slice(0, 80) + '…' : s;
    }

    function showRetransError(msg) {
        if (!retransError) return;
        if (msg) {
            retransError.textContent = msg;
            retransError.style.display = 'block';
        } else {
            retransError.textContent = '';
            retransError.style.display = 'none';
        }
    }

    function setRetransStatus(msg) {
        if (retransStatus) retransStatus.textContent = msg || '';
    }

    function openRetransModal() {
        if (activeIdx === null || activeIdx === undefined) return;
        const row = findRowByEsIdx(activeIdx);
        if (!row || !row.chunk_id) return;

        const { before, after } = findArrayNeighbors(row);

        retransCtx = {
            row,
            llmOutput: null,
            originalCurrent: row.text_in_chunk || row.es || '',
            originalSource: row.en || '',
            beforeRow: before,
            afterRow: after,
            beforeIncluded: false,
            afterIncluded: false,
            panelOpen: false,
            userEditedSource: false,
        };

        retransSource.value = retransCtx.originalSource;
        retransCurrent.value = retransCtx.originalCurrent;
        retransNew.value = '';
        retransNewRow.style.display = 'none';
        retransCost.textContent = '';
        retransReplace.disabled = true;
        showRetransError('');
        setRetransStatus('');

        // Expansion panel — show one or both neighbor rows when available
        if (retransExpandPanel) retransExpandPanel.style.display = 'none';
        if (retransExpandBefore) retransExpandBefore.checked = false;
        if (retransExpandAfter) retransExpandAfter.checked = false;
        if (retransExpandBeforeRow) {
            if (before && before.en) {
                retransExpandBeforeRow.style.display = 'flex';
                if (retransExpandBeforePreview) retransExpandBeforePreview.textContent = previewSnippet(before.en);
            } else {
                retransExpandBeforeRow.style.display = 'none';
            }
        }
        if (retransExpandAfterRow) {
            if (after && after.en) {
                retransExpandAfterRow.style.display = 'flex';
                if (retransExpandAfterPreview) retransExpandAfterPreview.textContent = previewSnippet(after.en);
            } else {
                retransExpandAfterRow.style.display = 'none';
            }
        }

        // Restore context count from localStorage (clamped to [0, 5])
        if (retransContextCount) {
            let stored = 1;
            try {
                const v = window.localStorage && localStorage.getItem('retranslate.context_count');
                if (v !== null && v !== undefined) {
                    const n = parseInt(v, 10);
                    if (!isNaN(n)) stored = Math.max(0, Math.min(5, n));
                }
            } catch (e) { /* ignore */ }
            retransContextCount.value = String(stored);
        }

        // Alignment badge — always interactive (clickable button) so user can
        // expand source on either high- or low-confidence rows.
        const sim = (typeof row.similarity === 'number') ? row.similarity.toFixed(2) : '—';
        const conf = row.confidence || 'high';
        const tmpl = (conf === 'low')
            ? (i.retranslate_alignment_low || 'alignment: {sim} low')
            : (i.retranslate_alignment_high || 'alignment: {sim} high');
        if (retransAlign) {
            retransAlign.textContent = (tmpl || '').replace('{sim}', sim);
            retransAlign.className = 'retranslate-alignment-badge ' + (conf === 'low' ? 'is-low' : 'is-high');
            retransAlign.setAttribute('role', 'button');
            retransAlign.setAttribute('tabindex', '0');
            retransAlign.setAttribute('aria-expanded', 'false');
        }

        retransModal.style.display = 'flex';
        loadModelsOnce();
    }

    function closeRetransModal() {
        retransModal.style.display = 'none';
        if (retransConfirmOverlay) retransConfirmOverlay.style.display = 'none';
        if (retransExpandPanel) retransExpandPanel.style.display = 'none';
        if (retransExpandBefore) retransExpandBefore.checked = false;
        if (retransExpandAfter) retransExpandAfter.checked = false;
        if (retransAlign) retransAlign.setAttribute('aria-expanded', 'false');
        retransCtx = null;
        showRetransError('');
        setRetransStatus('');
    }

    function toggleExpandPanel() {
        if (!retransExpandPanel || !retransCtx) return;
        const opening = retransExpandPanel.style.display === 'none';
        retransExpandPanel.style.display = opening ? 'flex' : 'none';
        retransCtx.panelOpen = opening;
        if (retransAlign) retransAlign.setAttribute('aria-expanded', opening ? 'true' : 'false');
    }

    if (retransBtn) retransBtn.addEventListener('click', openRetransModal);
    if (retransDiscard) retransDiscard.addEventListener('click', closeRetransModal);
    if (retransModal) retransModal.addEventListener('click', e => {
        if (e.target === retransModal) closeRetransModal();
    });
    document.addEventListener('keydown', e => {
        if (e.key === 'Escape' && retransModal && retransModal.style.display === 'flex') {
            closeRetransModal();
        }
    });

    if (retransAlign) {
        retransAlign.addEventListener('click', toggleExpandPanel);
        retransAlign.addEventListener('keydown', e => {
            if (e.key === 'Enter' || e.key === ' ') {
                e.preventDefault();
                toggleExpandPanel();
            }
        });
    }
    if (retransExpandBefore) retransExpandBefore.addEventListener('change', () => {
        if (!retransCtx) return;
        retransCtx.beforeIncluded = !!retransExpandBefore.checked;
        rebuildSourceFromExpansion();
    });
    if (retransExpandAfter) retransExpandAfter.addEventListener('change', () => {
        if (!retransCtx) return;
        retransCtx.afterIncluded = !!retransExpandAfter.checked;
        rebuildSourceFromExpansion();
    });
    if (retransSource) retransSource.addEventListener('input', () => {
        if (retransCtx) retransCtx.userEditedSource = true;
    });
    if (retransContextCount) {
        const normalizeContextCount = () => {
            const raw = parseInt(retransContextCount.value, 10);
            const n = Math.max(0, Math.min(5, isNaN(raw) ? 1 : raw));
            if (String(n) !== retransContextCount.value) retransContextCount.value = String(n);
            try {
                if (window.localStorage) localStorage.setItem('retranslate.context_count', String(n));
            } catch (e) { /* ignore */ }
        };
        const persistContextCount = () => {
            const raw = parseInt(retransContextCount.value, 10);
            if (isNaN(raw)) return;
            if (raw < 0 || raw > 5) return;
            try {
                if (window.localStorage) localStorage.setItem('retranslate.context_count', String(raw));
            } catch (e) { /* ignore */ }
        };
        retransContextCount.addEventListener('change', normalizeContextCount);
        retransContextCount.addEventListener('blur', normalizeContextCount);
        retransContextCount.addEventListener('input', persistContextCount);
    }

    if (retransRun) retransRun.addEventListener('click', () => {
        if (!retransCtx || !retransCtx.row) return;
        const source = (retransSource.value || '').trim();
        if (!source) {
            showRetransError(i.retranslate_empty_source || 'Source text cannot be empty.');
            return;
        }
        const model = retransModelSel.value || null;
        showRetransError('');
        setRetransStatus(i.retranslate_working || 'Calling LLM…');
        retransRun.disabled = true;

        if (model && window.localStorage) {
            try { localStorage.setItem('retranslate.preferred_model', model); } catch (e) { /* ignore */ }
        }

        fetch('/api/sentence/retranslate', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                project_id: projectId,
                chapter_id: chapter,
                chunk_id: retransCtx.row.chunk_id,
                es_idx: retransCtx.row.es_idx,
                source_text: source,
                model: model,
                context_text: buildContextText(),
                expected_chunk_mtime: retransCtx.row.chunk_mtime,
            }),
        })
            .then(r => r.json().then(d => ({ status: r.status, body: d })))
            .then(({ status, body }) => {
                retransRun.disabled = false;
                setRetransStatus('');
                if (status !== 200 || !body.ok) {
                    showRetransError(body.error || `HTTP ${status}`);
                    return;
                }
                retransCtx.llmOutput = body.new_translation;
                retransNew.value = body.new_translation;
                retransNewRow.style.display = 'block';
                const tmpl = i.retranslate_cost || '{model} · {pin}→{pout} tokens · ${cost}';
                retransCost.textContent = tmpl
                    .replace('{model}', body.model)
                    .replace('{pin}', body.prompt_tokens)
                    .replace('{pout}', body.completion_tokens)
                    .replace('{cost}', body.cost_usd.toFixed(4));
                retransReplace.disabled = false;
            })
            .catch(err => {
                retransRun.disabled = false;
                setRetransStatus('');
                showRetransError((i.network_error || 'Network error: ') + err.message);
            });
    });

    if (retransReset) retransReset.addEventListener('click', () => {
        if (retransCtx && retransCtx.llmOutput !== null) {
            retransNew.value = retransCtx.llmOutput;
            showRetransError('');
        }
    });

    function showRetransConfirm() {
        if (!retransCtx || !retransCtx.row) return;
        const newText = (retransNew.value || '').trim();
        if (!newText) {
            showRetransError(i.retranslate_empty_new || 'New translation cannot be empty.');
            return;
        }
        if (retransConfirmTitle) retransConfirmTitle.textContent = i.retranslate_confirm_title || 'Replace this translation?';
        if (retransConfirmWarn) retransConfirmWarn.textContent = i.retranslate_confirm_warning || '';
        if (retransConfirmYes) retransConfirmYes.textContent = i.retranslate_confirm_yes || 'Yes, replace';
        if (retransConfirmNo) retransConfirmNo.textContent = i.retranslate_confirm_no || 'Cancel';
        if (retransConfirmOverlay) retransConfirmOverlay.style.display = 'flex';
    }

    if (retransReplace) retransReplace.addEventListener('click', showRetransConfirm);
    if (retransConfirmNo) retransConfirmNo.addEventListener('click', () => {
        if (retransConfirmOverlay) retransConfirmOverlay.style.display = 'none';
    });

    if (retransConfirmYes) retransConfirmYes.addEventListener('click', () => {
        if (!retransCtx || !retransCtx.row) return;
        const currentText = retransCurrent.value || '';
        const newText = (retransNew.value || '').trim();
        if (!currentText || !newText) return;
        if (retransConfirmOverlay) retransConfirmOverlay.style.display = 'none';

        // Capture scroll anchor (mirrors remove-text)
        let scrollAnchor = null;
        if (alignmentData) {
            const prev = (alignmentData.alignments || [])
                .filter(a => a.type !== 'image' && typeof a.es_idx === 'number' && a.es_idx < activeIdx)
                .pop();
            if (prev && typeof prev.es === 'string') {
                scrollAnchor = prev.es.slice(0, 30);
            }
        }

        retransReplace.disabled = true;
        setRetransStatus(i.retranslate_replacing || 'Replacing and re-aligning…');
        showRetransError('');

        fetch('/api/sentence/replace', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                project_id: projectId,
                chapter_id: chapter,
                chunk_id: retransCtx.row.chunk_id,
                es_idx: retransCtx.row.es_idx,
                current_translation: currentText,
                new_translation: newText,
                expected_chunk_mtime: retransCtx.row.chunk_mtime,
            }),
        })
            .then(r => r.json().then(d => ({ status: r.status, body: d })))
            .then(({ status, body }) => {
                if (status !== 200 || !body.ok) {
                    let msg = body.error || `HTTP ${status}`;
                    if (status === 422 && i.retranslate_no_match) msg = i.retranslate_no_match;
                    showRetransError(msg);
                    setRetransStatus('');
                    retransReplace.disabled = false;
                    return;
                }
                closeRetransModal();
                closeSheet();
                loadAndRender(scrollAnchor);
            })
            .catch(err => {
                showRetransError((i.network_error || 'Network error: ') + err.message);
                setRetransStatus('');
                retransReplace.disabled = false;
            });
    });
})();
