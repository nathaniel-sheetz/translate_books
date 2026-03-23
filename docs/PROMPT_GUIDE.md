# Prompt Template Guide

## Table of Contents

1. [Introduction](#introduction)
2. [Quick Start](#quick-start)
3. [Template Basics](#template-basics)
4. [Standard Variables](#standard-variables)
5. [Creating Custom Prompts](#creating-custom-prompts)
6. [Project Overrides](#project-overrides)
7. [Testing Prompts](#testing-prompts)
8. [Best Practices](#best-practices)
9. [Examples](#examples)
10. [Troubleshooting](#troubleshooting)

---

## Introduction

The Book Translation Workflow uses **Jinja2 template engine** for all LLM prompts. This provides:

- **Flexibility**: Customize prompts per project without changing code
- **Consistency**: Standard variables across all prompts
- **Maintainability**: Version control your prompts alongside code
- **Reproducibility**: Track which prompt version was used for each translation

Templates support variable substitution, conditional sections, and dynamic content generation.

---

## Quick Start

### Using Default Prompts

By default, projects use the global templates in `prompts/`:

```bash
# Initialize a project (uses default prompts automatically)
book-translate init my_book

# Translate using default translation prompt
book-translate translate my_book --mode api
```

### Customizing a Prompt

To customize a prompt for your specific project:

```bash
# Copy global template to your project
cp prompts/translation.txt.jinja projects/my_book/prompts/

# Edit the project-specific version
nano projects/my_book/prompts/translation.txt.jinja

# System automatically uses your custom version
book-translate translate my_book --mode api
```

---

## Template Basics

### Jinja2 Syntax Overview

Prompts use Jinja2 template syntax:

**Variables** (substituted with values):
```jinja2
{{variable_name}}
```

**Conditionals** (include section only if variable exists):
```jinja2
{% if variable_name %}
This text only appears if variable_name is provided.
{% endif %}
```

**Comments** (not included in rendered output):
```jinja2
{# This is a comment explaining the template #}
```

### Example Template

```jinja2
{# Basic translation prompt with conditional glossary #}
You are translating from {{source_language}} to {{target_language}}.

Project: {{project_name}}

{% if glossary %}
Use these translations consistently:
{{glossary}}
{% endif %}

Translate this text:
{{source_text}}
```

When rendered with variables:
- `source_language = "en"`
- `target_language = "es"`
- `project_name = "don_quixote"`
- `glossary = "- Sancho: Sancho\n- Rocinante: Rocinante"`
- `source_text = "In a village of La Mancha..."`

Result:
```
You are translating from en to es.

Project: don_quixote

Use these translations consistently:
- Sancho: Sancho
- Rocinante: Rocinante

Translate this text:
In a village of La Mancha...
```

---

## Standard Variables

All translation prompts have access to these standard variables:

### Core Variables (Always Available)

| Variable | Type | Description | Example |
|----------|------|-------------|---------|
| `project_name` | str | Project identifier | `"don_quixote"` |
| `source_language` | str | Source language code | `"en"` |
| `target_language` | str | Target language code | `"es"` |
| `chunk_id` | str | Current chunk ID | `"ch01_chunk_003"` |
| `chapter_id` | str | Current chapter ID | `"chapter_01"` |

### Content Variables

| Variable | Type | Description | Required For |
|----------|------|-------------|--------------|
| `source_text` | str | Text to translate | Translation prompts |
| `excerpt` | str | Sample text for analysis | Context generation |

### Optional Enhancement Variables

| Variable | Type | Description | Default |
|----------|------|-------------|---------|
| `glossary` | str | Formatted glossary terms | `None` (not included) |
| `book_context` | str | Book description, genre, style | `None` |
| `style_guide` | str | Translation style guidelines | `None` |

### Custom Variables

You can add project-specific variables via `config.json`:

```json
{
  "translation": {
    "prompt_template": "translation.txt.jinja",
    "prompt_variables": {
      "formality_level": "formal",
      "target_audience": "adult readers",
      "preserve_archaisms": true
    }
  }
}
```

Access in templates:
```jinja2
{% if formality_level %}
Use {{formality_level}} register throughout.
{% endif %}
```

---

## Creating Custom Prompts

### Step 1: Start with a Template

Begin with an existing prompt:

```bash
# Copy default translation prompt
cp prompts/translation.txt.jinja my_custom_prompt.txt.jinja
```

### Step 2: Modify for Your Needs

Edit the template to match your book's requirements:

```jinja2
{# Custom prompt for translating poetry #}
You are translating poetry from {{source_language}} to {{target_language}}.

Project: {{project_name}}

IMPORTANT: This is poetry translation. Focus on:
- Preserving meter and rhythm where possible
- Maintaining rhyme schemes when feasible
- Capturing emotional tone over literal meaning
- Using natural, flowing {{target_language}}

{% if glossary %}
Key terms to use consistently:
{{glossary}}
{% endif %}

{% if book_context %}
Context about this poem:
{{book_context}}
{% endif %}

Poem to translate:
{{source_text}}

Provide only the translated poem, maintaining line breaks.
```

### Step 3: Test the Template

Test your template before using it in production:

```bash
# Validate syntax
book-translate validate-prompt my_custom_prompt.txt.jinja

# Test render with sample data
book-translate test-prompt my_custom_prompt.txt.jinja \
  --project my_book \
  --sample-text "The rose is red"
```

### Step 4: Use in Your Project

Add to your project configuration:

```json
{
  "translation": {
    "prompt_template": "my_custom_prompt.txt.jinja"
  }
}
```

---

## Project Overrides

### Override Resolution Order

The system searches for prompts in this order:

1. **Project-specific**: `projects/{project_name}/prompts/{template_name}`
2. **Global default**: `prompts/{template_name}`
3. **Error**: If neither found, raises `FileNotFoundError`

### Creating an Override

**Method 1: Manual Copy**
```bash
# Copy to project directory
mkdir -p projects/my_book/prompts
cp prompts/translation.txt.jinja projects/my_book/prompts/

# Edit project version
nano projects/my_book/prompts/translation.txt.jinja
```

**Method 2: Using CLI** (when implemented)
```bash
# Create override from default
book-translate customize-prompt my_book translation.txt.jinja

# Opens editor automatically
```

**Method 3: Programmatic**
```python
from pathlib import Path
from src.utils.file_io import create_project_prompt_override

create_project_prompt_override(
    project_path=Path("projects/my_book"),
    prompt_name="translation.txt.jinja"
)
```

### Mixing Defaults and Overrides

You can override some prompts while using defaults for others:

```
projects/my_book/
├── prompts/
│   └── translation.txt.jinja      ← Custom translation prompt
└── config.json

# Context generation still uses default:
prompts/
├── translation.txt.jinja           ← Default (unused for my_book)
└── context_generation.txt.jinja   ← Default (used by my_book)
```

---

## Testing Prompts

### Syntax Validation

Check template syntax without rendering:

```bash
# Validate single template
book-translate validate-prompt translation.txt.jinja

# Validate all prompts in project
book-translate validate-prompts projects/my_book
```

Returns:
- ✓ Valid Jinja2 syntax
- ✓ File exists and is readable
- ⚠ Warnings for unused variables
- ✗ Errors for syntax problems

### Test Rendering

Render template with sample data:

```bash
book-translate test-prompt translation.txt.jinja \
  --project my_book \
  --source-text "Sample text to translate" \
  --glossary glossary.json \
  --show-variables
```

Output:
```
=== Variables Used ===
project_name: my_book
source_language: en
target_language: es
glossary: [formatted glossary]
source_text: Sample text to translate

=== Rendered Prompt ===
[Full rendered prompt shown here]

=== Stats ===
Length: 450 characters
Estimated tokens: ~112
```

### A/B Testing Prompts

Compare results from different prompts:

```bash
# Translate same chunk with two prompts
book-translate translate my_book chunk_01 \
  --prompt translation_v1.txt.jinja \
  --output translation_v1.txt

book-translate translate my_book chunk_01 \
  --prompt translation_v2.txt.jinja \
  --output translation_v2.txt

# Compare results
diff translation_v1.txt translation_v2.txt
```

---

## Best Practices

### Template Design

**DO**:
- ✓ Use conditional blocks for optional variables: `{% if var %}...{% endif %}`
- ✓ Add comments explaining each section: `{# This section... #}`
- ✓ Keep prompts focused on single responsibility
- ✓ Test with missing/partial variables
- ✓ Keep total length under 2000 tokens when rendered
- ✓ Use clear, specific instructions
- ✓ Provide examples in the prompt when helpful

**DON'T**:
- ✗ Hard-code project-specific details (use variables)
- ✗ Use undefined variables without conditionals
- ✗ Make prompts excessively verbose
- ✗ Include sensitive data in templates
- ✗ Assume variables will always be present
- ✗ Use complex logic (if/else chains)—keep templates simple

### Variable Naming

Use descriptive, consistent names:
- `source_text` not `text` or `input`
- `book_context` not `context` (ambiguous)
- `target_language` not `lang` (unclear)

### Documentation

Document your custom prompts:

```jinja2
{#
Custom translation prompt for poetry
Author: Jane Doe
Version: 2.1
Last Updated: 2025-01-28

This prompt is optimized for:
- Poetry and verse translation
- Maintaining meter and rhyme
- Preserving emotional resonance

Variables required:
- source_text (required)
- project_name (required)
- glossary (optional)
- book_context (optional)
#}

You are translating poetry from {{source_language}} to {{target_language}}.
...
```

### Version Control

Track prompt changes in git:

```bash
# Commit prompt changes with descriptive messages
git add projects/my_book/prompts/translation.txt.jinja
git commit -m "Update translation prompt: add meter preservation instructions"
```

Tag important versions:
```bash
git tag -a prompt-v2.0 -m "Translation prompt v2.0 - improved glossary handling"
```

---

## Examples

### Example 1: Basic Translation Prompt

**File**: `prompts/translation.txt.jinja`

```jinja2
{# Default translation prompt for general fiction #}
You are translating a book from {{source_language}} to {{target_language}}.

Project: {{project_name}}

{% if book_context %}
Book Context:
{{book_context}}
{% endif %}

{% if glossary %}
Translation Glossary (use these exact translations):
{{glossary}}
{% endif %}

{% if style_guide %}
Style Guidelines:
{{style_guide}}
{% endif %}

Instructions:
- Preserve paragraph structure exactly
- Maintain the author's tone and style
- Use natural, fluent {{target_language}}
- Keep special formatting (emphasis, etc.)
- Translate idioms to natural equivalents

Source Text:
{{source_text}}

Provide only the translated text without explanation.
```

### Example 2: Context Generation Prompt

**File**: `prompts/context_generation.txt.jinja`

```jinja2
{# Generate book context from excerpt #}
You are preparing context information for translating a book.

Book: {{project_name}}
Translation: {{source_language}} → {{target_language}}

Based on this excerpt from the book, provide:

1. **Genre & Style**: What genre is this? What's the writing style? (formal/informal, modern/archaic, descriptive/sparse)

2. **Time Period & Setting**: When and where is this set? Are there historical or cultural elements to consider?

3. **Tone**: What's the emotional tone? (serious, humorous, dark, uplifting, etc.)

4. **Translation Considerations**: What specific challenges might arise in {{target_language}} translation?

Excerpt:
{{excerpt}}

Provide a concise summary (2-3 paragraphs) that will guide consistent translation throughout the book.
```

### Example 3: Custom Prompt for Technical Books

```jinja2
{# Technical manual translation #}
You are translating a technical manual from {{source_language}} to {{target_language}}.

Project: {{project_name}}

{% if glossary %}
CRITICAL: Use these exact technical terms:
{{glossary}}
{% endif %}

Translation Requirements:
- Accuracy over fluency (precision is critical)
- Preserve all technical terminology
- Maintain numbered lists and formatting exactly
- Keep code snippets, commands, and paths unchanged
- Translate UI labels as shown in glossary
- Keep ALL placeholders like {{example}} intact

{% if formality_level %}
Formality: Use {{formality_level}} register
{% endif %}

Source Text:
{{source_text}}

Provide only the translated text. Do not translate code, commands, or placeholders.
```

---

## Troubleshooting

### Error: Template Not Found

**Problem**: `FileNotFoundError: Template 'translation.txt.jinja' not found`

**Solutions**:
1. Check filename spelling (case-sensitive)
2. Verify template exists in `prompts/` or `projects/{name}/prompts/`
3. Check config.json `prompt_template` path

```bash
# List available templates
ls prompts/
ls projects/my_book/prompts/
```

### Error: Undefined Variable

**Problem**: `jinja2.UndefinedError: 'glossary' is undefined`

**Cause**: Template uses variable without conditional check

**Fix**: Wrap optional variables in conditionals:

❌ **Wrong**:
```jinja2
Use these terms:
{{glossary}}
```

✓ **Correct**:
```jinja2
{% if glossary %}
Use these terms:
{{glossary}}
{% endif %}
```

### Error: Template Syntax Error

**Problem**: `jinja2.TemplateSyntaxError: unexpected '}'`

**Cause**: Malformed Jinja2 syntax

**Fix**: Check for:
- Unmatched `{% if %}` without `{% endif %}`
- Wrong brackets: `{glossary}` should be `{{glossary}}`
- Missing closing tags

Validate before using:
```bash
book-translate validate-prompt my_template.txt.jinja
```

### Warning: Large Prompt Size

**Problem**: Warning: Rendered prompt is 3500 tokens (recommended: <2000)

**Solutions**:
1. Simplify instructions
2. Move detailed guidelines to `style_guide` variable
3. Reduce glossary to essential terms only
4. Split into multiple specialized prompts

### Variable Not Substituting

**Problem**: Rendered prompt shows `{{variable_name}}` literally

**Causes**:
1. Variable not passed to renderer
2. Typo in variable name
3. Template loaded as plain text, not Jinja2

**Debug**:
```bash
# Show which variables are being passed
book-translate test-prompt my_template.txt.jinja \
  --project my_book \
  --show-variables \
  --debug
```

---

## Related Documentation

- **[DESIGN.md](DESIGN.md)**: Architecture and data models (see Prompt Management section)
- **[prompts/README.md](prompts/README.md)**: Directory structure and available templates
- **[prompts/VARIABLES.md](prompts/VARIABLES.md)**: Complete variable reference
- **[GETTING_STARTED.md](GETTING_STARTED.md)**: Customizing prompts tutorial

---

## Quick Reference Card

### Common Variable Patterns

```jinja2
{# Required variable #}
{{project_name}}

{# Optional variable #}
{% if glossary %}{{glossary}}{% endif %}

{# Optional section #}
{% if book_context %}
Context: {{book_context}}
{% endif %}

{# Comment #}
{# This explains the template #}

{# Custom variable with default #}
Register: {{formality_level | default("neutral")}}
```

### CLI Commands Quick Reference

```bash
# Validate syntax
book-translate validate-prompt <template>

# Test render
book-translate test-prompt <template> --project <name>

# Create project override
cp prompts/<template> projects/<name>/prompts/

# List variables in template
book-translate show-variables <template>
```

---

**Document Version**: 1.0
**Last Updated**: 2025-01-28
**For Book Translation Workflow v1.0**
