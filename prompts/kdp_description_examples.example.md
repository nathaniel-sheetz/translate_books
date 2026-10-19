# KDP description examples

The drafting step of the `kdp-publish` skill reads this file before it writes a
book's Amazon description. Copy it to `kdp_description_examples.md` (gitignored)
and replace the examples with descriptions from your own published listings:
the skill imitates whatever is here, so real listings that sold are the best
guide. Keep the annotations, since they tell the model *why* each part is there.

## The shape every description follows

1. **The opening bullets (required by KDP).** A differentiated public-domain
   book's description must *open* with a bulleted summary of what is original
   in this edition, at most 80 characters per bullet. One short line ending in
   a colon may introduce it ("Esta edición incluye:"), as in KDP's own example.
   Name only what is actually in the book: the translation, the notes, the new
   cover, the translated images.
2. **The hook (one paragraph).** What the book is and why a reader wants it,
   in present-day Mexican Spanish. Open with the story or the subject, not with
   the author. No plot past the first act (KDP bars spoilers).
3. **Why it lasts (one short paragraph).** Its place: the readers it has had,
   what it is known for. Make claims you can verify, never "the best".
4. **This edition (a short paragraph or 2–4 bullets).** The specifics from
   `facts.json`: who it is for (age, reading aloud, classroom), what the notes
   explain, what changed in the images. This is where the edition earns its
   descriptor.
5. **Nothing else.** No reviews or quotes, no prices, no links, no "Kindle
   Unlimited", no emoji, no keyword lists, and never the name Project Gutenberg.

Allowed HTML: `<p> <br> <b> <i> <u> <em> <strong> <ul> <ol> <li> <h4> <h5> <h6>`.
The 4,000-character limit counts the tags.

---

## Example 1: a children's animal novel, with a new cover

```html
<ul><li>Traducción nueva al español de México</li><li>Portada nueva</li></ul>
<p>En lo más hondo del bosque nace un cervatillo. Junto a su madre aprende los nombres de las cosas: el prado, la lluvia, el miedo. Y aprende también que en el bosque hay un peligro que ningún animal sabe nombrar.</p>
<p>Publicada en 1923, esta novela ha acompañado a generaciones de lectores que crecieron con ella mucho antes de conocer cualquier película. Es una historia sobre crecer, sobre la pérdida y sobre aprender a valerse por uno mismo.</p>
<p><b>Esta edición</b> presenta una traducción nueva, escrita en un español claro y natural para lectores de 10 años en adelante, ideal también para leerse en voz alta en familia.</p>
```

**Why it works:** the bullets are 59 characters; the hook is the story, not the
author; "1923" and "generaciones de lectores" are claims that can be checked;
the edition paragraph names the audience and the use (reading aloud).

## Example 2: an annotated nonfiction classic

```html
<ul><li>Traducción nueva al español</li><li>Más de 60 notas explicativas</li></ul>
<p>¿Por qué brilla una luciérnaga? ¿Cómo sabe una abeja volver a casa? Un tío curioso responde a las preguntas de sus sobrinos con experimentos sencillos y con la naturaleza que los rodea.</p>
<p>Escrito para despertar la curiosidad científica de los niños, este libro sigue siendo una de las introducciones más amenas a las ciencias naturales.</p>
<h4>En esta edición</h4>
<ul><li>Notas que explican lo que la ciencia sabe hoy y que el autor no podía saber.</li><li>Ilustraciones originales con los rótulos traducidos al español.</li></ul>
```

**Why it works:** the notes are a real differentiation, so they lead the
bullets and get their own section; the second list is the edition's detail,
the first is KDP's required summary.
