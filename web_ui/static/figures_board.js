/* Figures board (/figures/<id>). One card per planned figure: the picture, the
   printed captions, the Spanish captions (edited here), the chapter and
   paragraph it goes after, a preview of where it lands on each side, and its
   approval. Every change saves at once to figures.json through
   POST /api/project/<id>/figures/<fid>; the reply is the card's row rebuilt,
   with the new preview. Nothing here edits the book. */
(function () {
    "use strict";

    const board = document.getElementById("fb-board");
    const project = board.dataset.project;
    const S = JSON.parse(board.dataset.strings || "{}");
    const api = `/api/project/${encodeURIComponent(project)}/figures`;
    const bar = document.getElementById("fb-bar");
    const barCount = document.getElementById("fb-bar-count");
    const barSave = document.getElementById("fb-bar-save");

    let state = { figures: [], chapters: [] };
    const paragraphCache = new Map();

    function fmt(s, vars) {
        return String(s || "").replace(/\{(\w+)\}/g, (_, k) => (k in vars ? vars[k] : `{${k}}`));
    }

    function el(tag, attrs, ...kids) {
        const n = document.createElement(tag);
        for (const [k, v] of Object.entries(attrs || {})) {
            if (v === null || v === undefined || v === false) continue;
            if (k === "class") n.className = v;
            else if (k === "text") n.textContent = v;
            else if (k.startsWith("on")) n.addEventListener(k.slice(2), v);
            else n.setAttribute(k, v === true ? "" : v);
        }
        for (const kid of kids.flat()) {
            if (kid === null || kid === undefined || kid === false) continue;
            n.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
        }
        return n;
    }

    function clip(s, n) {
        s = String(s || "");
        return s.length > n ? s.slice(0, n - 1) + "…" : s;
    }

    function setSave(msg, bad) {
        barSave.textContent = msg || "";
        barSave.classList.toggle("fb-bad", !!bad);
    }

    function updateBar() {
        const figs = state.figures;
        const approved = figs.filter((f) => f.status === "approved").length;
        const inserted = figs.filter((f) => f.status === "inserted").length;
        barCount.textContent = fmt(S.count, { approved, inserted, total: figs.length });
        bar.hidden = figs.length === 0;
    }

    async function paragraphsFor(chapter) {
        if (!paragraphCache.has(chapter)) {
            const r = await fetch(`${api}/paragraphs?chapter=${encodeURIComponent(chapter)}`);
            const data = await r.json();
            paragraphCache.set(chapter, r.ok ? data.paragraphs : []);
        }
        return paragraphCache.get(chapter);
    }

    async function save(fid, edit) {
        setSave(S.saving);
        try {
            const r = await fetch(`${api}/${encodeURIComponent(fid)}`, {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify(edit),
            });
            const data = await r.json();
            if (!r.ok) throw new Error(data.error || r.status);
            const i = state.figures.findIndex((f) => f.id === fid);
            if (i >= 0) state.figures[i] = data;
            const old = board.querySelector(`[data-fid="${CSS.escape(fid)}"]`);
            if (old) old.replaceWith(card(data));
            updateBar();
            setSave(S.saved);
        } catch (e) {
            setSave(fmt(S.save_failed, { error: e.message }), true);
        }
    }

    function anchorSelect(f) {
        const sel = el("select", { class: "fb-anchor", "aria-label": S.after,
            disabled: f.status === "inserted" });
        sel.append(el("option", { value: f.anchor_en || "", text: clip(f.anchor_en, 110) }));
        // Filled on first focus: a chapter's paragraphs are fetched once.
        let filled = false;
        sel.addEventListener("focus", async () => {
            if (filled || !f.chapter_id) return;
            filled = true;
            const paras = await paragraphsFor(f.chapter_id);
            sel.textContent = "";
            for (const p of paras) {
                sel.append(el("option", { value: p.anchor, text: clip(p.text, 110),
                    selected: p.anchor === f.anchor_en }));
            }
        });
        sel.addEventListener("change", () => save(f.id, { anchor_en: sel.value }));
        return sel;
    }

    function chapterSelect(f) {
        const sel = el("select", { class: "fb-chapter", "aria-label": S.chapter,
            disabled: f.status === "inserted" });
        for (const c of state.chapters) {
            sel.append(el("option", { value: c.chapter_id, text: clip(c.heading, 70),
                selected: c.chapter_id === f.chapter_id }));
        }
        sel.addEventListener("change", async () => {
            // A new chapter needs a paragraph in it: take its first.
            const paras = await paragraphsFor(sel.value);
            save(f.id, { chapter_id: sel.value, anchor_en: paras.length ? paras[0].anchor : "" });
        });
        return sel;
    }

    function captions(f) {
        const en = f.captions_en || [];
        const es = f.captions_es || [];
        const locked = f.status === "inserted";
        return en.map((text, i) => {
            const area = el("textarea", { class: "fb-es", rows: 3, "aria-label": S.caption_es,
                disabled: locked });
            area.value = es[i] || "";
            area.addEventListener("change", () => {
                const all = en.map((_, k) => (k === i ? area.value
                    : (board.querySelectorAll(`[data-fid="${CSS.escape(f.id)}"] .fb-es`)[k] || {}).value || ""));
                save(f.id, { captions_es: all });
            });
            return el("div", { class: "fb-caption" },
                el("div", { class: "fb-label", text: S.caption_en }),
                el("p", { class: "fb-en", text }),
                el("div", { class: "fb-label", text: S.caption_es }),
                area);
        });
    }

    function card(f) {
        const pic = el("a", { href: `${api}/${encodeURIComponent(f.id)}/picture`, target: "_blank",
            class: "fb-pic" },
            el("img", { src: `${api}/${encodeURIComponent(f.id)}/picture`, alt: f.alt_en || f.id,
                loading: "lazy" }));
        const approve = el("label", { class: "fb-approve" },
            el("input", { type: "checkbox", checked: f.status === "approved" || f.status === "inserted",
                disabled: f.status === "inserted" || !!f.problem || !!f.missing_es,
                onchange: (e) => save(f.id, { status: e.target.checked ? "approved" : "draft" }) }),
            f.status === "inserted" ? S.inserted : (f.status === "approved" ? S.approved : S.approve));
        const alt = el("input", { type: "text", class: "fb-alt", value: f.alt_es || "",
            "aria-label": S.alt_es, placeholder: S.alt_es, disabled: f.status === "inserted",
            onchange: (e) => save(f.id, { alt_es: e.target.value }) });
        const notes = [];
        if (f.doubtful_en) notes.push(el("p", { class: "br-warn", text: S.doubtful }));
        if (f.missing_es && f.status !== "inserted") notes.push(el("p", { class: "br-warn", text: S.missing_es }));
        if (f.problem) notes.push(el("p", { class: "br-warn", text: fmt(S.problem, { why: f.problem }) }));
        const preview = f.preview ? el("div", { class: "fb-preview" },
            el("div", { class: "fb-label", text: S.lands_en }),
            el("p", { class: "fb-ctx", text: clip(f.preview.en_context, 260) }),
            el("div", { class: "fb-label", text: S.lands_es }),
            el("p", { class: "fb-ctx", text: clip(f.preview.es_context, 260) })) : null;
        return el("section", { class: `fb-card fb-${f.status || "draft"}`, "data-fid": f.id },
            el("div", { class: "fb-head" },
                el("strong", { text: f.subject || f.id }),
                el("span", { class: "br-muted", text: ` ${f.id}` }),
                f.facing_page ? el("span", { class: "br-muted", text: ` · ${fmt(S.facing, { page: f.facing_page })}` }) : null,
                approve),
            el("div", { class: "fb-body" },
                pic,
                el("div", { class: "fb-fields" },
                    notes,
                    captions(f),
                    el("div", { class: "fb-label", text: S.alt_es }), alt,
                    el("div", { class: "fb-label", text: S.chapter }), chapterSelect(f),
                    el("div", { class: "fb-label", text: S.after }), anchorSelect(f),
                    preview)));
    }

    async function load() {
        const status = document.getElementById("fb-status");
        try {
            const r = await fetch(api);
            state = await r.json();
            if (!r.ok) throw new Error(state.error || r.status);
        } catch (e) {
            if (status) status.textContent = fmt(S.save_failed, { error: e.message });
            return;
        }
        board.textContent = "";
        if (!state.figures.length) {
            board.append(el("p", { class: "br-status", text: S.empty }));
        }
        for (const f of state.figures) board.append(card(f));
        updateBar();
    }

    document.getElementById("fb-refresh").addEventListener("click", () => {
        paragraphCache.clear();
        load();
    });
    load();
})();
