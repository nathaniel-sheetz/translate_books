# Writing guide for a book's site texts and «Sobre este libro»

The `book-metadata` skill reads this before it drafts. It holds the voice, the writing rules and the texts to
imitate. This is the tracked example, with invented books. Copy it to `prompts/book_metadata_guide.md`
(gitignored) and make it yours: replace the models with texts you have approved, and add a dated rule
whenever your feedback on a draft changes how a text is written.

## Voice

- **Language and variety:** Spanish, the variety your readers use.
- **Edition facts and translation decisions are impersonal:** «Se omite el prefacio», «Se corrigen sin
  marcarlos…».
- **Anything addressed to the parent uses _usted_:** «Léalo antes si su hijo es sensible».
- **The adaptations cover every _kind_ of change** the edition made. They need not list every instance.

## Rules for every text

- **Short.** A few sentences per topic. One example where a list could go.
- **No evidence for points that aren't problems.** «El libro no tiene malas palabras» is enough.
- **Don't list what the edition already handles.** The parent notes leave out anything fixed in the
  translation or covered by a note. The adaptations name those changes by category.
- **No plot beyond what the parent needs.** Name the event and its chapter, not the character.
- **Sources stay out of the texts.** A curriculum or a review that told you the reading age is evidence for
  the forms, not something to cite.

## The edition (title page and copyright page)

The copyright page is `edition.notice`. Start from the draft the facts give, which follows the house
template: the original title and year, the translator, what this edition includes, the rights line, the
imprint.

- **The notes sentence gives the count in digits:** «29 notas del traductor». The check compares it with
  the built EPUB.
- **A Bible version's notice stays word for word.** It is the wording the version's publisher requires.
- **The source is named by its publisher and year,** never by the site the scan or the text came from.
- **Subjects:** two or three BISAC headings, then the catalog's own subject headings.

Model:

```text
Título original: Evenings in the Orchard

Publicado originalmente en 1890.

Traducción al español: Ana Pérez.

Esta edición incluye una traducción nueva al español, 24 notas del traductor y una portada nueva.

Traducción © 2026 Ana Pérez. La obra original es de dominio público.

Editorial Ejemplo
```

## Description (`descripcion.md`) and the card summary (`resumen`)

Why this book is worth reading. Nothing about the edition.

This is the book's one description. The site shows it, the EPUB carries it as its description, and the
store description is built from it, so it has no link, no price and no program name, and every fact in it
can be checked.

- A bold hook: questions for a book that answers questions, the theme in one line for a novel.
- One paragraph on what the book is and what the child gets from it.
- A closing line with the date, a credential and how to read it.
- `resumen` is one sentence.

Model:

```markdown
**¿Por qué el río nunca se acaba? ¿Quién le enseñó a la golondrina el camino?**

Cada tarde, la abuela Inés saca a sus nietos al huerto y les cuenta algo verdadero: de dónde viene la lluvia, cómo hace su nido el hornero, por qué el trigo se dobla antes de la cosecha. En cuarenta capítulos breves, el libro enseña a mirar despacio y a preguntar el porqué de las cosas.

Publicado en 1890 y leído en las escuelas durante medio siglo. Para leer en voz alta, un capítulo a la vez.
```

## «Sobre esta edición» (`adaptations`)

Three bullets: the source edition, what is left out and where the images come from; the silent fixes; the
notes. A book with no notes says so in the third bullet.

- **A deliberate edit to the author's content is declared by its purpose,** not by what the original
  said: «algunos pasajes se retocan para mayor precisión, como…», with one or two examples by chapter.
  Retelling the removed passage puts it back in front of the reader.
- **What every translation does is not a change:** Spanish names for invented characters, metric units
  beside the old ones.

Model:

```markdown
- Esta edición traduce, sin abreviar, la edición inglesa de 1890. Se omite el prefacio del editor. Las ilustraciones son las de esa edición, con sus pies traducidos; la portada es nueva.
- Se corrigen sin marcarlos algunos descuidos evidentes, como nombres científicos ya en desuso.
- Al final del libro hay **24 notas del traductor**. Ponen al día la ciencia y explican monedas y medidas de la época. Las más importantes señalan los consejos que ya no son seguros.
```

## «Lo que los padres deben saber» (`parent_notes`)

- Open with a short summary of the content review: tone, then a sentence on each main concern and how the
  book resolves it. No chapter references in that paragraph.
- Then a bold lead per concern the edition doesn't handle, with its chapter.
- Keep what the parent has to judge or act on: a sad or strong passage, a short list of other strong but
  brief passages, and worldview points the edition doesn't change.
- **A book that teaches from one tradition says which,** in a short paragraph for parents of another: what
  the author assumes and teaches that they may not share. State it plainly and without argument.

Model:

```markdown
Es un libro sereno, casi todo huerto y campo. Hay una muerte, contada con ternura, y algunas ideas de la época sobre la medicina que las notas corrigen.

**La muerte del abuelo (cap. 31).** Es el pasaje más triste del libro. Léalo antes si su hijo ha perdido a alguien.

**Otros pasajes fuertes, pero breves.** Una tormenta que destruye la cosecha (cap. 12) y la matanza del cerdo (cap. 19).
```

## «Sobre este libro» (`about_book.json`)

The short form of the two texts above, as plain paragraphs: edition facts, translation decisions, then a
summary for the parent. A child reading alone sees this page before chapter 1, so it names no death and no
event from the last chapters: it gives the hard chapters by number. It ends with a `[LIBRE]` paragraph that
links to the site.

Model:

```text
Esta edición traduce, sin abreviar, la edición inglesa de 1890. Se omite el prefacio del editor.

Se corrigen sin marcarlos algunos descuidos evidentes. Al final del libro hay 24 notas del traductor, que ponen al día la ciencia y señalan los consejos que ya no son seguros.

Es un libro sereno. El capítulo 31 es triste; quizá quiera leerlo antes si su hijo es sensible.

[LIBRE] En [nuestro sitio]({{SITIO}}) encontrará una guía más completa para los padres.
```

## Forms

Say here where you look up the recommended form (a curriculum's book list, a publisher's reading age) and how
its levels map to `I`, `II`, `III/IV` and `V/VI`.
