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

    // Load alignment data and annotations in parallel
    Promise.all([
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
            scrollToAnchorParam();
        })
        .catch(err => {
            content.innerHTML = `<p class="empty-state">${i.error_prefix || 'Error: '}${err.message}</p>`;
        });

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

    function updateStats() {
        if (!readerStats) return;
        const annCount = Object.keys(annotationsMap).length;
        readerStats.innerHTML = annCount > 0 ? STICKY_NOTE_SVG + annCount : '';
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
})();
