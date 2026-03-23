/**
 * Frontend logic for Translation Web UI
 */

let sessionId = null;
let currentChunkId = null;
let chunksList = [];
let totalChunks = 0;
let completedChunks = 0;
let currentMode = 'translation'; // 'translation' or 'review'

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

// Setup form handler
document.getElementById('setup-form').addEventListener('submit', async (e) => {
    e.preventDefault();

    const submitBtn = e.target.querySelector('button[type="submit"]');
    submitBtn.disabled = true;
    submitBtn.textContent = I18N.t('setup.loadingButton');

    try {
        // Set UI language from form
        const uiLanguage = document.getElementById('ui-language').value;
        I18N.setLanguage(uiLanguage);

        const data = {
            chunks_dir: document.getElementById('chunks-dir').value,
            project_name: document.getElementById('project-name').value,
            source_language: document.getElementById('source-language').value,
            target_language: document.getElementById('target-language').value,
            glossary_path: document.getElementById('glossary-path').value || null,
            style_guide_path: document.getElementById('style-guide-path').value || null,
            include_context: document.getElementById('include-context').checked,
            context_paragraphs: parseInt(document.getElementById('context-paragraphs').value) || 3
        };

        const response = await fetch('/api/load-project', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify(data)
        });

        const result = await response.json();

        if (!response.ok || result.error) {
            alert(result.error || I18N.t('alert.loadProjectFailed'));
            submitBtn.disabled = false;
            submitBtn.textContent = I18N.t('setup.loadButton');
            return;
        }

        sessionId = result.session_id;
        chunksList = result.chunks_list || [];
        totalChunks = result.total_chunks;
        completedChunks = result.completed_chunks;

        // Update project title
        document.getElementById('project-title').textContent = data.project_name;

        // Render chunks sidebar
        renderChunksSidebar();

        // Update progress display
        updateProgress();

        // Hide setup, show workspace
        document.getElementById('setup-panel').style.display = 'none';
        document.getElementById('workspace').style.display = 'grid';

        // Check if all complete
        if (result.all_complete) {
            showCompletion(result.total_chunks);
        } else {
            // Load first chunk
            loadChunk(result.next_chunk);
        }
    } catch (error) {
        alert(I18N.t('alert.loadProjectError', { message: error.message }));
        submitBtn.disabled = false;
        submitBtn.textContent = I18N.t('setup.loadButton');
    }
});

// Show/hide context options based on checkbox
document.getElementById('include-context').addEventListener('change', (e) => {
    document.getElementById('context-options').style.display = e.target.checked ? 'block' : 'none';
});

/**
 * Render chunks sidebar
 */
function renderChunksSidebar() {
    const chunksList_el = document.getElementById('chunks-list');

    const chunksHtml = chunksList.map(chunk => {
        // Use display_status from backend (pending, in_review, or translated)
        const status = chunk.display_status || (chunk.has_translation ? 'translated' : 'pending');

        // Determine status text and annotation count display
        let statusText;
        if (status === 'pending') {
            statusText = I18N.t('chunk.status.pending');
        } else if (status === 'in_review') {
            const count = chunk.annotation_count || 0;
            statusText = I18N.plural(count, 'chunk.status.notes', 'chunk.status.notesPlural');
        } else {
            statusText = I18N.t('chunk.status.done');
        }

        // Format chunk display name
        const displayInfo = formatChunkDisplay(chunk.chunk_id, chunk.chapter_id, chunk.position);

        return `
            <div class="chunk-item ${status}" data-chunk-id="${chunk.chunk_id}">
                <span class="chunk-number">${displayInfo.sidebarLabel}</span>
                <span class="chunk-status">${statusText} (${chunk.word_count}w)</span>
            </div>
        `;
    }).join('');

    chunksList_el.innerHTML = chunksHtml;

    // Add click handlers
    chunksList_el.querySelectorAll('.chunk-item').forEach(item => {
        item.addEventListener('click', async () => {
            const chunkId = item.dataset.chunkId;
            await loadChunkById(chunkId);
        });
    });
}

/**
 * Update progress display
 */
function updateProgress() {
    const total = chunksList.length;

    // Count chunks by status
    const completed = chunksList.filter(c => c.display_status === 'translated').length;
    const inReview = chunksList.filter(c => c.display_status === 'in_review').length;
    const pending = chunksList.filter(c => c.display_status === 'pending').length;

    // Update text
    document.getElementById('progress-text').textContent =
        I18N.t('workspace.progressText', { completed, inReview, pending });

    // Calculate percentages
    const completePercent = total > 0 ? (completed / total) * 100 : 0;
    const reviewPercent = total > 0 ? (inReview / total) * 100 : 0;
    const pendingPercent = total > 0 ? (pending / total) * 100 : 0;

    // Update segmented progress bar
    document.getElementById('progress-bar-complete').style.width = `${completePercent}%`;
    document.getElementById('progress-bar-review').style.width = `${reviewPercent}%`;
    document.getElementById('progress-bar-pending').style.width = `${pendingPercent}%`;
}

/**
 * Update annotation count for a chunk and re-render sidebar
 */
function updateChunkAnnotationCount(chunkId, annotationCount) {
    const chunk = chunksList.find(c => c.chunk_id === chunkId);
    if (chunk) {
        chunk.annotation_count = annotationCount;

        // Update display_status based on annotation count
        if (!chunk.has_translation) {
            chunk.display_status = 'pending';
        } else if (annotationCount > 0) {
            chunk.display_status = 'in_review';
        } else {
            chunk.display_status = 'translated';
        }

        // Re-render sidebar and progress
        renderChunksSidebar();
        updateProgress();
    }
}

// Expose globally for review.js to call
window.updateChunkAnnotationCount = updateChunkAnnotationCount;

/**
 * Load a chunk by ID (for navigation)
 * @param {String} chunkId - The chunk ID to load
 * @param {String} forceMode - Optional: 'translation' or 'review' to override smart routing
 */
async function loadChunkById(chunkId, forceMode = null) {
    try {
        const response = await fetch(`/api/load-chunk?session_id=${sessionId}&chunk_id=${chunkId}`);
        const chunkData = await response.json();

        if (!response.ok || chunkData.error) {
            alert(I18N.t('alert.loadChunkFailed', { error: chunkData.error || 'Unknown error' }));
            return;
        }

        loadChunk(chunkData, forceMode);
    } catch (error) {
        alert(I18N.t('alert.loadChunkFailed', { error: error.message }));
    }
}

/**
 * Load a chunk into the UI with smart mode selection
 * @param {Object} chunkData - The chunk data from the server
 * @param {String} forceMode - Optional: 'translation' or 'review' to override smart routing
 */
function loadChunk(chunkData, forceMode = null) {
    if (!chunkData) {
        showCompletion();
        return;
    }

    currentChunkId = chunkData.chunk_id;

    // Update chunk info (shared across both modes)
    const displayInfo = formatChunkDisplay(
        chunkData.chunk_id,
        chunkData.chapter_id,
        chunkData.position
    );
    document.getElementById('chunk-title').textContent = displayInfo.headerTitle;
    document.getElementById('chunk-id').textContent = chunkData.chunk_id;
    document.getElementById('word-count').textContent = chunkData.word_count;
    document.getElementById('paragraph-count').textContent = chunkData.paragraph_count;

    // Highlight active chunk in sidebar
    document.querySelectorAll('.chunk-item').forEach(item => {
        item.classList.remove('active');
        if (item.dataset.chunkId === currentChunkId) {
            item.classList.add('active');
        }
    });

    // Determine which mode to enter
    if (forceMode) {
        // Explicit mode requested
        if (forceMode === 'translation') {
            enterTranslationMode(chunkData);
        } else {
            enterReviewMode();
        }
    } else {
        // SMART MODE SELECTION: Check if chunk has translation
        const hasTranslation = chunkData.translated_text && chunkData.translated_text.trim() !== '';

        if (hasTranslation) {
            enterReviewMode();
        } else {
            enterTranslationMode(chunkData);
        }
    }
}

/**
 * Enter translation mode
 */
function enterTranslationMode(chunkData) {
    // Hide review mode
    document.getElementById('review-mode').style.display = 'none';

    // Show workspace grid layout
    document.getElementById('workspace').style.display = 'grid';

    // Update prompt display
    document.getElementById('prompt-display').textContent = chunkData.rendered_prompt;

    // Pre-fill translation if exists
    document.getElementById('translation-input').value = chunkData.translated_text || '';
    if (!chunkData.translated_text) {
        document.getElementById('translation-input').focus();
    }

    // Show translation sections, hide completion
    document.getElementById('chunk-info').style.display = 'block';
    document.getElementById('prompt-section').style.display = 'block';
    document.getElementById('translation-section').style.display = 'block';
    document.getElementById('completion').style.display = 'none';

    // Update mode toggle
    updateModeToggle('translation');
}

/**
 * Enter review mode
 */
function enterReviewMode() {
    // Keep workspace visible for sidebar and chunk-info, hide translation sections
    document.getElementById('workspace').style.display = 'grid';
    document.getElementById('prompt-section').style.display = 'none';
    document.getElementById('translation-section').style.display = 'none';
    document.getElementById('completion').style.display = 'none';

    // Keep chunk-info visible
    document.getElementById('chunk-info').style.display = 'block';

    // Show review mode
    document.getElementById('review-mode').style.display = 'block';

    // Load review mode
    if (typeof ReviewMode !== 'undefined') {
        ReviewMode.enter(sessionId, currentChunkId);
    }

    // Update mode toggle
    updateModeToggle('review');
}

/**
 * Update mode toggle button visibility and text
 */
function updateModeToggle(mode) {
    currentMode = mode;
    const toggleContainer = document.getElementById('mode-toggle-container');
    const toggleBtn = document.getElementById('mode-toggle-btn');
    const toggleIcon = document.getElementById('mode-toggle-icon');
    const toggleText = document.getElementById('mode-toggle-text');

    // Only show toggle for translated chunks
    const chunkData = chunksList.find(c => c.chunk_id === currentChunkId);
    if (!chunkData || !chunkData.has_translation) {
        toggleContainer.style.display = 'none';
        return;
    }

    toggleContainer.style.display = 'flex';

    if (mode === 'translation') {
        toggleBtn.classList.remove('in-review-mode');
        toggleIcon.textContent = '👁️';
        toggleText.textContent = I18N.t('mode.switchToReview');
    } else {
        toggleBtn.classList.add('in-review-mode');
        toggleIcon.textContent = '✏️';
        toggleText.textContent = I18N.t('mode.switchToTranslation');
    }
}

/**
 * Show completion message
 */
function showCompletion(totalChunks) {
    // Hide working sections
    document.getElementById('chunk-info').style.display = 'none';
    document.getElementById('prompt-section').style.display = 'none';
    document.getElementById('translation-section').style.display = 'none';

    // Show completion
    if (totalChunks) {
        document.getElementById('completion-message').textContent =
            I18N.t('completion.message', { total: totalChunks });
    }
    document.getElementById('completion').style.display = 'block';
}

/**
 * Copy prompt to clipboard
 */
document.getElementById('copy-btn').addEventListener('click', async () => {
    const promptText = document.getElementById('prompt-display').textContent;

    try {
        await navigator.clipboard.writeText(promptText);

        // Visual feedback
        const btn = document.getElementById('copy-btn');
        const originalText = btn.textContent;
        btn.textContent = I18N.t('translation.copiedButton');
        btn.classList.add('copied');

        setTimeout(() => {
            btn.textContent = originalText;
            btn.classList.remove('copied');
        }, 2000);
    } catch (err) {
        alert(I18N.t('alert.copyFailed', { message: err.message }));
    }
});

/**
 * Save translation and load next chunk
 */
document.getElementById('submit-btn').addEventListener('click', async () => {
    const translation = document.getElementById('translation-input').value.trim();

    if (!translation) {
        alert(I18N.t('alert.noTranslation'));
        return;
    }

    // Disable submit button
    const btn = document.getElementById('submit-btn');
    const originalText = btn.textContent;
    btn.disabled = true;
    btn.textContent = I18N.t('translation.savingButton');

    try {
        const response = await fetch('/api/save-translation', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({
                chunk_id: currentChunkId,
                translation: translation,
                session_id: sessionId
            })
        });

        const result = await response.json();

        if (!response.ok || result.error) {
            alert(result.error || I18N.t('alert.saveFailed'));
            btn.disabled = false;
            btn.textContent = originalText;
            return;
        }

        // Re-enable button
        btn.disabled = false;
        btn.textContent = originalText;

        // Update chunks list - mark current chunk as translated
        const chunkIndex = chunksList.findIndex(c => c.chunk_id === currentChunkId);
        if (chunkIndex >= 0 && !chunksList[chunkIndex].has_translation) {
            chunksList[chunkIndex].has_translation = true;
            completedChunks++;
        }

        // Re-render sidebar and update progress
        renderChunksSidebar();
        updateProgress();

        // Immediately load next chunk (auto-advance)
        if (result.all_complete) {
            showCompletion(result.total_chunks);
        } else {
            loadChunk(result.next_chunk);
        }
    } catch (error) {
        alert(I18N.t('alert.saveError', { message: error.message }));
        btn.disabled = false;
        btn.textContent = originalText;
    }
});

/**
 * Keyboard shortcuts
 */
document.addEventListener('keydown', (e) => {
    // Ctrl/Cmd + Enter to submit translation
    if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') {
        const textarea = document.getElementById('translation-input');
        if (document.activeElement === textarea && textarea.value.trim()) {
            document.getElementById('submit-btn').click();
        }
    }

    // Ctrl/Cmd + Shift + C to copy prompt
    if ((e.ctrlKey || e.metaKey) && e.shiftKey && e.key === 'C') {
        e.preventDefault();
        document.getElementById('copy-btn').click();
    }
});

/**
 * Handle mode toggle button click
 */
document.getElementById('mode-toggle-btn').addEventListener('click', () => {
    if (currentMode === 'translation') {
        enterReviewMode();
    } else {
        // Reload chunk and force translation mode
        loadChunkById(currentChunkId, 'translation');
    }
});

/**
 * Review mode toggle - saves translation first, then enters review mode
 */
document.getElementById('enter-review-btn').addEventListener('click', async () => {
    if (!currentChunkId) {
        alert(I18N.t('alert.noChunkLoaded'));
        return;
    }

    // Get translation from textarea
    const translation = document.getElementById('translation-input').value.trim();

    if (!translation) {
        alert(I18N.t('alert.noTranslation'));
        return;
    }

    // Disable button during save
    const btn = document.getElementById('enter-review-btn');
    const originalText = btn.textContent;
    btn.disabled = true;
    btn.textContent = I18N.t('translation.savingButton');

    try {
        // Save translation first
        const response = await fetch('/api/save-translation', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({
                chunk_id: currentChunkId,
                translation: translation,
                session_id: sessionId
            })
        });

        const result = await response.json();

        if (!response.ok || result.error) {
            alert(result.error || I18N.t('alert.saveFailed'));
            btn.disabled = false;
            btn.textContent = originalText;
            return;
        }

        // Update chunks list - mark current chunk as translated
        const chunkIndex = chunksList.findIndex(c => c.chunk_id === currentChunkId);
        if (chunkIndex >= 0 && !chunksList[chunkIndex].has_translation) {
            chunksList[chunkIndex].has_translation = true;
            completedChunks++;
        }

        // Re-render sidebar and update progress
        renderChunksSidebar();
        updateProgress();

        // Re-enable button
        btn.disabled = false;
        btn.textContent = originalText;

        // Now enter review mode with the saved translation
        enterReviewMode();

    } catch (error) {
        alert(I18N.t('alert.saveError', { message: error.message }));
        btn.disabled = false;
        btn.textContent = originalText;
    }
});
