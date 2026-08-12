/* Dashboard — unified project workflow */

(function() {
    'use strict';

    var PROJECT = window.DASHBOARD_PROJECT_ID;
    var PROJECT_TITLE = window.DASHBOARD_PROJECT_TITLE;
    var FIXED_QUESTIONS = window.DASHBOARD_FIXED_QUESTIONS;
    var projectStatus = null;
    var SPLIT_PATTERNS = null;

    // LLM config — loaded dynamically from /api/llm-config
    var LLM_CONFIG = null;
    var MODELS = {};  // provider_id -> [{id, name}]

    function loadLLMConfig() {
        return apiGet('/api/llm-config').then(function(config) {
            LLM_CONFIG = config;
            MODELS = {};
            config.providers.forEach(function(p) {
                MODELS[p.id] = p.models.map(function(m) {
                    return { id: m.id, name: m.name, supports_thinking: !!m.supports_thinking };
                });
            });
            return config;
        });
    }

    function populateProviderSelect(selectId) {
        var select = document.getElementById(selectId);
        if (!select || !LLM_CONFIG) return;
        select.innerHTML = '';
        LLM_CONFIG.providers.forEach(function(p) {
            var opt = document.createElement('option');
            opt.value = p.id;
            opt.textContent = p.name + (p.available ? '' : ' (no API key)');
            opt.disabled = !p.available;
            select.appendChild(opt);
        });
        select.value = LLM_CONFIG.default_provider;
    }

    function populateModelSelect(providerSelectId, modelSelectId) {
        var providerSelect = document.getElementById(providerSelectId);
        var modelSelect = document.getElementById(modelSelectId);
        if (!providerSelect || !modelSelect) return;
        var provider = providerSelect.value;
        modelSelect.innerHTML = '';
        (MODELS[provider] || []).forEach(function(m) {
            var opt = document.createElement('option');
            opt.value = m.id;
            opt.textContent = m.name;
            modelSelect.appendChild(opt);
        });
        // Pre-select the default model if it belongs to this provider
        if (LLM_CONFIG && provider === LLM_CONFIG.default_provider) {
            modelSelect.value = LLM_CONFIG.default_model;
        }
    }

    // True when a thinking checkbox exists, is visible, and is checked.
    function thinkingChecked(checkboxId) {
        var cb = document.getElementById(checkboxId);
        return !!(cb && cb.offsetParent !== null && cb.checked);
    }

    // Show a "thinking" checkbox only for models that expose a toggleable
    // thinking mode (Sonnet 5 / Opus 4.7-4.8). Hidden + reset otherwise.
    function updateThinkingVisibility(providerSelectId, modelSelectId, wrapId) {
        var wrap = document.getElementById(wrapId);
        if (!wrap) return;
        var provider = (document.getElementById(providerSelectId) || {}).value;
        var modelId = (document.getElementById(modelSelectId) || {}).value;
        var entry = (MODELS[provider] || []).find(function(m) { return m.id === modelId; });
        var supported = !!(entry && entry.supports_thinking);
        wrap.style.display = supported ? '' : 'none';
        if (!supported) {
            var cb = wrap.querySelector('input[type="checkbox"]');
            if (cb) cb.checked = false;
        }
    }

    function bindProviderModelPair(providerSelectId, modelSelectId, onChange) {
        var providerSelect = document.getElementById(providerSelectId);
        if (!providerSelect) return;
        providerSelect.addEventListener('change', function() {
            populateModelSelect(providerSelectId, modelSelectId);
            if (onChange) onChange();
        });
        if (onChange) {
            var modelSelect = document.getElementById(modelSelectId);
            if (modelSelect) modelSelect.addEventListener('change', onChange);
        }
    }

    function initAllLLMSelectors() {
        // Style guide
        populateProviderSelect('style-provider');
        populateModelSelect('style-provider', 'style-model');
        bindProviderModelPair('style-provider', 'style-model');

        // Glossary
        populateProviderSelect('glossary-provider');
        populateModelSelect('glossary-provider', 'glossary-model');
        bindProviderModelPair('glossary-provider', 'glossary-model');

        // Batch translate (sequential realtime)
        populateProviderSelect('batch-provider');
        populateModelSelect('batch-provider', 'batch-model');
        bindProviderModelPair('batch-provider', 'batch-model', function() {
            updateBatchCostEstimate();
            updateThinkingVisibility('batch-provider', 'batch-model', 'batch-thinking-wrap');
        });
        updateThinkingVisibility('batch-provider', 'batch-model', 'batch-thinking-wrap');

        // Batch API (async, 50% off)
        populateProviderSelect('batch-api-provider');
        populateModelSelect('batch-api-provider', 'batch-api-model');
        bindProviderModelPair('batch-api-provider', 'batch-api-model', function() {
            updateBatchApiCostEstimate();
            updateThinkingVisibility('batch-api-provider', 'batch-api-model', 'batch-api-thinking-wrap');
        });
        updateThinkingVisibility('batch-api-provider', 'batch-api-model', 'batch-api-thinking-wrap');
    }

    // ========================================================================
    // Helpers
    // ========================================================================

    function apiGet(url) {
        return fetch(url).then(function(r) { return r.json(); });
    }

    function apiPost(url, body) {
        return fetch(url, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: body !== undefined ? JSON.stringify(body) : undefined,
        }).then(function(r) { return r.json(); });
    }

    function apiPatch(url, body) {
        return fetch(url, {
            method: 'PATCH',
            headers: { 'Content-Type': 'application/json' },
            body: body !== undefined ? JSON.stringify(body) : undefined,
        }).then(function(r) {
            return r.json().then(function(data) {
                return { ok: r.ok, status: r.status, data: data };
            });
        });
    }

    function apiPostForm(url, formData) {
        return fetch(url, { method: 'POST', body: formData })
            .then(function(r) { return r.json(); });
    }

    function setStatus(id, msg, type) {
        var el = document.getElementById(id);
        if (el) {
            el.textContent = msg;
            el.className = 'status-msg' + (type ? ' ' + type : '');
        }
    }

    function copyToClipboard(text) {
        navigator.clipboard.writeText(text).catch(function() {
            var ta = document.createElement('textarea');
            ta.value = text;
            document.body.appendChild(ta);
            ta.select();
            document.execCommand('copy');
            document.body.removeChild(ta);
        });
    }

    // Wire up all static btn-copy buttons (data-target points to a textarea id)
    document.querySelectorAll('.btn-copy[data-target]').forEach(function(btn) {
        btn.addEventListener('click', function() {
            var target = document.getElementById(btn.dataset.target);
            if (!target) return;
            var text = target.value;
            var originalText = btn.textContent;
            function showFeedback(ok) {
                btn.textContent = ok ? 'Copied!' : 'Copy failed';
                setTimeout(function() { btn.textContent = originalText; }, 1500);
            }
            if (navigator.clipboard && navigator.clipboard.writeText) {
                navigator.clipboard.writeText(text).then(function() {
                    showFeedback(true);
                }).catch(function() {
                    // Fallback for when clipboard API is blocked
                    try {
                        target.select();
                        document.execCommand('copy');
                        showFeedback(true);
                    } catch (e) {
                        showFeedback(false);
                    }
                });
            } else {
                try {
                    target.select();
                    document.execCommand('copy');
                    showFeedback(true);
                } catch (e) {
                    showFeedback(false);
                }
            }
        });
    });

    function escapeHtml(str) {
        var div = document.createElement('div');
        div.textContent = str;
        return div.innerHTML.replace(/"/g, '&quot;');
    }

    // Inline ↵ markers for \n in suggestions (avoids pre-wrap growing the card).
    function formatSuggestionHtml(text) {
        if (!text) return '';
        var normalized = text.replace(/\r\n?/g, '\n');
        if (normalized.indexOf('\n') === -1) return escapeHtml(normalized);
        var breakTitle = 'Suggested line break';
        var parts = normalized.split('\n');
        var html = '';
        for (var i = 0; i < parts.length; i++) {
            if (parts[i]) html += escapeHtml(parts[i]);
            if (i < parts.length - 1) {
                html += '<span class="eval-break-mark" title="' + escapeHtml(breakTitle) +
                    '" aria-label="' + escapeHtml(breakTitle) + '">\u21B5</span>';
            }
        }
        return html;
    }

    function truncate(str, len) {
        if (!str) return '';
        return str.length > len ? str.substring(0, len) + '...' : str;
    }

    // ========================================================================
    // Stepper navigation
    // ========================================================================

    var stages = ['source', 'split', 'chunk', 'style-guide', 'glossary', 'translate', 'review', 'export'];
    var currentStage = null;

    function navigateTo(stage) {
        if (stages.indexOf(stage) === -1) stage = 'source';
        var changed = currentStage !== stage;
        currentStage = stage;
        // The Review rollup is the one stage expensive enough to be worth
        // fetching on demand rather than with every status poll.
        if (stage === 'review' && changed && typeof refreshReviewStatus === 'function') {
            refreshReviewStatus();
        }

        // Update hash without triggering hashchange
        history.replaceState(null, '', '#' + stage);

        // Update stepper
        document.querySelectorAll('.stepper li').forEach(function(li) {
            li.classList.toggle('active', li.dataset.stage === stage);
        });

        // Update panels
        document.querySelectorAll('.stage-panel').forEach(function(panel) {
            panel.classList.toggle('active', panel.id === 'stage-' + stage);
        });
    }

    // Stepper click handlers
    document.querySelectorAll('.stepper li').forEach(function(li) {
        li.addEventListener('click', function() {
            navigateTo(li.dataset.stage);
        });
    });

    // Handle hash on load
    window.addEventListener('hashchange', function() {
        var hash = location.hash.replace('#', '');
        if (hash && stages.indexOf(hash) !== -1) {
            navigateTo(hash);
        }
    });

    // ========================================================================
    // Status loading
    // ========================================================================

    function loadStatus() {
        return apiGet('/api/project/' + PROJECT + '/status').then(function(data) {
            projectStatus = data;
            updateStepperBadges(data);
            populateStages(data);
            return data;
        });
    }

    function refreshStatusBadges() {
        return apiGet('/api/project/' + PROJECT + '/status').then(function(data) {
            projectStatus = data;
            updateStepperBadges(data);
            // Update chapter row translated counts in-place (without rebuilding the table)
            if (data.chapters) {
                data.chapters.forEach(function(ch) {
                    var row = document.querySelector('tr[data-chapter-id="' + ch.id + '"]');
                    if (!row) return;
                    var total = ch.chunk_count || 0;
                    var translated = ch.translated_count || 0;
                    var cells = row.querySelectorAll('td');
                    if (cells.length >= 5) {
                        cells[3].textContent = translated + '/' + total;
                        var pill = cells[4].querySelector('.status-pill');
                        if (pill) {
                            var statusClass = total === 0 ? 'pending'
                                : translated === total ? 'done'
                                : translated > 0 ? 'partial' : 'pending';
                            var statusLabel = total === 0 ? 'no chunks'
                                : translated === total ? 'done'
                                : translated > 0 ? 'partial' : 'pending';
                            pill.className = 'status-pill ' + statusClass;
                            pill.textContent = statusLabel;
                        }
                    }
                });
            }
            return data;
        });
    }

    function updateStepperBadges(status) {
        var steps = document.querySelectorAll('.stepper li');

        steps.forEach(function(li) {
            li.classList.remove('done');
            var stage = li.dataset.stage;
            var badge = document.getElementById('badge-' + stage);

            switch (stage) {
                case 'source':
                    if (status.has_source) {
                        li.classList.add('done');
                        badge.textContent = status.source_words ? status.source_words.toLocaleString() + ' words' : '';
                    }
                    break;
                case 'split':
                    if (status.chapter_count > 0) {
                        li.classList.add('done');
                        badge.textContent = status.chapter_count + ' ch';
                    }
                    break;
                case 'chunk':
                    if (status.total_chunks > 0) {
                        li.classList.add('done');
                        badge.textContent = status.total_chunks + ' chunks';
                    }
                    break;
                case 'style-guide':
                    if (status.has_style_guide) {
                        li.classList.add('done');
                    }
                    break;
                case 'glossary':
                    if (status.glossary_count > 0) {
                        li.classList.add('done');
                        badge.textContent = status.glossary_count + ' terms';
                    }
                    break;
                case 'translate':
                    if (status.total_chunks > 0) {
                        badge.textContent = status.translated_chunks + '/' + status.total_chunks;
                        if (status.translated_chunks === status.total_chunks) {
                            li.classList.add('done');
                        }
                    }
                    break;
                case 'review':
                    // Text comes from /review-status (outstanding findings);
                    // only the done state is cheap enough to derive here.
                    if (status.alignment_count >= status.chapter_count && status.chapter_count > 0) {
                        li.classList.add('done');
                    }
                    break;
                case 'export':
                    // Badge updated by populateExportStage
                    break;
            }
        });
    }

    function populateStages(status) {
        populateSourceStage(status);
        populateSplitStage(status);
        populateChunkStage(status);
        populateStyleGuideStage(status);
        populateGlossaryStage(status);
        populateTranslateStage(status);
        // Review renders from /review-status, not from here — see
        // refreshReviewStatus().
        populateExportStage(status);
    }

    // ========================================================================
    // Stage 1: Source
    // ========================================================================

    function populateSourceStage(status) {
        if (status.has_source) {
            document.getElementById('source-loaded').style.display = '';
            document.getElementById('source-upload').style.display = 'none';
            document.getElementById('source-stats').innerHTML =
                '<span>Words: ' + (status.source_words || 0).toLocaleString() + '</span>' +
                '<span>Size: ' + formatBytes(status.source_size || 0) + '</span>';
            document.getElementById('source-preview').textContent = status.source_preview || '';
            // Show Gutenberg provenance if applicable
            var originEl = document.getElementById('source-origin');
            if (status.gutenberg_url) {
                originEl.style.display = '';
                originEl.innerHTML = 'Source: <a href="' + escapeHtml(status.gutenberg_url) + '" target="_blank">Project Gutenberg</a>';
            } else {
                originEl.style.display = 'none';
            }
            // Image-fetch status banner: warn if any [IMAGE:...] placeholder
            // doesn't have a matching file on disk.
            updateImageStatusBanner(status);
        } else {
            document.getElementById('source-loaded').style.display = 'none';
            document.getElementById('source-upload').style.display = '';
        }
    }

    function updateImageStatusBanner(status) {
        var banner = document.getElementById('image-status-banner');
        var msg = document.getElementById('image-status-msg');
        var btn = document.getElementById('btn-retry-images');
        var expected = status.images_expected || 0;
        var missing = (status.images_missing || []).length;
        if (expected > 0 && missing > 0) {
            banner.style.display = '';
            banner.classList.add('warn');
            var preview = (status.images_missing || []).slice(0, 5).join(', ');
            if (missing > 5) preview += ', \u2026';
            msg.innerHTML = '\u26A0 ' + missing + ' of ' + expected +
                ' image' + (expected === 1 ? '' : 's') +
                ' failed to download during ingest. Missing: <code>' +
                escapeHtml(preview) + '</code>';
            btn.disabled = !status.gutenberg_url;
            btn.title = status.gutenberg_url ? '' : 'No gutenberg_url in project.json \u2014 cannot retry';
        } else {
            banner.style.display = 'none';
            banner.classList.remove('warn');
        }
    }

    document.getElementById('btn-retry-images').addEventListener('click', function() {
        var btn = this;
        btn.disabled = true;
        setStatus('retry-images-status', 'Downloading\u2026', '');
        apiPost('/api/project/' + PROJECT + '/fetch-missing-images', {}).then(function(data) {
            btn.disabled = false;
            if (data.error) {
                setStatus('retry-images-status', data.error, 'error');
                return;
            }
            var failed = (data.failed || []).length;
            var summary = data.downloaded + ' downloaded';
            if (failed) summary += ', ' + failed + ' still failing';
            setStatus('retry-images-status', summary, failed ? 'error' : 'success');
            loadStatus();
        }).catch(function() {
            btn.disabled = false;
            setStatus('retry-images-status', 'Network error', 'error');
        });
    });

    function formatBytes(bytes) {
        if (bytes < 1024) return bytes + ' B';
        if (bytes < 1048576) return (bytes / 1024).toFixed(1) + ' KB';
        return (bytes / 1048576).toFixed(1) + ' MB';
    }

    // Replace source button
    document.getElementById('btn-replace-source').addEventListener('click', function() {
        document.getElementById('source-loaded').style.display = 'none';
        document.getElementById('source-upload').style.display = '';
    });

    // Upload zone
    var uploadZone = document.getElementById('upload-zone');
    var fileInput = document.getElementById('source-file-input');

    uploadZone.addEventListener('click', function() { fileInput.click(); });
    uploadZone.addEventListener('dragover', function(e) {
        e.preventDefault();
        uploadZone.classList.add('dragover');
    });
    uploadZone.addEventListener('dragleave', function() {
        uploadZone.classList.remove('dragover');
    });
    uploadZone.addEventListener('drop', function(e) {
        e.preventDefault();
        uploadZone.classList.remove('dragover');
        if (e.dataTransfer.files.length > 0) {
            handleSourceFile(e.dataTransfer.files[0]);
        }
    });
    fileInput.addEventListener('change', function() {
        if (fileInput.files.length > 0) {
            handleSourceFile(fileInput.files[0]);
        }
    });

    function handleSourceFile(file) {
        var reader = new FileReader();
        reader.onload = function() {
            document.getElementById('source-paste').value = reader.result;
            submitSource();
        };
        reader.readAsText(file);
    }

    document.getElementById('btn-ingest').addEventListener('click', submitSource);

    function submitSource() {
        var text = document.getElementById('source-paste').value.trim();
        if (!text) {
            setStatus('ingest-status', 'No text provided', 'error');
            return;
        }
        setStatus('ingest-status', 'Uploading...', '');
        apiPost('/api/project/' + PROJECT + '/ingest', { text: text }).then(function(data) {
            if (data.error) {
                setStatus('ingest-status', data.error, 'error');
            } else {
                setStatus('ingest-status', 'Source saved', 'success');
                loadStatus();
            }
        });
    }

    // Source mode toggle (File/Paste vs Gutenberg)
    document.querySelectorAll('.mode-btn').forEach(function(btn) {
        btn.addEventListener('click', function() {
            document.querySelectorAll('.mode-btn').forEach(function(b) { b.classList.remove('active'); });
            btn.classList.add('active');
            var mode = btn.dataset.mode;
            document.getElementById('source-mode-file').style.display = mode === 'file' ? '' : 'none';
            document.getElementById('source-mode-gutenberg').style.display = mode === 'gutenberg' ? '' : 'none';
        });
    });

    // Gutenberg import
    document.getElementById('btn-gutenberg-ingest').addEventListener('click', function() {
        var url = document.getElementById('gutenberg-url').value.trim();
        if (!url) {
            setStatus('gutenberg-status', 'Please enter a URL', 'error');
            return;
        }
        var downloadImages = document.getElementById('gutenberg-download-images').checked;
        var btn = document.getElementById('btn-gutenberg-ingest');
        btn.disabled = true;
        setStatus('gutenberg-status', 'Fetching from Gutenberg\u2026 this may take a moment', '');

        apiPost('/api/project/' + PROJECT + '/ingest-gutenberg', {
            url: url,
            download_images: downloadImages
        }).then(function(data) {
            btn.disabled = false;
            if (data.error) {
                setStatus('gutenberg-status', data.error, 'error');
                return;
            }
            setStatus('gutenberg-status', 'Import complete \u2014 ' + data.words.toLocaleString() + ' words', 'success');
            showGutenbergReport(data.chapter_report, data.suggested_pattern, data.images_downloaded, data.images_skipped);
            loadStatus();
        }).catch(function() {
            btn.disabled = false;
            setStatus('gutenberg-status', 'Network error', 'error');
        });
    });

    function showGutenbergReport(chapters, suggestedPattern, imgDown, imgSkip) {
        var reportDiv = document.getElementById('gutenberg-report');
        if (!chapters || chapters.length === 0) {
            reportDiv.style.display = 'none';
            return;
        }
        reportDiv.style.display = '';
        var html = '<table class="report-table"><thead><tr>' +
            '<th>#</th><th>Heading</th><th>Words</th><th>Est. Chunks</th>' +
            '</tr></thead><tbody>';
        chapters.forEach(function(ch) {
            html += '<tr><td>' + ch.number + '</td>' +
                '<td>' + escapeHtml(ch.heading) + '</td>' +
                '<td>' + (ch.words || 0).toLocaleString() + '</td>' +
                '<td>' + (ch.chunks || 0) + '</td></tr>';
        });
        html += '</tbody></table>';
        if (imgDown) {
            html += '<p style="font-size:13px;color:#666;margin-top:8px">' +
                imgDown + ' images downloaded' +
                (imgSkip ? ', ' + imgSkip + ' failed' : '') + '</p>';
        }
        document.getElementById('gutenberg-report-table').innerHTML = html;

        var patternMsg = document.getElementById('gutenberg-suggested-pattern');
        if (suggestedPattern) {
            patternMsg.innerHTML = 'Suggested split pattern: <strong>' + escapeHtml(suggestedPattern) + '</strong> (will be auto-applied in Stage 2)';
        } else {
            patternMsg.textContent = 'Could not auto-detect a chapter heading pattern.';
        }
    }

    // Book title save
    document.getElementById('btn-save-title').addEventListener('click', function() {
        var titleInput = document.getElementById('book-title-input');
        var spanishTitleInput = document.getElementById('book-spanish-title-input');
        var title = titleInput.value.trim();
        var spanishTitle = spanishTitleInput.value.trim();
        setStatus('title-save-status', 'Saving...', '');
        apiPost('/api/project/' + PROJECT + '/config', { title: title || PROJECT, spanish_title: spanishTitle }).then(function(data) {
            if (data.error) {
                setStatus('title-save-status', data.error, 'error');
            } else {
                var display = data.config && data.config.title ? data.config.title : PROJECT;
                document.getElementById('sidebar-project-title').textContent = display;
                document.title = display + ' \u2014 Dashboard';
                setStatus('title-save-status', 'Saved', 'success');
            }
        });
    });

    // ========================================================================
    // Stage 2: Split
    // ========================================================================

    function loadSplitPatterns() {
        return fetch('/api/split-patterns')
            .then(function(r) { return r.json(); })
            .then(function(data) {
                SPLIT_PATTERNS = data.patterns;
                var sel = document.getElementById('split-pattern');
                sel.innerHTML = '';
                Object.keys(data.patterns).forEach(function(key) {
                    var opt = document.createElement('option');
                    opt.value = key;
                    opt.textContent = data.patterns[key].label;
                    sel.appendChild(opt);
                });
            });
    }

    function populateSplitStage(status) {
        // Auto-populate pattern from Gutenberg ingest suggestion
        if (status.suggested_split_pattern && !(status.chapters && status.chapters.length > 0)) {
            var suggested = status.suggested_split_pattern;
            var sel = document.getElementById('split-pattern');
            var optionExists = Array.from(sel.options).some(function(o) { return o.value === suggested; });
            if (optionExists) {
                sel.value = suggested;
            } else {
                sel.value = 'custom';
            }
            document.getElementById('custom-regex-field').style.display =
                sel.value === 'custom' ? '' : 'none';
        }
        if (status.chapters && status.chapters.length > 0) {
            document.getElementById('split-existing').style.display = '';
            document.getElementById('split-existing-count').textContent =
                'Current saved split: ' + status.chapters.length + ' chapters';
            var cards = document.getElementById('split-existing-cards');
            cards.innerHTML = '';
            status.chapters.forEach(function(ch) {
                cards.appendChild(renderExistingChapterCard(ch));
            });
        } else {
            document.getElementById('split-existing').style.display = 'none';
        }
    }

    function renderExistingChapterCard(ch) {
        var card = document.createElement('div');
        card.className = 'chapter-card';
        var kind = ch.kind || 'chapter';
        var isMatter = kind === 'front_matter' || kind === 'back_matter';

        var nameSpan = document.createElement('span');
        nameSpan.className = 'ch-name';
        nameSpan.textContent = ch.name;
        card.appendChild(nameSpan);

        if (isMatter) {
            var badge = document.createElement('span');
            badge.className = 'ch-badge ch-badge-' + (kind === 'front_matter' ? 'front' : 'back');
            badge.textContent = kind === 'front_matter' ? 'front matter' : 'back matter';
            card.appendChild(badge);

            card.appendChild(buildLabelEditor(ch));
        } else {
            var wordsSpan = document.createElement('span');
            wordsSpan.className = 'ch-words';
            wordsSpan.textContent = (ch.words || 0) + ' words';
            card.appendChild(wordsSpan);

            var previewSpan = document.createElement('span');
            previewSpan.className = 'ch-preview';
            previewSpan.textContent = truncate(ch.preview, 80);
            card.appendChild(previewSpan);
        }
        return card;
    }

    function buildLabelEditor(ch) {
        var wrap = document.createElement('span');
        wrap.className = 'ch-label-editor';

        var input = document.createElement('input');
        input.type = 'text';
        input.className = 'ch-label-input';
        input.placeholder = 'Heading shown in reader / EPUB';
        if (ch.label) input.value = ch.label;

        var btn = document.createElement('button');
        btn.type = 'button';
        btn.className = 'btn-save ch-label-save';
        btn.textContent = 'Save';

        var msg = document.createElement('span');
        msg.className = 'status-msg ch-label-status';

        btn.addEventListener('click', function() {
            var newLabel = input.value.trim();
            msg.textContent = 'Saving…';
            msg.className = 'status-msg ch-label-status';
            apiPatch(
                '/api/project/' + PROJECT
                    + '/chapter-manifest/' + encodeURIComponent(ch.id),
                { label: newLabel }
            ).then(function(res) {
                if (!res.ok) {
                    msg.textContent = (res.data && res.data.error) || 'Save failed';
                    msg.className = 'status-msg ch-label-status error';
                    return;
                }
                ch.label = res.data.label ?? null;
                msg.textContent = newLabel ? 'Saved' : 'Cleared';
                msg.className = 'status-msg ch-label-status success';
            }).catch(function() {
                msg.textContent = 'Network error';
                msg.className = 'status-msg ch-label-status error';
            });
        });

        wrap.appendChild(input);
        wrap.appendChild(btn);
        wrap.appendChild(msg);
        return wrap;
    }

    document.getElementById('split-pattern').addEventListener('change', function() {
        document.getElementById('custom-regex-field').style.display =
            this.value === 'custom' ? '' : 'none';
    });

    document.getElementById('btn-split-preview').addEventListener('click', function() {
        var config = getSplitConfig();
        setStatus('split-status', 'Previewing...', '');
        apiPost('/api/project/' + PROJECT + '/split/preview', config).then(function(data) {
            if (data.error) {
                setStatus('split-status', data.error, 'error');
                return;
            }
            setStatus('split-status', '', '');
            showSplitPreview(data.chapters);
        });
    });

    document.getElementById('btn-split-confirm').addEventListener('click', function() {
        var existingVisible =
            document.getElementById('split-existing').style.display !== 'none';
        if (existingVisible) {
            var msg =
                'This will OVERWRITE the currently saved split.\n\n' +
                'All current chapter files will be replaced with the new split shown above. ' +
                'Any per-chapter edits made to the source text will be lost.\n\n' +
                'Continue?';
            if (!confirm(msg)) {
                return;
            }
        }
        var config = getSplitConfig();
        setStatus('split-status', 'Splitting...', '');
        apiPost('/api/project/' + PROJECT + '/split', config).then(function(data) {
            if (data.error) {
                setStatus('split-status', data.error, 'error');
            } else {
                setStatus('split-status', 'Split complete', 'success');
                document.getElementById('split-preview-area').style.display = 'none';
                document.getElementById('btn-split-confirm').style.display = 'none';
                document.getElementById('split-existing').style.display = 'none';
                loadStatus();
            }
        });
    });

    function getSplitConfig() {
        function parseLines(elId) {
            var raw = (document.getElementById(elId) || {}).value || '';
            return raw.split('\n').map(function(s) { return s.trim(); })
                .filter(function(s) { return s.length > 0; });
        }
        return {
            pattern_type: document.getElementById('split-pattern').value,
            custom_regex: document.getElementById('split-custom-regex').value,
            min_chapter_size: parseInt(document.getElementById('split-min-size').value, 10) || 500,
            front_matter_titles: parseLines('split-front-matter'),
            back_matter_titles: parseLines('split-back-matter'),
        };
    }

    function showSplitPreview(chapters) {
        var area = document.getElementById('split-preview-area');
        area.style.display = '';
        document.getElementById('split-preview-count').textContent =
            chapters.length + ' chapters detected';
        var cards = document.getElementById('split-preview-cards');
        cards.innerHTML = '';
        chapters.forEach(function(ch) {
            var card = document.createElement('div');
            card.className = 'chapter-card';
            var kind = ch.kind || 'chapter';
            var badge = '';
            if (kind === 'front_matter') {
                badge = '<span class="ch-badge ch-badge-front">front matter</span>';
            } else if (kind === 'back_matter') {
                badge = '<span class="ch-badge ch-badge-back">back matter</span>';
            }
            card.innerHTML =
                '<span class="ch-name">' + escapeHtml(ch.name || ch.title || 'Chapter') + '</span>' +
                badge +
                '<span class="ch-words">' + (ch.words || ch.word_count || 0) + ' words</span>' +
                '<span class="ch-preview">' + escapeHtml(truncate(ch.preview || ch.first_line || '', 80)) + '</span>';
            cards.appendChild(card);
        });
        document.getElementById('btn-split-confirm').style.display = '';
    }

    // ========================================================================
    // Stage 3: Chunk
    // ========================================================================

    // Read the global default chunking config from the Stage 3 form: the
    // visible Target Size plus the Advanced min/max ratios and overlap.
    function getChunkDefault() {
        // Use Number.isFinite to preserve explicit 0 values (|| would treat
        // 0 as falsy and substitute the default, silently overriding the
        // user's choice for overlap_paragraphs / min_overlap_words).
        function intOrDefault(id, def) {
            var n = parseInt(document.getElementById(id).value, 10);
            return Number.isFinite(n) ? n : def;
        }
        function floatOrDefault(id, def) {
            var n = parseFloat(document.getElementById(id).value);
            return Number.isFinite(n) ? n : def;
        }
        return {
            target_size: intOrDefault('chunk-target-size', 2000),
            min_ratio: floatOrDefault('chunk-min-ratio', 0.25),
            max_ratio: floatOrDefault('chunk-max-ratio', 1.5),
            overlap_paragraphs: intOrDefault('chunk-overlap-para', 0),
            min_overlap_words: intOrDefault('chunk-overlap-words', 0),
        };
    }

    // Last fetched difficulty result, kept so the per-chapter badges survive a
    // status reload (chapter cards are rebuilt on every loadStatus()).
    // Shape: { book: metrics, chapters: { chapter_id: metrics } }.
    var lastDifficulty = null;

    var DIFF_HARD_THRESHOLD = 0.66;
    var DIFF_MED_THRESHOLD = 0.33;

    function difficultyBucket(d) {
        if (d >= DIFF_HARD_THRESHOLD) return 'hard';
        if (d >= DIFF_MED_THRESHOLD) return 'med';
        return 'easy';
    }

    // A score badge whose tooltip breaks down the combined number into its
    // length and rarity sub-scores plus the raw stats — so you can see which
    // factor is driving a score and tune the weights.
    function renderDifficultyBadge(m) {
        var badge = document.createElement('span');
        badge.className = 'difficulty-badge diff-' + difficultyBucket(m.difficulty);
        badge.textContent = 'Difficulty ' + m.difficulty.toFixed(2);
        var dialectPart = (m.dialect_score && m.dialect_score > 0)
            ? ' · Dialect ' + m.dialect_score.toFixed(2) + ' (' + (m.dialect_marker_count || 0) + ' markers)'
            : '';
        badge.title =
            'Length ' + m.length_score.toFixed(2) +
            ' · Rarity ' + m.rarity_score.toFixed(2) +
            dialectPart +
            ' · ' + Math.round(m.sentence_length_weighted) + 'w/sent' +
            ' · ' + (m.rare_word_fraction * 100).toFixed(1) + '% rare';
        return badge;
    }

    function renderBookDifficulty() {
        var el = document.getElementById('chunk-difficulty-summary');
        if (!el) return;
        el.innerHTML = '';
        if (!lastDifficulty || !lastDifficulty.book) return;
        var b = lastDifficulty.book;
        el.appendChild(renderDifficultyBadge(b));
        var sug = document.createElement('button');
        sug.type = 'button';
        sug.className = 'btn-link diff-suggest';
        sug.textContent = 'Use suggested book target (' + b.suggested_target_size + ')';
        sug.addEventListener('click', function() {
            var input = document.getElementById('chunk-target-size');
            if (input) input.value = b.suggested_target_size;
        });
        el.appendChild(sug);
    }

    // Inject the difficulty badge + a "Suggest" link into each chapter card.
    // Re-applied after every populateChunkStage so badges persist across reloads.
    function applyDifficultyToCards() {
        if (!lastDifficulty) return;
        var map = lastDifficulty.chapters || {};
        document.querySelectorAll('#chunk-chapter-list .chapter-card').forEach(function(card) {
            var old = card.querySelector('.difficulty-wrap');
            if (old) old.remove();
            var m = map[card.dataset.chapterId];
            if (!m) return;

            var wrap = document.createElement('div');
            wrap.className = 'difficulty-wrap';
            wrap.appendChild(renderDifficultyBadge(m));

            var sug = document.createElement('button');
            sug.type = 'button';
            sug.className = 'btn-link diff-suggest';
            sug.textContent = 'Suggest ' + m.suggested_target_size;
            sug.addEventListener('click', function() {
                var input = card.querySelector('.chapter-target-input');
                if (input) input.value = m.suggested_target_size;
            });
            wrap.appendChild(sug);

            var targetField = card.querySelector('.chapter-target-field');
            card.insertBefore(wrap, targetField);
        });
    }

    function populateChunkStage(status) {
        // Pre-fill the global default inputs from the project's persisted
        // chunking_config (saved on the last successful chunk run). Fall
        // back to the HTML-default system values when nothing is saved.
        var cc = status.chunking_config;
        if (cc) {
            var fields = [
                ['chunk-target-size', cc.target_size],
                ['chunk-min-ratio', cc.min_ratio],
                ['chunk-max-ratio', cc.max_ratio],
                ['chunk-overlap-para', cc.overlap_paragraphs],
                ['chunk-overlap-words', cc.min_overlap_words],
            ];
            fields.forEach(function(pair) {
                var el = document.getElementById(pair[0]);
                if (el && Number.isFinite(pair[1])) el.value = pair[1];
            });
        }

        var list = document.getElementById('chunk-chapter-list');
        list.innerHTML = '';

        if (!status.chapters || status.chapters.length === 0) {
            list.innerHTML = '<p class="stage-subtitle">No chapters yet. Split the source text first.</p>';
            return;
        }

        status.chapters.forEach(function(ch) {
            var card = document.createElement('div');
            card.className = 'chapter-card';
            card.dataset.chapterId = ch.id;
            var translated = ch.translated_count || 0;
            var chunkInfo = ch.chunk_count > 0
                ? ch.chunk_count + ' chunks' + (translated > 0 ? ' · ' + translated + ' translated' : '')
                : 'Not chunked';

            var info = document.createElement('div');
            info.className = 'chapter-card-info';
            info.innerHTML =
                '<span class="ch-name">' + escapeHtml(ch.name) + '</span>' +
                '<span class="ch-words">' + (ch.words || ch.word_count || 0) + ' words · ' + chunkInfo + '</span>';
            card.appendChild(info);

            // Inline per-chapter Target override. Blank = inherit the global
            // default ("auto"). Pre-filled from the persisted override.
            var targetWrap = document.createElement('div');
            targetWrap.className = 'dash-field chapter-target-field';
            var lbl = document.createElement('label');
            lbl.textContent = 'Target';
            var tInput = document.createElement('input');
            tInput.type = 'number';
            tInput.min = '100';
            tInput.className = 'chapter-target-input';
            tInput.placeholder = 'auto';
            if (Number.isFinite(ch.chunk_target_override)) {
                tInput.value = ch.chunk_target_override;
            }
            targetWrap.appendChild(lbl);
            targetWrap.appendChild(tInput);
            card.appendChild(targetWrap);

            var btn = document.createElement('button');
            btn.type = 'button';
            btn.className = ch.chunk_count > 0
                ? (translated > 0 ? 'btn-danger' : 'btn-secondary')
                : 'btn-secondary';
            btn.textContent = ch.chunk_count > 0 ? 'Rechunk' : 'Chunk';
            btn.dataset.chapterId = ch.id;
            btn.dataset.chapterName = ch.name;
            btn.dataset.translatedCount = String(translated);
            btn.addEventListener('click', onRechunkChapterClick);
            card.appendChild(btn);

            list.appendChild(card);
        });

        // Re-attach any previously fetched difficulty badges to the fresh cards.
        applyDifficultyToCards();
    }

    // Read a chapter card's Target input. Blank ⇒ null (inherit global / auto).
    function getCardTarget(card) {
        var input = card ? card.querySelector('.chapter-target-input') : null;
        if (!input) return null;
        var raw = input.value.trim();
        if (raw === '') return null;
        var n = parseInt(raw, 10);
        return Number.isFinite(n) ? n : null;
    }

    function onRechunkChapterClick(ev) {
        var btn = ev.currentTarget;
        var card = btn.closest('.chapter-card');
        var chapterId = btn.dataset.chapterId;
        var chapterName = btn.dataset.chapterName || chapterId;
        var translated = parseInt(btn.dataset.translatedCount, 10) || 0;

        if (translated > 0) {
            var msg = 'This chapter has ' + translated + ' translated chunk' +
                (translated === 1 ? '' : 's') +
                '. Rechunking will delete those translations. Continue?';
            if (!confirm(msg)) return;
        }

        var payload = { target_size: getCardTarget(card) };
        setStatus('chunk-status', 'Rechunking ' + chapterName + '...', '');
        apiPost('/api/project/' + PROJECT + '/chapters/' + chapterId + '/rechunk', payload).then(function(data) {
            if (data.error) {
                setStatus('chunk-status', data.error, 'error');
            } else {
                setStatus('chunk-status', 'Rechunked ' + chapterName + ' (' + (data.chunk_count || 0) + ' chunks)', 'success');
                loadStatus();
            }
        });
    }

    var analyzeBtn = document.getElementById('btn-analyze-difficulty');
    if (analyzeBtn) {
        analyzeBtn.addEventListener('click', function() {
            analyzeBtn.disabled = true;
            setStatus('chunk-status', 'Analyzing difficulty…', '');
            apiGet('/api/project/' + PROJECT + '/difficulty').then(function(data) {
                analyzeBtn.disabled = false;
                if (!data || data.error) {
                    setStatus('chunk-status', (data && data.error) || 'Analysis failed', 'error');
                    return;
                }
                var chMap = {};
                (data.chapters || []).forEach(function(c) { chMap[c.chapter_id] = c; });
                lastDifficulty = { book: data.book, chapters: chMap };
                renderBookDifficulty();
                applyDifficultyToCards();
                setStatus('chunk-status', 'Difficulty analyzed — click Suggest to fill targets', 'success');
            }).catch(function() {
                analyzeBtn.disabled = false;
                setStatus('chunk-status', 'Analysis failed', 'error');
            });
        });
    }

    document.getElementById('btn-chunk-all').addEventListener('click', function() {
        // Gather the global default plus each card's per-chapter target.
        var chapters = {};
        document.querySelectorAll('#chunk-chapter-list .chapter-card').forEach(function(card) {
            var id = card.dataset.chapterId;
            if (id) chapters[id] = { target_size: getCardTarget(card) };
        });
        var payload = { default: getChunkDefault(), chapters: chapters };
        setStatus('chunk-status', 'Chunking...', '');
        apiPost('/api/project/' + PROJECT + '/chunk-all', payload).then(function(data) {
            if (data.error) {
                setStatus('chunk-status', data.error, 'error');
            } else {
                setStatus('chunk-status', 'Chunked ' + (data.total_chunks || 0) + ' chunks', 'success');
                loadStatus();
            }
        });
    });

    // ========================================================================
    // Stage 4: Style Guide
    // ========================================================================

    var pendingStyleContent = null;

    // Force-rescan full text features (controls conditional questions).
    var rescanBtn = document.getElementById('btn-style-rescan');
    if (rescanBtn) {
        rescanBtn.addEventListener('click', function() {
            rescanBtn.disabled = true;
            setStatus('style-rescan-status', 'Rescanning full text...', 'info');
            apiPost('/api/setup/' + PROJECT + '/text-features/rescan', {}).then(function(data) {
                if (data && data.ok) {
                    setStatus(
                        'style-rescan-status',
                        'Rescan complete (' + (data.conditional_count || 0) + ' conditional questions). Reloading...',
                        'success'
                    );
                    setTimeout(function() { window.location.reload(); }, 400);
                } else {
                    rescanBtn.disabled = false;
                    setStatus('style-rescan-status', (data && data.error) ? data.error : 'Rescan failed', 'error');
                }
            }).catch(function(e) {
                rescanBtn.disabled = false;
                setStatus('style-rescan-status', 'Rescan failed: ' + (e && e.message ? e.message : e), 'error');
            });
        });
    }

    function populateStyleGuideStage(status) {
        if (status.has_style_guide && status.style_guide_content) {
            document.getElementById('style-guide-existing').style.display = '';
            document.getElementById('style-guide-wizard').style.display = 'none';
            document.getElementById('style-guide-preview').textContent = status.style_guide_content;
            document.getElementById('light-style-guide-input').value = status.light_style_guide_content || '';
        } else {
            document.getElementById('style-guide-existing').style.display = 'none';
            document.getElementById('style-guide-wizard').style.display = '';
        }
    }

    // Edit = inline-edit the existing style guide text. Rebuild = wipe and
    // re-run the Q&A → LLM-generate wizard. These used to be the same button.
    function setStyleEditMode(editing) {
        var preview = document.getElementById('style-guide-preview');
        var editor = document.getElementById('style-guide-editor');
        var actions = document.getElementById('style-guide-edit-actions');
        var editBtn = document.getElementById('btn-edit-style');
        var rebuildBtn = document.getElementById('btn-rebuild-style');
        if (editing) {
            editor.value = preview.textContent || '';
            preview.style.display = 'none';
            editor.style.display = '';
            actions.style.display = '';
            if (editBtn) editBtn.disabled = true;
            if (rebuildBtn) rebuildBtn.disabled = true;
        } else {
            editor.style.display = 'none';
            actions.style.display = 'none';
            preview.style.display = '';
            if (editBtn) editBtn.disabled = false;
            if (rebuildBtn) rebuildBtn.disabled = false;
            setStatus('style-edit-status', '', '');
        }
    }

    document.getElementById('btn-edit-style').addEventListener('click', function() {
        setStyleEditMode(true);
    });

    document.getElementById('btn-cancel-style-edit').addEventListener('click', function() {
        setStyleEditMode(false);
    });

    document.getElementById('btn-save-style-edit').addEventListener('click', function() {
        var editor = document.getElementById('style-guide-editor');
        var newContent = editor.value || '';
        if (!newContent.trim()) {
            setStatus('style-edit-status', 'Style guide cannot be empty.', 'error');
            return;
        }
        setStatus('style-edit-status', 'Saving...', '');
        apiPost('/api/setup/' + PROJECT + '/style-guide', { content: newContent })
        .then(function(data) {
            if (data && data.ok) {
                document.getElementById('style-guide-preview').textContent = newContent;
                setStyleEditMode(false);
                setStatus('style-edit-status', 'Saved!', 'success');
                setTimeout(function() { setStatus('style-edit-status', '', ''); }, 2000);
                loadStatus();
            } else {
                setStatus('style-edit-status', 'Error: ' + ((data && data.error) || 'unknown'), 'error');
            }
        })
        .catch(function(e) {
            setStatus('style-edit-status', 'Error: ' + (e && e.message ? e.message : e), 'error');
        });
    });

    document.getElementById('btn-rebuild-style').addEventListener('click', function() {
        if (!confirm('Rebuild the style guide from scratch?\n\nThis hides the current guide and reopens the Q&A wizard. Your existing style.json stays on disk until you regenerate.')) {
            return;
        }
        setStyleEditMode(false);
        document.getElementById('style-guide-existing').style.display = 'none';
        document.getElementById('style-guide-wizard').style.display = '';
    });

    document.getElementById('btn-save-light-style').addEventListener('click', function() {
        var value = document.getElementById('light-style-guide-input').value;
        var statusEl = document.getElementById('light-style-save-status');
        statusEl.textContent = 'Saving...';
        apiPost('/api/setup/' + PROJECT + '/style-guide/light', { light_content: value })
        .then(function(data) {
            if (data && data.ok) {
                statusEl.textContent = 'Saved!';
            } else {
                statusEl.textContent = 'Error: ' + ((data && data.error) || 'unknown');
            }
            setTimeout(function() { statusEl.textContent = ''; }, 3000);
        })
        .catch(function(e) {
            statusEl.textContent = 'Error: ' + (e && e.message ? e.message : e);
        });
    });

    // Collect answers from fixed + extra questions
    var extraQuestions = [];

    function isBlockSkipped(block) {
        var skip = block && block.querySelector('.skip-checkbox');
        return !!(skip && skip.checked);
    }

    function applySkipState(block, skipped) {
        block.classList.toggle('question-skipped', skipped);
        block.querySelectorAll('input[type="radio"], .custom-input').forEach(function(el) {
            el.disabled = skipped;
            if (skipped && el.type === 'radio') el.checked = false;
        });
        var preview = block.querySelector('.effect-preview');
        if (preview && skipped) preview.textContent = '';
        var qid = block.dataset.qid;
        if (qid) syncGlossaryQASkip(qid, skipped);
    }

    function syncGlossaryQASkip(qid, skipped) {
        var item = document.querySelector('.glossary-qa-item[data-qid="' + qid + '"]');
        if (!item) return;
        item.classList.toggle('glossary-qa-item-skipped', skipped);
        var cb = item.querySelector('input[type="checkbox"]');
        if (cb) {
            cb.disabled = skipped;
            if (skipped) cb.checked = false;
        }
    }

    function wireSkipCheckbox(block) {
        var skip = block.querySelector('.skip-checkbox');
        if (!skip) return;
        skip.addEventListener('change', function() {
            applySkipState(block, skip.checked);
        });
    }

    function collectAnswers() {
        var answers = {};
        document.querySelectorAll('.question-block').forEach(function(block) {
            if (isBlockSkipped(block)) return;
            var qid = block.dataset.qid;
            var checked = block.querySelector('input[name="q_' + qid + '"]:checked');
            if (!checked) return;
            if (checked.value === 'custom') {
                var customInput = block.querySelector('.custom-input');
                answers[qid] = customInput ? customInput.value : '';
            } else {
                answers[qid] = parseInt(checked.value, 10);
            }
        });
        extraQuestions.forEach(function(q) {
            var block = document.querySelector('.question-block[data-qid="' + q.id + '"]');
            if (block && isBlockSkipped(block)) return;
            var checked = document.querySelector('input[name="q_' + q.id + '"]:checked');
            if (checked) {
                answers[q.id] = parseInt(checked.value, 10);
            }
        });
        return answers;
    }

    // Custom input toggle + skip wiring
    document.querySelectorAll('.question-block').forEach(function(block) {
        wireSkipCheckbox(block);
        block.querySelectorAll('input[type="radio"]').forEach(function(radio) {
            radio.addEventListener('change', function() {
                var customInput = block.querySelector('.custom-input');
                if (customInput) {
                    customInput.style.display = radio.value === 'custom' && radio.checked ? '' : 'none';
                }
                // Show effect preview
                var qid = block.dataset.qid;
                var preview = block.querySelector('.effect-preview');
                if (preview && radio.value !== 'custom') {
                    var idx = parseInt(radio.value, 10);
                    var q = FIXED_QUESTIONS.find(function(fq) { return fq.id === qid; });
                    if (q && q.options[idx]) {
                        preview.textContent = q.options[idx].style_guide_effect || '';
                    }
                } else if (preview) {
                    preview.textContent = '';
                }
            });
        });
    });

    // Show questions prompt
    document.getElementById('btn-show-questions-prompt').addEventListener('click', function() {
        var area = document.getElementById('questions-prompt-area');
        if (area.style.display === 'none') {
            var answers = collectAnswers();
            apiPost('/api/setup/' + PROJECT + '/prompts/questions', { answers: answers })
                .then(function(data) {
                    document.getElementById('questions-prompt-text').value = data.prompt || data.error || '';
                    area.style.display = '';
                });
        } else {
            area.style.display = 'none';
        }
    });

    // Parse extra questions
    document.getElementById('btn-parse-questions').addEventListener('click', function() {
        try {
            var text = document.getElementById('questions-paste').value.trim();
            // Strip markdown fences
            var match = text.match(/```(?:json)?\s*\n?([\s\S]*?)```/);
            if (match) text = match[1].trim();
            var questions = JSON.parse(text);
            if (!Array.isArray(questions)) throw new Error('Expected JSON array');
            extraQuestions = questions;
            renderExtraQuestions(questions);
            buildGlossaryQASelection();
        } catch (e) {
            alert('Failed to parse questions: ' + e.message);
        }
    });

    // Generate questions via API
    document.getElementById('btn-generate-questions-api').addEventListener('click', function() {
        var btn = this;
        var answers = collectAnswers();
        var provider = document.getElementById('style-provider').value;
        var model = document.getElementById('style-model').value;
        btn.disabled = true;
        setStatus('questions-api-status', 'Generating questions...', 'info');

        apiPost('/api/setup/' + PROJECT + '/questions/generate', {
            answers: answers,
            provider: provider,
            model: model,
        }).then(function(data) {
            btn.disabled = false;
            if (data.error && !data.questions && !data.raw_text) {
                setStatus('questions-api-status', data.error, 'error');
                return;
            }
            if (data.questions) {
                extraQuestions = data.questions;
                renderExtraQuestions(data.questions);
                buildGlossaryQASelection();
                setStatus('questions-api-status', data.questions.length + ' questions generated', 'success');
            } else if (data.raw_text) {
                document.getElementById('questions-paste').value = data.raw_text;
                setStatus('questions-api-status', 'Response was not valid JSON. Pasted raw text for manual editing.', 'error');
            }
        }).catch(function() {
            btn.disabled = false;
            setStatus('questions-api-status', 'Request failed', 'error');
        });
    });

    function renderExtraQuestions(questions) {
        var container = document.getElementById('extra-questions');
        container.innerHTML = '';
        questions.forEach(function(q) {
            var block = document.createElement('div');
            block.className = 'question-block';
            block.dataset.qid = q.id;
            var html = '<label class="skip-toggle" title="Skip this question and exclude it from the style guide prompt">' +
                '<input type="checkbox" class="skip-checkbox" data-qid="' + q.id + '"> Skip</label>';
            html += '<label class="question-label">' + escapeHtml(q.question) + '</label>';
            (q.options || []).forEach(function(opt, i) {
                var label = typeof opt === 'string' ? opt : opt.label;
                html += '<label class="option-label">' +
                    '<input type="radio" name="q_' + q.id + '" value="' + i + '"' +
                    (i === (q.default || 0) ? ' checked' : '') + '> ' +
                    escapeHtml(label) + '</label>';
            });
            html += '<div class="effect-preview" data-qid="' + q.id + '"></div>';
            block.innerHTML = html;

            wireSkipCheckbox(block);

            // Wire change handlers for effect preview
            block.querySelectorAll('input[type="radio"]').forEach(function(radio) {
                radio.addEventListener('change', function() {
                    var preview = block.querySelector('.effect-preview');
                    if (!preview) return;
                    var idx = parseInt(radio.value, 10);
                    var opts = q.options || [];
                    var opt = opts[idx];
                    var effect = (opt && typeof opt === 'object') ? (opt.style_guide_effect || '') : '';
                    preview.textContent = effect;
                });
            });

            // Show effect for default selection
            var defaultIdx = q.default || 0;
            var defaultOpt = (q.options || [])[defaultIdx];
            var defaultEffect = (defaultOpt && typeof defaultOpt === 'object') ? (defaultOpt.style_guide_effect || '') : '';
            var preview = block.querySelector('.effect-preview');
            if (preview) preview.textContent = defaultEffect;

            container.appendChild(block);
        });
    }

    // Generate fallback style guide (no LLM)
    document.getElementById('btn-generate-fallback').addEventListener('click', function() {
        var answers = collectAnswers();
        apiPost('/api/setup/' + PROJECT + '/style-guide/fallback', { answers: answers, extra_questions: extraQuestions })
            .then(function(data) {
                if (data.error) {
                    alert(data.error);
                    return;
                }
                pendingStyleContent = data.content;
                document.getElementById('style-result-preview').textContent = data.content;
                document.getElementById('style-guide-result').style.display = '';
            });
    });

    // Generate style guide via API
    document.getElementById('btn-generate-style-api').addEventListener('click', function() {
        var btn = this;
        var answers = collectAnswers();
        var provider = document.getElementById('style-provider').value;
        var model = document.getElementById('style-model').value;
        btn.disabled = true;
        setStatus('style-api-status', 'Generating style guide...', 'info');

        apiPost('/api/setup/' + PROJECT + '/style-guide/generate', {
            answers: answers,
            extra_questions: extraQuestions,
            provider: provider,
            model: model,
        }).then(function(data) {
            btn.disabled = false;
            if (data.error) {
                setStatus('style-api-status', data.error, 'error');
                return;
            }
            pendingStyleContent = data.content;
            document.getElementById('style-result-preview').textContent = data.content;
            document.getElementById('style-guide-result').style.display = '';
            setStatus('style-api-status', 'Generated successfully', 'success');
        }).catch(function() {
            btn.disabled = false;
            setStatus('style-api-status', 'Request failed', 'error');
        });
    });

    // Show LLM prompt for style guide
    document.getElementById('btn-show-style-prompt').addEventListener('click', function() {
        var area = document.getElementById('style-prompt-area');
        if (area.style.display === 'none') {
            var answers = collectAnswers();
            apiPost('/api/setup/' + PROJECT + '/prompts/style-guide', { answers: answers, extra_questions: extraQuestions })
                .then(function(data) {
                    document.getElementById('style-prompt-text').value = data.prompt || data.error || '';
                    area.style.display = '';
                });
        } else {
            area.style.display = 'none';
        }
    });

    // Use pasted style guide
    document.getElementById('btn-use-pasted-style').addEventListener('click', function() {
        var text = document.getElementById('style-paste').value.trim();
        if (!text) return;
        pendingStyleContent = text;
        document.getElementById('style-result-preview').textContent = text;
        document.getElementById('style-guide-result').style.display = '';
    });

    // Save style guide
    document.getElementById('btn-save-style').addEventListener('click', function() {
        if (!pendingStyleContent) return;
        apiPost('/api/setup/' + PROJECT + '/style-guide', { content: pendingStyleContent })
            .then(function(data) {
                if (data.error) {
                    setStatus('style-save-status', data.error, 'error');
                } else {
                    setStatus('style-save-status', 'Saved!', 'success');
                    loadStatus();
                }
            });
    });

    // ========================================================================
    // Stage 5: Glossary
    // ========================================================================

    var glossaryCandidates = [];
    var glossaryProposals = [];
    var glossaryEditMode = false;
    var GLOSSARY_TYPES = ['character', 'place', 'concept', 'technical', 'other'];

    function buildGlossaryQASelection() {
        var qaDiv = document.getElementById('glossary-qa-selection');
        if (!qaDiv) return;
        qaDiv.innerHTML = '';
        function appendQAItem(q, defaultChecked) {
            var block = document.querySelector('.question-block[data-qid="' + q.id + '"]');
            var skipped = block ? isBlockSkipped(block) : false;
            var item = document.createElement('label');
            item.className = 'glossary-qa-item' + (skipped ? ' glossary-qa-item-skipped' : '');
            item.dataset.qid = q.id;
            var checkedAttr = (defaultChecked && !skipped) ? ' checked' : '';
            var disabledAttr = skipped ? ' disabled' : '';
            item.innerHTML = '<input type="checkbox"' + checkedAttr + disabledAttr + ' value="' + q.id + '"> ' +
                '<span class="glossary-qa-question">' + escapeHtml(q.question) + '</span>';
            qaDiv.appendChild(item);
        }
        FIXED_QUESTIONS.forEach(function(q) {
            appendQAItem(q, q.glossary_relevant === true);
        });
        extraQuestions.forEach(function(q) {
            appendQAItem(q, false);
        });
    }

    function collectGlossaryGuidance() {
        var answers = collectAnswers();
        var allQuestions = FIXED_QUESTIONS.concat(extraQuestions);
        var questionsMap = {};
        allQuestions.forEach(function(q) { questionsMap[q.id] = q; });
        var lines = [];
        document.querySelectorAll('#glossary-qa-selection input:checked').forEach(function(cb) {
            var q = questionsMap[cb.value];
            if (!q) return;
            var answer = answers[q.id];
            if (typeof answer === 'number' && q.options && answer < q.options.length) {
                var opt = q.options[answer];
                var effect = (typeof opt === 'object' && opt.style_guide_effect) ? opt.style_guide_effect : (typeof opt === 'object' ? opt.label : String(opt));
                lines.push(effect);
            } else if (typeof answer === 'string' && answer.trim()) {
                lines.push(q.question + ': ' + answer);
            }
        });
        return lines.join('\n\n');
    }

    function populateGlossaryStage(status) {
        if (status.glossary_count > 0) {
            document.getElementById('glossary-existing').style.display = '';
            document.getElementById('glossary-existing-count').textContent = status.glossary_count;
        } else {
            document.getElementById('glossary-existing').style.display = 'none';
        }
        buildGlossaryQASelection();
    }

    document.getElementById('btn-add-more-terms').addEventListener('click', function() {
        glossaryEditMode = false;
        document.getElementById('glossary-existing').style.display = 'none';
    });

    // Zipf sensitivity slider readout (defaults mirror extract_glossary_candidates.py)
    var zipfSlider = document.getElementById('extract-zipf-slider');
    var zipfReadout = document.getElementById('extract-zipf-readout');
    function updateZipfReadout() {
        var offset = parseFloat(zipfSlider.value) || 0;
        var mixed = (3.0 + offset).toFixed(2);
        var cap = (4.0 + offset).toFixed(2);
        zipfReadout.textContent = mixed + ' / ' + cap;
    }
    zipfSlider.addEventListener('input', updateZipfReadout);
    updateZipfReadout();

    // Extract candidates
    document.getElementById('btn-extract-candidates').addEventListener('click', function() {
        setStatus('extract-status', 'Extracting...', '');
        var offset = parseFloat(zipfSlider.value) || 0;
        apiPost('/api/setup/' + PROJECT + '/extract-candidates', { zipf_offset: offset }).then(function(data) {
            if (data.error) {
                setStatus('extract-status', data.error, 'error');
                return;
            }
            glossaryCandidates = data.candidates || [];
            setStatus('extract-status', glossaryCandidates.length + ' candidates found', 'success');
            var count = document.getElementById('candidates-count');
            count.style.display = '';
            count.textContent = glossaryCandidates.length + ' candidates extracted';
        });
    });

    function getGlossaryContextMode() {
        var sel = document.getElementById('glossary-context-mode');
        return (sel && sel.value) || 'full-text';
    }

    // Show glossary prompt
    document.getElementById('btn-show-glossary-prompt').addEventListener('click', function() {
        var btn = this;
        var area = document.getElementById('glossary-prompt-area');
        if (area.style.display !== 'none') {
            area.style.display = 'none';
            return;
        }
        var mode = getGlossaryContextMode();
        var label = mode === 'word'
            ? 'Building word-mode prompt (scanning book for first appearances)...'
            : 'Building prompt...';
        btn.disabled = true;
        setStatus('glossary-prompt-status', label, 'info');
        apiPost('/api/setup/' + PROJECT + '/prompts/glossary', {
            candidates: glossaryCandidates,
            glossary_guidance: collectGlossaryGuidance(),
            context_mode: mode,
        }).then(function(data) {
            btn.disabled = false;
            document.getElementById('glossary-prompt-text').value = data.prompt || data.error || '';
            area.style.display = '';
            setStatus('glossary-prompt-status', '', '');
        }).catch(function(e) {
            btn.disabled = false;
            setStatus('glossary-prompt-status', 'Failed: ' + (e && e.message ? e.message : 'request error'), 'error');
        });
    });

    // Generate glossary via API
    document.getElementById('btn-generate-glossary-api').addEventListener('click', function() {
        var btn = this;
        var provider = document.getElementById('glossary-provider').value;
        var model = document.getElementById('glossary-model').value;
        btn.disabled = true;
        setStatus('glossary-api-status', 'Generating glossary...', 'info');

        apiPost('/api/setup/' + PROJECT + '/glossary/generate', {
            candidates: glossaryCandidates,
            glossary_guidance: collectGlossaryGuidance(),
            provider: provider,
            model: model,
            context_mode: getGlossaryContextMode(),
        }).then(function(data) {
            btn.disabled = false;
            if (data.error && !data.terms && !data.raw_text) {
                setStatus('glossary-api-status', data.error, 'error');
                return;
            }
            if (data.terms) {
                glossaryProposals = data.terms;
                glossaryEditMode = false;
                renderGlossaryProposals(glossaryProposals);
                setStatus('glossary-api-status', data.terms.length + ' terms generated', 'success');
            } else if (data.raw_text) {
                document.getElementById('glossary-paste').value = data.raw_text;
                setStatus('glossary-api-status', 'Response was not valid JSON. Pasted raw text for manual editing.', 'error');
            }
        }).catch(function() {
            btn.disabled = false;
            setStatus('glossary-api-status', 'Request failed', 'error');
        });
    });

    // Parse glossary response
    document.getElementById('btn-parse-glossary').addEventListener('click', function() {
        try {
            var text = document.getElementById('glossary-paste').value.trim();
            var match = text.match(/```(?:json)?\s*\n?([\s\S]*?)```/);
            if (match) text = match[1].trim();
            glossaryProposals = JSON.parse(text);
            if (!Array.isArray(glossaryProposals)) throw new Error('Expected JSON array');
            glossaryEditMode = false;
            renderGlossaryProposals(glossaryProposals);
        } catch (e) {
            alert('Failed to parse glossary: ' + e.message);
        }
    });

    function renderGlossaryProposals(proposals) {
        var review = document.getElementById('glossary-review');
        review.style.display = '';
        var tbody = document.querySelector('#glossary-proposals-table tbody');
        tbody.innerHTML = '';
        proposals.forEach(function(p, i) {
            var tr = document.createElement('tr');
            tr.dataset.index = i;
            var currentType = (p.type || 'other').toLowerCase();
            if (GLOSSARY_TYPES.indexOf(currentType) === -1) currentType = 'other';
            var typeOptions = GLOSSARY_TYPES.map(function(t) {
                var sel = (t === currentType) ? ' selected' : '';
                return '<option value="' + t + '"' + sel + '>' + t + '</option>';
            }).join('');
            tr.dataset.alternatives = JSON.stringify(p.alternatives || []);
            tr.innerHTML =
                '<td><input type="text" value="' + escapeHtml(p.english || '') + '" class="gl-english"></td>' +
                '<td><input type="text" value="' + escapeHtml(p.spanish || '') + '" class="gl-spanish"></td>' +
                '<td><select class="gl-type">' + typeOptions + '</select></td>' +
                '<td><input type="text" value="' + escapeHtml(p.context || '') + '" class="gl-context"></td>' +
                '<td class="term-actions">' +
                    '<button class="accepted gl-accept">Keep</button> ' +
                    '<button class="gl-reject">Drop</button>' +
                '</td>';
            tbody.appendChild(tr);
        });

        // Accept/reject handlers
        tbody.querySelectorAll('.gl-reject').forEach(function(btn) {
            btn.addEventListener('click', function() {
                var tr = btn.closest('tr');
                tr.classList.toggle('rejected');
                btn.textContent = tr.classList.contains('rejected') ? 'Undo' : 'Drop';
            });
        });
    }

    // Save glossary
    document.getElementById('btn-save-glossary').addEventListener('click', function() {
        var terms = [];
        document.querySelectorAll('#glossary-proposals-table tbody tr').forEach(function(tr) {
            if (tr.classList.contains('rejected')) return;
            var eng = tr.querySelector('.gl-english').value.trim();
            var spa = tr.querySelector('.gl-spanish').value.trim();
            if (!eng || !spa) return;  // skip blank rows
            terms.push({
                english: eng,
                spanish: spa,
                type: tr.querySelector('.gl-type').value,
                context: tr.querySelector('.gl-context').value,
                alternatives: JSON.parse(tr.dataset.alternatives || '[]'),
            });
        });
        var payload = { terms: terms, mode: glossaryEditMode ? 'replace' : 'merge' };
        apiPost('/api/setup/' + PROJECT + '/glossary', payload).then(function(data) {
            if (data.error) {
                setStatus('glossary-save-status', data.error, 'error');
            } else {
                var msg = glossaryEditMode
                    ? 'Saved ' + terms.length + ' terms (replaced)'
                    : 'Saved ' + terms.length + ' terms';
                glossaryEditMode = false;
                setStatus('glossary-save-status', msg, 'success');
                loadStatus();
            }
        });
    });

    // Edit existing glossary
    document.getElementById('btn-edit-glossary').addEventListener('click', function() {
        apiGet('/api/setup/' + PROJECT + '/glossary').then(function(data) {
            if (data.error) {
                alert('Failed to load glossary: ' + data.error);
                return;
            }
            glossaryProposals = data.terms || [];
            glossaryEditMode = true;
            renderGlossaryProposals(glossaryProposals);
            document.getElementById('glossary-existing').style.display = 'none';
        });
    });

    // Add a blank row to the glossary table
    document.getElementById('btn-add-glossary-row').addEventListener('click', function() {
        // Capture current edits before re-rendering so they aren't lost.
        var rows = document.querySelectorAll('#glossary-proposals-table tbody tr');
        if (rows.length) {
            glossaryProposals = [];
            rows.forEach(function(tr) {
                if (tr.classList.contains('rejected')) return;
                glossaryProposals.push({
                    english: tr.querySelector('.gl-english').value,
                    spanish: tr.querySelector('.gl-spanish').value,
                    type: tr.querySelector('.gl-type').value,
                    context: tr.querySelector('.gl-context').value,
                    alternatives: JSON.parse(tr.dataset.alternatives || '[]'),
                });
            });
        }
        glossaryProposals.push({english:'', spanish:'', type:'other', context:'', alternatives:[]});
        renderGlossaryProposals(glossaryProposals);
    });

    // ========================================================================
    // Evaluator card
    // ========================================================================

    var evalSummaryCache = null;  // {chunk_id: {errors, warnings, info}}
    var evalChapterCache = null;  // {chapter_id: {errors, warnings, info}}
    var EVAL_NAMES = ['length', 'paragraph', 'dictionary', 'glossary', 'completeness', 'blacklist', 'grammar'];

    function loadExistingEvaluation(chunkId) {
        apiGet('/api/project/' + PROJECT + '/evaluations/' + chunkId).then(function(data) {
            if (data && !data.error && data.aggregated) {
                renderEvalCard(chunkId, data);
            }
        });
    }

    function refreshEvalSummary() {
        return apiGet('/api/project/' + PROJECT + '/evaluations/summary').then(function(data) {
            if (data && !data.error) {
                evalSummaryCache = data.summary || {};
                evalChapterCache = data.by_chapter || {};
            }
            return evalSummaryCache;
        });
    }

    function buildFeedbackMap(feedbackRecords) {
        // Build a lookup keyed by "<eval_name>\x00<issue_index>" -> feedback_type.
        // Feedback is append-only; the last record for a given key wins so
        // that users can "correct" their label.
        var map = {};
        if (!feedbackRecords || !feedbackRecords.length) return map;
        for (var i = 0; i < feedbackRecords.length; i++) {
            var r = feedbackRecords[i];
            if (!r || !r.eval_name) continue;
            var key = r.eval_name + '\x00' + (r.issue_index != null ? r.issue_index : 0);
            map[key] = r.feedback_type || 'labeled';
        }
        return map;
    }

    function renderEvalCard(chunkId, evaluation) {
        var container = document.getElementById('eval-card-container-' + chunkId);
        if (!container) return;

        var isStale = !!evaluation.stale;
        var agg = evaluation.aggregated || {};
        var bySeverity = agg.issues_by_severity || {};
        var errors = isStale ? 0 : (bySeverity.error || 0);
        var warnings = isStale ? 0 : (bySeverity.warning || 0);
        var info = isStale ? 0 : (bySeverity.info || 0);
        var score = isStale ? null : agg.average_score;
        var issues = evaluation.normalized_issues || evaluation.issues || [];
        var feedbackMap = buildFeedbackMap(evaluation.feedback);

        var html = '<article class="eval-card">';
        if (isStale) {
            html += '<div class="eval-stale-banner" role="status">';
            html += 'Evaluation stale — translation changed';
            if (evaluation.stale_reason) {
                html += ': ' + escapeHtml(evaluation.stale_reason);
            }
            html += '. Re-run evaluators or the judge before trusting these findings.';
            html += '</div>';
        }
        html += '<header class="eval-card-header">';
        html += '<div class="eval-summary-chips">';
        if (isStale) {
            html += '<span class="eval-chip warnings">stale</span>';
        } else {
            if (errors > 0) html += '<span class="eval-chip errors">✗ ' + errors + '</span>';
            if (warnings > 0) html += '<span class="eval-chip warnings">⚠ ' + warnings + '</span>';
            if (info > 0) html += '<span class="eval-chip info">ℹ ' + info + '</span>';
            if (errors === 0 && warnings === 0 && info === 0) {
                html += '<span class="eval-chip pass">✓ all passed</span>';
            }
        }
        if (score !== null && score !== undefined) {
            html += '<span class="eval-chip score">score ' + score.toFixed(2) + '</span>';
        }
        html += '</div>';
        html += '<div class="eval-card-actions">';
        html += '<button class="btn-secondary" data-action="eval-rerun" data-chunk-id="' + escapeHtml(chunkId) + '">Rerun evaluators</button>';
        html += '<button class="btn-secondary" data-action="eval-llm-judge" data-chunk-id="' + escapeHtml(chunkId) + '">Run LLM judge</button>';
        html += '</div>';
        html += '</header>';

        html += '<div class="eval-card-body">';
        if (isStale) {
            html += '<div class="eval-empty">Findings hidden — translation changed since last evaluation. Rerun evaluators or the judge.</div>';
        } else if (issues.length === 0) {
            html += '<div class="eval-empty">All evaluators passed.</div>';
        } else {
            html += renderEvalSections(chunkId, issues, feedbackMap);
        }

        // Add LLM judge section if present
        if (evaluation.llm_judge) {
            html += renderLlmJudgeSection(evaluation.llm_judge);
        }
        html += '</div>';
        html += '</article>';

        container.innerHTML = html;

        bindEvalCardHandlers(chunkId, container);
    }

    function renderEvalSections(chunkId, issues, feedbackMap) {
        // Group issues by eval_name
        var grouped = {};
        issues.forEach(function(issue) {
            var name = issue.eval_name || 'unknown';
            if (!grouped[name]) grouped[name] = [];
            grouped[name].push(issue);
        });

        var html = '';
        // Render in canonical evaluator order, then any extras
        var allNames = EVAL_NAMES.slice();
        Object.keys(grouped).forEach(function(name) {
            if (allNames.indexOf(name) === -1) allNames.push(name);
        });
        allNames.forEach(function(name) {
            if (!grouped[name]) return;
            var list = grouped[name];
            var sevCounts = { error: 0, warning: 0, info: 0 };
            list.forEach(function(i) {
                if (sevCounts[i.severity] !== undefined) sevCounts[i.severity] += 1;
            });
            html += '<section class="eval-section" data-eval-name="' + escapeHtml(name) + '">';
            html += '<header class="eval-section-header" data-action="toggle-section">';
            html += '<h5>' + escapeHtml(name) + ' <span class="eval-section-counts">';
            html += (sevCounts.error ? sevCounts.error + ' err ' : '');
            html += (sevCounts.warning ? sevCounts.warning + ' warn ' : '');
            html += (sevCounts.info ? sevCounts.info + ' info' : '');
            html += '</span></h5>';
            html += '<span>' + list.length + '</span>';
            html += '</header>';
            html += '<div class="eval-section-body">';
            list.forEach(function(issue, idx) {
                html += renderIssueRow(chunkId, name, issue, idx, feedbackMap);
            });
            html += '</div>';
            html += '</section>';
        });
        return html;
    }

    function renderIssueRow(chunkId, evalName, issue, idx, feedbackMap) {
        var sev = issue.severity || 'info';
        var sevIcon = sev === 'error' ? '✗' : sev === 'warning' ? '⚠' : 'ℹ';
        var issueIdx = issue.issue_index !== undefined ? issue.issue_index : idx;
        var feedbackType = feedbackMap
            ? feedbackMap[evalName + '\x00' + issueIdx]
            : undefined;
        var labeledClass = feedbackType ? ' labeled' : '';
        var html = '<div class="eval-issue severity-' + escapeHtml(sev) + labeledClass + '" ' +
            'data-eval-name="' + escapeHtml(evalName) + '" ' +
            'data-issue-index="' + issueIdx + '"' +
            (feedbackType ? ' data-feedback-type="' + escapeHtml(feedbackType) + '"' : '') +
            '>';

        html += '<div class="eval-issue-head">';
        html += '<span class="eval-severity-icon ' + escapeHtml(sev) + '">' + sevIcon + '</span>';
        html += '<span class="eval-evaluator-tag">' + escapeHtml(evalName) + '</span>';
        html += '<span class="eval-issue-message">' + escapeHtml(issue.message || '') + '</span>';
        html += '</div>';

        // Context line
        var loc = issue.location;
        if (loc && (loc.snippet_before || loc.match || loc.snippet_after)) {
            html += '<div class="eval-issue-context">…' +
                escapeHtml(loc.snippet_before || '') +
                '<mark>' + escapeHtml(loc.match || '') + '</mark>' +
                escapeHtml(loc.snippet_after || '') + '…</div>';
        } else if (loc && loc.paragraph_text) {
            html += '<div class="eval-issue-context">' + escapeHtml(loc.paragraph_text) + '</div>';
        } else {
            html += '<div class="eval-issue-context no-location">(no location — evaluator gap)</div>';
        }

        if (issue.suggestion) {
            html += '<div class="eval-issue-suggestion">\uD83D\uDCA1 ' + formatSuggestionHtml(issue.suggestion) + '</div>';
        }

        html += '<div class="eval-issue-actions">';
        var feedbackTypes = [
            { type: 'false_positive', label: 'false positive' },
            { type: 'bad_message', label: 'bad message' },
            { type: 'missing_context_gap', label: 'gap' },
        ];
        feedbackTypes.forEach(function(ft) {
            var extra = feedbackType === ft.type ? ' labeled' : '';
            html += '<button class="eval-feedback-btn' + extra + '" data-action="feedback" data-type="' +
                ft.type + '">' + ft.label + '</button>';
        });
        html += '<button class="eval-raw-toggle" data-action="toggle-raw">raw</button>';
        if (feedbackType) {
            html += '<span class="eval-feedback-tag" title="previous feedback">labeled: ' +
                escapeHtml(feedbackType.replace(/_/g, ' ')) + '</span>';
        }
        html += '<span class="eval-feedback-flash" style="display:none"></span>';
        html += '</div>';

        // Raw metadata (hidden by default)
        var rawParts = [];
        if (loc && loc.raw_location) rawParts.push('location: ' + loc.raw_location);
        if (issue.metadata_excerpt) {
            try {
                var meta = JSON.stringify(issue.metadata_excerpt, null, 2);
                if (meta && meta !== '{}') rawParts.push('metadata: ' + meta);
            } catch (e) {}
        }
        if (rawParts.length) {
            html += '<pre class="eval-issue-raw" style="display:none">' + escapeHtml(rawParts.join('\n')) + '</pre>';
        }

        html += '</div>';
        return html;
    }

    function renderLlmJudgeSection(judge) {
        var html = '<section class="eval-section" data-eval-name="llm_judge">';
        html += '<header class="eval-section-header">';
        html += '<h5>LLM judge';
        if (judge.score !== null && judge.score !== undefined) {
            html += ' <span class="eval-section-counts">score ' + Number(judge.score).toFixed(2) + '</span>';
        }
        html += '</h5>';
        html += '</header>';
        html += '<div class="eval-section-body">';
        if (judge.error) {
            html += '<div class="eval-empty">Error: ' + escapeHtml(judge.error) + '</div>';
        } else if (judge.issues && judge.issues.length) {
            judge.issues.forEach(function(issue, idx) {
                html += renderIssueRow('', 'llm_judge', issue, idx);
            });
        } else if (judge.notes) {
            html += '<div class="eval-issue-suggestion">' + formatSuggestionHtml(judge.notes) + '</div>';
        } else {
            html += '<div class="eval-empty">No notes from LLM judge.</div>';
        }
        html += '</div>';
        html += '</section>';
        return html;
    }

    function bindEvalCardHandlers(chunkId, container) {
        // Rerun
        var rerunBtn = container.querySelector('[data-action="eval-rerun"]');
        if (rerunBtn) {
            rerunBtn.addEventListener('click', function() {
                rerunBtn.disabled = true;
                rerunBtn.textContent = 'Rerunning...';
                apiPost('/api/project/' + PROJECT + '/evaluations/' + chunkId + '/rerun', {})
                    .then(function(data) {
                        rerunBtn.disabled = false;
                        rerunBtn.textContent = 'Rerun evaluators';
                        if (data && !data.error) {
                            renderEvalCard(chunkId, data.evaluation || data);
                            refreshEvalSummary().then(updateChapterTableBadges);
                        } else {
                            alert('Rerun failed: ' + (data.error || 'unknown error'));
                        }
                    });
            });
        }

        // LLM judge
        var judgeBtn = container.querySelector('[data-action="eval-llm-judge"]');
        if (judgeBtn) {
            judgeBtn.addEventListener('click', function() {
                judgeBtn.disabled = true;
                judgeBtn.textContent = 'Running LLM judge...';
                apiPost('/api/project/' + PROJECT + '/evaluations/' + chunkId + '/llm_judge', {})
                    .then(function(data) {
                        judgeBtn.disabled = false;
                        judgeBtn.textContent = 'Run LLM judge';
                        if (data && !data.error) {
                            renderEvalCard(chunkId, data);
                        } else {
                            alert('LLM judge failed: ' + (data.error || 'unknown error'));
                        }
                    });
            });
        }

        // Section toggles
        container.querySelectorAll('[data-action="toggle-section"]').forEach(function(header) {
            header.addEventListener('click', function() {
                var section = header.closest('.eval-section');
                if (section) section.classList.toggle('collapsed');
            });
        });

        // Raw toggles
        container.querySelectorAll('[data-action="toggle-raw"]').forEach(function(btn) {
            btn.addEventListener('click', function(e) {
                e.stopPropagation();
                var issueEl = btn.closest('.eval-issue');
                if (!issueEl) return;
                var raw = issueEl.querySelector('.eval-issue-raw');
                if (raw) raw.style.display = raw.style.display === 'none' ? 'block' : 'none';
            });
        });

        // Feedback
        container.querySelectorAll('[data-action="feedback"]').forEach(function(btn) {
            btn.addEventListener('click', function(e) {
                e.stopPropagation();
                var issueEl = btn.closest('.eval-issue');
                if (!issueEl) return;
                var evalName = issueEl.dataset.evalName;
                var issueIdx = parseInt(issueEl.dataset.issueIndex, 10);
                var feedbackType = btn.dataset.type;
                btn.disabled = true;
                apiPost('/api/project/' + PROJECT + '/evaluations/' + chunkId + '/feedback', {
                    eval_name: evalName,
                    issue_index: isNaN(issueIdx) ? 0 : issueIdx,
                    feedback_type: feedbackType,
                }).then(function(data) {
                    btn.disabled = false;
                    if (data && !data.error) {
                        // Clear any previously-labeled sibling so only the
                        // current choice is highlighted (matches post-reload
                        // rendering, which shows only the latest feedback).
                        issueEl.querySelectorAll('.eval-feedback-btn.labeled').forEach(function(b) {
                            if (b !== btn) b.classList.remove('labeled');
                        });
                        btn.classList.add('labeled');
                        issueEl.classList.add('labeled');
                        issueEl.dataset.feedbackType = feedbackType;

                        // Update (or insert) the "labeled: <type>" tag so the
                        // visual state matches what a reload would show.
                        var actions = btn.parentNode;
                        var tag = actions.querySelector('.eval-feedback-tag');
                        var tagText = 'labeled: ' + feedbackType.replace(/_/g, ' ');
                        if (tag) {
                            tag.textContent = tagText;
                        } else {
                            tag = document.createElement('span');
                            tag.className = 'eval-feedback-tag';
                            tag.title = 'previous feedback';
                            tag.textContent = tagText;
                            var flashEl = actions.querySelector('.eval-feedback-flash');
                            if (flashEl) actions.insertBefore(tag, flashEl);
                            else actions.appendChild(tag);
                        }

                        var flash = issueEl.querySelector('.eval-feedback-flash');
                        if (flash) {
                            flash.textContent = '✓ thanks, logged';
                            flash.style.display = 'inline';
                            setTimeout(function() { flash.style.display = 'none'; }, 2500);
                        }
                    } else {
                        alert('Feedback failed: ' + (data.error || 'unknown error'));
                    }
                });
            });
        });
    }

    function updateChapterTableBadges() {
        if (!evalChapterCache) return;
        var tbody = document.getElementById('translate-chapter-tbody');
        if (!tbody) return;
        tbody.querySelectorAll('tr[data-chapter-id]').forEach(function(tr) {
            var cid = tr.dataset.chapterId;
            var sum = evalChapterCache[cid];
            var existing = tr.querySelector('.eval-badge-container');
            if (existing) existing.remove();
            if (!sum || (sum.errors === 0 && sum.warnings === 0 && !sum.stale)) return;
            var nameCell = tr.children[1];
            if (!nameCell) return;
            var span = document.createElement('span');
            span.className = 'eval-badge-container';
            var h = '';
            if (sum.stale > 0) h += '<span class="eval-badge warnings">stale</span>';
            if (sum.errors > 0) h += '<span class="eval-badge errors">✗ ' + sum.errors + '</span>';
            if (sum.warnings > 0) h += '<span class="eval-badge warnings">⚠ ' + sum.warnings + '</span>';
            span.innerHTML = h;
            nameCell.appendChild(span);
        });
    }

    // ========================================================================
    // Stage 6: Translate
    // ========================================================================

    var expandedChapter = null;
    var chunkCache = {};  // chapter_id -> [chunk data]

    function populateTranslateStage(status) {
        var tbody = document.getElementById('translate-chapter-tbody');
        // If chunk-detail-container was moved into the tbody, rescue it before wiping
        var container = document.getElementById('chunk-detail-container');
        if (container && tbody.contains(container)) {
            document.getElementById('stage-translate').appendChild(container);
            container.innerHTML = '';
        }
        expandedChapter = null;
        tbody.innerHTML = '';

        if (!status.chapters || status.chapters.length === 0) {
            tbody.innerHTML = '<tr><td colspan="6">No chapters available. Split and chunk first.</td></tr>';
            return;
        }

        status.chapters.forEach(function(ch) {
            var total = ch.chunk_count || 0;
            var translated = ch.translated_count || 0;
            var statusClass = total === 0 ? 'pending'
                : translated === total ? 'done'
                : translated > 0 ? 'partial'
                : 'pending';
            var statusLabel = total === 0 ? 'no chunks'
                : translated === total ? 'done'
                : translated > 0 ? 'partial'
                : 'pending';

            var tr = document.createElement('tr');
            tr.dataset.chapterId = ch.id;
            tr.innerHTML =
                '<td><input type="checkbox" class="ch-select" value="' + ch.id + '"' +
                    (total === 0 ? ' disabled' : '') + '></td>' +
                '<td>' + escapeHtml(ch.name) + '</td>' +
                '<td>' + total + '</td>' +
                '<td>' + translated + '/' + total + '</td>' +
                '<td><span class="status-pill ' + statusClass + '">' + statusLabel + '</span></td>' +
                '<td>' +
                    (total > 0 ? '<button class="btn-secondary ch-expand" style="padding:3px 10px;font-size:12px">Expand</button> ' : '') +
                    (ch.has_alignment ? '<a href="/read/' + PROJECT + '/' + ch.id + '" target="_blank" class="btn-secondary" style="padding:3px 10px;font-size:12px;text-decoration:none">Read</a>' : '') +
                '</td>';
            tbody.appendChild(tr);

            // Expand handler
            var expandBtn = tr.querySelector('.ch-expand');
            if (expandBtn) {
                expandBtn.addEventListener('click', function() {
                    toggleChapterExpand(ch.id, tr);
                });
            }
        });

        updateBatchButtonState();
        refreshEvalSummary().then(updateChapterTableBadges);
    }

    // Select all checkbox
    document.getElementById('select-all-chapters').addEventListener('change', function() {
        var checked = this.checked;
        document.querySelectorAll('.ch-select:not(:disabled)').forEach(function(cb) {
            cb.checked = checked;
        });
        updateBatchButtonState();
    });

    // Update batch button when checkboxes change
    document.getElementById('translate-chapter-tbody').addEventListener('change', function(e) {
        if (e.target.classList.contains('ch-select')) {
            updateBatchButtonState();
        }
    });

    function updateBatchButtonState() {
        var anyChecked = document.querySelectorAll('.ch-select:checked').length > 0;
        document.getElementById('btn-batch-translate').disabled = !anyChecked;
        document.getElementById('btn-batch-api').disabled = !anyChecked;
    }

    function getSelectedChapterIds() {
        var ids = [];
        document.querySelectorAll('.ch-select:checked').forEach(function(cb) {
            ids.push(cb.value);
        });
        return ids;
    }

    // Expand/collapse chapter to show chunks
    function toggleChapterExpand(chapterId, tr) {
        var container = document.getElementById('chunk-detail-container');

        if (expandedChapter === chapterId) {
            container.innerHTML = '';
            expandedChapter = null;
            return;
        }

        expandedChapter = chapterId;
        container.innerHTML = '<div class="chunk-detail"><span class="spinner"></span> Loading chunks...</div>';

        // Move detail after the clicked row
        tr.parentNode.insertBefore(createPlaceholder(container), tr.nextSibling);

        apiGet('/api/project/' + PROJECT + '/chapters/' + chapterId + '/chunks').then(function(data) {
            if (data.error) {
                container.innerHTML = '<div class="chunk-detail">' + escapeHtml(data.error) + '</div>';
                return;
            }
            chunkCache[chapterId] = data.chunks;
            renderChunkDetail(chapterId, data.chunks);
        });
    }

    function createPlaceholder(container) {
        // We need a table row to hold the detail below the chapter row
        var detailRow = document.createElement('tr');
        detailRow.id = 'chunk-detail-row';
        var td = document.createElement('td');
        td.colSpan = 6;
        td.style.padding = '0';
        td.appendChild(container);
        detailRow.appendChild(td);
        return detailRow;
    }

    function renderChunkDetail(chapterId, chunks) {
        var container = document.getElementById('chunk-detail-container');
        var html = '<div class="chunk-detail">';
        html += '<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px">';
        html += '<strong>' + escapeHtml(chapterId) + ': ' + chunks.length + ' chunks</strong>';
        html += '<button class="btn-secondary" onclick="document.getElementById(\'chunk-detail-container\').innerHTML=\'\'" style="padding:3px 10px;font-size:12px">Collapse</button>';
        html += '</div>';

        // Chunk tabs
        html += '<div class="chunk-tabs">';
        chunks.forEach(function(chunk, i) {
            var isTranslated = chunk.has_translation;
            html += '<button class="chunk-tab' +
                (i === 0 ? ' active' : '') +
                (isTranslated ? ' translated' : '') +
                '" data-chunk-index="' + i + '">' +
                'Chunk ' + i +
                (isTranslated ? ' &bull;' : '') +
                '</button>';
        });
        html += '</div>';

        // Chunk content area
        html += '<div id="chunk-content-area"></div>';
        html += '</div>';

        container.innerHTML = html;

        // Tab click handlers
        container.querySelectorAll('.chunk-tab').forEach(function(tab) {
            tab.addEventListener('click', function() {
                container.querySelectorAll('.chunk-tab').forEach(function(t) { t.classList.remove('active'); });
                tab.classList.add('active');
                var idx = parseInt(tab.dataset.chunkIndex, 10);
                loadChunkContent(chapterId, chunks[idx], idx);
            });
        });

        // Load first chunk
        if (chunks.length > 0) {
            loadChunkContent(chapterId, chunks[0], 0);
        }
    }

    function loadChunkContent(chapterId, chunk, index) {
        var area = document.getElementById('chunk-content-area');

        var html = '';

        // Source text
        html += '<div class="chunk-section">';
        html += '<div class="chunk-section-header"><h4>Source Text</h4>';
        html += '<span style="font-size:12px;color:#999">' + (chunk.word_count || '?') + ' words</span></div>';
        html += '<div class="chunk-source-text">' + escapeHtml(chunk.source_text || '') + '</div>';
        html += '</div>';

        // Prompt
        html += '<div class="chunk-section">';
        html += '<div class="chunk-section-header"><h4>Translation Prompt</h4>';
        html += '<button class="btn-secondary chunk-copy-prompt" style="padding:3px 10px;font-size:12px">Copy Prompt</button></div>';
        html += '<textarea class="chunk-prompt-text" id="chunk-prompt-textarea" rows="6" readonly>Loading prompt...</textarea>';
        html += '</div>';

        // Translation
        html += '<div class="chunk-section">';
        html += '<div class="chunk-section-header"><h4>Translation</h4></div>';
        html += '<textarea class="chunk-translate-area" id="chunk-translate-textarea" placeholder="Paste translation here...">' +
            escapeHtml(chunk.translated_text || '') + '</textarea>';
        html += '<div class="llm-selector-row" style="margin-bottom:8px; display:flex; align-items:center; gap:8px; flex-wrap:wrap;">';
        html += '<label style="font-size:13px;color:#666">Provider</label>';
        html += '<select id="chunk-provider" class="llm-provider-select" style="padding:4px 8px;"></select>';
        html += '<label style="font-size:13px;color:#666">Model</label>';
        html += '<select id="chunk-model" class="llm-model-select" style="padding:4px 8px;"></select>';
        html += '</div>';
        html += '<div class="btn-row">';
        html += '<button class="btn-save" id="btn-save-chunk-translation">Save Translation</button>';
        html += '<button class="btn-primary" id="btn-auto-translate-chunk">Auto-Translate via API</button>';
        html += '<span class="status-msg" id="chunk-translate-status"></span>';
        html += '</div>';
        html += '</div>';

        // Evaluator card placeholder
        html += '<div id="eval-card-container-' + chunk.id + '" class="eval-card-container"></div>';

        area.innerHTML = html;

        // Load any existing evaluation for this chunk
        loadExistingEvaluation(chunk.id);

        populateProviderSelect('chunk-provider');
        populateModelSelect('chunk-provider', 'chunk-model');
        bindProviderModelPair('chunk-provider', 'chunk-model');

        // Load prompt
        apiGet('/api/project/' + PROJECT + '/chunks/' + chunk.id + '/prompt').then(function(data) {
            var ta = document.getElementById('chunk-prompt-textarea');
            if (ta) ta.value = data.prompt || data.error || '';
        });

        // Copy prompt
        var copyBtn = area.querySelector('.chunk-copy-prompt');
        copyBtn.addEventListener('click', function() {
            var ta = document.getElementById('chunk-prompt-textarea');
            if (!ta) return;
            var text = ta.value;
            var originalText = copyBtn.textContent;
            function showFeedback(ok) {
                copyBtn.textContent = ok ? 'Copied!' : 'Copy failed';
                setTimeout(function() { copyBtn.textContent = originalText; }, 1500);
            }
            if (navigator.clipboard && navigator.clipboard.writeText) {
                navigator.clipboard.writeText(text).then(function() {
                    showFeedback(true);
                }).catch(function() {
                    try { ta.select(); document.execCommand('copy'); showFeedback(true); }
                    catch (e) { showFeedback(false); }
                });
            } else {
                try { ta.select(); document.execCommand('copy'); showFeedback(true); }
                catch (e) { showFeedback(false); }
            }
        });

        // Save translation
        document.getElementById('btn-save-chunk-translation').addEventListener('click', function() {
            var text = document.getElementById('chunk-translate-textarea').value.trim();
            if (!text) {
                setStatus('chunk-translate-status', 'No translation text', 'error');
                return;
            }
            setStatus('chunk-translate-status', 'Saving...', '');
            apiPost('/api/project/' + PROJECT + '/chunks/' + chunk.id + '/translate', {
                translated_text: text,
            }).then(function(data) {
                if (data.error) {
                    setStatus('chunk-translate-status', data.error, 'error');
                } else {
                    setStatus('chunk-translate-status', 'Saved!', 'success');
                    // Update local cache
                    chunk.has_translation = true;
                    chunk.translated_text = text;
                    // Update tab appearance
                    var tab = document.querySelector('.chunk-tab[data-chunk-index="' + index + '"]');
                    if (tab) tab.classList.add('translated');
                    // Lightweight status refresh: update badges without rebuilding the translate stage
                    refreshStatusBadges();
                    if (data.evaluation) {
                        renderEvalCard(chunk.id, data.evaluation);
                        refreshEvalSummary().then(updateChapterTableBadges);
                    }
                }
            });
        });

        // Auto-translate
        document.getElementById('btn-auto-translate-chunk').addEventListener('click', function() {
            setStatus('chunk-translate-status', 'Translating via API...', '');
            this.disabled = true;
            var btn = this;
            apiPost('/api/project/' + PROJECT + '/translate/realtime', {
                chunk_id: chunk.id,
                provider: document.getElementById('chunk-provider').value,
                model: document.getElementById('chunk-model').value,
            }).then(function(data) {
                btn.disabled = false;
                if (data.error) {
                    setStatus('chunk-translate-status', data.error, 'error');
                } else {
                    document.getElementById('chunk-translate-textarea').value = data.translated_text || '';
                    setStatus('chunk-translate-status', 'Done!', 'success');
                    chunk.has_translation = true;
                    chunk.translated_text = data.translated_text;
                    var tab = document.querySelector('.chunk-tab[data-chunk-index="' + index + '"]');
                    if (tab) tab.classList.add('translated');
                    // Lightweight status refresh: update badges without rebuilding the translate stage
                    refreshStatusBadges();
                    if (data.evaluation) {
                        renderEvalCard(chunk.id, data.evaluation);
                        refreshEvalSummary().then(updateChapterTableBadges);
                    }
                }
            });
        });
    }

    // ── Batch translation ──

    document.getElementById('btn-batch-translate').addEventListener('click', function() {
        resetBatchProgressUI();
        document.getElementById('batch-modal').classList.add('visible');
        updateBatchCostEstimate();
    });

    document.getElementById('btn-translate-all-untranslated').addEventListener('click', function() {
        // Select all chapters with untranslated chunks
        document.querySelectorAll('.ch-select:not(:disabled)').forEach(function(cb) {
            var tr = cb.closest('tr');
            var statusPill = tr.querySelector('.status-pill');
            if (statusPill && !statusPill.classList.contains('done')) {
                cb.checked = true;
            }
        });
        updateBatchButtonState();
        resetBatchProgressUI();
        document.getElementById('batch-modal').classList.add('visible');
        updateBatchCostEstimate();
    });

    document.getElementById('batch-modal-close').addEventListener('click', closeBatchModal);
    document.getElementById('batch-modal').addEventListener('click', function(e) {
        if (e.target === this) closeBatchModal();
    });

    // Restore the batch-translate modal's progress UI to its pristine state so a
    // previous run's "Complete!" bar can't linger into the next translation.
    function resetBatchProgressUI() {
        var prog = document.getElementById('batch-progress');
        if (prog) prog.style.display = 'none';
        var txt = document.getElementById('batch-progress-text');
        if (txt) { txt.textContent = 'Translating...'; txt.style.color = ''; }
        var fill = document.getElementById('batch-progress-fill');
        if (fill) { fill.style.width = '0%'; fill.classList.remove('failed'); }
        // The error banner and spinner are the two bits a failed run leaves
        // behind; without clearing them the next run opens pre-failed.
        var errEl = document.getElementById('batch-error-detail');
        if (errEl) { errEl.textContent = ''; errEl.style.display = 'none'; }
        var spinner = document.querySelector('#batch-progress .spinner');
        if (spinner) spinner.style.display = '';
        var start = document.getElementById('btn-start-batch');
        if (start) start.disabled = false;
        var cancel = document.getElementById('btn-cancel-batch');
        if (cancel) cancel.style.display = 'none';
    }

    function closeBatchModal() {
        document.getElementById('batch-modal').classList.remove('visible');
        resetBatchProgressUI();
    }

    // (provider/model dropdown setup is handled by initAllLLMSelectors)

    // True once a cost-estimate response has populated the always_include_* checkboxes
    // from the server's suggestions. Until then the Start handler must not send explicit
    // flag values — an unresolved (or errored) estimate would otherwise ship `false` and
    // override the saved per-book config / whole-book auto-enable.
    var batchSuggestionsReady = false;

    function updateBatchCostEstimate() {
        var ids = getSelectedChapterIds();
        var provider = document.getElementById('batch-provider').value;
        var model = document.getElementById('batch-model').value;
        var el = document.getElementById('batch-cost-estimate');
        var featEl = document.getElementById('batch-feature-summary');
        var startBtn = document.getElementById('btn-start-batch');
        el.textContent = 'Estimating cost...';
        if (featEl) featEl.textContent = '';
        batchSuggestionsReady = false;
        if (startBtn) startBtn.disabled = true;

        apiPost('/api/project/' + PROJECT + '/translate/cost-estimate', {
            chapter_ids: ids,
            provider: provider,
            model: model,
            include_translated: true,
        }).then(function(data) {
            if (startBtn) startBtn.disabled = false;
            if (data.error) {
                el.textContent = data.error;
            } else {
                var cost = (data.estimated_cost || 0);
                var already = data.already_translated_count || 0;
                var total = data.total_chunks || data.chunk_count || 0;
                var dialogueN = data.dialogue_chunk_count || 0;
                var imageN = data.image_chunk_count || 0;
                if (featEl) {
                    featEl.textContent =
                        dialogueN + ' of ' + total + ' selected chunks contain dialogue · ' +
                        imageN + ' of ' + total + ' contain image placeholders';
                }
                var dialogueCb = document.getElementById('batch-always-dialogue');
                var imagesCb = document.getElementById('batch-always-images');
                if (dialogueCb) dialogueCb.checked = !!data.suggested_always_dialogue;
                if (imagesCb) imagesCb.checked = !!data.suggested_always_images;
                batchSuggestionsReady = true;
                var html = '<strong>' + (data.chunk_count || 0) + '</strong> chunks to translate<br>' +
                    'Estimated cost: <strong>$' + cost.toFixed(4) + '</strong>' +
                    '<br><span style="font-size:12px;color:#666">Batch API would be $' +
                    (cost * 0.5).toFixed(4) + ' (50% off)</span>';
                if (already > 0) {
                    html += '<br><span style="color:#b45309;font-size:13px;">' +
                        already + ' chunk(s) already translated — existing translations will be replaced.</span>';
                }
                el.innerHTML = html;
            }
        });
    }

    // Start batch translation
    document.getElementById('btn-start-batch').addEventListener('click', function() {
        var ids = getSelectedChapterIds();
        var provider = document.getElementById('batch-provider').value;
        var model = document.getElementById('batch-model').value;
        var alwaysDialogue = document.getElementById('batch-always-dialogue');
        var alwaysImages = document.getElementById('batch-always-images');

        this.disabled = true;
        document.getElementById('batch-progress').style.display = '';
        document.getElementById('btn-cancel-batch').style.display = '';

        var payload = {
            chapter_ids: ids,
            provider: provider,
            model: model,
            include_translated: true,
            enable_thinking: thinkingChecked('batch-thinking'),
        };
        // Only send explicit flag values once the estimate has populated the
        // checkboxes from server suggestions; otherwise omit them so the backend
        // falls back to the saved config / whole-book auto-enable.
        if (batchSuggestionsReady) {
            payload.always_include_dialogue = alwaysDialogue ? !!alwaysDialogue.checked : false;
            payload.always_include_image_instructions = alwaysImages ? !!alwaysImages.checked : false;
        }

        apiPost('/api/project/' + PROJECT + '/translate/batch', payload).then(function(data) {
            if (data.error) {
                setStatus('translate-batch-status', data.error, 'error');
                return;
            }
            // Connect SSE for progress
            startBatchSSE(data.job_id, data.total_chunks || 0);
        });
    });

    var batchEventSource = null;

    function startBatchSSE(jobId, totalChunks) {
        var completed = 0;
        var failed = 0;
        var firstError = '';
        var cacheReadTotal = 0;
        var cacheCreatedTotal = 0;
        var evalsTotal = 0;
        var evalsDone = 0;
        batchEventSource = new EventSource('/api/project/' + PROJECT + '/translate/sse?job_id=' + jobId);

        // "Translated 2/4" alone reads as success even when the other two blew
        // up, so the failure count rides along as soon as there is one.
        function progressLabel(prefix) {
            var txt = prefix + ' ' + completed + '/' + totalChunks;
            if (failed > 0) txt += ' · ' + failed + ' failed';
            return txt;
        }

        batchEventSource.addEventListener('chunk_done', function(e) {
            completed++;
            var pct = totalChunks > 0 ? Math.round((completed + failed) / totalChunks * 100) : 0;
            try {
                var payload = JSON.parse(e.data);
                cacheReadTotal += (payload.cache_read_input_tokens || 0);
                cacheCreatedTotal += (payload.cache_creation_input_tokens || 0);
            } catch (err) { /* ignore */ }
            document.getElementById('batch-progress-text').textContent = progressLabel('Translated');
            document.getElementById('batch-progress-fill').style.width = pct + '%';
        });

        batchEventSource.addEventListener('chunk_error', function(e) {
            failed++;
            var data = {};
            try { data = JSON.parse(e.data); } catch (err) { /* ignore */ }
            console.error('Chunk error:', data);
            if (!firstError) {
                firstError = (data.chunk_id ? data.chunk_id + ': ' : '') +
                    (data.error || 'unknown error');
            }
            // Advance the bar on failures too. A stalled bar is the one thing a
            // run where every chunk fails must not look like.
            var pct = totalChunks > 0 ? Math.round((completed + failed) / totalChunks * 100) : 0;
            document.getElementById('batch-progress-fill').style.width = pct + '%';
            document.getElementById('batch-progress-text').textContent = progressLabel('Translated');
        });

        batchEventSource.addEventListener('evals_started', function(e) {
            try {
                var payload = JSON.parse(e.data);
                evalsTotal = payload.total || 0;
            } catch (err) { /* ignore */ }
            evalsDone = 0;
            document.getElementById('batch-progress-text').textContent =
                'Running checks ' + evalsDone + '/' + evalsTotal + '…';
        });

        batchEventSource.addEventListener('chunk_evaluated', function() {
            evalsDone++;
            document.getElementById('batch-progress-text').textContent =
                'Running checks ' + evalsDone + '/' + evalsTotal + '…';
        });

        batchEventSource.addEventListener('batch_complete', function(e) {
            batchEventSource.close();
            batchEventSource = null;
            var payload = {};
            try { payload = JSON.parse(e.data); } catch (err) { /* ignore */ }

            // Trust the server's tally over the events we happened to catch —
            // a reconnect mid-run can cost us a chunk_error.
            var errorCount = payload.error_count != null ? payload.error_count : failed;
            var okCount = payload.translated_count != null ? payload.translated_count : completed;
            // The job can die after every chunk landed — a crash in the eval or
            // align phase. Counts alone read as a clean run there, so the abort
            // has to be its own signal or the modal says "Complete!" of a run
            // that did not complete.
            var fatal = payload.fatal || '';
            if (payload.errors && payload.errors.length && !firstError) {
                firstError = payload.errors[0];
            }
            if (fatal && !firstError) firstError = fatal;

            document.getElementById('btn-start-batch').disabled = false;
            document.getElementById('btn-cancel-batch').style.display = 'none';
            var textEl = document.getElementById('batch-progress-text');
            var errEl = document.getElementById('batch-error-detail');

            if (errorCount > 0 || fatal) {
                // Nothing auto-closes on a failed run. The modal is the only
                // place the reason exists, and closing it throws the reason away.
                document.getElementById('batch-progress-fill').style.width = '100%';
                document.getElementById('batch-progress-fill').classList.add('failed');
                var spinner = document.querySelector('#batch-progress .spinner');
                if (spinner) spinner.style.display = 'none';
                textEl.textContent = okCount + ' of ' + totalChunks + ' translated · ' +
                    (errorCount > 0 ? errorCount + ' failed' : 'run did not finish');
                textEl.style.color = '#b91c1c';
                if (errEl) {
                    errEl.textContent = firstError || 'See the server log for details.';
                    errEl.style.display = '';
                }
                try { loadStatus(); } catch (err) { console.error('loadStatus after batch failed:', err); }
                return;
            }

            var msg = 'Complete!';
            var read = payload.cache_read_input_tokens || cacheReadTotal || 0;
            var created = payload.cache_creation_input_tokens || cacheCreatedTotal || 0;
            if (read > 0) {
                msg += ' Cache saved ~' + read.toLocaleString() + ' input tokens';
                if (created > 0) {
                    msg += ' (' + created.toLocaleString() + ' written)';
                }
            }
            textEl.textContent = msg;
            // Schedule the close before loadStatus() so a rebuild error can't strand the modal open.
            setTimeout(closeBatchModal, 1500);
            try { loadStatus(); } catch (err) { console.error('loadStatus after batch failed:', err); }
        });

        batchEventSource.onerror = function() {
            batchEventSource.close();
            batchEventSource = null;
            document.getElementById('batch-progress-text').textContent = 'Connection lost';
            document.getElementById('btn-start-batch').disabled = false;
        };
    }

    document.getElementById('btn-cancel-batch').addEventListener('click', function() {
        if (batchEventSource) {
            batchEventSource.close();
            batchEventSource = null;
        }
        // TODO: signal server to cancel
        document.getElementById('batch-progress').style.display = 'none';
        this.style.display = 'none';
        document.getElementById('btn-start-batch').disabled = false;
    });

    // ── Batch API (async, 50% discount) ──

    document.getElementById('btn-batch-api').addEventListener('click', function() {
        document.getElementById('batch-api-modal').classList.add('visible');
        updateBatchApiCostEstimate();
    });

    document.getElementById('batch-api-modal-close').addEventListener('click', closeBatchApiModal);
    document.getElementById('batch-api-modal').addEventListener('click', function(e) {
        if (e.target === this) closeBatchApiModal();
    });

    function closeBatchApiModal() {
        document.getElementById('batch-api-modal').classList.remove('visible');
        document.getElementById('batch-api-submit-status').textContent = '';
    }

    function updateBatchApiCostEstimate() {
        var ids = getSelectedChapterIds();
        var provider = document.getElementById('batch-api-provider').value;
        var model = document.getElementById('batch-api-model').value;
        var el = document.getElementById('batch-api-cost-estimate');
        el.textContent = 'Estimating cost...';

        apiPost('/api/project/' + PROJECT + '/translate/cost-estimate', {
            chapter_ids: ids,
            provider: provider,
            model: model,
            include_translated: true,
        }).then(function(data) {
            if (data.error) {
                el.textContent = data.error;
            } else {
                var fullCost = (data.estimated_cost || 0);
                var batchCost = fullCost * 0.5;
                var already = data.already_translated_count || 0;
                var html = '<strong>' + (data.chunk_count || 0) + '</strong> chunks to translate<br>' +
                    'Realtime cost: $' + fullCost.toFixed(4) + '<br>' +
                    'Batch API cost: <strong>$' + batchCost.toFixed(4) + '</strong> (50% off)';
                if (already > 0) {
                    html += '<br><span style="color:#b45309;font-size:13px;">' +
                        already + ' chunk(s) already translated — existing translations will be replaced.</span>';
                }
                el.innerHTML = html;
            }
        });
    }

    document.getElementById('btn-submit-batch-api').addEventListener('click', function() {
        var ids = getSelectedChapterIds();
        var provider = document.getElementById('batch-api-provider').value;
        var model = document.getElementById('batch-api-model').value;
        var statusEl = document.getElementById('batch-api-submit-status');
        var btn = this;

        btn.disabled = true;
        statusEl.textContent = 'Submitting...';
        statusEl.className = 'status-msg';

        apiPost('/api/project/' + PROJECT + '/batch-api/submit', {
            chapter_ids: ids,
            provider: provider,
            model: model,
            include_translated: true,
            enable_thinking: thinkingChecked('batch-api-thinking'),
        }).then(function(data) {
            btn.disabled = false;
            if (data.error) {
                statusEl.textContent = data.error;
                statusEl.className = 'status-msg error';
            } else {
                statusEl.textContent = 'Batch submitted! Job ID: ' + data.job_id +
                    ' (' + data.chunk_count + ' chunks). Check back in 1-24 hours.';
                statusEl.className = 'status-msg success';
                loadBatchApiJobs();
            }
        });
    });

    // Batch API jobs panel

    function loadBatchApiJobs() {
        fetch('/api/project/' + PROJECT + '/batch-api/jobs')
            .then(function(r) { return r.json(); })
            .then(function(data) {
                var jobs = data.jobs || [];
                var panel = document.getElementById('batch-api-jobs-panel');
                var tbody = document.getElementById('batch-api-jobs-tbody');

                if (jobs.length === 0) {
                    panel.style.display = 'none';
                    return;
                }

                panel.style.display = '';
                tbody.innerHTML = '';

                jobs.forEach(function(job) {
                    var tr = document.createElement('tr');
                    var safeJobId = escapeHtml(job.job_id || '');
                    var statusClass = job.status === 'completed' ? 'done' :
                        (job.status === 'failed' ? 'error' : 'partial');

                    var submitted = job.submitted_at ? new Date(job.submitted_at).toLocaleString() : '—';

                    // Build action buttons using data attributes to avoid inline JS injection
                    var dismissBtn = document.createElement('button');
                    dismissBtn.className = 'btn-small';
                    dismissBtn.title = 'Remove from list';
                    dismissBtn.style.cssText = 'margin-left:6px;background:none;border:1px solid #cbd5e1;color:#64748b;padding:2px 6px;';
                    dismissBtn.textContent = '×';
                    dismissBtn.addEventListener('click', (function(id) {
                        return function() { dismissBatchApiJob(id); };
                    })(job.job_id));

                    var actionsTd = document.createElement('td');
                    if (job.status === 'completed') {
                        var retrievedSpan = document.createElement('span');
                        retrievedSpan.style.color = '#16a34a';
                        retrievedSpan.textContent = 'Retrieved';
                        actionsTd.appendChild(retrievedSpan);
                    } else if (job.status === 'ended') {
                        var retrieveBtn = document.createElement('button');
                        retrieveBtn.className = 'btn-small btn-primary';
                        retrieveBtn.textContent = 'Retrieve Results';
                        retrieveBtn.addEventListener('click', (function(id) {
                            return function() { retrieveBatchApiJob(id); };
                        })(job.job_id));
                        actionsTd.appendChild(retrieveBtn);
                    } else {
                        var checkBtn = document.createElement('button');
                        checkBtn.className = 'btn-small btn-secondary';
                        checkBtn.textContent = 'Check Status';
                        checkBtn.addEventListener('click', (function(id) {
                            return function() { checkBatchApiJob(id); };
                        })(job.job_id));
                        actionsTd.appendChild(checkBtn);
                    }
                    if (job.status === 'completed') {
                        actionsTd.appendChild(dismissBtn);
                    }

                    tr.innerHTML =
                        '<td><code style="font-size:12px">' + safeJobId.substring(0, 16) + '</code></td>' +
                        '<td>' + escapeHtml(job.provider || '') + ' / ' + escapeHtml((job.model || '').split('/').pop()) + '</td>' +
                        '<td>' + (job.chunk_count || 0) + '</td>' +
                        '<td><span class="status-pill ' + escapeHtml(statusClass) + '">' + escapeHtml(job.status || 'unknown') + '</span></td>' +
                        '<td style="font-size:12px">' + escapeHtml(submitted) + '</td>';
                    tr.appendChild(actionsTd);
                    tbody.appendChild(tr);
                });
            });
    }

    window.dismissBatchApiJob = function(jobId) {
        fetch('/api/project/' + PROJECT + '/batch-api/jobs/' + jobId, { method: 'DELETE' })
            .then(function() { loadBatchApiJobs(); });
    };

    // Make these accessible from inline onclick
    window.checkBatchApiJob = function(jobId) {
        apiPost('/api/project/' + PROJECT + '/batch-api/jobs/' + jobId + '/check', {})
            .then(function(data) {
                if (data.error) {
                    setStatus('translate-batch-status', data.error, 'error');
                } else if (data.status === 'completed' || data.status === 'ended') {
                    setStatus('translate-batch-status', 'Batch complete! Click Retrieve Results.', 'success');
                } else {
                    setStatus('translate-batch-status',
                        'Status: ' + data.status +
                        ' (succeeded: ' + (data.succeeded_count || 0) +
                        ', failed: ' + (data.failed_count || 0) + ')', 'info');
                }
                loadBatchApiJobs();
            });
    };

    window.retrieveBatchApiJob = function(jobId) {
        setStatus('translate-batch-status', 'Retrieving batch results...', 'info');
        apiPost('/api/project/' + PROJECT + '/batch-api/jobs/' + jobId + '/retrieve', {})
            .then(function(data) {
                if (data.error) {
                    setStatus('translate-batch-status', data.error, 'error');
                } else {
                    var msg = 'Retrieved ' + data.translated_count + '/' + data.total_count +
                        ' translations. Chapters updated: ' + (data.chapters_affected || []).join(', ');
                    var el = document.getElementById('translate-batch-status');
                    if (el) {
                        el.textContent = msg;
                        el.className = 'status-msg success';
                        var clearBtn = document.createElement('button');
                        clearBtn.textContent = '×';
                        clearBtn.title = 'Dismiss';
                        clearBtn.style.cssText = 'margin-left:8px;background:none;border:none;cursor:pointer;font-size:16px;line-height:1;color:inherit;opacity:0.6;';
                        clearBtn.addEventListener('click', function() {
                            el.textContent = '';
                            el.className = 'status-msg';
                        });
                        el.appendChild(clearBtn);
                    }
                    loadStatus();
                }
                loadBatchApiJobs();
            });
    };

    // Load batch API jobs when entering translate stage
    loadBatchApiJobs();

    // ========================================================================
    // Stage 7: Review
    // ========================================================================

    // The Review stage renders from its own endpoint, not from /status: the
    // rollup opens and hashes every chunk in the book to decide what is stale,
    // which is far too much work to repeat on every dashboard poll. It is
    // fetched when the stage is opened and after anything that could change it.
    var REVIEW_TYPES = window.DASHBOARD_REVIEW_TYPES ||
        ['blacklist', 'grammar', 'dictionary', 'completeness', 'dialogue', 'address'];
    var REVIEW_LABELS = window.DASHBOARD_REVIEW_LABELS || {};
    // Short column-friendly tags for the finding chips; the full label is the
    // chip's tooltip.
    var REVIEW_ABBR = {
        blacklist: 'bl', grammar: 'gr', dictionary: 'dic',
        completeness: 'cmp', dialogue: 'dlg', address: 'adr',
    };
    var JUDGE_GROUPS = [
        { key: 'coded', tag: 'CD', label: 'Deterministic evaluators' },
        { key: 'dialogue', tag: 'DL', label: 'Dialogue judge' },
        { key: 'address', tag: 'AD', label: 'Address judge' },
    ];
    var JUDGE_STATE = {
        done:    { glyph: '✓', cls: 'pip-done', word: 'up to date' },
        partial: { glyph: '◑', cls: 'pip-partial', word: 'partly run' },
        stale:   { glyph: '⚠', cls: 'pip-stale', word: 'stale (text changed since)' },
        not_run: { glyph: '○', cls: 'pip-none', word: 'not run' },
    };

    var reviewStatus = null;          // last /review-status payload
    var reviewStatusLoading = false;
    var reviewStatusPending = false;  // a refresh asked for while one was in flight

    function reviewSelectedChapters() {
        var ids = [];
        document.querySelectorAll('#review-tbody .review-pick:checked').forEach(function(cb) {
            ids.push(cb.dataset.chapter);
        });
        return ids;
    }

    // null = whole book, which is what both run endpoints take for "no selection".
    function reviewScope() {
        var ids = reviewSelectedChapters();
        return ids.length ? ids : null;
    }

    function updateReviewScopeHint() {
        var hint = document.getElementById('review-scope-hint');
        if (!hint) return;
        var ids = reviewSelectedChapters();
        hint.textContent = ids.length
            ? (ids.length === 1 ? '1 chapter selected' : ids.length + ' chapters selected')
            : 'whole book';
    }

    function findingChipsHtml(ch) {
        var counts = ch.flag_counts || {};
        var html = '';
        REVIEW_TYPES.forEach(function(type) {
            var n = counts[type] || 0;
            if (!n) return;
            var label = REVIEW_LABELS[type] || type;
            html += '<a class="finding-chip finding-' + type +
                '" href="/read/' + PROJECT + '/' + encodeURIComponent(ch.id) + '?review=' + encodeURIComponent(type) +
                '" target="_blank" title="' + escapeHtml(n + ' · ' + label +
                ' — open in the reader') + '">' +
                n + ' ' + (REVIEW_ABBR[type] || type) + '</a> ';
        });
        return html || '<span class="review-muted">&mdash; clean</span>';
    }

    function judgePipsHtml(ch) {
        var judges = ch.judges || {};
        return JUDGE_GROUPS.map(function(g) {
            var info = judges[g.key] || { state: 'not_run', fresh: 0, stale: 0, missing: 0 };
            var meta = JUDGE_STATE[info.state] || JUDGE_STATE.not_run;
            var total = (info.fresh || 0) + (info.stale || 0) + (info.missing || 0);
            // "0/5 chunks fresh" on a judge that never ran reads like it ran and
            // came back empty. Only quote the fraction once something has run.
            var tip = g.label + ': ' + meta.word +
                (total && info.state !== 'not_run'
                    ? ' — ' + (info.fresh || 0) + '/' + total + ' chunks fresh' : '');
            return '<span class="judge-pip ' + meta.cls + '" title="' + escapeHtml(tip) + '">' +
                g.tag + meta.glyph + '</span>';
        }).join(' ');
    }

    function notesCellHtml(ch) {
        var ann = ch.annotations || {};
        var bits = [];
        if (ann.review) bits.push(ann.review + ' to review');
        if (ann.footnotes_total) {
            bits.push((ann.footnotes_filled || 0) + '/' + ann.footnotes_total + ' notes');
        }
        var html = bits.length ? escapeHtml(bits.join(' · ')) : '<span class="review-muted">&mdash;</span>';
        if (ch.gap_count) {
            html += ' <span class="review-gap" title="' + escapeHtml(
                ch.gap_chars + ' characters of source text with no translation') + '">' +
                ch.gap_count + ' gaps</span>';
        }
        return html;
    }

    function refreshReviewStatus() {
        // A refresh asked for mid-flight is queued, not dropped: a job's
        // `complete` handler fires one, and if it lands during the boot fetch
        // the table would otherwise keep showing pre-run findings until the
        // user left and re-entered the stage.
        if (reviewStatusLoading) {
            reviewStatusPending = true;
            return Promise.resolve(reviewStatus);
        }
        reviewStatusLoading = true;
        return apiGet('/api/project/' + PROJECT + '/review-status')
            .then(function(data) {
                reviewStatusLoading = false;
                if (data && data.ok) {
                    reviewStatus = data;
                    renderReviewTable(data);
                    updateReviewBadge(data);
                }
                return drainPendingReviewStatus(data);
            })
            .catch(function(e) {
                reviewStatusLoading = false;
                // Say so. A silent catch leaves an empty tbody that is
                // indistinguishable from a book with nothing to review.
                var tbody = document.getElementById('review-tbody');
                if (tbody && !tbody.querySelector('tr[data-chapter-id]')) {
                    tbody.innerHTML = '<tr><td colspan="6">Could not load review status: ' +
                        escapeHtml(String(e && e.message ? e.message : e)) + '</td></tr>';
                }
                return drainPendingReviewStatus(null);
            });
    }

    function drainPendingReviewStatus(data) {
        if (!reviewStatusPending) return data;
        reviewStatusPending = false;
        return refreshReviewStatus();
    }

    function updateReviewBadge(data) {
        var badge = document.getElementById('badge-review');
        if (!badge) return;
        var totals = (data && data.totals) || {};
        var findings = totals.findings || 0;
        var translated = totals.translated_count || 0;
        if (!translated) { badge.textContent = ''; return; }
        badge.textContent = findings
            ? findings + (findings === 1 ? ' finding' : ' findings')
            : '✓ clean';
    }

    function renderReviewTable(data) {
        var tbody = document.getElementById('review-tbody');
        if (!tbody) return;
        tbody.innerHTML = '';
        var alignAllBtn = document.getElementById('btn-align-all');
        var chapters = (data && data.chapters) || [];

        if (!chapters.length) {
            tbody.innerHTML = '<tr><td colspan="6">No chapters available.</td></tr>';
            if (alignAllBtn) alignAllBtn.style.display = 'none';
            return;
        }

        var unaligned = [];
        var rows = 0;

        chapters.forEach(function(ch) {
            var translated = ch.translated_count || 0;
            if (translated === 0 || !ch.chunk_count) return;
            rows++;
            if (!ch.has_alignment) unaligned.push(ch.id);
            var notes = (ch.annotations || {}).review || 0;

            var tr = document.createElement('tr');
            tr.dataset.chapterId = ch.id;
            tr.innerHTML =
                '<td><input type="checkbox" class="review-pick" data-chapter="' + escapeHtml(ch.id) + '"></td>' +
                '<td><div>' + escapeHtml(ch.display_label || ch.id) + '</div>' +
                    '<div class="review-muted">' + translated + '/' + ch.chunk_count + ' chunks</div></td>' +
                '<td>' + findingChipsHtml(ch) + '</td>' +
                '<td class="judge-cell">' + judgePipsHtml(ch) + '</td>' +
                '<td>' + notesCellHtml(ch) + '</td>' +
                '<td class="review-actions">' +
                    (!ch.has_alignment
                        ? '<button class="btn-primary ch-align" data-chapter="' + escapeHtml(ch.id) + '">Align</button> '
                        : '<button class="btn-secondary ch-realign" data-chapter="' + escapeHtml(ch.id) +
                          '" data-annotations="' + notes + '">Realign</button> ') +
                    '<a href="/read/' + PROJECT + '/' + encodeURIComponent(ch.id) + '" target="_blank" class="btn-secondary">Read</a> ' +
                    '<button class="btn-secondary ch-rerun-coded" data-chapter="' + escapeHtml(ch.id) + '">Rerun</button> ' +
                    '<button class="btn-secondary ch-run-judges" data-chapter="' + escapeHtml(ch.id) + '">Judges…</button>' +
                '</td>';
            tbody.appendChild(tr);
        });

        if (!rows) {
            tbody.innerHTML = '<tr><td colspan="6">No translated chapters yet.</td></tr>';
        }

        if (alignAllBtn) {
            alignAllBtn.style.display = unaligned.length ? '' : 'none';
            if (unaligned.length) {
                alignAllBtn.textContent = 'Align All Unaligned (' + unaligned.length + ')';
                alignAllBtn.disabled = false;
            }
        }
        bindReviewRowHandlers(unaligned);
        var selectAll = document.getElementById('select-all-review');
        if (selectAll) selectAll.checked = false;
        updateReviewScopeHint();
    }

    function bindReviewRowHandlers(unaligned) {
        var tbody = document.getElementById('review-tbody');

        tbody.querySelectorAll('.review-pick').forEach(function(cb) {
            cb.addEventListener('change', updateReviewScopeHint);
        });

        tbody.querySelectorAll('.ch-align').forEach(function(btn) {
            btn.addEventListener('click', function() {
                var chId = btn.dataset.chapter;
                btn.disabled = true;
                btn.textContent = 'Aligning...';
                apiPost('/api/project/' + PROJECT + '/align/' + chId, {}).then(function(data) {
                    if (data.error) {
                        btn.textContent = 'Error';
                        alert(data.error);
                    } else {
                        refreshStatusBadges();
                        refreshReviewStatus();
                    }
                });
            });
        });

        tbody.querySelectorAll('.ch-realign').forEach(function(btn) {
            btn.addEventListener('click', function() {
                var chId = btn.dataset.chapter;
                var noteCount = parseInt(btn.dataset.annotations || '0', 10);
                if (noteCount > 0) {
                    var msg = 'Realign ' + chId + '? ' + noteCount +
                        ' annotation(s) on this chapter will be re-anchored by ' +
                        'matching sentence text. Annotations on sentences whose ' +
                        'text changed may become orphaned.';
                    if (!window.confirm(msg)) return;
                }
                btn.disabled = true;
                btn.textContent = 'Realigning...';
                apiPost('/api/project/' + PROJECT + '/align/' + chId, {}).then(function(data) {
                    if (data.error) {
                        btn.textContent = 'Error';
                        btn.disabled = false;
                        alert(data.error);
                        return;
                    }
                    var orphans = data.orphaned_annotations || 0;
                    if (orphans > 0) {
                        alert('Realigned ' + chId + '. ' + orphans +
                            ' annotation(s) could not be re-anchored and are now orphaned.');
                    }
                    refreshStatusBadges();
                    refreshReviewStatus();
                });
            });
        });

        tbody.querySelectorAll('.ch-rerun-coded').forEach(function(btn) {
            btn.addEventListener('click', function() {
                startCodedRun([btn.dataset.chapter]);
            });
        });

        tbody.querySelectorAll('.ch-run-judges').forEach(function(btn) {
            btn.addEventListener('click', function() {
                openJudgesModal([btn.dataset.chapter]);
            });
        });

        // Align All is re-bound here rather than at render so it picks up the
        // current unaligned list instead of a stale closure from a prior render.
        var alignAllBtn = document.getElementById('btn-align-all');
        if (alignAllBtn && unaligned.length) {
            var fresh = alignAllBtn.cloneNode(true);
            alignAllBtn.parentNode.replaceChild(fresh, alignAllBtn);
            fresh.addEventListener('click', function() {
                fresh.disabled = true;
                var total = unaligned.length;
                function next(i) {
                    if (i >= total) {
                        refreshStatusBadges();
                        refreshReviewStatus();
                        return;
                    }
                    fresh.textContent = 'Aligning ' + (i + 1) + ' of ' + total + '...';
                    apiPost('/api/project/' + PROJECT + '/align/' + unaligned[i]).then(function(data) {
                        if (data.error) {
                            fresh.textContent = 'Error on ' + unaligned[i];
                            alert('Error aligning ' + unaligned[i] + ': ' + data.error);
                            return;
                        }
                        next(i + 1);
                    });
                }
                next(0);
            });
        }
    }

    // ── Review: job progress (shared by both run endpoints) ──

    function openJobModal(title) {
        document.getElementById('review-job-title').textContent = title;
        document.getElementById('review-job-text').textContent = 'Starting…';
        document.getElementById('review-job-fill').style.width = '0%';
        document.getElementById('review-job-spinner').style.display = '';
        var err = document.getElementById('review-job-error');
        err.style.display = 'none';
        err.textContent = '';
        document.getElementById('btn-review-job-close').disabled = true;
        document.getElementById('review-job-modal').classList.add('visible');
    }

    function finishJobModal(text, errorText) {
        document.getElementById('review-job-text').textContent = text;
        document.getElementById('review-job-spinner').style.display = 'none';
        document.getElementById('review-job-fill').style.width = '100%';
        if (errorText) {
            var err = document.getElementById('review-job-error');
            err.textContent = errorText;
            err.style.display = '';
        }
        document.getElementById('btn-review-job-close').disabled = false;
    }

    // One SSE consumer for every review job. `onProgress` gets each non-terminal
    // event; the terminal `complete` always arrives, even when the job body
    // died (the server attaches `fatal`), so this never hangs on a crash.
    function streamJob(jobId, total, label, onDone) {
        var source = new EventSource('/api/project/' + PROJECT + '/jobs/' + jobId + '/sse');
        var seen = 0;

        function progress(e) {
            var data = {};
            try { data = JSON.parse(e.data); } catch (err) { return; }
            seen = data.index || (seen + 1);
            var pct = total ? Math.round((seen / total) * 100) : 0;
            document.getElementById('review-job-fill').style.width = pct + '%';
            document.getElementById('review-job-text').textContent =
                label + ' ' + seen + ' of ' + total + '…';
        }

        source.addEventListener('chunk_done', progress);
        source.addEventListener('target_done', progress);
        source.addEventListener('chunk_error', progress);
        source.addEventListener('complete', function(e) {
            source.close();
            var data = {};
            try { data = JSON.parse(e.data); } catch (err) { /* keep defaults */ }
            var errText = data.fatal || '';
            if (!errText && data.error_count) {
                errText = data.error_count + ' failed: ' + (data.errors || []).join('; ');
            }
            var summary = data.fatal ? 'Stopped' :
                ((data.evaluated !== undefined ? data.evaluated : data.ran || 0) +
                 ' of ' + total + ' done');
            if (data.estimated_cost) summary += ' (~$' + Number(data.estimated_cost).toFixed(4) + ')';
            finishJobModal(summary, errText);
            refreshReviewStatus();
            if (onDone) onDone(data);
        });
        source.onerror = function() {
            source.close();
            finishJobModal('Connection lost', 'The progress stream dropped. The job may still be running — reopen the Review tab to see the result.');
        };
    }

    function startCodedRun(chapterIds) {
        var scope = chapterIds && chapterIds.length ? chapterIds : reviewScope();
        openJobModal('Rerunning deterministic evaluators');
        apiPost('/api/project/' + PROJECT + '/review/run-coded', { chapter_ids: scope })
            .then(function(data) {
                if (!data || data.error || !data.job_id) {
                    finishJobModal('Could not start', (data && data.error) || 'No job started.');
                    return;
                }
                streamJob(data.job_id, data.total, 'Evaluated');
            })
            // Without this, a 500 (which returns HTML, so `r.json()` rejects)
            // leaves the modal on "Starting…" with Close disabled and no way
            // out but a page reload.
            .catch(function(e) {
                finishJobModal('Could not start', String(e && e.message ? e.message : e));
            });
    }

    // ── Review: LLM judge panel ──

    var judgesScope = null;   // chapter ids for the pending run, or null = book

    function selectedJudges() {
        var out = [];
        document.querySelectorAll('#judges-modal .judge-pick:checked').forEach(function(cb) {
            out.push(cb.value);
        });
        return out;
    }

    function openJudgesModal(chapterIds) {
        judgesScope = chapterIds && chapterIds.length ? chapterIds : reviewScope();
        document.getElementById('judges-scope').textContent =
            'Scope: ' + (judgesScope ? judgesScope.join(', ') : 'whole book');
        document.getElementById('judges-cost-estimate').textContent = '';
        setStatus('judges-modal-status', '', '');
        populateProviderSelect('judges-provider');
        populateModelSelect('judges-provider', 'judges-model');
        document.getElementById('judges-modal').classList.add('visible');
    }

    function postJudges(confirm) {
        var judges = selectedJudges();
        if (!judges.length) {
            setStatus('judges-modal-status', 'Pick at least one judge.', 'error');
            return Promise.resolve(null);
        }
        return apiPost('/api/project/' + PROJECT + '/review/run-judges', {
            chapter_ids: judgesScope,
            judges: judges,
            provider: (document.getElementById('judges-provider') || {}).value,
            model: (document.getElementById('judges-model') || {}).value,
            confirm: !!confirm,
        });
    }

    function initReviewControls() {
        var selectAll = document.getElementById('select-all-review');
        if (selectAll) {
            selectAll.addEventListener('change', function() {
                document.querySelectorAll('#review-tbody .review-pick').forEach(function(cb) {
                    cb.checked = selectAll.checked;
                });
                updateReviewScopeHint();
            });
        }

        var rerunBtn = document.getElementById('btn-rerun-coded');
        if (rerunBtn) rerunBtn.addEventListener('click', function() { startCodedRun(null); });

        var judgesBtn = document.getElementById('btn-run-judges');
        if (judgesBtn) judgesBtn.addEventListener('click', function() { openJudgesModal(null); });

        // Bound once, here rather than in openJudgesModal: the modal is a single
        // DOM node reopened from two entry points, so binding on open would stack
        // a duplicate change listener every time it is shown.
        bindProviderModelPair('judges-provider', 'judges-model');

        var closeJudges = document.getElementById('judges-modal-close');
        if (closeJudges) {
            closeJudges.addEventListener('click', function() {
                document.getElementById('judges-modal').classList.remove('visible');
            });
        }

        var estimateBtn = document.getElementById('btn-judges-estimate');
        if (estimateBtn) {
            estimateBtn.addEventListener('click', function() {
                setStatus('judges-modal-status', 'Estimating…', 'info');
                var judges = selectedJudges();
                if (!judges.length) {
                    setStatus('judges-modal-status', 'Pick at least one judge.', 'error');
                    return;
                }
                // `dry_run` is explicit rather than leaning on `cost_limit: 0`
                // to force `needs_confirm`: a zero-priced provider makes
                // `estimated_cost > 0` false and the endpoint would start a
                // real run behind an "Estimated $0.0000" message.
                apiPost('/api/project/' + PROJECT + '/review/run-judges', {
                    chapter_ids: judgesScope, judges: judges,
                    provider: (document.getElementById('judges-provider') || {}).value,
                    model: (document.getElementById('judges-model') || {}).value,
                    dry_run: true,
                }).then(function(data) {
                    if (!data || data.error) {
                        // Drop the previous estimate: leaving it under an error
                        // reads as a price for a run that cannot happen.
                        document.getElementById('judges-cost-estimate').textContent = '';
                        setStatus('judges-modal-status', (data && data.error) || 'No estimate returned.', 'error');
                        return;
                    }
                    setStatus('judges-modal-status', '', '');
                    document.getElementById('judges-cost-estimate').textContent =
                        'Estimated $' + Number(data.estimated_cost || 0).toFixed(4) +
                        ' over ' + (data.target_count || 0) + ' chunks.';
                }).catch(function(e) {
                    document.getElementById('judges-cost-estimate').textContent = '';
                    setStatus('judges-modal-status', String(e && e.message ? e.message : e), 'error');
                });
            });
        }

        var runBtn = document.getElementById('btn-judges-run');
        if (runBtn) {
            runBtn.addEventListener('click', function() {
                setStatus('judges-modal-status', 'Starting…', 'info');
                postJudges(false).then(function(data) {
                    if (!data) return;
                    if (data.error) {
                        setStatus('judges-modal-status', data.error, 'error');
                        return;
                    }
                    if (data.status === 'needs_confirm') {
                        var msg = 'Estimated $' + Number(data.estimated_cost).toFixed(4) +
                            ' over ' + data.target_count + ' chunks, above the $' +
                            Number(data.cost_limit).toFixed(2) + ' limit. Run anyway?';
                        if (!window.confirm(msg)) {
                            setStatus('judges-modal-status', 'Cancelled.', '');
                            return;
                        }
                        postJudges(true).then(function(confirmed) {
                            if (!confirmed || confirmed.error || !confirmed.job_id) {
                                setStatus('judges-modal-status',
                                    (confirmed && confirmed.error) || 'No job started.', 'error');
                                return;
                            }
                            document.getElementById('judges-modal').classList.remove('visible');
                            openJobModal('Running LLM judges');
                            streamJob(confirmed.job_id, confirmed.total, 'Judged');
                        }).catch(function(e) {
                            setStatus('judges-modal-status', String(e && e.message ? e.message : e), 'error');
                        });
                        return;
                    }
                    if (!data.job_id) {
                        setStatus('judges-modal-status', 'No job started.', 'error');
                        return;
                    }
                    document.getElementById('judges-modal').classList.remove('visible');
                    openJobModal('Running LLM judges');
                    streamJob(data.job_id, data.total, 'Judged');
                }).catch(function(e) {
                    setStatus('judges-modal-status', String(e && e.message ? e.message : e), 'error');
                });
            });
        }

        var jobClose = document.getElementById('btn-review-job-close');
        if (jobClose) {
            jobClose.addEventListener('click', function() {
                document.getElementById('review-job-modal').classList.remove('visible');
            });
        }
    }

    initReviewControls();

    // ========================================================================
    // Stage 8: Export
    // ========================================================================

    function populateExportStage(status) {
        apiGet('/api/project/' + PROJECT + '/epub-status').then(function(data) {
            document.getElementById('epub-translated-count').textContent = data.translated_chapters;
            document.getElementById('epub-total-count').textContent = data.total_chapters;

            // Pre-populate title/author (prefer spanish_title for epub)
            var titleInput = document.getElementById('epub-title');
            var authorInput = document.getElementById('epub-author');
            if (!titleInput.value && (data.spanish_title || data.title)) titleInput.value = data.spanish_title || data.title;
            if (!authorInput.value && data.author) authorInput.value = data.author;

            // Pre-populate optional Dublin Core metadata fields from project.json.
            // Only fill when the field is empty so unsaved user edits aren't clobbered.
            var metaFields = [
                ['epub-translator', 'translator'],
                ['epub-source-title', 'source_title'],
                ['epub-publisher', 'publisher'],
                ['epub-description', 'description'],
                ['epub-rights', 'rights'],
            ];
            metaFields.forEach(function(pair) {
                var el = document.getElementById(pair[0]);
                if (el && !el.value && data[pair[1]]) el.value = data[pair[1]];
            });

            // Cover image preview — mirrors what _resolve_cover() picks at build time.
            var coverThumb = document.getElementById('epub-cover-thumb');
            var coverEmpty = document.getElementById('epub-cover-empty');
            if (coverThumb && coverEmpty) {
                if (data.cover_filename) {
                    var bust = data.cover_mtime ? ('?t=' + data.cover_mtime) : '';
                    coverThumb.src = '/projects/' + PROJECT + '/images/' + data.cover_filename + bust;
                    coverThumb.style.display = '';
                    coverEmpty.style.display = 'none';
                } else {
                    coverThumb.removeAttribute('src');
                    coverThumb.style.display = 'none';
                    coverEmpty.style.display = '';
                }
            }

            // Update badge
            var badge = document.getElementById('badge-export');
            if (data.epub_exists) {
                badge.textContent = 'ready';
                var exportLi = document.querySelector('.stepper li[data-stage="export"]');
                if (exportLi) exportLi.classList.add('done');
            }

            // Show download link if epub already exists
            if (data.epub_exists) {
                var dlBtn = document.getElementById('btn-download-epub');
                dlBtn.href = '/api/project/' + PROJECT + '/download-epub';
                dlBtn.textContent = 'Download ' + data.epub_filename;
                dlBtn.style.display = '';
            }
        });

        loadTranslatorNote();
    }

    // -- Translator note: load + blur autosave with sequence-token guard --
    function loadTranslatorNote() {
        apiGet('/api/project/' + PROJECT + '/translator-note').then(function(data) {
            if (!data) return;
            var hEl = document.getElementById('epub-translator-heading');
            var bEl = document.getElementById('epub-translator-note');
            if (hEl && typeof data.heading === 'string') hEl.value = data.heading;
            if (bEl && typeof data.body === 'string') bEl.value = data.body;
        });
    }

    var _translatorNoteSeq = 0;
    var _translatorNoteLastApplied = 0;

    function setNoteStatus(text, color, onClick) {
        // Update both indicators in lockstep so users see status near both fields.
        var ids = ['epub-translator-heading-status', 'epub-translator-note-status'];
        ids.forEach(function(id) {
            var el = document.getElementById(id);
            if (!el) return;
            el.textContent = text;
            el.style.color = color || '';
            el.style.cursor = onClick ? 'pointer' : '';
            el.onclick = onClick || null;
        });
    }

    function fadeNoteStatus(delayMs) {
        setTimeout(function() {
            var ids = ['epub-translator-heading-status', 'epub-translator-note-status'];
            ids.forEach(function(id) {
                var el = document.getElementById(id);
                if (el && el.textContent === 'Saved') {
                    el.textContent = '';
                }
            });
        }, delayMs);
    }

    function saveTranslatorNote() {
        var hEl = document.getElementById('epub-translator-heading');
        var bEl = document.getElementById('epub-translator-note');
        var heading = hEl ? hEl.value : '';
        var body = bEl ? bEl.value : '';
        var seq = ++_translatorNoteSeq;
        setNoteStatus('Saving…', '#888', null);
        return fetch('/api/project/' + PROJECT + '/translator-note', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ heading: heading, body: body }),
        }).then(function(r) {
            // Sequence-token guard: only the latest POST may update the indicator.
            if (seq < _translatorNoteLastApplied) return;
            _translatorNoteLastApplied = seq;
            if (r.ok) {
                setNoteStatus('Saved', '#080', null);
                fadeNoteStatus(2000);
            } else {
                setNoteStatus('Save failed — click to retry', '#c00', saveTranslatorNote);
            }
        }).catch(function() {
            if (seq < _translatorNoteLastApplied) return;
            _translatorNoteLastApplied = seq;
            setNoteStatus('Save failed — click to retry', '#c00', saveTranslatorNote);
        });
    }

    ['epub-translator-heading', 'epub-translator-note'].forEach(function(id) {
        var el = document.getElementById(id);
        if (el) el.addEventListener('blur', saveTranslatorNote);
    });

    document.getElementById('btn-build-epub').addEventListener('click', function() {
        var btn = this;
        var statusEl = document.getElementById('epub-status');
        var title = document.getElementById('epub-title').value.trim();
        var author = document.getElementById('epub-author').value.trim();
        var hEl = document.getElementById('epub-translator-heading');
        var bEl = document.getElementById('epub-translator-note');
        var translatorHeading = hEl ? hEl.value : '';
        var translatorNote = bEl ? bEl.value : '';

        // Read optional DC metadata. We always include these keys in the POST
        // body (even when blank) so users can clear a previously-saved value.
        function readMeta(id) {
            var el = document.getElementById(id);
            return el ? el.value.trim() : '';
        }
        var translator = readMeta('epub-translator');
        var sourceTitle = readMeta('epub-source-title');
        var publisher = readMeta('epub-publisher');
        var description = readMeta('epub-description');
        var rights = readMeta('epub-rights');

        btn.disabled = true;
        btn.textContent = 'Building...';
        statusEl.textContent = '';
        document.getElementById('btn-download-epub').style.display = 'none';

        apiPost('/api/project/' + PROJECT + '/build-epub', {
            title: title || undefined,
            author: author || undefined,
            translator: translator,
            source_title: sourceTitle,
            publisher: publisher,
            description: description,
            rights: rights,
            translator_heading: translatorHeading,
            translator_note: translatorNote,
        }).then(function(data) {
            btn.disabled = false;
            btn.textContent = 'Build EPUB';
            if (data.error) {
                statusEl.textContent = 'Error: ' + data.error;
                statusEl.style.color = '#c00';
            } else {
                statusEl.textContent = data.chapters_included + ' chapters, ' + formatBytes(data.size_bytes);
                statusEl.style.color = '#080';
                var dlBtn = document.getElementById('btn-download-epub');
                dlBtn.href = '/api/project/' + PROJECT + '/download-epub';
                dlBtn.textContent = 'Download ' + data.filename;
                dlBtn.style.display = '';

                // Update badge
                var badge = document.getElementById('badge-export');
                badge.textContent = 'ready';
                var exportLi = document.querySelector('.stepper li[data-stage="export"]');
                if (exportLi) exportLi.classList.add('done');
            }
        });
    });

    // ========================================================================
    // Init
    // ========================================================================

    loadLLMConfig().then(function() {
        initAllLLMSelectors();
        return loadSplitPatterns();
    }).then(function() { return loadStatus(); }).then(function() {
        var hash = location.hash.replace('#', '');
        if (hash && stages.indexOf(hash) !== -1) {
            navigateTo(hash);
        } else {
            // Navigate to first incomplete stage
            if (!projectStatus.has_source) navigateTo('source');
            else if (projectStatus.chapter_count === 0) navigateTo('split');
            else if (projectStatus.total_chunks === 0) navigateTo('chunk');
            else if (!projectStatus.has_style_guide) navigateTo('style-guide');
            else if (projectStatus.glossary_count === 0) navigateTo('glossary');
            else if (projectStatus.translated_chunks < projectStatus.total_chunks) navigateTo('translate');
            else navigateTo('review');
        }
        // One rollup on boot so the sidebar badge reads "12 findings" instead of
        // nothing; after this it is refreshed only on demand.
        if (projectStatus.translated_chunks > 0) refreshReviewStatus();
    });

})();
