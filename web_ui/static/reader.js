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

    const content = document.getElementById('reader-content');
    const bottomSheet = document.getElementById('bottom-sheet');
    const sheetOverlay = document.getElementById('sheet-overlay');
    const sheetEn = document.getElementById('sheet-en');
    const sheetTextarea = document.getElementById('sheet-textarea');
    const btnSave = document.getElementById('btn-save');
    const sheetClose = document.getElementById('sheet-close');
    const sheetHandle = document.getElementById('sheet-handle');
    const readerStats = document.getElementById('reader-stats');

    // Annotation elements
    const annTypeButtons = document.querySelectorAll('.ann-type-btn');
    const annRemoveBtn = document.getElementById('ann-remove');
    const annNoteRow = document.getElementById('ann-note-row');
    const annNoteInput = document.getElementById('ann-note');
    const btnAnnSave = document.getElementById('btn-ann-save');
    const annExisting = document.getElementById('ann-existing');

    let alignmentData = null;
    let annotationsMap = {};   // es_idx -> annotation record
    let activeIdx = null;
    let selectedAnnType = null;

    const ANN_LABELS = {
        word_choice: '\u{1f4ac} Word choice',
        inconsistency: '\u26a0 Inconsistency',
        footnote: '\u{1f4d6} Footnote',
        flag: '\u{1f6a9} Flag',
    };

    // Load alignment data and annotations in parallel
    Promise.all([
        fetch(`/api/alignment/${projectId}/${chapter}`).then(r => {
            if (!r.ok) throw new Error('Alignment not found');
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
            if (readerStats) {
                const annCount = Object.keys(annotationsMap).length;
                let stats = `${data.es_count} sentences`;
                if (annCount > 0) stats += ` · ${annCount} annotated`;
                readerStats.textContent = stats;
            }
        })
        .catch(err => {
            content.innerHTML = `<p class="empty-state">Error: ${err.message}</p>`;
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

        // Show sheet (collapsed)
        bottomSheet.classList.add('visible');
        bottomSheet.classList.remove('expanded');
        sheetOverlay.classList.add('visible');
    }

    function resetAnnotationUI() {
        selectedAnnType = null;
        annTypeButtons.forEach(btn => btn.classList.remove('selected'));
        annNoteRow.style.display = 'none';
        annNoteInput.value = '';
        annExisting.style.display = 'none';
        annExisting.textContent = '';
        annRemoveBtn.classList.remove('has-annotation');
    }

    function closeSheet() {
        bottomSheet.classList.remove('visible', 'expanded');
        sheetOverlay.classList.remove('visible');

        const prev = content.querySelector('.sentence.active');
        if (prev) prev.classList.remove('active');

        activeIdx = null;
        resetAnnotationUI();
    }

    function expandSheet() {
        bottomSheet.classList.add('expanded');
        sheetTextarea.focus();
    }

    // --- Annotation type button handling ---

    annTypeButtons.forEach(btn => {
        btn.addEventListener('click', () => {
            const type = btn.dataset.type;

            if (selectedAnnType === type) {
                // Deselect
                btn.classList.remove('selected');
                selectedAnnType = null;
                annNoteRow.style.display = 'none';
            } else {
                // Select this type
                annTypeButtons.forEach(b => b.classList.remove('selected'));
                btn.classList.add('selected');
                selectedAnnType = type;
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
                    // Update local state
                    annotationsMap[activeIdx] = payload;

                    // Update sentence highlight
                    const el = content.querySelector(`[data-es-idx="${activeIdx}"]`);
                    if (el) {
                        // Remove any existing annotation class
                        el.className = el.className.replace(/\bann-\w+/g, '');
                        el.classList.add('ann-' + selectedAnnType);
                    }

                    // Update stats
                    updateStats();
                    closeSheet();
                }
            })
            .catch(err => alert('Error: ' + err.message))
            .finally(() => {
                btnAnnSave.disabled = false;
                btnAnnSave.textContent = 'Save';
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

        fetch('/api/annotation', {
            method: 'DELETE',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                project_id: projectId,
                chapter_id: chapter,
                es_idx: activeIdx,
            }),
        })
            .then(r => r.json())
            .then(result => {
                if (result.removed) {
                    delete annotationsMap[activeIdx];

                    const el = content.querySelector(`[data-es-idx="${activeIdx}"]`);
                    if (el) {
                        el.className = el.className.replace(/\bann-\w+/g, '');
                    }

                    updateStats();
                    closeSheet();
                }
            })
            .catch(err => alert('Error: ' + err.message));
    });

    function updateStats() {
        if (!readerStats || !alignmentData) return;
        const annCount = Object.keys(annotationsMap).length;
        let stats = `${alignmentData.es_count} sentences`;
        if (annCount > 0) stats += ` · ${annCount} annotated`;
        readerStats.textContent = stats;
    }

    // Tap overlay to close
    sheetOverlay.addEventListener('click', closeSheet);
    sheetClose.addEventListener('click', closeSheet);

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
        btnSave.textContent = 'Saving...';

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
                    alert('Error saving: ' + (result.error || 'Unknown error'));
                }
            })
            .catch(err => {
                alert('Network error: ' + err.message);
            })
            .finally(() => {
                btnSave.disabled = false;
                btnSave.textContent = 'Save';
            });
    });

    // Keyboard shortcut: Escape to close sheet
    document.addEventListener('keydown', e => {
        if (e.key === 'Escape') closeSheet();
    });
})();
