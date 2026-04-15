/* Setup page — style guide wizard + glossary bootstrap */

(function() {
    'use strict';

    // State
    var extraQuestions = [];
    var glossaryCandidates = [];
    var glossaryProposals = [];

    // ========================================================================
    // Tab switching
    // ========================================================================

    document.querySelectorAll('.tab-btn').forEach(function(btn) {
        btn.addEventListener('click', function() {
            document.querySelectorAll('.tab-btn').forEach(function(b) { b.classList.remove('active'); });
            document.querySelectorAll('.tab-panel').forEach(function(p) { p.classList.remove('active'); });
            btn.classList.add('active');
            document.getElementById('tab-' + btn.dataset.tab).classList.add('active');
        });
    });

    // ========================================================================
    // Helpers
    // ========================================================================

    function collectAnswers() {
        var answers = {};
        // Fixed questions
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
        // Extra LLM questions
        extraQuestions.forEach(function(q) {
            var checked = document.querySelector('input[name="q_' + q.id + '"]:checked');
            if (checked) {
                answers[q.id] = parseInt(checked.value, 10);
            }
        });
        return answers;
    }

    function apiPost(url, body) {
        return fetch(url, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body),
        }).then(function(r) { return r.json(); });
    }

    function copyToClipboard(text) {
        if (navigator.clipboard && navigator.clipboard.writeText) {
            navigator.clipboard.writeText(text).catch(function() { fallbackCopy(text); });
        } else {
            fallbackCopy(text);
        }
    }

    function fallbackCopy(text) {
        var ta = document.createElement('textarea');
        ta.value = text;
        ta.style.position = 'fixed';
        ta.style.opacity = '0';
        document.body.appendChild(ta);
        ta.select();
        document.execCommand('copy');
        document.body.removeChild(ta);
    }

    // ========================================================================
    // Custom input toggle + effect preview
    // ========================================================================

    // Build a lookup of question data by id
    var questionsById = {};
    FIXED_QUESTIONS.forEach(function(q) { questionsById[q.id] = q; });

    function updateEffectPreview(block, qid, value) {
        var preview = block.querySelector('.effect-preview[data-qid="' + qid + '"]');
        if (!preview) return;
        var q = questionsById[qid];
        if (!q) return;
        var idx = parseInt(value, 10);
        if (isNaN(idx) || idx < 0 || idx >= q.options.length) {
            preview.textContent = '';
            return;
        }
        preview.textContent = q.options[idx].style_guide_effect || '';
    }

    document.querySelectorAll('.question-block').forEach(function(block) {
        var qid = block.dataset.qid;
        var radios = block.querySelectorAll('input[type="radio"]');
        var customInput = block.querySelector('.custom-input');

        radios.forEach(function(r) {
            r.addEventListener('change', function() {
                if (customInput) {
                    customInput.style.display = r.value === 'custom' && r.checked ? 'block' : 'none';
                }
                updateEffectPreview(block, qid, r.value);
            });
        });

        // Show effect for initially selected option
        var checked = block.querySelector('input[type="radio"]:checked');
        if (checked) updateEffectPreview(block, qid, checked.value);
    });

    // ========================================================================
    // Copy buttons
    // ========================================================================

    document.querySelectorAll('.btn-copy').forEach(function(btn) {
        btn.addEventListener('click', function() {
            var target = document.getElementById(btn.dataset.target);
            if (target) {
                copyToClipboard(target.value);
                btn.textContent = 'Copied!';
                setTimeout(function() { btn.textContent = 'Copy'; }, 1500);
            }
        });
    });

    // ========================================================================
    // STYLE GUIDE: Show questions prompt
    // ========================================================================

    document.getElementById('btn-show-questions-prompt').addEventListener('click', function() {
        var area = document.getElementById('questions-prompt-area');
        if (area.style.display !== 'none') {
            area.style.display = 'none';
            return;
        }
        var answers = collectAnswers();
        apiPost('/api/setup/' + PROJECT_ID + '/prompts/questions', {
            answers: answers,
            target_lang: 'Spanish',
            locale: 'mx',
        }).then(function(data) {
            document.getElementById('questions-prompt-text').value = data.prompt;
            area.style.display = 'block';
        });
    });

    // ========================================================================
    // STYLE GUIDE: Parse pasted questions
    // ========================================================================

    document.getElementById('btn-parse-questions').addEventListener('click', function() {
        var raw = document.getElementById('questions-paste').value.trim();
        if (!raw) return;

        try {
            // Try to extract JSON from markdown fences
            var match = raw.match(/```(?:json)?\s*\n?([\s\S]*?)```/);
            var jsonStr = match ? match[1].trim() : raw;
            extraQuestions = JSON.parse(jsonStr);
        } catch (e) {
            alert('Could not parse JSON: ' + e.message);
            return;
        }

        var container = document.getElementById('extra-questions');
        container.innerHTML = '';

        extraQuestions.forEach(function(q) {
            var block = document.createElement('div');
            block.className = 'question-block';
            block.dataset.qid = q.id;

            var label = document.createElement('label');
            label.className = 'question-label';
            label.textContent = q.question;
            block.appendChild(label);

            if (q.context) {
                var ctx = document.createElement('p');
                ctx.className = 'section-hint';
                ctx.textContent = q.context;
                block.appendChild(ctx);
            }

            q.options.forEach(function(opt, i) {
                var optLabel = document.createElement('label');
                optLabel.className = 'option-label';
                var radio = document.createElement('input');
                radio.type = 'radio';
                radio.name = 'q_' + q.id;
                radio.value = i;
                if (i === (q.default || 0)) radio.checked = true;
                optLabel.appendChild(radio);
                optLabel.appendChild(document.createTextNode(' ' + opt.label));
                block.appendChild(optLabel);
            });

            // Effect preview + register for lookup
            var preview = document.createElement('div');
            preview.className = 'effect-preview';
            preview.dataset.qid = q.id;
            block.appendChild(preview);
            questionsById[q.id] = q;

            // Wire up change handlers
            block.querySelectorAll('input[type="radio"]').forEach(function(r) {
                r.addEventListener('change', function() {
                    updateEffectPreview(block, q.id, r.value);
                });
            });

            // Show effect for default selection
            updateEffectPreview(block, q.id, q.default || 0);

            container.appendChild(block);
        });
    });

    // ========================================================================
    // STYLE GUIDE: Generate fallback (no LLM)
    // ========================================================================

    document.getElementById('btn-generate-fallback').addEventListener('click', function() {
        var answers = collectAnswers();
        apiPost('/api/setup/' + PROJECT_ID + '/style-guide/fallback', {
            answers: answers,
            extra_questions: extraQuestions,
        }).then(function(data) {
            document.getElementById('style-preview').textContent = data.content;
        });
    });

    // ========================================================================
    // STYLE GUIDE: Show generation prompt
    // ========================================================================

    document.getElementById('btn-show-style-prompt').addEventListener('click', function() {
        var area = document.getElementById('style-prompt-area');
        if (area.style.display !== 'none') {
            area.style.display = 'none';
            return;
        }
        var answers = collectAnswers();
        apiPost('/api/setup/' + PROJECT_ID + '/prompts/style-guide', {
            answers: answers,
            extra_questions: extraQuestions,
            target_lang: 'Spanish',
            locale: 'mx',
        }).then(function(data) {
            document.getElementById('style-prompt-text').value = data.prompt;
            area.style.display = 'block';
        });
    });

    // ========================================================================
    // STYLE GUIDE: Use pasted text
    // ========================================================================

    document.getElementById('btn-use-pasted-style').addEventListener('click', function() {
        var text = document.getElementById('style-paste').value.trim();
        if (!text) return;
        // Strip markdown fences if present
        var match = text.match(/```(?:markdown|text)?\s*\n?([\s\S]*?)```/);
        if (match) text = match[1].trim();
        document.getElementById('style-preview').textContent = text;
    });

    // ========================================================================
    // STYLE GUIDE: Save
    // ========================================================================

    document.getElementById('btn-save-style').addEventListener('click', function() {
        var content = document.getElementById('style-preview').textContent;
        if (!content || content === '(no style guide yet)') {
            alert('Generate or paste a style guide first.');
            return;
        }
        apiPost('/api/setup/' + PROJECT_ID + '/style-guide', { content: content })
        .then(function(data) {
            document.getElementById('style-save-status').textContent = 'Saved!';
            setTimeout(function() {
                document.getElementById('style-save-status').textContent = '';
            }, 3000);
        });
    });

    // ========================================================================
    // GLOSSARY: Extract candidates
    // ========================================================================

    document.getElementById('btn-extract').addEventListener('click', function() {
        var status = document.getElementById('extract-status');
        status.textContent = 'Extracting...';
        apiPost('/api/setup/' + PROJECT_ID + '/extract-candidates', {})
        .then(function(data) {
            glossaryCandidates = data.candidates;
            status.textContent = data.total + ' candidates found';
            renderGlossaryQASection();
            document.getElementById('glossary-bootstrap-section').style.display = 'block';
        })
        .catch(function(e) {
            status.textContent = 'Error: ' + e.message;
        });
    });

    function renderGlossaryQASection() {
        var answers = collectAnswers();
        var container = document.getElementById('glossary-qa-checks');
        container.innerHTML = '';

        var allQ = FIXED_QUESTIONS.map(function(q) { return { q: q, isFixed: true }; })
            .concat(extraQuestions.map(function(q) { return { q: q, isFixed: false }; }));

        allQ.forEach(function(item) {
            var q = item.q;
            var answer = answers[q.id];
            if (answer === undefined) return;

            var label = '';
            if (typeof answer === 'number' && q.options && answer < q.options.length) {
                label = q.options[answer].label;
            } else if (typeof answer === 'string' && answer.trim()) {
                label = answer;
            } else {
                return;
            }

            var defaultChecked = item.isFixed && q.glossary_relevant === true;

            var wrap = document.createElement('label');
            wrap.className = 'glossary-qa-item';

            var cb = document.createElement('input');
            cb.type = 'checkbox';
            cb.className = 'glossary-qa-check';
            cb.dataset.qid = q.id;
            cb.checked = defaultChecked;
            wrap.appendChild(cb);

            var qSpan = document.createElement('span');
            qSpan.className = 'glossary-qa-question';
            qSpan.textContent = ' ' + q.question;
            wrap.appendChild(qSpan);

            var aSpan = document.createElement('span');
            aSpan.className = 'glossary-qa-answer';
            aSpan.textContent = ' (' + label + ')';
            wrap.appendChild(aSpan);

            container.appendChild(wrap);
        });

        document.getElementById('glossary-qa-section').style.display = 'block';
    }

    function collectGlossaryGuidance() {
        var answers = collectAnswers();
        var lines = [];
        document.querySelectorAll('.glossary-qa-check:checked').forEach(function(cb) {
            var qid = cb.dataset.qid;
            var q = null;
            for (var i = 0; i < FIXED_QUESTIONS.length; i++) {
                if (FIXED_QUESTIONS[i].id === qid) { q = FIXED_QUESTIONS[i]; break; }
            }
            if (!q) {
                for (var i = 0; i < extraQuestions.length; i++) {
                    if (extraQuestions[i].id === qid) { q = extraQuestions[i]; break; }
                }
            }
            if (!q) return;
            var answer = answers[qid];
            if (typeof answer === 'number' && q.options && answer < q.options.length) {
                var effect = q.options[answer].style_guide_effect || q.options[answer].label;
                lines.push(effect);
            } else if (typeof answer === 'string' && answer.trim()) {
                lines.push(q.question + ': ' + answer);
            }
        });
        return lines.join('\n\n');
    }

    // ========================================================================
    // GLOSSARY: Show bootstrap prompt
    // ========================================================================

    document.getElementById('btn-show-glossary-prompt').addEventListener('click', function() {
        var area = document.getElementById('glossary-prompt-area');
        if (area.style.display !== 'none') {
            area.style.display = 'none';
            return;
        }
        apiPost('/api/setup/' + PROJECT_ID + '/prompts/glossary', {
            candidates: glossaryCandidates,
            target_lang: 'Spanish',
            glossary_guidance: collectGlossaryGuidance(),
        }).then(function(data) {
            document.getElementById('glossary-prompt-text').value = data.prompt;
            area.style.display = 'block';
        });
    });

    // ========================================================================
    // GLOSSARY: Parse pasted proposals
    // ========================================================================

    document.getElementById('btn-parse-glossary').addEventListener('click', function() {
        var raw = document.getElementById('glossary-paste').value.trim();
        if (!raw) return;

        try {
            var match = raw.match(/```(?:json)?\s*\n?([\s\S]*?)```/);
            var jsonStr = match ? match[1].trim() : raw;
            glossaryProposals = JSON.parse(jsonStr);
        } catch (e) {
            alert('Could not parse JSON: ' + e.message);
            return;
        }

        renderGlossaryTable(glossaryProposals);
        document.getElementById('glossary-review-section').style.display = 'block';
    });

    function renderGlossaryTable(proposals) {
        var container = document.getElementById('glossary-table-container');
        var html = '<table class="glossary-table">';
        html += '<thead><tr><th>English</th><th>Spanish</th><th>Type</th><th>Context</th><th>Action</th></tr></thead>';
        html += '<tbody>';
        proposals.forEach(function(p, i) {
            html += '<tr data-idx="' + i + '">';
            html += '<td>' + escapeHtml(p.english) + '</td>';
            html += '<td><input type="text" value="' + escapeHtml(p.spanish) + '" data-field="spanish"></td>';
            html += '<td>' + escapeHtml(p.type || 'other') + '</td>';
            html += '<td>' + escapeHtml(p.context || '') + '</td>';
            html += '<td class="term-actions">';
            html += '<button class="accepted" data-action="accept" data-idx="' + i + '">&#10003;</button>';
            html += '<button data-action="reject" data-idx="' + i + '">&#10007;</button>';
            html += '</td></tr>';
        });
        html += '</tbody></table>';
        container.innerHTML = html;

        // Mark all as accepted by default
        proposals.forEach(function(p) { p._accepted = true; });

        container.addEventListener('click', function(e) {
            var btn = e.target.closest('button[data-action]');
            if (!btn) return;
            var idx = parseInt(btn.dataset.idx, 10);
            var row = container.querySelector('tr[data-idx="' + idx + '"]');
            if (btn.dataset.action === 'reject') {
                proposals[idx]._accepted = false;
                row.classList.add('rejected');
                row.querySelector('[data-action="accept"]').classList.remove('accepted');
                btn.classList.add('rejected-btn');
            } else {
                proposals[idx]._accepted = true;
                row.classList.remove('rejected');
                btn.classList.add('accepted');
                row.querySelector('[data-action="reject"]').classList.remove('rejected-btn');
            }
        });

        // Track inline edits
        container.addEventListener('input', function(e) {
            if (e.target.dataset.field === 'spanish') {
                var row = e.target.closest('tr');
                var idx = parseInt(row.dataset.idx, 10);
                proposals[idx].spanish = e.target.value;
            }
        });
    }

    function escapeHtml(str) {
        var div = document.createElement('div');
        div.textContent = str || '';
        return div.innerHTML;
    }

    // ========================================================================
    // GLOSSARY: Save
    // ========================================================================

    document.getElementById('btn-save-glossary').addEventListener('click', function() {
        var accepted = glossaryProposals.filter(function(p) { return p._accepted; });
        if (accepted.length === 0) {
            alert('No terms accepted.');
            return;
        }
        // Strip internal fields
        var terms = accepted.map(function(p) {
            return {
                english: p.english,
                spanish: p.spanish,
                type: p.type || 'other',
                context: p.context || '',
                alternatives: p.alternatives || [],
            };
        });
        apiPost('/api/setup/' + PROJECT_ID + '/glossary', { terms: terms })
        .then(function(data) {
            var msg = 'Saved! ' + data.total + ' total terms (' + data.new + ' new)';
            document.getElementById('glossary-save-status').textContent = msg;
            setTimeout(function() {
                document.getElementById('glossary-save-status').textContent = '';
            }, 5000);
        });
    });

})();
