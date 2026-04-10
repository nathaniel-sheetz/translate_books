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
                    return { id: m.id, name: m.name };
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

        // Batch translate
        populateProviderSelect('batch-provider');
        populateModelSelect('batch-provider', 'batch-model');
        bindProviderModelPair('batch-provider', 'batch-model', updateBatchCostEstimate);
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
        return div.innerHTML;
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
        currentStage = stage;

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
                    if (status.alignment_count > 0) {
                        badge.textContent = status.alignment_count + ' aligned';
                        if (status.alignment_count >= status.chapter_count && status.chapter_count > 0) {
                            li.classList.add('done');
                        }
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
        populateReviewStage(status);
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
        } else {
            document.getElementById('source-loaded').style.display = 'none';
            document.getElementById('source-upload').style.display = '';
        }
    }

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
        var title = titleInput.value.trim();
        setStatus('title-save-status', 'Saving...', '');
        apiPost('/api/project/' + PROJECT + '/config', { title: title || PROJECT }).then(function(data) {
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
                status.chapters.length + ' chapters detected';
            var cards = document.getElementById('split-existing-cards');
            cards.innerHTML = '';
            status.chapters.forEach(function(ch) {
                var card = document.createElement('div');
                card.className = 'chapter-card';
                card.innerHTML =
                    '<span class="ch-name">' + escapeHtml(ch.name) + '</span>' +
                    '<span class="ch-words">' + (ch.words || 0) + ' words</span>' +
                    '<span class="ch-preview">' + escapeHtml(truncate(ch.preview, 80)) + '</span>';
                cards.appendChild(card);
            });
        } else {
            document.getElementById('split-existing').style.display = 'none';
        }
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
        var config = getSplitConfig();
        setStatus('split-status', 'Splitting...', '');
        apiPost('/api/project/' + PROJECT + '/split', config).then(function(data) {
            if (data.error) {
                setStatus('split-status', data.error, 'error');
            } else {
                setStatus('split-status', 'Split complete', 'success');
                document.getElementById('split-preview-area').style.display = 'none';
                document.getElementById('btn-split-confirm').style.display = 'none';
                loadStatus();
            }
        });
    });

    document.getElementById('btn-resplit').addEventListener('click', function() {
        if (confirm('This will overwrite existing chapter files. Continue?')) {
            document.getElementById('split-existing').style.display = 'none';
        }
    });

    function getSplitConfig() {
        return {
            pattern_type: document.getElementById('split-pattern').value,
            custom_regex: document.getElementById('split-custom-regex').value,
            min_chapter_size: parseInt(document.getElementById('split-min-size').value, 10) || 500,
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
            card.innerHTML =
                '<span class="ch-name">' + escapeHtml(ch.name || ch.title || 'Chapter') + '</span>' +
                '<span class="ch-words">' + (ch.words || ch.word_count || 0) + ' words</span>' +
                '<span class="ch-preview">' + escapeHtml(truncate(ch.preview || ch.first_line || '', 80)) + '</span>';
            cards.appendChild(card);
        });
        document.getElementById('btn-split-confirm').style.display = '';
    }

    // ========================================================================
    // Stage 3: Chunk
    // ========================================================================

    function populateChunkStage(status) {
        var list = document.getElementById('chunk-chapter-list');
        list.innerHTML = '';

        if (!status.chapters || status.chapters.length === 0) {
            list.innerHTML = '<p class="stage-subtitle">No chapters yet. Split the source text first.</p>';
            return;
        }

        status.chapters.forEach(function(ch) {
            var card = document.createElement('div');
            card.className = 'chapter-card';
            var chunkInfo = ch.chunk_count > 0
                ? ch.chunk_count + ' chunks'
                : 'Not chunked';
            card.innerHTML =
                '<span class="ch-name">' + escapeHtml(ch.name) + '</span>' +
                '<span class="ch-words">' + chunkInfo + '</span>';
            list.appendChild(card);
        });
    }

    document.getElementById('btn-chunk-all').addEventListener('click', function() {
        var config = {
            target_size: parseInt(document.getElementById('chunk-target-size').value, 10) || 2000,
            overlap_paragraphs: parseInt(document.getElementById('chunk-overlap-para').value, 10) || 2,
            min_overlap_words: parseInt(document.getElementById('chunk-overlap-words').value, 10) || 100,
        };
        setStatus('chunk-status', 'Chunking...', '');
        apiPost('/api/project/' + PROJECT + '/chunk-all', config).then(function(data) {
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

    function populateStyleGuideStage(status) {
        if (status.has_style_guide && status.style_guide_content) {
            document.getElementById('style-guide-existing').style.display = '';
            document.getElementById('style-guide-wizard').style.display = 'none';
            document.getElementById('style-guide-preview').textContent = status.style_guide_content;
        } else {
            document.getElementById('style-guide-existing').style.display = 'none';
            document.getElementById('style-guide-wizard').style.display = '';
        }
    }

    document.getElementById('btn-edit-style').addEventListener('click', function() {
        document.getElementById('style-guide-existing').style.display = 'none';
        document.getElementById('style-guide-wizard').style.display = '';
    });

    // Collect answers from fixed + extra questions
    var extraQuestions = [];

    function collectAnswers() {
        var answers = {};
        document.querySelectorAll('.question-block').forEach(function(block) {
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
            var checked = document.querySelector('input[name="q_' + q.id + '"]:checked');
            if (checked) {
                answers[q.id] = parseInt(checked.value, 10);
            }
        });
        return answers;
    }

    // Custom input toggle
    document.querySelectorAll('.question-block').forEach(function(block) {
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
            var html = '<label class="question-label">' + escapeHtml(q.question) + '</label>';
            (q.options || []).forEach(function(opt, i) {
                var label = typeof opt === 'string' ? opt : opt.label;
                html += '<label class="option-label">' +
                    '<input type="radio" name="q_' + q.id + '" value="' + i + '"' +
                    (i === (q.default || 0) ? ' checked' : '') + '> ' +
                    escapeHtml(label) + '</label>';
            });
            html += '<div class="effect-preview" data-qid="' + q.id + '"></div>';
            block.innerHTML = html;

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

    function buildGlossaryQASelection() {
        var qaDiv = document.getElementById('glossary-qa-selection');
        if (!qaDiv) return;
        qaDiv.innerHTML = '';
        FIXED_QUESTIONS.forEach(function(q) {
            var defaultChecked = q.glossary_relevant === true;
            var item = document.createElement('label');
            item.className = 'glossary-qa-item';
            item.innerHTML = '<input type="checkbox"' + (defaultChecked ? ' checked' : '') + ' value="' + q.id + '"> ' +
                '<span class="glossary-qa-question">' + escapeHtml(q.question) + '</span>';
            qaDiv.appendChild(item);
        });
        extraQuestions.forEach(function(q) {
            var item = document.createElement('label');
            item.className = 'glossary-qa-item';
            item.innerHTML = '<input type="checkbox" value="' + q.id + '"> ' +
                '<span class="glossary-qa-question">' + escapeHtml(q.question) + '</span>';
            qaDiv.appendChild(item);
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
        document.getElementById('glossary-existing').style.display = 'none';
    });

    // Extract candidates
    document.getElementById('btn-extract-candidates').addEventListener('click', function() {
        setStatus('extract-status', 'Extracting...', '');
        apiPost('/api/setup/' + PROJECT + '/extract-candidates', {}).then(function(data) {
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

    // Show glossary prompt
    document.getElementById('btn-show-glossary-prompt').addEventListener('click', function() {
        var area = document.getElementById('glossary-prompt-area');
        if (area.style.display === 'none') {
            apiPost('/api/setup/' + PROJECT + '/prompts/glossary', {
                candidates: glossaryCandidates,
                glossary_guidance: collectGlossaryGuidance(),
            }).then(function(data) {
                document.getElementById('glossary-prompt-text').value = data.prompt || data.error || '';
                area.style.display = '';
            });
        } else {
            area.style.display = 'none';
        }
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
        }).then(function(data) {
            btn.disabled = false;
            if (data.error && !data.terms && !data.raw_text) {
                setStatus('glossary-api-status', data.error, 'error');
                return;
            }
            if (data.terms) {
                glossaryProposals = data.terms;
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
            tr.innerHTML =
                '<td><input type="text" value="' + escapeHtml(p.english || '') + '" class="gl-english"></td>' +
                '<td><input type="text" value="' + escapeHtml(p.spanish || '') + '" class="gl-spanish"></td>' +
                '<td>' + escapeHtml(p.type || 'other') + '</td>' +
                '<td>' + escapeHtml(truncate(p.context || '', 40)) + '</td>' +
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
            terms.push({
                english: tr.querySelector('.gl-english').value,
                spanish: tr.querySelector('.gl-spanish').value,
                type: tr.cells[2].textContent,
                context: tr.cells[3].textContent,
            });
        });
        apiPost('/api/setup/' + PROJECT + '/glossary', { terms: terms }).then(function(data) {
            if (data.error) {
                setStatus('glossary-save-status', data.error, 'error');
            } else {
                setStatus('glossary-save-status', 'Saved ' + terms.length + ' terms', 'success');
                loadStatus();
            }
        });
    });

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

        area.innerHTML = html;

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
                    loadStatus();
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
                    loadStatus();
                }
            });
        });
    }

    // ── Batch translation ──

    document.getElementById('btn-batch-translate').addEventListener('click', function() {
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
        document.getElementById('batch-modal').classList.add('visible');
        updateBatchCostEstimate();
    });

    document.getElementById('batch-modal-close').addEventListener('click', closeBatchModal);
    document.getElementById('batch-modal').addEventListener('click', function(e) {
        if (e.target === this) closeBatchModal();
    });

    function closeBatchModal() {
        document.getElementById('batch-modal').classList.remove('visible');
    }

    // (provider/model dropdown setup is handled by initAllLLMSelectors)

    function updateBatchCostEstimate() {
        var ids = getSelectedChapterIds();
        var provider = document.getElementById('batch-provider').value;
        var model = document.getElementById('batch-model').value;
        var el = document.getElementById('batch-cost-estimate');
        el.textContent = 'Estimating cost...';

        apiPost('/api/project/' + PROJECT + '/translate/cost-estimate', {
            chapter_ids: ids,
            provider: provider,
            model: model,
        }).then(function(data) {
            if (data.error) {
                el.textContent = data.error;
            } else {
                el.innerHTML = '<strong>' + (data.chunk_count || 0) + '</strong> chunks to translate<br>' +
                    'Estimated cost: <strong>$' + (data.estimated_cost || 0).toFixed(4) + '</strong>';
            }
        });
    }

    // Start batch translation
    document.getElementById('btn-start-batch').addEventListener('click', function() {
        var ids = getSelectedChapterIds();
        var provider = document.getElementById('batch-provider').value;
        var model = document.getElementById('batch-model').value;

        this.disabled = true;
        document.getElementById('batch-progress').style.display = '';
        document.getElementById('btn-cancel-batch').style.display = '';

        apiPost('/api/project/' + PROJECT + '/translate/batch', {
            chapter_ids: ids,
            provider: provider,
            model: model,
        }).then(function(data) {
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
        batchEventSource = new EventSource('/api/project/' + PROJECT + '/translate/sse?job_id=' + jobId);

        batchEventSource.addEventListener('chunk_done', function(e) {
            completed++;
            var pct = totalChunks > 0 ? Math.round(completed / totalChunks * 100) : 0;
            document.getElementById('batch-progress-text').textContent =
                'Translated ' + completed + '/' + totalChunks;
            document.getElementById('batch-progress-fill').style.width = pct + '%';
        });

        batchEventSource.addEventListener('chunk_error', function(e) {
            var data = JSON.parse(e.data);
            console.error('Chunk error:', data);
        });

        batchEventSource.addEventListener('batch_complete', function() {
            batchEventSource.close();
            batchEventSource = null;
            document.getElementById('batch-progress-text').textContent = 'Complete!';
            document.getElementById('btn-start-batch').disabled = false;
            document.getElementById('btn-cancel-batch').style.display = 'none';
            loadStatus();
            setTimeout(closeBatchModal, 1500);
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

    // ========================================================================
    // Stage 7: Review
    // ========================================================================

    function populateReviewStage(status) {
        var tbody = document.getElementById('review-tbody');
        tbody.innerHTML = '';

        if (!status.chapters || status.chapters.length === 0) {
            tbody.innerHTML = '<tr><td colspan="5">No chapters available.</td></tr>';
            return;
        }

        var hasAnyTranslated = false;

        status.chapters.forEach(function(ch) {
            var translated = ch.translated_count || 0;
            var total = ch.chunk_count || 0;
            if (translated === 0 || total === 0) return;
            hasAnyTranslated = true;

            var hasAlignment = ch.has_alignment;
            var confidence = ch.alignment_confidence;
            var annotations = ch.annotation_count || 0;
            var reviewed = ch.reviewed;

            var tr = document.createElement('tr');
            tr.innerHTML =
                '<td>' + escapeHtml(ch.name) + '</td>' +
                '<td>' + (hasAlignment
                    ? '<span class="' + (confidence < 90 ? 'confidence-warn' : 'confidence-ok') + '">' + confidence + '%</span>'
                    : '&mdash;') + '</td>' +
                '<td>' + (annotations > 0 ? annotations + ' notes' : '&mdash;') + '</td>' +
                '<td>' + (reviewed ? '&#10003;' : '&mdash;') + '</td>' +
                '<td>' +
                    (!hasAlignment ? '<button class="btn-primary ch-align" data-chapter="' + ch.id + '" style="padding:3px 10px;font-size:12px">Align</button> ' : '') +
                    '<a href="/read/' + PROJECT + '/' + ch.id + '" target="_blank" class="btn-secondary" style="padding:3px 10px;font-size:12px;text-decoration:none">Read</a>' +
                '</td>';
            tbody.appendChild(tr);
        });

        if (!hasAnyTranslated) {
            tbody.innerHTML = '<tr><td colspan="5">No translated chapters yet.</td></tr>';
        }

        // Align button handlers
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
                        btn.textContent = 'Done';
                        loadStatus();
                    }
                });
            });
        });
    }

    // ========================================================================
    // Stage 8: Export
    // ========================================================================

    function populateExportStage(status) {
        apiGet('/api/project/' + PROJECT + '/epub-status').then(function(data) {
            document.getElementById('epub-translated-count').textContent = data.translated_chapters;
            document.getElementById('epub-total-count').textContent = data.total_chapters;

            // Pre-populate title/author
            var titleInput = document.getElementById('epub-title');
            var authorInput = document.getElementById('epub-author');
            if (!titleInput.value && data.title) titleInput.value = data.title;
            if (!authorInput.value && data.author) authorInput.value = data.author;

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
    }

    document.getElementById('btn-build-epub').addEventListener('click', function() {
        var btn = this;
        var statusEl = document.getElementById('epub-status');
        var title = document.getElementById('epub-title').value.trim();
        var author = document.getElementById('epub-author').value.trim();

        btn.disabled = true;
        btn.textContent = 'Building...';
        statusEl.textContent = '';
        document.getElementById('btn-download-epub').style.display = 'none';

        apiPost('/api/project/' + PROJECT + '/build-epub', {
            title: title || undefined,
            author: author || undefined,
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
    });

})();
