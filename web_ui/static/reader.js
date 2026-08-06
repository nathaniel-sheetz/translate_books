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

    // Opt-in redesigned sheet. When on, the classic sheet is hidden but still
    // driven here; the v2 skin (reader_sheet_v2.js) mirrors this state and routes
    // its actions back through ReaderCore (exposed at the end of this module).
    const V2 = (window.READER_UI_VERSION === 'v2');

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
    const btnAnnNav = document.getElementById('btn-ann-nav');
    const annNavCount = document.getElementById('ann-nav-count');
    const btnFnNav = document.getElementById('btn-fn-nav');
    const fnNavCount = document.getElementById('fn-nav-count');

    // Annotation elements
    const annTypeButtons = document.querySelectorAll('.ann-type-btn');
    const annRemoveBtn = document.getElementById('ann-remove');
    const annNoteRow = document.getElementById('ann-note-row');
    const annNoteInput = document.getElementById('ann-note');
    const btnAnnSave = document.getElementById('btn-ann-save');
    const annTypeLabel = document.getElementById('ann-type-label');
    const annExisting = document.getElementById('ann-existing');

    // Review-mode elements (bottom-sheet tab strip + findings list)
    const sheetTabs = document.getElementById('sheet-tabs');
    const sheetTabAnnotate = document.getElementById('sheet-tab-annotate');
    const sheetTabErrors = document.getElementById('sheet-tab-errors');
    const sheetErrors = document.getElementById('sheet-errors');
    const sheetAnnotation = document.getElementById('sheet-annotation');
    const sheetEditArea = document.getElementById('sheet-edit-area');

    let alignmentData = null;
    let annotationsMap = {};   // es_idx -> [annotation records] (oldest→newest)
    let activeIdx = null;
    let activeRepSubId = null; // sub_id of the annotation the classic single-slot UI targets
    let selectedAnnType = null;

    // Sentence highlight color is chosen by type ranking when a sentence carries
    // several annotations: word_choice > inconsistency > footnote > flag(Other).
    const ANN_RANK = ['word_choice', 'inconsistency', 'footnote', 'flag'];
    function annHighlightType(list) {
        if (!list || !list.length) return null;
        for (const t of ANN_RANK) {
            if (list.some(a => a.type === t)) return t;
        }
        return list[list.length - 1].type;  // unknown type: fall back to newest
    }
    function repaintHighlight(idx) {
        const el = content.querySelector(`[data-es-idx="${idx}"]`);
        if (!el) return;
        el.className = el.className.replace(/\bann-\w+/g, '');
        const hlType = annHighlightType(annotationsMap[idx]);
        if (hlType) el.classList.add('ann-' + hlType);
    }

    // --- Review mode (opt-in overlay of evaluator findings) ---
    // Selection is chosen on the chapter-list page and persisted per project;
    // the reader only reads it. When off, the reader behaves exactly as before.
    const REVIEW_TYPES = ['blacklist', 'grammar', 'dictionary', 'completeness', 'dialogue', 'address'];

    function loadReviewConfig() {
        try {
            const raw = localStorage.getItem('reader_review:' + projectId);
            if (!raw) return { on: false, types: [] };
            const c = JSON.parse(raw) || {};
            return { on: !!c.on, types: Array.isArray(c.types) ? c.types : [] };
        } catch (e) {
            return { on: false, types: [] };
        }
    }

    const reviewConfig = loadReviewConfig();
    let reviewMap = {};   // es_idx (string) -> [finding, ...] filtered to enabled types

    // Load alignment data and annotations in parallel.
    // Exposed as a function so the removal flow can re-bootstrap after a
    // synchronous recombine + realign on the server.
    function loadAndRender(scrollPrefix) {
        const fetches = [
            fetch(`/api/alignment/${projectId}/${chapter}`).then(r => {
                if (!r.ok) throw new Error(i.error_alignment || 'Alignment not found');
                return r.json();
            }),
            fetch(`/api/annotations/${projectId}/${chapter}`).then(r => r.json()),
        ];
        // Only pay for the review fetch when review mode is enabled — when off,
        // the reader load is byte-for-byte identical to before.
        if (reviewConfig.on) {
            fetches.push(
                fetch(`/api/project/${projectId}/review/${chapter}`)
                    .then(r => {
                        if (!r.ok) return { _reviewFailed: true, by_es_idx: {}, stale_chunks: 0 };
                        return r.json();
                    })
                    .catch(() => ({ _reviewFailed: true, by_es_idx: {}, stale_chunks: 0 }))
            );
        }
        return Promise.all(fetches)
            .then(results => {
                const data = results[0];
                const annData = results[1];
                alignmentData = data;

                // Build annotations map: es_idx -> [records], oldest→newest.
                annotationsMap = {};
                for (const ann of (annData.annotations || [])) {
                    (annotationsMap[ann.es_idx] || (annotationsMap[ann.es_idx] = [])).push(ann);
                }
                for (const k of Object.keys(annotationsMap)) {
                    annotationsMap[k].sort((a, b) =>
                        String(a.timestamp || '').localeCompare(String(b.timestamp || '')) ||
                        String(a.sub_id || '').localeCompare(String(b.sub_id || '')));
                }

                const reviewData = reviewConfig.on ? results[2] : null;
                if (reviewData && reviewData._reviewFailed) {
                    showToast(i.review_load_failed || 'Could not load review findings.');
                    buildReviewMap(null);
                } else {
                    buildReviewMap(reviewData);
                    if (reviewData && reviewData.stale_chunks > 0) {
                        const tmpl = i.review_stale_chunks || '{n} chunk(s) skipped (stale after edit)';
                        showToast(tmpl.replace('{n}', String(reviewData.stale_chunks)));
                    }
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
            } else if (a.verse_line_break) {
                const br = document.createElement('span');
                br.className = 'verse-break';
                content.appendChild(br);
            }

            const span = document.createElement('span');
            span.className = 'sentence';
            span.dataset.esIdx = a.es_idx;

            // Review mode: paint evaluator findings onto the sentence; otherwise
            // render plain text exactly as before.
            const findings = reviewConfig.on ? reviewMap[a.es_idx] : null;
            if (findings && findings.length) {
                paintSentence(span, a.es, findings);
            } else {
                span.textContent = a.es + ' ';
            }

            if (a.confidence === 'low') {
                span.classList.add('low-confidence');
            }
            if (a.corrected) {
                span.classList.add('corrected');
            }

            // Apply annotation highlight (ranked when several share a sentence)
            const hlType = annHighlightType(annotationsMap[a.es_idx]);
            if (hlType) {
                span.classList.add('ann-' + hlType);
            }

            span.addEventListener('click', (e) => onSentenceTap(a, e));

            content.appendChild(span);
        }
    }

    // Resolve the word directly under a tap so a new annotation can pre-fill it as
    // a footnote anchor / mnemonic. We read caret position from the tap rather than
    // restructuring the DOM into per-word spans (review highlights may split the
    // sentence across multiple text nodes / spans).
    // Returns null when the tap misses a word (whitespace) or no event is present
    // (programmatic calls) — callers treat null as "no word captured".
    function wordAtPoint(evt) {
        if (!evt || evt.clientX == null || evt.clientY == null) return null;
        let node = null, offset = 0;
        if (document.caretRangeFromPoint) {            // Chrome/WebKit
            const r = document.caretRangeFromPoint(evt.clientX, evt.clientY);
            if (r) { node = r.startContainer; offset = r.startOffset; }
        } else if (document.caretPositionFromPoint) {  // Firefox
            const p = document.caretPositionFromPoint(evt.clientX, evt.clientY);
            if (p) { node = p.offsetNode; offset = p.offset; }
        }
        if (!node || node.nodeType !== Node.TEXT_NODE) return null;
        const text = node.textContent || '';
        const isWord = (ch) => ch != null && (/[\p{L}\p{N}]/u.test(ch) || ch === "'" || ch === '’' || ch === '-');
        let s = offset, e = offset;
        while (s > 0 && isWord(text[s - 1])) s--;
        while (e < text.length && isWord(text[e])) e++;
        return text.slice(s, e).replace(/^['’-]+|['’-]+$/g, '').trim() || null;
    }

    function onSentenceTap(alignment, evt) {
        // Deactivate previous
        const prev = content.querySelector('.sentence.active');
        if (prev) prev.classList.remove('active');

        // Activate this sentence
        const el = content.querySelector(`[data-es-idx="${alignment.es_idx}"]`);
        if (el) el.classList.add('active');

        activeIdx = alignment.es_idx;

        // Capture the tapped word (if any) to pre-fill a new annotation below.
        const tappedWord = wordAtPoint(evt);

        // Populate bottom sheet
        sheetEn.textContent = alignment.en;
        sheetTextarea.value = alignment.es;

        // Reset annotation UI
        resetAnnotationUI();

        // Classic single-slot UI shows one representative (newest) annotation;
        // the v2 skin (below) shows the full list.
        const annList = annotationsMap[alignment.es_idx] || [];
        const rep = annList.length ? annList[annList.length - 1] : null;
        activeRepSubId = rep ? (rep.sub_id || null) : null;
        if (rep) {
            // Highlight the matching type button
            const matchBtn = document.querySelector(`.ann-type-btn[data-type="${rep.type}"]`);
            if (matchBtn) matchBtn.classList.add('selected');
            selectedAnnType = rep.type;
            annTypeLabel.textContent = ANN_TYPE_NAMES[rep.type] || rep.type;
            annTypeLabel.style.display = 'block';

            // Show existing note
            if (rep.content) {
                annExisting.textContent = rep.content;
                annExisting.style.display = 'block';
            }

            // Show remove button
            annRemoveBtn.classList.add('has-annotation');

            // Pre-fill note input
            annNoteInput.value = rep.content || '';
        }

        // Review mode: populate the Errors tab and default to it when this
        // sentence carries findings (otherwise land on Annotate/Edit).
        let defaultErrors = false;
        if (reviewConfig.on) {
            const findings = reviewMap[alignment.es_idx] || [];
            renderErrorsList(alignment.es_idx, findings);
            updateErrorsTabCount(findings.length);
            sheetTabs.style.display = 'flex';
            defaultErrors = findings.length > 0;
            setSheetTab(defaultErrors ? 'errors' : 'annotate');
        } else {
            sheetTabs.style.display = 'none';
            setSheetTab('annotate');
        }

        // Show sheet — auto-expand on desktop, collapsed on mobile (avoids keyboard
        // popup). When landing on the Errors tab there is no input to focus, so
        // expand it (both platforms) without stealing focus to the textarea.
        bottomSheet.classList.add('visible');
        sheetOverlay.classList.add('visible');
        if (isDesktop) {
            bottomSheet.classList.add('expanded');
            if (!defaultErrors) sheetTextarea.focus();
        } else if (defaultErrors) {
            bottomSheet.classList.add('expanded');
        } else {
            bottomSheet.classList.remove('expanded');
        }

        // Hand the same state to the v2 skin (which owns the visible sheet).
        if (V2 && window.ReaderSheetV2) {
            window.ReaderSheetV2.onOpen({
                esIdx: alignment.es_idx,
                en: alignment.en,
                es: alignment.es,
                anns: annotationsMap[alignment.es_idx] || [],
                findings: reviewConfig.on ? (reviewMap[alignment.es_idx] || []) : [],
                reviewOn: reviewConfig.on,
                defaultErrors: defaultErrors,
                tappedWord: tappedWord,
            });
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
        if (V2 && window.ReaderSheetV2) window.ReaderSheetV2.onClose();

        const prev = content.querySelector('.sentence.active');
        if (prev) prev.classList.remove('active');

        activeIdx = null;
        resetAnnotationUI();
        resetReviewSheet();

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

    // ── Review mode: findings map, highlight painting, and the Errors tab ──────

    function buildReviewMap(reviewData) {
        reviewMap = {};
        if (!reviewData || !reviewData.by_es_idx) return;
        const enabled = reviewConfig.types;
        for (const esIdx in reviewData.by_es_idx) {
            const list = (reviewData.by_es_idx[esIdx] || [])
                .filter(f => enabled.indexOf(f.eval_name) !== -1);
            if (list.length) reviewMap[esIdx] = list;
        }
    }

    // Paint a sentence: wrap each locatable finding's offending text in a
    // per-type highlight span; fall back to a whole-sentence tint for findings
    // whose span can't be located (e.g. multi-sentence dialogue excerpts).
    function paintSentence(span, esText, findings) {
        const ranges = [];                 // {start, end, type}
        const sentenceLevelTypes = new Set();

        const wordFindings = findings
            .filter(f => f.match_start !== null && f.match)
            .sort((a, b) => (a.match_start || 0) - (b.match_start || 0));

        let cursor = 0;
        for (const f of wordFindings) {
            let idx = esText.indexOf(f.match, cursor);
            if (idx === -1) idx = esText.indexOf(f.match);
            if (idx === -1) {
                // Located in the chunk but not in the rendered sentence text —
                // tint the whole sentence rather than dropping the signal.
                sentenceLevelTypes.add(f.eval_name);
                continue;
            }
            ranges.push({ start: idx, end: idx + f.match.length, type: f.eval_name });
            cursor = idx + f.match.length;
        }
        for (const f of findings) {
            if (f.match_start === null || !f.match) sentenceLevelTypes.add(f.eval_name);
        }

        // Drop overlapping ranges (first-in-order wins) so slicing stays valid.
        ranges.sort((a, b) => a.start - b.start);
        const clean = [];
        let lastEnd = 0;
        for (const r of ranges) {
            if (r.start < lastEnd) continue;
            clean.push(r);
            lastEnd = r.end;
        }

        if (clean.length) {
            let html = '';
            let pos = 0;
            for (const r of clean) {
                html += escapeHtml(esText.slice(pos, r.start));
                html += '<span class="review-hl review-' + r.type + '">' +
                        escapeHtml(esText.slice(r.start, r.end)) + '</span>';
                pos = r.end;
            }
            html += escapeHtml(esText.slice(pos)) + ' ';
            span.innerHTML = html;
        } else {
            span.textContent = esText + ' ';
        }

        if (sentenceLevelTypes.size) {
            span.classList.add('review-flagged');
            sentenceLevelTypes.forEach(t => span.classList.add('review-' + t));
        }
    }

    // Re-render one sentence's review visuals after its findings change.
    function refreshSentenceReview(esIdx) {
        const el = content.querySelector(`[data-es-idx="${esIdx}"]`);
        if (!el) return;
        const a = alignmentData &&
            alignmentData.alignments.find(x => x.es_idx === Number(esIdx));
        const esText = a ? a.es : (el.textContent || '').replace(/\s+$/, '');
        el.classList.remove('review-flagged');
        REVIEW_TYPES.forEach(t => el.classList.remove('review-' + t));
        const findings = reviewMap[esIdx];
        if (findings && findings.length) {
            paintSentence(el, esText, findings);
        } else {
            el.textContent = esText + ' ';
        }
    }

    function updateErrorsTabCount(n) {
        if (!sheetTabErrors) return;
        sheetTabErrors.textContent = (i.review_tab_errors || 'Errors') + ' (' + n + ')';
        sheetTabErrors.disabled = n === 0;
    }

    function setSheetTab(tab) {
        const errors = tab === 'errors';
        if (sheetErrors) sheetErrors.style.display = errors ? 'block' : 'none';
        if (sheetAnnotation) sheetAnnotation.style.display = errors ? 'none' : '';
        if (sheetEditArea) sheetEditArea.style.display = errors ? 'none' : '';
        if (sheetTabAnnotate) sheetTabAnnotate.classList.toggle('active', !errors);
        if (sheetTabErrors) sheetTabErrors.classList.toggle('active', errors);
    }

    function resetReviewSheet() {
        if (sheetTabs) sheetTabs.style.display = 'none';
        if (sheetErrors) {
            sheetErrors.innerHTML = '';
            sheetErrors.style.display = 'none';
        }
        // Restore the annotate/edit panels the errors tab may have hidden.
        if (sheetAnnotation) sheetAnnotation.style.display = '';
        if (sheetEditArea) sheetEditArea.style.display = '';
    }

    function renderErrorsList(esIdx, findings) {
        if (!sheetErrors) return;
        sheetErrors.innerHTML = '';
        if (!findings || !findings.length) {
            const empty = document.createElement('div');
            empty.className = 'review-empty';
            empty.textContent = i.review_no_errors || 'No findings.';
            sheetErrors.appendChild(empty);
            return;
        }
        const typeLabels = i.review_types || {};
        findings.forEach(f => {
            const item = document.createElement('div');
            item.className = 'review-item review-item-' + f.eval_name;

            const head = document.createElement('div');
            head.className = 'review-item-head';
            const typeEl = document.createElement('span');
            typeEl.className = 'review-item-type review-' + f.eval_name;
            typeEl.textContent = typeLabels[f.eval_name] || f.eval_name;
            head.appendChild(typeEl);
            const sevKey = 'review_sev_' + (f.severity || 'info');
            const sevEl = document.createElement('span');
            sevEl.className = 'review-item-sev sev-' + (f.severity || 'info');
            sevEl.textContent = i[sevKey] || f.severity || '';
            head.appendChild(sevEl);
            item.appendChild(head);

            if (f.message) {
                const msg = document.createElement('div');
                msg.className = 'review-item-msg';
                msg.textContent = f.message;
                item.appendChild(msg);
            }
            if (f.excerpt) {
                const ex = document.createElement('div');
                ex.className = 'review-item-excerpt';
                ex.textContent = f.excerpt;
                item.appendChild(ex);
            }
            if (f.suggestion) {
                const sug = document.createElement('div');
                sug.className = 'review-item-suggestion';
                const lbl = document.createElement('span');
                lbl.className = 'review-sug-label';
                lbl.textContent = (i.review_suggestion_label || 'Suggestion:') + ' ';
                sug.appendChild(lbl);
                appendSuggestionText(sug, f.suggestion);
                item.appendChild(sug);
            }

            const fb = document.createElement('div');
            fb.className = 'review-item-fb';
            ['resolved', 'false_positive', 'bad_message', 'missing_context_gap']
                .forEach(ftype => {
                    const b = document.createElement('button');
                    b.type = 'button';
                    b.className = 'review-fb-btn review-fb-' + ftype;
                    b.textContent = i['review_fb_' + ftype] || ftype;
                    b.addEventListener('click', () => submitFeedback(esIdx, f, ftype, item));
                    fb.appendChild(b);
                });
            item.appendChild(fb);

            sheetErrors.appendChild(item);
        });
    }

    // Record feedback on a finding, then drop it (and any sibling locations of
    // the same underlying issue) from the map + highlights. Mirrors the offline
    // enqueue fallback used elsewhere so it still works without a network.
    function submitFeedback(esIdx, finding, feedbackType, itemEl) {
        const payload = {
            eval_name: finding.eval_name,
            issue_index: finding.issue_index,
            feedback_type: feedbackType,
        };
        const url = `/api/project/${projectId}/evaluations/${finding.chunk_id}/feedback`;

        if (itemEl) {
            itemEl.querySelectorAll('button').forEach(b => { b.disabled = true; });
        }

        const finish = () => {
            // Server dismisses by (eval_name, issue_index) for the chunk, so a
            // fanned-out issue with multiple locations clears everywhere at once.
            const affected = [];
            for (const key in reviewMap) {
                const before = reviewMap[key].length;
                reviewMap[key] = reviewMap[key].filter(x => !(
                    x.eval_name === finding.eval_name &&
                    x.issue_index === finding.issue_index &&
                    x.chunk_id === finding.chunk_id
                ));
                if (reviewMap[key].length !== before) affected.push(key);
                if (!reviewMap[key].length) delete reviewMap[key];
            }
            affected.forEach(k => refreshSentenceReview(k));

            const remaining = reviewMap[esIdx] || [];
            renderErrorsList(esIdx, remaining);
            updateErrorsTabCount(remaining.length);
            if (!remaining.length) {
                if (feedbackType === 'resolved') {
                    setSheetTab('annotate');
                } else {
                    closeSheet(esIdx);
                }
            }
        };

        fetch(url, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload),
        })
            .then(r => { if (!r.ok) throw new Error('http ' + r.status); return r.json(); })
            .then(finish)
            .catch(() => {
                if (itemEl) {
                    itemEl.querySelectorAll('button').forEach(b => { b.disabled = false; });
                }
                try {
                    enqueue(url, 'POST', payload);
                } catch (e) { /* localStorage full — queue unavailable */ }
                showToast(i.review_fb_failed || 'Feedback not saved; queued for retry.');
            });
    }

    if (sheetTabAnnotate) {
        sheetTabAnnotate.addEventListener('click', () => setSheetTab('annotate'));
    }
    if (sheetTabErrors) {
        sheetTabErrors.addEventListener('click', () => {
            if (!sheetTabErrors.disabled) setSheetTab('errors');
        });
        // Static half of the label; the count is appended per sentence.
        updateErrorsTabCount(0);
    }
    if (sheetTabAnnotate) {
        sheetTabAnnotate.textContent = i.review_tab_annotate || 'Annotate';
    }

    // Transient highlight on a sentence we just jumped to — never .active, so
    // the annotate/edit bottom sheet stays closed. (D3) The keyframes and the
    // prefers-reduced-motion fallback live in reader.css.
    function flashLanded(el) {
        if (!el) return;
        el.classList.add('search-landed');
        setTimeout(() => el.classList.remove('search-landed'), 1800);
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
        // Search result deep-links pass &hl=1 to request a transient flash on
        // landing. Other ?anchor= landings (remove-text, chunk-edit) omit it
        // and are unaffected — they scroll but never flash. (D3)
        const flash = params.get('hl') === '1';
        // Optional disambiguator from search results: when several es share the
        // same prefix, &esi=<es_idx> picks the exact sentence. Falls back to
        // first-prefix-match if the idx is absent or was renumbered by realign.
        const esi = params.get('esi');

        let match = null;
        if (esi !== null && esi !== '') {
            for (const a of alignmentData.alignments) {
                if (a && String(a.es_idx) === esi &&
                    typeof a.es === 'string' && a.es.startsWith(prefix)) {
                    match = a;
                    break;
                }
            }
        }
        if (!match) {
            for (const a of alignmentData.alignments) {
                if (a && typeof a.es === 'string' && a.es.startsWith(prefix)) {
                    match = a;
                    break;
                }
            }
        }
        if (!match) return;
        const el = content.querySelector(`[data-es-idx="${match.es_idx}"]`);
        if (!el) return;
        // Strip the anchor/hl/esi params from the URL so refreshes don't keep
        // jumping or re-flashing.
        params.delete('anchor');
        params.delete('hl');
        params.delete('esi');
        const newSearch = params.toString();
        const newUrl = window.location.pathname + (newSearch ? '?' + newSearch : '');
        window.history.replaceState({}, '', newUrl);
        // Defer to give the browser a frame to lay out the content
        setTimeout(() => {
            const top = el.getBoundingClientRect().top + window.scrollY - 60;
            window.scrollTo({ top, behavior: 'instant' });
            if (flash) flashLanded(el);
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

    // Save annotation. Extracted so the v2 skin can persist directly with an
    // explicit (type, content), while the classic button path (below) keeps
    // reading from selectedAnnType + the note input exactly as before.
    function doSaveAnnotation(type, text, subId) {
        if (activeIdx === null || !type) return;

        const idx = activeIdx;
        const payload = {
            project_id: projectId,
            chapter_id: chapter,
            es_idx: idx,
            type: type,
            content: text,
        };
        // A sub_id means "edit this annotation"; its absence means "create a new
        // one" (the server assigns a fresh sub_id and returns it).
        if (subId) payload.sub_id = subId;

        function applySaved(savedSubId) {
            const sid = savedSubId || subId || null;
            const rec = { es_idx: idx, type: type, content: text, sub_id: sid };
            const list = annotationsMap[idx] || (annotationsMap[idx] = []);
            const pos = sid ? list.findIndex(a => (a.sub_id || null) === sid) : -1;
            if (pos >= 0) list[pos] = rec; else list.push(rec);
            repaintHighlight(idx);
            updateStats();
            // v2: keep the sheet open and refresh the card list so the user can
            // add another annotation. Classic: close as before.
            if (V2 && window.ReaderSheetV2) {
                activeRepSubId = sid;
                window.ReaderSheetV2.setAnnotations(annotationsMap[idx] || []);
            } else {
                closeSheet();
            }
        }

        btnAnnSave.disabled = true;
        btnAnnSave.textContent = '...';

        fetch('/api/annotation', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload),
        })
            .then(r => r.json())
            .then(result => { if (result.saved) applySaved(result.sub_id); })
            .catch(() => {
                // Offline: mint a client sub_id so the optimistic record stays
                // addressable for later edit/delete.
                if (!payload.sub_id) payload.sub_id = 'u' + Math.random().toString(16).slice(2, 10);
                enqueue('/api/annotation', 'POST', payload);
                applySaved(payload.sub_id);
            })
            .finally(() => {
                btnAnnSave.disabled = false;
                btnAnnSave.textContent = i.save || 'Save';
            });
    }

    // Save annotation (classic single-slot button edits the representative when
    // one exists, otherwise creates a new annotation).
    btnAnnSave.addEventListener('click', () => {
        if (activeIdx === null || !selectedAnnType) return;
        doSaveAnnotation(selectedAnnType, annNoteInput.value.trim(), activeRepSubId);
    });

    // Allow Enter key in note input to save
    annNoteInput.addEventListener('keydown', e => {
        if (e.key === 'Enter') {
            e.preventDefault();
            btnAnnSave.click();
        }
    });

    // Remove one annotation by sub_id (null → the legacy single-slot record).
    function doRemoveAnnotation(idx, subId) {
        if (idx === null || idx === undefined) return;

        const deletePayload = {
            project_id: projectId,
            chapter_id: chapter,
            es_idx: idx,
        };
        if (subId) deletePayload.sub_id = subId;

        function applyRemoveUI() {
            const list = annotationsMap[idx] || [];
            const next = list.filter(a => (a.sub_id || null) !== (subId || null));
            if (next.length) annotationsMap[idx] = next; else delete annotationsMap[idx];
            repaintHighlight(idx);
            updateStats();
            // v2: keep the sheet open and refresh; classic: close as before.
            if (V2 && window.ReaderSheetV2) {
                activeRepSubId = next.length ? (next[next.length - 1].sub_id || null) : null;
                window.ReaderSheetV2.setAnnotations(annotationsMap[idx] || []);
            } else {
                closeSheet();
            }
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
    }

    // Remove annotation (classic single-slot button targets the representative).
    annRemoveBtn.addEventListener('click', () => {
        doRemoveAnnotation(activeIdx, activeRepSubId);
    });

    // --- Top-bar tours: review notes and footnotes walk separately ---
    //
    // Footnotes feed the endnote pipeline; word choice / inconsistency / Other
    // are things to resolve. One shared tour walking both drowned the review
    // notes on footnote-heavy books, so each class gets its own counter+button.
    // `match` for the annotations tour is "not a footnote" rather than an
    // allowlist so an unknown or legacy type still shows up somewhere — the same
    // fallback ANN_RANK and app.py's type coercion make.
    const tours = [
        { btn: btnAnnNav, out: annNavCount, stops: [], pos: -1, match: a => a.type !== 'footnote' },
        { btn: btnFnNav,  out: fnNavCount,  stops: [], pos: -1, match: a => a.type === 'footnote' },
    ];

    // Keep the reader's place across a save or delete: re-derive pos from the
    // sentence last landed on rather than restarting the tour. Pure — mirrored
    // by tests/test_web_ui/test_reader_topbar_nav.py::rederive_tour_pos.
    function rederiveTourPos(prevStops, prevPos, newStops) {
        const landed = prevPos >= 0 ? prevStops[prevPos] : undefined;
        if (landed === undefined) return -1;
        const exact = newStops.indexOf(landed);
        if (exact !== -1) return exact;
        const after = newStops.findIndex(i => i > landed);
        // No later stop (or empty): park at the end so the next tap wraps to the
        // top. Empty → length-1 === -1, which jumpTour treats as "not started".
        return after === -1 ? newStops.length - 1 : after - 1;
    }

    function updateStats() {
        for (const tour of tours) {
            if (!tour.btn || !tour.out) continue;
            // The count is matching *records* (mirroring the chapter-list badge);
            // a stop is a sentence, so two footnotes on one sentence count 2 and
            // stop once.
            let count = 0;
            const stops = [];
            for (const idx of Object.keys(annotationsMap)) {
                const hits = (annotationsMap[idx] || []).filter(tour.match).length;
                if (!hits) continue;
                count += hits;
                stops.push(Number(idx));
            }
            stops.sort((a, b) => a - b);

            tour.pos = rederiveTourPos(tour.stops, tour.pos, stops);
            tour.stops = stops;

            tour.out.textContent = count ? String(count) : '';
            tour.btn.hidden = count === 0;
        }
    }

    function jumpTour(tour) {
        if (!tour.stops.length) return;
        tour.pos = (tour.pos + 1) % tour.stops.length;
        const el = content.querySelector(`[data-es-idx="${tour.stops[tour.pos]}"]`);
        if (!el) return;
        // .sentence carries scroll-margin-top, so the fixed top bar is cleared.
        el.scrollIntoView({ behavior: 'smooth', block: 'start' });
        flashLanded(el);
    }

    for (const tour of tours) {
        if (tour.btn) tour.btn.addEventListener('click', () => jumpTour(tour));
    }

    // --- Toast (used by the realign flow; minimal, no deps) ---
    let toastEl = null;
    let toastTimer = null;
    function showToast(msg) {
        if (!toastEl) {
            toastEl = document.createElement('div');
            toastEl.className = 'reader-toast';
            document.body.appendChild(toastEl);
        }
        toastEl.textContent = msg;
        toastEl.classList.add('visible');
        if (toastTimer) clearTimeout(toastTimer);
        toastTimer = setTimeout(() => toastEl.classList.remove('visible'), 2400);
    }

    // --- Realign current chapter from inside the reader ---
    // Server endpoint recombines chunks, realigns, and re-anchors annotations.
    // While in flight, lock the controls that race those writes
    // (alignment JSON or chunk files). Reading and annotating stay live.
    const btnAlign = document.getElementById('btn-align');
    const btnAlignTitle = btnAlign ? (btnAlign.getAttribute('title') || 'Realign chapter') : '';
    let aligning = false;

    function lockChunkMutators(on) {
        const ids = ['btn-save', 'sheet-remove-text',
                     'sheet-retranslate', 'sheet-edit-chunk'];
        for (const id of ids) {
            const el = document.getElementById(id);
            if (!el) continue;
            el.disabled = on;
            el.classList.toggle('disabled-during-align', on);
            if (on) el.setAttribute('title', i.realign_locked || 'Disabled while realigning…');
            else el.removeAttribute('title');
        }
    }

    function showRealignButton() {
        if (btnAlign) btnAlign.hidden = false;
    }
    function hideRealignButton() {
        if (btnAlign) btnAlign.hidden = true;
    }

    // First sentence whose top edge is below the topbar — where the user is
    // anchored at refresh time (not click time, so scrolling during the wait
    // is honored).
    function getTopVisibleSentencePrefix() {
        const headerH = 56;
        const spans = content.querySelectorAll('.sentence');
        for (const s of spans) {
            if (s.getBoundingClientRect().top >= headerH) {
                return (s.textContent || '').trim().slice(0, 30);
            }
        }
        return null;
    }

    if (btnAlign) {
        btnAlign.addEventListener('click', () => {
            if (aligning) return;
            aligning = true;
            btnAlign.classList.add('aligning');
            btnAlign.disabled = true;
            btnAlign.setAttribute('title', i.realign_working || 'Realigning…');
            lockChunkMutators(true);

            fetch(`/api/project/${projectId}/align/${chapter}`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: '{}',
            })
            .then(r => r.json())
            .then(data => {
                if (data && data.error) {
                    showToast((i.realign_failed || 'Realign failed: ') + data.error);
                    return;
                }
                const prefix = getTopVisibleSentencePrefix();
                return Promise.resolve(loadAndRender(prefix)).then(() => {
                    hideRealignButton();
                    const orphans = (data && data.orphaned_annotations) || 0;
                    if (orphans > 0) {
                        const tmpl = i.realign_orphans || 'Realigned. {n} annotation(s) orphaned.';
                        showToast(tmpl.replace('{n}', String(orphans)));
                    } else {
                        showToast(i.realign_done || 'Chapter realigned.');
                    }
                });
            })
            .catch(err => {
                showToast((i.realign_failed || 'Realign failed: ') + (err && err.message ? err.message : err));
            })
            .finally(() => {
                aligning = false;
                btnAlign.classList.remove('aligning');
                btnAlign.disabled = false;
                btnAlign.setAttribute('title', btnAlignTitle);
                lockChunkMutators(false);
            });
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
            chunk_offset_start: alignment.chunk_offset_start,
            chunk_offset_end: alignment.chunk_offset_end,
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

                    showRealignButton();
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
                showRealignButton();
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
    const removeConfirmEsLabel = document.getElementById('remove-confirm-preview-es-label');
    const removeConfirmEnLabel = document.getElementById('remove-confirm-preview-en-label');
    const removeConfirmEsText = document.getElementById('remove-confirm-preview-es');
    const removeConfirmEnText = document.getElementById('remove-confirm-preview-en');
    if (removeConfirmOverlay) {
        const cTitle = document.getElementById('remove-confirm-title');
        const cWarn = document.getElementById('remove-confirm-warning');
        if (cTitle) cTitle.textContent = i.remove_confirm_title || 'Are you sure?';
        if (cWarn) cWarn.textContent = i.remove_confirm_warning || 'This action cannot be undone.';
        if (removeConfirmYes) removeConfirmYes.textContent = i.remove_confirm_yes || 'Yes, remove';
        if (removeConfirmNo) removeConfirmNo.textContent = i.remove_confirm_no || 'Go back';
        if (removeConfirmEsLabel) removeConfirmEsLabel.textContent = i.remove_confirm_preview_es_label || 'Spanish:';
        if (removeConfirmEnLabel) removeConfirmEnLabel.textContent = i.remove_confirm_preview_en_label || 'English:';
    }

    function fillConfirmPreview(esText, enText) {
        const noneLabel = i.remove_confirm_preview_none || '(nothing selected)';
        if (removeConfirmEsText) {
            if (esText) {
                removeConfirmEsText.textContent = esText;
                removeConfirmEsText.classList.remove('is-empty');
            } else {
                removeConfirmEsText.textContent = noneLabel;
                removeConfirmEsText.classList.add('is-empty');
            }
        }
        if (removeConfirmEnText) {
            if (enText) {
                removeConfirmEnText.textContent = enText;
                removeConfirmEnText.classList.remove('is-empty');
            } else {
                removeConfirmEnText.textContent = noneLabel;
                removeConfirmEnText.classList.add('is-empty');
            }
        }
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
    // Tracks whether each pane's current selection is the still-untouched
    // default suggestion. When the user diverges in one pane, an unmodified
    // default in the other pane is cleared so it can't be silently deleted.
    let esIsDefault = false;
    let enIsDefault = false;

    function divergeFromDefault(lang) {
        if (lang === 'es') {
            esIsDefault = false;
            if (enIsDefault) {
                enSel = null;
                enIsDefault = false;
                if (removalCtx) renderPane(removeEnPane, removalCtx.en_full, null);
            }
        } else {
            enIsDefault = false;
            if (esIsDefault) {
                esSel = null;
                esIsDefault = false;
                if (removalCtx) renderPane(removeEsPane, removalCtx.es_full, null);
            }
        }
    }

    function escapeHtml(s) {
        return (s || '').replace(/[&<>"']/g, c => ({
            '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
        }[c]));
    }

    // Render suggestion text inline, marking \n with ↵ so paragraph breaks stay
    // visible without expanding the card (white-space: pre-wrap would grow it).
    function appendSuggestionText(container, text) {
        const normalized = (text || '').replace(/\r\n?/g, '\n');
        if (!normalized.includes('\n')) {
            container.appendChild(document.createTextNode(normalized));
            return;
        }
        const breakTitle = i.review_break_mark || 'Suggested line break';
        normalized.split('\n').forEach((part, i, parts) => {
            if (part) container.appendChild(document.createTextNode(part));
            if (i < parts.length - 1) {
                const mark = document.createElement('span');
                mark.className = 'review-break-mark';
                mark.textContent = '\u21B5';
                mark.title = breakTitle;
                mark.setAttribute('aria-label', breakTitle);
                container.appendChild(mark);
            }
        });
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
        esIsDefault = enIsDefault = false;
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
                esIsDefault = !!esSugg;
                enIsDefault = !!enSugg;
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
        esIsDefault = enIsDefault = false;
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
        divergeFromDefault(lang);
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
        divergeFromDefault('es');
        renderPane(removeEsPane, removalCtx.es_full, null);
        const s = window.getSelection(); if (s) s.removeAllRanges();
        updateActionButtons();
        updateRemoveButtons();
        showRemoveError('');
    });
    if (removeEnUnhi) removeEnUnhi.addEventListener('click', () => {
        if (!removalCtx) return;
        enSel = null;
        divergeFromDefault('en');
        renderPane(removeEnPane, removalCtx.en_full, null);
        const s = window.getSelection(); if (s) s.removeAllRanges();
        updateActionButtons();
        updateRemoveButtons();
        showRemoveError('');
    });

    removeEsReset.addEventListener('click', () => {
        if (!removalCtx) return;
        esSel = esSugg ? { ...esSugg } : null;
        esIsDefault = !!esSugg;
        renderPane(removeEsPane, removalCtx.es_full, esSel);
        scrollHighlightIntoView(removeEsPane);
        updateRemoveButtons();
        showRemoveError('');
    });
    removeEnReset.addEventListener('click', () => {
        if (!removalCtx) return;
        enSel = enSugg ? { ...enSugg } : null;
        enIsDefault = !!enSugg;
        renderPane(removeEnPane, removalCtx.en_full, enSel);
        scrollHighlightIntoView(removeEnPane);
        updateRemoveButtons();
        showRemoveError('');
    });
    if (removeEsClear) removeEsClear.addEventListener('click', () => {
        if (!removalCtx) return;
        esSel = null;
        divergeFromDefault('es');
        renderPane(removeEsPane, removalCtx.es_full, null);
        updateRemoveButtons();
        showRemoveError('');
    });
    if (removeEnClear) removeEnClear.addEventListener('click', () => {
        if (!removalCtx) return;
        enSel = null;
        divergeFromDefault('en');
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
        fillConfirmPreview(esCheck, enCheck);
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
                chunk_offset_start: currentText === retransCtx.originalCurrent ? retransCtx.row.chunk_offset_start : undefined,
                chunk_offset_end: currentText === retransCtx.originalCurrent ? retransCtx.row.chunk_offset_end : undefined,
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

    // ── ReaderCore: the seam the v2 skin drives ────────────────────────────────
    // Only the v2 layout needs this; it reuses this module's data + endpoints so
    // there is a single source of truth for persistence, modals, and re-render.
    if (V2) {
        window.ReaderCore = {
            // Persist an edit to the active sentence (reuses the correction flow,
            // which updates the sentence, shows the realign nudge, and closes).
            saveCorrection(text) {
                if (activeIdx === null) return;
                sheetTextarea.value = text;
                btnSave.click();
            },
            // Persist an annotation for the active sentence. A sub_id edits that
            // annotation; its absence creates a new one (several may coexist).
            saveAnnotation(type, content, subId) {
                doSaveAnnotation(type, (content || '').trim(), subId);
            },
            deleteAnnotation(subId) {
                doRemoveAnnotation(activeIdx, subId != null ? subId : activeRepSubId);
            },
            // Record reviewer feedback on a finding (drops it + siblings, repaints).
            submitFeedback(esIdx, finding, feedbackType) {
                submitFeedback(esIdx, finding, feedbackType, null);
            },
            // Chunk-level actions open the shared modals via the hidden classic
            // controls, so the whole retranslate / remove / boundary flow is reused.
            retranslate() { if (retransBtn) retransBtn.click(); },
            removeText() { if (removeBtn) removeBtn.click(); },
            editBoundaries() { if (sheetEditChunk) sheetEditChunk.click(); },
            close() { closeSheet(); },
        };
    }
})();
