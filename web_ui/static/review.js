/**
 * Review Mode Logic for Translation Web UI
 *
 * Handles side-by-side paragraph display, evaluation integration,
 * and word-level annotations.
 */

/**
 * Format chunk identifiers for user-friendly display
 * @param {string} chunkId - Full chunk ID (e.g., "chapter_01_chunk_000")
 * @param {string} chapterId - Chapter ID (e.g., "chapter_01")
 * @param {number} position - 0-based chunk position
 * @returns {object} Formatted display strings
 */
function formatChunkDisplay(chunkId, chapterId, position) {
    // Extract numeric part from chapter ID, removing leading zeros
    const chapterNum = parseInt(chapterId.replace(/^chapter_0*/, ''), 10);

    // Convert 0-based position to 1-based for display
    const chunkNum = position + 1;

    return {
        chapterNum: chapterNum,
        chunkNum: chunkNum,
        headerTitle: I18N.t('chunk.displayName', { chapterNum, chunkNum }),
        sidebarLabel: I18N.t('chunk.sidebarName', { chapterNum, chunkNum }),
        fullId: chunkId  // Keep original for reference
    };
}

const ReviewMode = {
    currentChunk: null,
    currentEvalResults: null,
    annotations: [],
    sessionId: null,
    workingSourceParagraphs: [],
    workingTranslationParagraphs: [],
    _lastKnownTranslationText: '',

    /**
     * Enter review mode for a chunk
     */
    async enter(sessionId, chunkId) {
        this.sessionId = sessionId;

        try {
            // Load chunk data
            const response = await fetch(`/api/get-chunk?session_id=${sessionId}&chunk_id=${chunkId}`);

            if (!response.ok) {
                const error = await response.json();
                alert(I18N.t('alert.loadChunkFailed', { error: error.error || 'Unknown error' }));
                return;
            }

            const data = await response.json();
            this.currentChunk = data;

            // Update title
            const displayInfo = formatChunkDisplay(
                data.chunk_id,
                data.chapter_id,
                data.position
            );
            document.getElementById('review-mode-title').textContent =
                I18N.t('review.title', { title: displayInfo.headerTitle });

            // Load existing annotations if present
            this.annotations = data.review_data?.annotations || [];

            // Check if chunk has translation
            const translatedText = data.translated_text || '';
            const sourceText = data.source_text || '';

            if (translatedText.trim() === '') {
                alert(I18N.t('alert.noTranslationYet'));
                document.getElementById('back-to-translate-btn').click();
                return;
            }

            // Render side-by-side view
            this.renderSideBySide(sourceText, translatedText);

            // Auto-run evaluation
            await this.runEvaluation();

        } catch (error) {
            console.error('Error entering review mode:', error);
            alert(I18N.t('alert.loadChunkFailed', { error: error.message }));
        }
    },

    /**
     * Render source and translation side-by-side with aligned paragraphs.
     * @param {string} sourceText
     * @param {string} translationText
     * @param {boolean} suppressMismatchAlert - Skip the paragraph-count alert (used during split/merge re-renders)
     */
    renderSideBySide(sourceText, translationText, suppressMismatchAlert = false) {
        // Split into paragraphs
        const sourceParagraphs = this.splitParagraphs(sourceText);
        const translationParagraphs = this.splitParagraphs(translationText);

        // Store working arrays for split/merge operations
        this.workingSourceParagraphs = [...sourceParagraphs];
        this.workingTranslationParagraphs = [...translationParagraphs];

        // Snapshot for annotation relocation
        this._lastKnownTranslationText = translationParagraphs.join('\n\n');

        // Warn if paragraph counts don't match
        if (!suppressMismatchAlert && sourceParagraphs.length !== translationParagraphs.length) {
            console.warn(`Paragraph count mismatch: ${sourceParagraphs.length} source vs ${translationParagraphs.length} translation`);
            alert(I18N.t('alert.paragraphMismatch', {
                source: sourceParagraphs.length,
                translation: translationParagraphs.length
            }));
        }

        // Render paired rows - each row contains one source paragraph and one translation paragraph
        const maxParagraphs = Math.max(sourceParagraphs.length, translationParagraphs.length);
        const pairedHtml = [];

        for (let idx = 0; idx < maxParagraphs; idx++) {
            const sourcePara = sourceParagraphs[idx] || '';
            const translationPara = translationParagraphs[idx] || '';

            // Source side (cursor-positionable for split, text editing locked)
            const sourceCell = `
                <div class="paragraph-row">
                    <div class="source-para">
                        <span class="para-number">${idx + 1}</span>
                        <div class="para-text source-text-locked" contenteditable="true" data-para-idx="${idx}">${this.escapeHtml(sourcePara)}</div>
                        <div class="para-actions source-para-actions">
                            <button class="para-btn source-split-btn" data-para-idx="${idx}" title="Split source paragraph here">↕</button>
                            ${idx > 0 ? `<button class="para-btn source-merge-btn" data-para-idx="${idx}" title="Merge with source paragraph above">↑</button>` : ''}
                        </div>
                    </div>
            `;

            // Translation side (editable with word wrapping)
            const words = translationPara.split(/\s+/).filter(w => w.length > 0);
            const wrappedWords = words.map((word, wordIdx) => {
                const globalWordIndex = this.getGlobalWordIndex(translationParagraphs, idx, wordIdx);
                const cleanWord = this.escapeHtml(word);

                // Check if this word has an annotation
                const annotation = this.annotations.find(ann => ann.word_index === globalWordIndex);
                const annotationClass = annotation
                    ? (annotation.annotation_type === 'footnote' ? 'annotated-footnote' : 'annotated')
                    : '';

                return `<span class="word ${annotationClass}" data-word-index="${globalWordIndex}" data-word="${cleanWord}">${cleanWord}</span>`;
            }).join(' ');

            const translationCell = `
                    <div class="translation-para">
                        <span class="para-number">${idx + 1}</span>
                        <div class="para-text" contenteditable="true" data-para-idx="${idx}">
                            ${wrappedWords || '<span class="empty-para">(empty)</span>'}
                        </div>
                        <div class="para-actions">
                            <button class="para-btn split-btn" data-para-idx="${idx}" title="Split paragraph here">↕</button>
                            ${idx > 0 ? `<button class="para-btn merge-btn" data-para-idx="${idx}" title="Merge with paragraph above">↑</button>` : ''}
                        </div>
                    </div>
                </div>
            `;

            pairedHtml.push(sourceCell + translationCell);
        }

        // Replace the two-column structure with paired rows
        const container = document.querySelector('.side-by-side-container');
        container.innerHTML = `
            <div class="paired-paragraphs">
                ${pairedHtml.join('')}
            </div>
        `;

        // Lock source paragraphs against editing (cursor placement still allowed for split)
        document.querySelectorAll('.source-text-locked').forEach(el => {
            el.addEventListener('keydown', e => e.preventDefault());
            el.addEventListener('paste', e => e.preventDefault());
        });

        // Attach word-click handlers for annotations
        this.attachAnnotationHandlers();

        // Attach split/merge button handlers
        this.attachParagraphEditHandlers();

        // Highlight annotated words with tooltips
        this.highlightAnnotatedWords();
    },

    /**
     * Split text into paragraphs (same logic as ParagraphEvaluator)
     */
    splitParagraphs(text) {
        return text.split(/\n\s*\n/).map(p => p.trim()).filter(p => p.length > 0);
    },

    /**
     * Calculate global word index across all paragraphs
     */
    getGlobalWordIndex(paragraphs, paraIdx, wordIdxInPara) {
        let count = 0;
        for (let i = 0; i < paraIdx; i++) {
            count += paragraphs[i].split(/\s+/).filter(w => w.length > 0).length;
        }
        return count + wordIdxInPara;
    },

    /**
     * Get current translation text from editable paragraphs
     */
    getCurrentTranslationText() {
        const paraElements = document.querySelectorAll('.translation-para .para-text[contenteditable]');
        const paragraphs = Array.from(paraElements).map(el => {
            const text = el.textContent.trim();
            // Skip empty paragraphs marked as (empty)
            return text === '(empty)' ? '' : text;
        }).filter(p => p.length > 0);
        return paragraphs.join('\n\n');
    },

    /**
     * Run evaluation on current translation
     */
    async runEvaluation() {
        const btn = document.getElementById('run-eval-btn');
        const originalText = btn.textContent;
        btn.disabled = true;
        btn.textContent = I18N.t('review.evaluatingButton');

        try {
            // Get current translation text (may have been edited)
            const currentTranslation = this.getCurrentTranslationText();

            const response = await fetch('/api/evaluate-chunk', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({
                    session_id: this.sessionId,
                    chunk_id: this.currentChunk.chunk_id,
                    translation_override: currentTranslation
                })
            });

            if (!response.ok) {
                const error = await response.json();
                throw new Error(error.error || 'Evaluation failed');
            }

            const evalData = await response.json();
            this.currentEvalResults = evalData;

            // Display evaluation summary
            this.renderEvaluationSummary(evalData);

        } catch (error) {
            console.error('Evaluation error:', error);
            alert(I18N.t('alert.evalFailed', { message: error.message }));
        } finally {
            btn.disabled = false;
            btn.textContent = originalText;
        }
    },

    /**
     * Render evaluation results
     */
    renderEvaluationSummary(evalData) {
        const summary = evalData.summary;

        // Overall status
        const statusClass = summary.overall_passed ? 'passed' : 'failed';
        const statusIcon = summary.overall_passed ? '✓' : '✗';
        const statusText = summary.overall_passed ? I18N.t('eval.passed') : I18N.t('eval.failed');

        const resultsHtml = `
            <div class="eval-overview ${statusClass}">
                <strong>${statusIcon} ${statusText}</strong>
                - ${I18N.plural(summary.total_issues, 'eval.issue', 'eval.issuePlural')} found (
                    ${I18N.plural(summary.issues_by_severity.error || 0, 'eval.error', 'eval.errorPlural')},
                    ${I18N.plural(summary.issues_by_severity.warning || 0, 'eval.warning', 'eval.warningPlural')}
                )
            </div>
            <div class="eval-evaluators">
                ${evalData.results.map(r => `
                    <div class="evaluator ${r.passed ? 'passed' : 'failed'}">
                        ${r.passed ? '✓' : '✗'} ${r.eval_name}:
                        ${r.score !== null ? I18N.t('eval.score', { score: r.score.toFixed(2) }) : 'N/A'}
                        (${I18N.plural(r.issues.length, 'eval.issue', 'eval.issuePlural')})
                    </div>
                `).join('')}
            </div>
            <div class="eval-issues">
                <h4>Issues:</h4>
                ${this.renderIssues(evalData.results)}
            </div>
        `;

        document.getElementById('eval-stats').innerHTML = resultsHtml;
        document.getElementById('eval-summary').style.display = 'block';
    },

    /**
     * Render issues from evaluation results
     */
    renderIssues(results) {
        const allIssues = results.flatMap(r =>
            r.issues.map(issue => ({...issue, evaluator: r.eval_name}))
        );

        if (allIssues.length === 0) {
            return `<p class="no-issues">${I18N.t('eval.noIssues')}</p>`;
        }

        return allIssues.map(issue => `
            <div class="issue ${issue.severity}">
                <strong>${issue.severity.toUpperCase()}</strong> (${issue.evaluator}): ${issue.message}
                ${issue.suggestion ? `<br><em>Suggestion: ${issue.suggestion}</em>` : ''}
            </div>
        `).join('');
    },

    /**
     * Attach click handlers for word annotation
     */
    attachAnnotationHandlers() {
        const container = document.querySelector('.paired-paragraphs');
        if (!container) return;

        container.addEventListener('click', (e) => {
            if (e.target.classList.contains('word')) {
                const wordIndex = parseInt(e.target.dataset.wordIndex);
                const wordText = e.target.textContent.trim();
                this.showAnnotationPanel(wordIndex, wordText);
            }
        });
    },

    /**
     * Show annotation panel for a word
     */
    showAnnotationPanel(wordIndex, wordText) {
        document.getElementById('annotate-word-index').value = wordIndex;
        document.getElementById('annotate-word-text').textContent = wordText;

        // Check if annotation already exists for this word
        const existing = this.annotations.find(ann => ann.word_index === wordIndex);
        if (existing) {
            // Pre-fill form with existing annotation
            document.getElementById('annotation-type').value = existing.annotation_type;
            document.getElementById('annotation-content').value = existing.content || '';
            document.getElementById('annotation-tags').value = existing.tags.join(', ');

            // Show delete button for existing annotations
            const deleteBtn = document.getElementById('delete-annotation-btn');
            if (deleteBtn) {
                deleteBtn.style.display = 'inline-block';
            }
        } else {
            // Clear form for new annotation
            document.getElementById('annotation-type').value = 'usage_doubt';
            document.getElementById('annotation-content').value = '';
            document.getElementById('annotation-tags').value = '';

            // Hide delete button for new annotations
            const deleteBtn = document.getElementById('delete-annotation-btn');
            if (deleteBtn) {
                deleteBtn.style.display = 'none';
            }
        }

        document.getElementById('annotation-panel').style.display = 'block';
        document.getElementById('annotation-content').focus();
    },

    /**
     * Save annotation
     */
    async saveAnnotation(wordIndex, wordText, type, content, tags) {
        const context = this._captureWordContext(wordIndex);
        const annotation = {
            id: `ann_${Date.now()}`,
            word_index: wordIndex,
            word_text: wordText,
            annotation_type: type,
            content: content || null,
            tags: tags.split(',').map(t => t.trim()).filter(t => t),
            context_before: context.context_before,
            context_after: context.context_after,
            created_at: new Date().toISOString()
        };

        // Update or add annotation
        const existingIdx = this.annotations.findIndex(ann => ann.word_index === wordIndex);
        if (existingIdx >= 0) {
            // Update existing
            annotation.id = this.annotations[existingIdx].id;
            annotation.updated_at = new Date().toISOString();
            this.annotations[existingIdx] = annotation;
        } else {
            // Add new
            this.annotations.push(annotation);
        }

        // Save to backend
        try {
            const response = await fetch('/api/save-annotations', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({
                    session_id: this.sessionId,
                    chunk_id: this.currentChunk.chunk_id,
                    annotations: this._serializableAnnotations()
                })
            });

            if (!response.ok) {
                const error = await response.json();
                throw new Error(error.error || 'Failed to save annotation');
            }

            // Update chunk status in sidebar
            if (window.updateChunkAnnotationCount) {
                window.updateChunkAnnotationCount(this.currentChunk.chunk_id, this.annotations.filter(a => a.annotation_type !== 'footnote').length);
            }

            // Hide panel
            document.getElementById('annotation-panel').style.display = 'none';

            // Re-highlight annotated words
            this.highlightAnnotatedWords();

        } catch (error) {
            console.error('Error saving annotation:', error);
            alert(I18N.t('alert.annotationSaveFailed', { message: error.message }));
        }
    },

    /**
     * Delete an annotation
     */
    async deleteAnnotation(wordIndex) {
        if (!confirm(I18N.t('annotation.deleteConfirm'))) {
            return;
        }

        // Remove from local array
        this.annotations = this.annotations.filter(ann => ann.word_index !== wordIndex);

        // Save to backend
        try {
            const response = await fetch('/api/save-annotations', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({
                    session_id: this.sessionId,
                    chunk_id: this.currentChunk.chunk_id,
                    annotations: this._serializableAnnotations()
                })
            });

            if (!response.ok) {
                throw new Error(I18N.t('alert.annotationDeleteFailed'));
            }

            // Update sidebar
            if (window.updateChunkAnnotationCount) {
                window.updateChunkAnnotationCount(this.currentChunk.chunk_id, this.annotations.filter(a => a.annotation_type !== 'footnote').length);
            }

            // Hide panel and refresh highlights
            document.getElementById('annotation-panel').style.display = 'none';
            this.highlightAnnotatedWords();

        } catch (error) {
            console.error('Error deleting annotation:', error);
            alert(error.message);
        }
    },

    /**
     * Highlight words that have annotations
     */
    highlightAnnotatedWords() {
        // Remove all existing highlights
        document.querySelectorAll('.word.annotated, .word.annotated-footnote').forEach(el => {
            el.classList.remove('annotated', 'annotated-footnote');
            el.removeAttribute('title');
        });

        // Add highlights for annotated words
        this.annotations.forEach(ann => {
            const wordEl = document.querySelector(`[data-word-index="${ann.word_index}"]`);
            if (wordEl) {
                const cssClass = ann.annotation_type === 'footnote' ? 'annotated-footnote' : 'annotated';
                wordEl.classList.add(cssClass);
                const typeLabel = ann.annotation_type.replace('_', ' ').toUpperCase();
                wordEl.title = ann.content ? `${typeLabel}: ${ann.content}` : typeLabel;
            }
        });
    },

    /**
     * Return annotations array cleaned of internal flags (e.g., _orphaned) for backend serialization.
     */
    _serializableAnnotations() {
        return this.annotations.map(({ _orphaned, ...rest }) => rest);
    },

    /**
     * Capture surrounding word context for an annotation at the given word index.
     * Returns {context_before: string[], context_after: string[]} with up to 2 words each.
     */
    _captureWordContext(wordIndex) {
        const context_before = [];
        const context_after = [];
        for (let offset = 2; offset >= 1; offset--) {
            const el = document.querySelector(`[data-word-index="${wordIndex - offset}"]`);
            if (el) context_before.push(el.textContent.trim());
        }
        for (let offset = 1; offset <= 2; offset++) {
            const el = document.querySelector(`[data-word-index="${wordIndex + offset}"]`);
            if (el) context_after.push(el.textContent.trim());
        }
        return { context_before, context_after };
    },

    /**
     * Strip trailing punctuation from a word for fuzzy matching.
     */
    _normalizeWord(word) {
        return word.replace(/[.,;:!?"""''()[\]{}—–-]+$/g, '').toLowerCase();
    },

    /**
     * Relocate annotations after text has been edited.
     * Uses a two-pass approach: exact word match first, then context-only match.
     * Returns {relocated: number, orphaned: number}.
     */
    relocateAnnotations(oldText, newText) {
        if (!this.annotations.length) return { relocated: 0, orphaned: 0 };

        const newWords = newText.split(/\s+/).filter(w => w.length > 0);
        let relocated = 0;
        let orphaned = 0;

        // Track which new positions are already claimed
        const claimed = new Set();

        // Pass 1: Exact word match
        const unresolvedAnnotations = [];
        for (const ann of this.annotations) {
            // Find all positions where word_text matches
            const candidates = [];
            const normAnn = this._normalizeWord(ann.word_text);
            for (let i = 0; i < newWords.length; i++) {
                if (claimed.has(i)) continue;
                if (newWords[i] === ann.word_text || this._normalizeWord(newWords[i]) === normAnn) {
                    candidates.push(i);
                }
            }

            if (candidates.length === 1) {
                ann.word_index = candidates[0];
                claimed.add(candidates[0]);
                relocated++;
            } else if (candidates.length > 1) {
                // Score by context alignment
                const best = this._pickBestCandidate(candidates, ann, newWords);
                ann.word_index = best;
                claimed.add(best);
                relocated++;
            } else {
                unresolvedAnnotations.push(ann);
            }
        }

        // Pass 2: Context-only match (for edited/renamed words)
        for (const ann of unresolvedAnnotations) {
            if (!ann.context_before?.length && !ann.context_after?.length) {
                ann._orphaned = true;
                orphaned++;
                continue;
            }

            let bestIdx = -1;
            let bestScore = 0;
            let bestDist = Infinity;

            for (let i = 0; i < newWords.length; i++) {
                if (claimed.has(i)) continue;
                const score = this._contextScore(i, ann, newWords);
                const dist = Math.abs(i - ann.word_index);
                if (score > bestScore || (score === bestScore && dist < bestDist)) {
                    bestScore = score;
                    bestIdx = i;
                    bestDist = dist;
                }
            }

            if (bestScore >= 2 && bestIdx >= 0) {
                ann.word_index = bestIdx;
                ann.word_text = newWords[bestIdx];
                ann.updated_at = new Date().toISOString();
                claimed.add(bestIdx);
                relocated++;
            } else {
                ann._orphaned = true;
                orphaned++;
            }
        }

        return { relocated, orphaned };
    },

    /**
     * Score a candidate position by how well surrounding words match annotation context.
     */
    _contextScore(candidateIdx, ann, words) {
        let score = 0;
        const before = ann.context_before || [];
        const after = ann.context_after || [];

        // Check context_before: before[0] is 2 words back, before[1] is 1 word back
        for (let i = 0; i < before.length; i++) {
            const offset = before.length - i; // 2, 1
            const wordIdx = candidateIdx - offset;
            if (wordIdx >= 0 && wordIdx < words.length) {
                if (this._normalizeWord(words[wordIdx]) === this._normalizeWord(before[i])) {
                    score++;
                }
            }
        }

        // Check context_after: after[0] is 1 word forward, after[1] is 2 words forward
        for (let i = 0; i < after.length; i++) {
            const offset = i + 1; // 1, 2
            const wordIdx = candidateIdx + offset;
            if (wordIdx >= 0 && wordIdx < words.length) {
                if (this._normalizeWord(words[wordIdx]) === this._normalizeWord(after[i])) {
                    score++;
                }
            }
        }

        return score;
    },

    /**
     * Pick the best candidate position from multiple exact matches using context scoring.
     */
    _pickBestCandidate(candidates, ann, words) {
        let bestIdx = candidates[0];
        let bestScore = -1;
        let bestDist = Infinity;

        for (const idx of candidates) {
            const score = this._contextScore(idx, ann, words);
            const dist = Math.abs(idx - ann.word_index);
            if (score > bestScore || (score === bestScore && dist < bestDist)) {
                bestScore = score;
                bestIdx = idx;
                bestDist = dist;
            }
        }

        return bestIdx;
    },

    /**
     * Run relocation if the translation text has changed since last snapshot.
     * Returns true if relocation was performed.
     */
    _relocateIfChanged() {
        const currentText = this.getCurrentTranslationText();
        if (currentText === this._lastKnownTranslationText) return false;
        if (!this.annotations.length) {
            this._lastKnownTranslationText = currentText;
            return false;
        }

        const result = this.relocateAnnotations(this._lastKnownTranslationText, currentText);
        this._lastKnownTranslationText = currentText;

        if (result.orphaned > 0) {
            console.warn(`Annotation relocation: ${result.relocated} relocated, ${result.orphaned} orphaned`);
        }

        return true;
    },

    /**
     * Sync working paragraph arrays from current DOM state.
     * Call before split/merge to capture any manual text edits.
     */
    _syncWorkingArraysFromDOM() {
        const domTransParas = document.querySelectorAll('.translation-para .para-text[contenteditable]');
        if (domTransParas.length > 0) {
            this.workingTranslationParagraphs = Array.from(domTransParas).map(el => {
                const text = el.textContent.trim();
                return text === '(empty)' ? '' : text;
            }).filter(p => p.length > 0);
        }
        const domSourceParas = document.querySelectorAll('.source-para .source-text-locked');
        if (domSourceParas.length > 0) {
            this.workingSourceParagraphs = Array.from(domSourceParas)
                .map(el => el.textContent.trim())
                .filter(p => p.length > 0);
        }
    },

    /**
     * Attach event delegation handler for split/merge paragraph buttons.
     * Called after each renderSideBySide.
     */
    attachParagraphEditHandlers() {
        const container = document.querySelector('.paired-paragraphs');
        if (!container) return;

        container.addEventListener('click', (e) => {
            const splitBtn = e.target.closest('.split-btn');
            const mergeBtn = e.target.closest('.merge-btn');
            const sourceSplitBtn = e.target.closest('.source-split-btn');
            const sourceMergeBtn = e.target.closest('.source-merge-btn');

            if (splitBtn) this.splitParagraph(parseInt(splitBtn.dataset.paraIdx));
            if (mergeBtn) this.mergeParagraph(parseInt(mergeBtn.dataset.paraIdx));
            if (sourceSplitBtn) this.splitSourceParagraph(parseInt(sourceSplitBtn.dataset.paraIdx));
            if (sourceMergeBtn) this.mergeSourceParagraph(parseInt(sourceMergeBtn.dataset.paraIdx));
        });
    },

    /**
     * Split a translation paragraph at the cursor position.
     * The cursor must be placed inside the target paragraph before clicking Split.
     */
    splitParagraph(idx) {
        const paraEl = document.querySelector(`.translation-para .para-text[data-para-idx="${idx}"]`);
        if (!paraEl) return;

        const selection = window.getSelection();
        if (!selection.rangeCount) {
            alert('Click inside the paragraph where you want to split, then click ↕.');
            return;
        }
        const range = selection.getRangeAt(0);
        if (!paraEl.contains(range.startContainer)) {
            alert('Click inside the paragraph where you want to split, then click ↕.');
            return;
        }

        // Find split point by comparing cursor position against each word span
        const wordSpans = Array.from(paraEl.querySelectorAll('.word'));
        if (wordSpans.length < 2) {
            alert('Paragraph is too short to split (needs at least 2 words).');
            return;
        }

        let splitAt = wordSpans.length; // default: cursor past all words (invalid)
        for (let i = 0; i < wordSpans.length; i++) {
            const wordRange = document.createRange();
            wordRange.selectNodeContents(wordSpans[i]);
            // If cursor start is at or before the start of this word, split before word i
            if (range.compareBoundaryPoints(Range.START_TO_START, wordRange) <= 0) {
                splitAt = i;
                break;
            }
        }

        if (splitAt === 0) {
            alert('Cursor is at the beginning of the paragraph. Place cursor after the first word to split.');
            return;
        }
        if (splitAt >= wordSpans.length) {
            alert('Cursor is at the end of the paragraph. Place cursor before the last word to split.');
            return;
        }

        // Sync any manual text edits from DOM before mutating
        this._syncWorkingArraysFromDOM();

        // Relocate annotations if text was edited inline before split
        this._relocateIfChanged();

        const paraText = this.workingTranslationParagraphs[idx] || '';
        const paraWords = paraText.split(/\s+/).filter(w => w.length > 0);
        const before = paraWords.slice(0, splitAt).join(' ');
        const after = paraWords.slice(splitAt).join(' ');

        this.workingTranslationParagraphs.splice(idx, 1, before, after);
        this.renderSideBySide(
            this.workingSourceParagraphs.join('\n\n'),
            this.workingTranslationParagraphs.join('\n\n'),
            true
        );
    },

    /**
     * Merge a translation paragraph with the one above it.
     */
    mergeParagraph(idx) {
        if (idx === 0) return;

        this._syncWorkingArraysFromDOM();

        // Relocate annotations if text was edited inline before merge
        this._relocateIfChanged();

        const textPrev = this.workingTranslationParagraphs[idx - 1] || '';
        const textCurr = this.workingTranslationParagraphs[idx] || '';
        const merged = (textPrev + ' ' + textCurr).trim();

        this.workingTranslationParagraphs.splice(idx - 1, 2, merged);
        this.renderSideBySide(
            this.workingSourceParagraphs.join('\n\n'),
            this.workingTranslationParagraphs.join('\n\n'),
            true
        );
    },

    /**
     * Split a source paragraph at the cursor position.
     * Source cells are contenteditable for cursor placement but locked against text edits.
     */
    splitSourceParagraph(idx) {
        const paraEl = document.querySelector(`.source-para .source-text-locked[data-para-idx="${idx}"]`);
        if (!paraEl) return;

        const selection = window.getSelection();
        if (!selection.rangeCount) {
            alert('Click inside the source paragraph where you want to split, then click ↕.');
            return;
        }
        const range = selection.getRangeAt(0);
        if (!paraEl.contains(range.startContainer)) {
            alert('Click inside the source paragraph where you want to split, then click ↕.');
            return;
        }

        // Calculate character offset of cursor within the element using a TreeWalker
        let charOffset = 0;
        let found = false;
        const treeWalker = document.createTreeWalker(paraEl, NodeFilter.SHOW_TEXT);
        let textNode;
        while ((textNode = treeWalker.nextNode())) {
            if (textNode === range.startContainer) {
                charOffset += range.startOffset;
                found = true;
                break;
            }
            charOffset += textNode.length;
        }

        if (!found) {
            alert('Could not determine cursor position. Click inside the source text and try again.');
            return;
        }

        // Map character offset to word split index
        const fullText = paraEl.textContent;
        const words = fullText.split(/\s+/).filter(w => w.length > 0);

        if (words.length < 2) {
            alert('Paragraph is too short to split (needs at least 2 words).');
            return;
        }

        let splitAt = words.length;
        let pos = 0;
        for (let i = 0; i < words.length; i++) {
            const wordStart = fullText.indexOf(words[i], pos);
            if (charOffset <= wordStart) {
                splitAt = i;
                break;
            }
            pos = wordStart + words[i].length;
        }

        if (splitAt === 0) {
            alert('Cursor is at the beginning of the paragraph. Place cursor after the first word to split.');
            return;
        }
        if (splitAt >= words.length) {
            alert('Cursor is at the end of the paragraph. Place cursor before the last word to split.');
            return;
        }

        this._syncWorkingArraysFromDOM();

        const before = words.slice(0, splitAt).join(' ');
        const after = words.slice(splitAt).join(' ');

        this.workingSourceParagraphs.splice(idx, 1, before, after);
        this.renderSideBySide(
            this.workingSourceParagraphs.join('\n\n'),
            this.workingTranslationParagraphs.join('\n\n'),
            true
        );
    },

    /**
     * Merge a source paragraph with the one above it.
     */
    mergeSourceParagraph(idx) {
        if (idx === 0) return;

        this._syncWorkingArraysFromDOM();

        const textPrev = this.workingSourceParagraphs[idx - 1] || '';
        const textCurr = this.workingSourceParagraphs[idx] || '';
        const merged = (textPrev + ' ' + textCurr).trim();

        this.workingSourceParagraphs.splice(idx - 1, 2, merged);
        this.renderSideBySide(
            this.workingSourceParagraphs.join('\n\n'),
            this.workingTranslationParagraphs.join('\n\n'),
            true
        );
    },

    /**
     * Get current source text from source paragraph elements (for save).
     */
    getCurrentSourceText() {
        const paras = document.querySelectorAll('.source-para .source-text-locked');
        return Array.from(paras).map(el => el.textContent.trim()).filter(p => p.length > 0).join('\n\n');
    },

    /**
     * Escape HTML to prevent XSS
     */
    escapeHtml(text) {
        const div = document.createElement('div');
        div.textContent = text;
        return div.innerHTML;
    }
};

/**
 * Form submission handler for annotations
 */
document.getElementById('annotation-form').addEventListener('submit', (e) => {
    e.preventDefault();

    const wordIndex = parseInt(document.getElementById('annotate-word-index').value);
    const wordText = document.getElementById('annotate-word-text').textContent;
    const type = document.getElementById('annotation-type').value;
    const content = document.getElementById('annotation-content').value.trim();
    const tags = document.getElementById('annotation-tags').value;

    ReviewMode.saveAnnotation(wordIndex, wordText, type, content, tags);
});

/**
 * Cancel annotation
 */
document.getElementById('cancel-annotation').addEventListener('click', () => {
    document.getElementById('annotation-panel').style.display = 'none';
});

/**
 * Delete annotation
 */
document.getElementById('delete-annotation-btn').addEventListener('click', () => {
    const wordIndex = parseInt(document.getElementById('annotate-word-index').value);
    ReviewMode.deleteAnnotation(wordIndex);
});

/**
 * Run evaluation button
 */
document.getElementById('run-eval-btn').addEventListener('click', () => {
    ReviewMode.runEvaluation();
});

/**
 * Save review changes
 */
document.getElementById('save-review-btn').addEventListener('click', async () => {
    const btn = document.getElementById('save-review-btn');
    const originalText = btn.textContent;
    btn.disabled = true;
    btn.textContent = I18N.t('translation.savingButton');

    try {
        // Relocate annotations if text was edited inline
        const didRelocate = ReviewMode._relocateIfChanged();
        if (didRelocate) {
            // Filter out orphaned annotations and warn user
            const orphaned = ReviewMode.annotations.filter(a => a._orphaned);
            if (orphaned.length > 0) {
                const proceed = confirm(
                    `${orphaned.length} annotation(s) could not be matched to words after your edits and will be removed. Continue saving?`
                );
                if (!proceed) {
                    btn.disabled = false;
                    btn.textContent = originalText;
                    return;
                }
                ReviewMode.annotations = ReviewMode.annotations.filter(a => !a._orphaned);
            }
            // Save relocated annotations to backend
            await fetch('/api/save-annotations', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({
                    session_id: ReviewMode.sessionId,
                    chunk_id: ReviewMode.currentChunk.chunk_id,
                    annotations: ReviewMode._serializableAnnotations()
                })
            });
            if (window.updateChunkAnnotationCount) {
                window.updateChunkAnnotationCount(ReviewMode.currentChunk.chunk_id, ReviewMode.annotations.filter(a => a.annotation_type !== 'footnote').length);
            }
        }

        // Get current translation text
        const currentTranslation = ReviewMode.getCurrentTranslationText();

        // Save translation via existing API
        const response = await fetch('/api/save-translation', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({
                session_id: ReviewMode.sessionId,
                chunk_id: ReviewMode.currentChunk.chunk_id,
                translation: currentTranslation,
                source_text: ReviewMode.getCurrentSourceText()
            })
        });

        if (!response.ok) {
            const error = await response.json();
            throw new Error(error.error || 'Failed to save');
        }

        // Re-render to refresh word spans after annotation relocation
        if (didRelocate) {
            ReviewMode.renderSideBySide(
                ReviewMode.getCurrentSourceText(),
                currentTranslation,
                true
            );
        }

        alert(I18N.t('alert.reviewSaved'));

    } catch (error) {
        console.error('Error saving:', error);
        alert(I18N.t('alert.reviewSaveFailed', { message: error.message }));
    } finally {
        btn.disabled = false;
        btn.textContent = originalText;
    }
});
