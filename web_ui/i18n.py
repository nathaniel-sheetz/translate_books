"""Internationalization strings for the reader UI."""

STRINGS = {
    "en": {
        # Nav / project list
        "reader_title": "Reader",
        "projects_back": "Projects",
        "chapters_count": "chapters",
        "no_projects": "No projects with alignment data found. Run the pipeline first.",
        "no_projects_dir": "No projects directory found. Create a project and run the pipeline first.",
        "not_found": "Not Found",
        "chapter_not_found": 'Chapter "{chapter}" not found in project "{project}".',
        "project_not_found": 'Project "{project}" has no alignment data.',
        "run_alignment": "Run the alignment step first.",
        "language_label": "Language",
        "chapter_prefix": "Chapter",

        # Chapter list
        "pending_corrections": "Pending corrections not yet applied",
        "apply_corrections": "Apply",
        "applying": "Applying...",
        "apply_first_time": "First run may take ~1 min to load the alignment model.",
        "apply_done": "corrections applied to",
        "apply_chapters": "chapters",
        "badge_review": "to review",
        "badge_fn": "fn",
        "badge_flag": "flag",
        "badge_aligned": "aligned",
        "badge_reviewed": "reviewed",
        "badge_unread": "unread",

        # Reader view
        "sheet_label": "Original",
        "note_placeholder": "Optional note...",
        "edit_placeholder": "Edit translation...",
        "btn_save": "Save",
        "loading": "Loading...",

        # JS strings (injected as window.__i18n)
        "js": {
            "sentences": "sentences",
            "annotated": "annotated",
            "mark_reviewed": "Mark as reviewed",
            "reviewed_check": "Reviewed \u2713",
            "error_prefix": "Error: ",
            "saving": "Saving...",
            "save": "Save",
            "error_alignment": "Alignment not found",
            "error_saving": "Error saving: ",
            "network_error": "Network error: ",
            "ann_word_choice": "\U0001f4ac Word choice",
            "ann_inconsistency": "\u26a0 Inconsistency",
            "ann_footnote": "\U0001f4d6 Footnote",
            "ann_flag": "\U0001f6a9 Flag",
        },
    },
    "es": {
        # Nav / project list
        "reader_title": "Lector",
        "projects_back": "Proyectos",
        "chapters_count": "cap\u00edtulos",
        "no_projects": "No se encontraron proyectos con datos de alineaci\u00f3n. Ejecute el pipeline primero.",
        "no_projects_dir": "No se encontr\u00f3 directorio de proyectos. Cree un proyecto y ejecute el pipeline primero.",
        "not_found": "No encontrado",
        "chapter_not_found": 'Cap\u00edtulo "{chapter}" no encontrado en el proyecto "{project}".',
        "project_not_found": 'El proyecto "{project}" no tiene datos de alineaci\u00f3n.',
        "run_alignment": "Ejecute el paso de alineaci\u00f3n primero.",
        "language_label": "Idioma",
        "chapter_prefix": "Cap\u00edtulo",

        # Chapter list
        "pending_corrections": "Correcciones pendientes sin aplicar",
        "apply_corrections": "Aplicar",
        "applying": "Aplicando...",
        "apply_first_time": "La primera vez puede tardar ~1 min en cargar el modelo de alineaci\u00f3n.",
        "apply_done": "correcciones aplicadas a",
        "apply_chapters": "cap\u00edtulos",
        "badge_review": "por revisar",
        "badge_fn": "nota",
        "badge_flag": "marca",
        "badge_aligned": "alineado",
        "badge_reviewed": "revisado",
        "badge_unread": "sin leer",

        # Reader view
        "sheet_label": "Original",
        "note_placeholder": "Nota opcional...",
        "edit_placeholder": "Editar traducci\u00f3n...",
        "btn_save": "Guardar",
        "loading": "Cargando...",

        # JS strings
        "js": {
            "sentences": "oraciones",
            "annotated": "anotadas",
            "mark_reviewed": "Marcar como revisado",
            "reviewed_check": "Revisado \u2713",
            "error_prefix": "Error: ",
            "saving": "Guardando...",
            "save": "Guardar",
            "error_alignment": "Alineaci\u00f3n no encontrada",
            "error_saving": "Error al guardar: ",
            "network_error": "Error de red: ",
            "ann_word_choice": "\U0001f4ac Elecci\u00f3n de palabra",
            "ann_inconsistency": "\u26a0 Inconsistencia",
            "ann_footnote": "\U0001f4d6 Nota al pie",
            "ann_flag": "\U0001f6a9 Marca",
        },
    },
}


def get_strings(lang: str = "en") -> dict:
    """Return the translation dict for a language, falling back to English."""
    return STRINGS.get(lang, STRINGS["en"])
