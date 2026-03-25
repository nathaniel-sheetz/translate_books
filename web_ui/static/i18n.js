/**
 * Internationalization (i18n) System for Translation Web UI
 *
 * Provides English/Spanish language toggle for UI elements while keeping
 * technical data (chunk IDs, file paths, translation prompts) in English.
 */

const I18N = {
    currentLanguage: 'en',

    /**
     * Translation string catalog
     * Keys use dot notation: section.element.variant
     */
    strings: {
        en: {
            // Setup form
            'setup.title': 'Translation Project Setup',
            'setup.chunksDir': 'Chunks Folder: *',
            'setup.chunksDirPlaceholder': 'chunks/',
            'setup.chunksDirHelp': 'Path relative to where you run the server (if run from project root: <code>chunks/</code>; if run from web_ui/: <code>../chunks/</code>)',
            'setup.projectName': 'Project Name:',
            'setup.projectNamePlaceholder': 'Translation Project',
            'setup.sourceLanguage': 'Source Language:',
            'setup.sourceLanguagePlaceholder': 'English',
            'setup.targetLanguage': 'Target Language:',
            'setup.targetLanguagePlaceholder': 'Spanish',
            'setup.uiLanguage': 'Interface Language:',
            'setup.glossary': 'Glossary (optional):',
            'setup.glossaryPlaceholder': 'glossary.json',
            'setup.glossaryHelp': 'Path relative to where you run the server',
            'setup.styleGuide': 'Style Guide (optional):',
            'setup.styleGuidePlaceholder': 'style_guide.json',
            'setup.styleGuideHelp': 'Path relative to where you run the server',
            'setup.includeContext': 'Include previous chapter context',
            'setup.contextParagraphs': 'Context Paragraphs:',
            'setup.contextParagraphsHelp': 'Number of paragraphs from end of previous chapter',
            'setup.minContextChars': 'Min Context Characters:',
            'setup.minContextCharsHint': 'Minimum characters of context (dual-constraint with paragraph count)',
            'setup.loadButton': 'Load Project',
            'setup.loadingButton': 'Loading...',

            // Workspace header
            'workspace.title': 'Translation Workspace',
            'workspace.chunks': 'Chunks',
            'workspace.progressText': '{completed} complete, {inReview} in review, {pending} pending',

            // Chunk info
            'chunk.loading': 'Loading...',
            'chunk.id': 'ID:',
            'chunk.words': 'Words:',
            'chunk.paragraphs': 'Paragraphs:',
            'chunk.displayName': 'Chapter {chapterNum}, Chunk {chunkNum}',
            'chunk.sidebarName': 'Chapter {chapterNum}, #{chunkNum}',

            // Chunk status
            'chunk.status.pending': 'Pending',
            'chunk.status.done': '✓ Done',
            'chunk.status.notes': '📝 {count} note',
            'chunk.status.notesPlural': '📝 {count} notes',

            // Mode toggle
            'mode.switchToReview': 'Switch to Review Mode',
            'mode.switchToTranslation': 'Switch to Translation Mode',

            // Translation section
            'translation.promptTitle': 'Prompt to Copy',
            'translation.copyButton': 'Copy to Clipboard',
            'translation.copiedButton': 'Copied!',
            'translation.pasteTitle': 'Paste Translation Here',
            'translation.pastePlaceholder': 'Paste the LLM\'s translation here...',
            'translation.saveButton': 'Save and go to next chunk',
            'translation.savingButton': 'Saving...',
            'translation.reviewButton': 'Save and review chunk',

            // Completion
            'completion.title': '🎉 All Chunks Completed!',
            'completion.message': 'All {total} chunks have been translated.',
            'completion.newProjectButton': 'Start New Project',

            // Review mode
            'review.title': 'Review Mode - {title}',
            'review.runEvalButton': 'Run Evaluation',
            'review.evaluatingButton': 'Evaluating...',
            'review.saveButton': 'Save Changes',
            'review.evalTitle': 'Evaluation Results',
            'review.sideBySideHeader': 'Source (English) | Translation (Spanish) - Editable',

            // Annotation panel
            'annotation.title': 'Add Annotation',
            'annotation.word': 'Word:',
            'annotation.type': 'Type:',
            'annotation.note': 'Note:',
            'annotation.notePlaceholder': 'Enter your note (optional)...',
            'annotation.tags': 'Tags:',
            'annotation.tagsPlaceholder': 'Tags (comma-separated)',
            'annotation.saveButton': 'Save Annotation',
            'annotation.deleteButton': 'Delete',
            'annotation.cancelButton': 'Cancel',
            'annotation.deleteConfirm': 'Delete this annotation?',

            // Annotation types (display only - data values stay in English)
            'annotationType.usageDoubt': 'Usage doubt',
            'annotationType.translationDoubt': 'Translation doubt',
            'annotationType.problem': 'Problem',
            'annotationType.other': 'Other',
            'annotationType.footnote': 'Footnote',

            // Evaluation results
            'eval.passed': 'Passed',
            'eval.failed': 'Failed',
            'eval.issue': '{count} issue',
            'eval.issuePlural': '{count} issues',
            'eval.error': '{count} error',
            'eval.errorPlural': '{count} errors',
            'eval.warning': '{count} warning',
            'eval.warningPlural': '{count} warnings',
            'eval.noIssues': 'No issues found - great job!',
            'eval.score': 'Score {score}',

            // Alert/error messages
            'alert.loadProjectFailed': 'Failed to load project',
            'alert.loadProjectError': 'Error loading project: {message}',
            'alert.noTranslation': 'Please paste a translation before submitting.',
            'alert.copyFailed': 'Failed to copy to clipboard: {message}',
            'alert.saveFailed': 'Failed to save translation',
            'alert.saveError': 'Error saving translation: {message}',
            'alert.noChunkLoaded': 'No chunk loaded',
            'alert.noTranslationYet': 'This chunk has no translation yet. Please translate it first.',
            'alert.loadChunkFailed': 'Failed to load chunk: {error}',
            'alert.paragraphMismatch': 'Warning: Paragraph count mismatch ({source} source vs {translation} translation). Add/remove blank lines to align.',
            'alert.evalFailed': 'Evaluation failed: {message}',
            'alert.annotationSaveFailed': 'Failed to save annotation: {message}',
            'alert.annotationDeleteFailed': 'Failed to delete annotation',
            'alert.reviewSaved': 'Translation saved successfully!',
            'alert.reviewSaveFailed': 'Failed to save: {message}',
        },

        es: {
            // Formulario de configuración
            'setup.title': 'Configuración del Proyecto de Traducción',
            'setup.chunksDir': 'Carpeta de Fragmentos: *',
            'setup.chunksDirPlaceholder': 'chunks/',
            'setup.chunksDirHelp': 'Ruta relativa desde donde ejecutas el servidor (si se ejecuta desde la raíz del proyecto: <code>chunks/</code>; si se ejecuta desde web_ui/: <code>../chunks/</code>)',
            'setup.projectName': 'Nombre del Proyecto:',
            'setup.projectNamePlaceholder': 'Proyecto de Traducción',
            'setup.sourceLanguage': 'Idioma de Origen:',
            'setup.sourceLanguagePlaceholder': 'Inglés',
            'setup.targetLanguage': 'Idioma de Destino:',
            'setup.targetLanguagePlaceholder': 'Español',
            'setup.uiLanguage': 'Idioma de la Interfaz:',
            'setup.glossary': 'Glosario (opcional):',
            'setup.glossaryPlaceholder': 'glossary.json',
            'setup.glossaryHelp': 'Ruta relativa desde donde ejecutas el servidor',
            'setup.styleGuide': 'Guía de Estilo (opcional):',
            'setup.styleGuidePlaceholder': 'style_guide.json',
            'setup.styleGuideHelp': 'Ruta relativa desde donde ejecutas el servidor',
            'setup.includeContext': 'Incluir contexto del capítulo anterior',
            'setup.contextParagraphs': 'Párrafos de Contexto:',
            'setup.contextParagraphsHelp': 'Número de párrafos del final del capítulo anterior',
            'setup.minContextChars': 'Mín. Caracteres de Contexto:',
            'setup.minContextCharsHint': 'Mínimo de caracteres de contexto (restricción dual con el conteo de párrafos)',
            'setup.loadButton': 'Cargar Proyecto',
            'setup.loadingButton': 'Cargando...',

            // Encabezado del espacio de trabajo
            'workspace.title': 'Espacio de Trabajo de Traducción',
            'workspace.chunks': 'Fragmentos',
            'workspace.progressText': '{completed} completados, {inReview} en revisión, {pending} pendientes',

            // Información del fragmento
            'chunk.loading': 'Cargando...',
            'chunk.id': 'ID:',
            'chunk.words': 'Palabras:',
            'chunk.paragraphs': 'Párrafos:',
            'chunk.displayName': 'Capítulo {chapterNum}, Fragmento {chunkNum}',
            'chunk.sidebarName': 'Capítulo {chapterNum}, #{chunkNum}',

            // Estado del fragmento
            'chunk.status.pending': 'Pendiente',
            'chunk.status.done': '✓ Hecho',
            'chunk.status.notes': '📝 {count} nota',
            'chunk.status.notesPlural': '📝 {count} notas',

            // Alternar modo
            'mode.switchToReview': 'Cambiar a Modo de Revisión',
            'mode.switchToTranslation': 'Cambiar a Modo de Traducción',

            // Sección de traducción
            'translation.promptTitle': 'Prompt para Copiar',
            'translation.copyButton': 'Copiar al Portapapeles',
            'translation.copiedButton': '¡Copiado!',
            'translation.pasteTitle': 'Pegar Traducción Aquí',
            'translation.pastePlaceholder': 'Pega la traducción del LLM aquí...',
            'translation.saveButton': 'Guardar e ir al siguiente fragmento',
            'translation.savingButton': 'Guardando...',
            'translation.reviewButton': 'Guardar y revisar fragmento',

            // Finalización
            'completion.title': '🎉 ¡Todos los Fragmentos Completados!',
            'completion.message': 'Los {total} fragmentos han sido traducidos.',
            'completion.newProjectButton': 'Iniciar Nuevo Proyecto',

            // Modo de revisión
            'review.title': 'Modo de Revisión - {title}',
            'review.runEvalButton': 'Ejecutar Evaluación',
            'review.evaluatingButton': 'Evaluando...',
            'review.saveButton': 'Guardar Cambios',
            'review.evalTitle': 'Resultados de la Evaluación',
            'review.sideBySideHeader': 'Fuente (Inglés) | Traducción (Español) - Editable',

            // Panel de anotaciones
            'annotation.title': 'Agregar Anotación',
            'annotation.word': 'Palabra:',
            'annotation.type': 'Tipo:',
            'annotation.note': 'Nota:',
            'annotation.notePlaceholder': 'Ingresa tu nota (opcional)...',
            'annotation.tags': 'Etiquetas:',
            'annotation.tagsPlaceholder': 'Etiquetas (separadas por comas)',
            'annotation.saveButton': 'Guardar Anotación',
            'annotation.deleteButton': 'Eliminar',
            'annotation.cancelButton': 'Cancelar',
            'annotation.deleteConfirm': '¿Eliminar esta anotación?',

            // Tipos de anotación (solo visualización - los valores de datos permanecen en inglés)
            'annotationType.usageDoubt': 'Duda de uso',
            'annotationType.translationDoubt': 'Duda de traducción',
            'annotationType.problem': 'Problema',
            'annotationType.other': 'Otro',
            'annotationType.footnote': 'Nota al pie',

            // Resultados de evaluación
            'eval.passed': 'Aprobado',
            'eval.failed': 'Fallido',
            'eval.issue': '{count} problema',
            'eval.issuePlural': '{count} problemas',
            'eval.error': '{count} error',
            'eval.errorPlural': '{count} errores',
            'eval.warning': '{count} advertencia',
            'eval.warningPlural': '{count} advertencias',
            'eval.noIssues': '¡No se encontraron problemas - excelente trabajo!',
            'eval.score': 'Puntuación {score}',

            // Mensajes de alerta/error
            'alert.loadProjectFailed': 'Error al cargar el proyecto',
            'alert.loadProjectError': 'Error al cargar el proyecto: {message}',
            'alert.noTranslation': 'Por favor, pega una traducción antes de enviar.',
            'alert.copyFailed': 'Error al copiar al portapapeles: {message}',
            'alert.saveFailed': 'Error al guardar la traducción',
            'alert.saveError': 'Error al guardar la traducción: {message}',
            'alert.noChunkLoaded': 'No hay fragmento cargado',
            'alert.noTranslationYet': 'Este fragmento aún no tiene traducción. Por favor, tradúcelo primero.',
            'alert.loadChunkFailed': 'Error al cargar el fragmento: {error}',
            'alert.paragraphMismatch': 'Advertencia: Desajuste en el conteo de párrafos ({source} fuente vs {translation} traducción). Agrega/elimina líneas en blanco para alinear.',
            'alert.evalFailed': 'La evaluación falló: {message}',
            'alert.annotationSaveFailed': 'Error al guardar la anotación: {message}',
            'alert.annotationDeleteFailed': 'Error al eliminar la anotación',
            'alert.reviewSaved': '¡Traducción guardada exitosamente!',
            'alert.reviewSaveFailed': 'Error al guardar: {message}',
        }
    },

    /**
     * Get translated string by key with parameter substitution
     * @param {string} key - Translation key (e.g., 'setup.title')
     * @param {object} params - Optional parameters for substitution (e.g., {count: 5})
     * @returns {string} Translated string
     */
    t(key, params = {}) {
        let str = this.strings[this.currentLanguage][key];

        // Fallback to English if translation missing
        if (!str) {
            str = this.strings.en[key] || key;
        }

        // Simple template variable substitution: {varName}
        Object.keys(params).forEach(k => {
            str = str.replace(new RegExp(`\\{${k}\\}`, 'g'), params[k]);
        });

        return str;
    },

    /**
     * Handle pluralization for count-based strings
     * @param {number} count - The count value
     * @param {string} singularKey - Translation key for singular form
     * @param {string} pluralKey - Translation key for plural form
     * @param {object} params - Optional additional parameters
     * @returns {string} Translated string with count
     */
    plural(count, singularKey, pluralKey, params = {}) {
        const key = count === 1 ? singularKey : pluralKey;
        return this.t(key, { count, ...params });
    },

    /**
     * Set the UI language
     * @param {string} lang - Language code ('en' or 'es')
     */
    setLanguage(lang) {
        if (!this.strings[lang]) {
            console.warn(`Language '${lang}' not supported, falling back to English`);
            lang = 'en';
        }
        this.currentLanguage = lang;
        this.updateUI();
    },

    /**
     * Initialize i18n system
     * Always defaults to English (no auto-detection or persistence)
     */
    init() {
        this.currentLanguage = 'en';
    },

    /**
     * Update all UI elements with data-i18n attributes
     * This is called when language changes
     */
    updateUI() {
        // Update elements with data-i18n attribute
        document.querySelectorAll('[data-i18n]').forEach(el => {
            const key = el.dataset.i18n;

            // Handle placeholders for input fields
            if (el.placeholder !== undefined && el.dataset.i18nPlaceholder) {
                el.placeholder = this.t(el.dataset.i18nPlaceholder);
            }

            // Update text/HTML content (skip if has data-i18n-skip attribute)
            if (!el.dataset.i18nSkip) {
                // Use innerHTML for elements that contain HTML (small/help text)
                if (el.tagName === 'SMALL') {
                    el.innerHTML = this.t(key);
                } else {
                    el.textContent = this.t(key);
                }
            }
        });

        // Update placeholders separately
        document.querySelectorAll('[data-i18n-placeholder]').forEach(el => {
            if (el.placeholder !== undefined) {
                el.placeholder = this.t(el.dataset.i18nPlaceholder);
            }
        });

        // Trigger re-render of dynamic content (if workspace is visible)
        if (window.location.hash !== '#setup' && document.getElementById('workspace').style.display !== 'none') {
            if (typeof window.renderCurrentView === 'function') {
                window.renderCurrentView();
            }
        }
    }
};

// Auto-initialize on page load
if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', () => I18N.init());
} else {
    I18N.init();
}
