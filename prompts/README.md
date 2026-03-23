# Prompt Templates Directory

This directory contains default Jinja2 prompt templates used throughout the Book Translation Workflow.

## Overview

Templates in this directory serve as **global defaults** for all projects. Individual projects can override these defaults by creating their own versions in `projects/{project_name}/prompts/`.

## Directory Structure

```
prompts/
├── README.md                        # This file
├── VARIABLES.md                     # Complete variable reference
├── translation.txt.jinja            # Default translation prompt
└── context_generation.txt.jinja     # Context generation prompt
```

When Task 1.6 is implemented, this directory will contain the actual template files.

## How Templates Work

### Template Resolution

When the system needs a prompt template, it searches in this order:

1. **Project Override**: `projects/{project_name}/prompts/{template_name}`
   - If found, use this version
2. **Global Default**: `prompts/{template_name}` (this directory)
   - Fallback if no project override exists
3. **Error**: If neither exists, raise `FileNotFoundError`

### Example

Given this structure:
```
prompts/
├── translation.txt.jinja            # Global default
└── context_generation.txt.jinja     # Global default

projects/
└── don_quixote/
    └── prompts/
        └── translation.txt.jinja    # Project override
```

**For project "don_quixote"**:
- `translation.txt.jinja` → Uses project override (`projects/don_quixote/prompts/`)
- `context_generation.txt.jinja` → Uses global default (`prompts/`)

**For project "alice_in_wonderland"** (no overrides):
- `translation.txt.jinja` → Uses global default (`prompts/`)
- `context_generation.txt.jinja` → Uses global default (`prompts/`)

## Available Templates

### `translation.txt.jinja`

**Purpose**: Main prompt for translating text chunks

**Variables**:
- **Required**: `source_text`, `project_name`, `source_language`, `target_language`
- **Optional**: `glossary`, `book_context`, `style_guide`, `chunk_id`, `chapter_id`

**When Used**: During the translation stage (Phase 4)

**Example Usage**:
```python
from src.utils.file_io import load_and_render_prompt

prompt, path = load_and_render_prompt(
    prompt_name="translation.txt.jinja",
    project_path=Path("projects/my_book"),
    variables={
        "source_text": chunk.source_text,
        "project_name": "my_book",
        "source_language": "en",
        "target_language": "es",
        "glossary": formatted_glossary,
    }
)
```

### `context_generation.txt.jinja`

**Purpose**: Generate book context from excerpts to guide translation

**Variables**:
- **Required**: `excerpt`, `project_name`, `source_language`, `target_language`
- **Optional**: None

**When Used**: Initial project setup or when analyzing new content

**Example Usage**:
```python
prompt, path = load_and_render_prompt(
    prompt_name="context_generation.txt.jinja",
    project_path=Path("projects/my_book"),
    variables={
        "excerpt": first_chapter[:2000],  # First 2000 chars
        "project_name": "my_book",
        "source_language": "en",
        "target_language": "es",
    }
)
```

## Creating Custom Templates

### Option 1: Project Override (Recommended)

Override a default template for a specific project:

```bash
# Create project prompts directory
mkdir -p projects/my_book/prompts

# Copy default template
cp prompts/translation.txt.jinja projects/my_book/prompts/

# Edit for your project
nano projects/my_book/prompts/translation.txt.jinja
```

Your project will now use the customized version automatically.

### Option 2: Create New Global Template

Add a new template type to this directory:

```bash
# Create new template
nano prompts/my_new_template.txt.jinja

# Reference in project config
{
  "translation": {
    "prompt_template": "my_new_template.txt.jinja"
  }
}
```

All projects can now use this template.

## Template Syntax

Templates use **Jinja2** syntax:

### Variables
```jinja2
{{variable_name}}
```

### Conditionals
```jinja2
{% if variable_name %}
This section only appears if variable_name is provided.
{% endif %}
```

### Comments
```jinja2
{# This comment won't appear in the rendered output #}
```

### Complete Example
```jinja2
{# Translation prompt with optional glossary #}
You are translating from {{source_language}} to {{target_language}}.

Project: {{project_name}}

{% if glossary %}
Key Terms:
{{glossary}}
{% endif %}

Translate:
{{source_text}}
```

## Testing Templates

### Validate Syntax
```bash
# Check template syntax
book-translate validate-prompt prompts/translation.txt.jinja

# Validate all templates in directory
book-translate validate-prompts prompts/
```

### Test Rendering
```bash
# Render with sample data
book-translate test-prompt translation.txt.jinja \
  --project my_book \
  --source-text "Sample text" \
  --show-variables
```

## Best Practices

### For Global Templates (This Directory)

**DO**:
- ✓ Keep templates general and reusable
- ✓ Use conditionals for optional features
- ✓ Document all variables in comments
- ✓ Provide clear, unambiguous instructions
- ✓ Test with multiple variable combinations
- ✓ Keep length reasonable (<2000 tokens)

**DON'T**:
- ✗ Hard-code project-specific details
- ✗ Assume optional variables are always present
- ✗ Make prompts overly complex
- ✗ Include sensitive information

### For Project Overrides

**DO**:
- ✓ Start by copying a global template
- ✓ Document what you changed and why
- ✓ Test before using in production
- ✓ Version control your custom prompts
- ✓ Share successful patterns with team

**DON'T**:
- ✗ Override without good reason (defaults are tested)
- ✗ Forget to test with your project's data
- ✗ Make templates too project-specific (limits reuse)

## Variables Reference

For complete documentation of all available template variables, see:
- **[VARIABLES.md](VARIABLES.md)** - Complete variable reference with examples
- **[PROMPT_GUIDE.md](../PROMPT_GUIDE.md)** - Full guide to working with prompts

## File Naming Conventions

Templates should follow this naming pattern:

- **Name**: Descriptive, lowercase, underscores for spaces
- **Extension**: `.txt.jinja` (indicates text template using Jinja2)

**Examples**:
- ✓ `translation.txt.jinja` - Clear and descriptive
- ✓ `context_generation.txt.jinja` - Multi-word with underscores
- ✓ `quality_evaluation.txt.jinja` - Future template
- ✗ `trans.jinja` - Too abbreviated
- ✗ `TranslationPrompt.txt` - Not lowercase, wrong extension

## Maintenance

### Adding New Templates

1. Create template file in this directory
2. Add documentation to this README
3. Add variables to VARIABLES.md
4. Create tests in `tests/test_prompt_*`
5. Update DESIGN.md if needed
6. Commit with descriptive message

### Modifying Existing Templates

**IMPORTANT**: Changes to global templates affect ALL projects that don't have overrides.

Before modifying:
1. Test changes thoroughly
2. Check if any projects have overrides
3. Consider version compatibility
4. Update documentation
5. Announce breaking changes

### Deprecating Templates

To deprecate a template:
1. Add deprecation notice to template comments
2. Update this README with deprecation warning
3. Provide migration path to replacement
4. Keep deprecated template for backward compatibility
5. Remove in next major version

## Troubleshooting

### Template Not Found

**Error**: `FileNotFoundError: Template 'translation.txt.jinja' not found`

**Check**:
1. Filename is correct (case-sensitive)
2. File exists in `prompts/` or `projects/{name}/prompts/`
3. Config `prompt_template` path matches actual filename

### Template Syntax Error

**Error**: `jinja2.TemplateSyntaxError: unexpected '}'`

**Fix**:
1. Validate syntax: `book-translate validate-prompt <file>`
2. Check for unmatched `{% if %}` / `{% endif %}`
3. Verify bracket types: `{{variable}}` not `{variable}`

### Variable Not Rendering

**Problem**: Variable shows as literal `{{var}}` in output

**Solutions**:
1. Ensure template loaded via Jinja2 (not plain text)
2. Check variable name spelling
3. Verify variable passed to render function

## Related Documentation

- **[PROMPT_GUIDE.md](../PROMPT_GUIDE.md)** - Comprehensive guide to working with prompts
- **[VARIABLES.md](VARIABLES.md)** - Complete variable reference
- **[DESIGN.md](../design.md)** - Architecture and design decisions (Prompt Management section)
- **[GETTING_STARTED.md](../GETTING_STARTED.md)** - Quick start with custom prompts

## Questions?

For detailed guidance on:
- **Creating custom prompts**: See [PROMPT_GUIDE.md](../PROMPT_GUIDE.md#creating-custom-prompts)
- **Understanding variables**: See [VARIABLES.md](VARIABLES.md)
- **Testing templates**: See [PROMPT_GUIDE.md](../PROMPT_GUIDE.md#testing-prompts)
- **Troubleshooting**: See [PROMPT_GUIDE.md](../PROMPT_GUIDE.md#troubleshooting)

---

**Note**: This directory currently contains documentation only. Actual template files will be created during **Task 1.6: Default Prompt Templates** implementation.

**Document Version**: 1.0
**Last Updated**: 2025-01-28
