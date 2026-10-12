"""
``cover-compose`` — the house cover, made from the art: a colour band across the
top with the title on it, the emblem in the bottom corner. No Codex.

The image pass makes a cover's picture and nothing else; the lettering used to
be laid over it by hand in a slide and screenshotted. This does that step from
two files:

- **the template** (``assets/cover_template/template.json``, or the tracked
  ``template.example.json`` when a checkout has no house one) is the design: an
  ordered list of layers — ``polygon``, ``ellipse``, ``text``, ``image`` — in
  fractions of the cover, so it does not care how many pixels the art has.
- **the spec** (``<project>/cover/spec.json``) is the book: its title, subtitle,
  author, band colour, and any value of the template it overrides.

Three things are solved rather than read, because they depend on the picture
and on how long the title is:

- **where the art sits.** It is scaled to the cover's full width and never
  stretched. Art too short for the cover stands on the bottom edge, and the band
  has to reach down far enough to hide where it starts.
- **how deep the band is.** The template names one variable that may grow
  (``fit.grow``) and by how much.
- **the size of the title.** ``fit.try`` lists what to try, in order; the first
  under which every line of text stays inside the shape it names wins.

Nothing is written unless all of it fits, and no row of the cover is ever left
without art or band over it.

The art without lettering is kept at ``<project>/cover/art.<ext>`` and every
later compose starts from that file, so a band is never drawn over a band. Every
cover drawn is also kept as a draft (:mod:`src.image_pass.cover_drafts`), which
is what the image board shows and what ``--from-draft`` goes back to. The
finished cover reaches ``images/`` through :func:`src.image_pass.apply.install_image`:
same write-once backup, same ledger, and ``revert`` puts back what was there.
"""

from __future__ import annotations

import colorsys
import json
import os
import re
import shutil
import time
from pathlib import Path
from typing import Any, Optional

from src.image_pass import COVER_NAMES, MODE_COVER, cover_dir, images_dir
from src.image_pass import cover_drafts as drafts
from src.image_pass import ledger
from src.image_pass.apply import COVER_LONG_SIDE, has_original, install_image
from src.image_pass.inventory import probe_image
from src.image_pass.jobs import book_meta

TEMPLATE_DIR = Path(__file__).resolve().parents[2] / "assets" / "cover_template"
HOUSE_TEMPLATE = "template.json"
EXAMPLE_TEMPLATE = "template.example.json"

LAYER_TYPES = ("polygon", "ellipse", "text", "image")

# A book's `aspect` instead of [width, height]: the cover takes the picture's
# own shape, made taller by however far `art_top` pushes the picture down.
ASPECT_ART = "art"
_MAX_ART_PUSH = 0.4
ANCHORS = ("top-left", "top-right", "bottom-left", "bottom-right")

# A capital in small caps is the full size; everything else is a capital at this.
_SMALL_CAP_SCALE = 0.8

# Shapes are drawn this many times too large and reduced, for a clean slanted edge.
_SUPERSAMPLE = 4

# How much art the band overlaps where it hides the art's top edge, as a
# fraction of the cover's height, when the template does not say.
_DEFAULT_ART_OVERLAP = 0.004

# A band colour has to let the lettering read. The house covers sit a little
# above 3:1 (the glow and the shadow do the rest), so that is the floor.
_MIN_CONTRAST = 3.2

_SWATCH_HEIGHT = 900


class TemplateError(ValueError):
    """The template or the spec says something that cannot be drawn."""


# ---------------------------------------------------------------------------
# Template and spec
# ---------------------------------------------------------------------------

def template_path(explicit: Optional[Path] = None) -> Path:
    if explicit is not None:
        return Path(explicit)
    house = TEMPLATE_DIR / HOUSE_TEMPLATE
    return house if house.is_file() else TEMPLATE_DIR / EXAMPLE_TEMPLATE


def load_template(path: Optional[Path] = None) -> dict[str, Any]:
    """The design, with ``_dir`` added: where its fonts and images are."""
    source = template_path(path)
    try:
        doc = json.loads(source.read_text(encoding="utf-8-sig"))
    except OSError as exc:
        raise TemplateError(f"no cover template at {source}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise TemplateError(f"{source} is not valid JSON: {exc}") from exc
    problems = _template_problems(doc)
    if problems:
        raise TemplateError(f"{source}: " + "; ".join(problems))
    doc["_dir"] = str(source.parent)
    doc["_path"] = str(source)
    return doc


def _template_problems(doc: Any) -> list[str]:
    if not isinstance(doc, dict):
        return ["the template is not an object"]
    problems: list[str] = []
    aspect = doc.get("aspect")
    if not (isinstance(aspect, list) and len(aspect) == 2 and all(
        isinstance(n, (int, float)) and n > 0 for n in aspect
    )):
        problems.append("aspect must be [width, height]")
    layers = doc.get("layers")
    if not isinstance(layers, list) or not layers:
        return problems + ["layers must be a non-empty list"]
    seen: set[str] = set()
    for position, layer in enumerate(layers):
        if not isinstance(layer, dict):
            problems.append(f"layer {position} is not an object")
            continue
        name = layer.get("id")
        if not isinstance(name, str) or not name:
            problems.append(f"layer {position} has no id")
        elif name in seen:
            problems.append(f"two layers are called {name!r}")
        else:
            seen.add(name)
        if layer.get("type") not in LAYER_TYPES:
            problems.append(f"layer {name!r}: type must be one of {list(LAYER_TYPES)}")
        if layer.get("anchor", ANCHORS[0]) not in ANCHORS:
            problems.append(f"layer {name!r}: anchor must be one of {list(ANCHORS)}")
    return problems


def spec_path(project_dir: Path) -> Path:
    return cover_dir(project_dir) / "spec.json"


def load_spec(project_dir: Path) -> dict[str, Any]:
    path = spec_path(project_dir)
    if not path.is_file():
        return {}
    try:
        doc = json.loads(path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        raise TemplateError(f"{path} is not valid JSON: {exc}") from exc
    if not isinstance(doc, dict):
        raise TemplateError(f"{path} is not an object")
    return doc


def _write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def merge_spec(spec: dict[str, Any], overrides: dict[str, Any]) -> dict[str, Any]:
    """``spec`` with ``overrides`` laid over it. ``colors``, ``vars`` and each
    layer merge key by key; a value of ``None`` removes what it names."""
    merged = json.loads(json.dumps(spec))
    for key, value in (overrides or {}).items():
        if key in ("colors", "vars") and isinstance(value, dict):
            target = merged.setdefault(key, {})
            for name, item in value.items():
                if item is None:
                    target.pop(name, None)
                else:
                    target[name] = item
        elif key == "layers" and isinstance(value, dict):
            target = merged.setdefault("layers", {})
            for name, fields in value.items():
                layer = target.setdefault(name, {})
                for field, item in (fields or {}).items():
                    if item is None:
                        layer.pop(field, None)
                    else:
                        layer[field] = item
                if not layer:
                    target.pop(name)
        elif value is None:
            merged.pop(key, None)
        else:
            merged[key] = value
    for key in ("colors", "vars", "layers"):
        if key in merged and not merged[key]:
            merged.pop(key)
    return merged


def _applied_template(template: dict[str, Any], spec: dict[str, Any]) -> dict[str, Any]:
    """The template as this book draws it: the spec's colours, variables and
    per-layer fields over the design's."""
    overrides = spec.get("layers") or {}
    known = {layer["id"] for layer in template["layers"]}
    unknown = sorted(set(overrides) - known)
    if unknown:
        raise TemplateError(
            f"the spec overrides layer(s) the template does not have: {unknown} "
            f"(it has {sorted(known)})"
        )
    out = dict(template)
    out["vars"] = {**(template.get("vars") or {}), **(spec.get("vars") or {})}
    out["colors"] = {**(template.get("colors") or {}), **(spec.get("colors") or {})}
    out["layers"] = [{**layer, **(overrides.get(layer["id"]) or {})} for layer in template["layers"]]
    if spec.get("aspect"):
        aspect = spec["aspect"]
        if aspect != ASPECT_ART and not (
            isinstance(aspect, list) and len(aspect) == 2
            and all(isinstance(n, (int, float)) and not isinstance(n, bool) and n > 0 for n in aspect)
        ):
            raise TemplateError(f"aspect must be [width, height] or {ASPECT_ART!r}, not {aspect!r}")
        out["aspect"] = aspect
    return out


# ---------------------------------------------------------------------------
# Values, colours, geometry
# ---------------------------------------------------------------------------

def _num(raw: Any, variables: dict[str, Any]) -> float:
    if isinstance(raw, str):
        name = raw[1:] if raw.startswith("$") else raw
        if name not in variables:
            raise TemplateError(f"{raw!r} is not a number or a variable in vars")
        raw = variables[name]
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise TemplateError(f"{raw!r} is not a number")
    return float(raw)


def parse_color(raw: Any, colors: Optional[dict[str, Any]] = None) -> tuple[int, int, int]:
    """``#rrggbb``, or ``$name`` looked up in ``colors``."""
    seen: set[str] = set()
    while isinstance(raw, str) and raw.startswith("$"):
        name = raw[1:]
        if name in seen or not colors or name not in colors:
            raise TemplateError(f"{raw!r} is not a colour in colors")
        seen.add(name)
        raw = colors[name]
    if not isinstance(raw, str) or not re.fullmatch(r"#?[0-9a-fA-F]{6}", raw):
        raise TemplateError(f"{raw!r} is not a colour: write it as #rrggbb")
    digits = raw.lstrip("#")
    return int(digits[0:2], 16), int(digits[2:4], 16), int(digits[4:6], 16)


def to_hex(rgb: tuple[int, int, int]) -> str:
    return "#{:02X}{:02X}{:02X}".format(*rgb)


def _luminance(rgb: tuple[int, int, int]) -> float:
    def channel(value: int) -> float:
        c = value / 255
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = (channel(v) for v in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(a: tuple[int, int, int], b: tuple[int, int, int]) -> float:
    """WCAG contrast ratio between two colours, 1 to 21."""
    hi, lo = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def _origin(layer: dict[str, Any], variables: dict[str, Any], width: int, height: int
            ) -> tuple[float, float, int, int]:
    """The layer's own corner, in pixels, and which way it extends from there.

    ``at`` is measured from the anchored corner of the cover, in fractions of
    the cover's *height* on both axes: a margin stays the same margin whatever
    the cover's proportions.
    """
    at = layer.get("at") or [0, 0]
    if not isinstance(at, list) or len(at) != 2:
        raise TemplateError(f"layer {layer['id']!r}: at must be [x, y]")
    dx, dy = _num(at[0], variables) * height, _num(at[1], variables) * height
    anchor = layer.get("anchor", ANCHORS[0])
    x, sx = (dx, 1) if anchor.endswith("left") else (width - dx, -1)
    y, sy = (dy, 1) if anchor.startswith("top") else (height - dy, -1)
    return x, y, sx, sy


def _box(layer: dict[str, Any], variables: dict[str, Any], width: int, height: int
         ) -> list[float]:
    size = layer.get("size")
    if not isinstance(size, list) or len(size) != 2:
        raise TemplateError(f"layer {layer['id']!r}: size must be [width, height]")
    x, y, sx, sy = _origin(layer, variables, width, height)
    w, h = _num(size[0], variables) * height, _num(size[1], variables) * height
    x0, x1 = (x, x + w) if sx > 0 else (x - w, x)
    y0, y1 = (y, y + h) if sy > 0 else (y - h, y)
    return [x0, y0, x1, y1]


def _points(layer: dict[str, Any], variables: dict[str, Any], width: int, height: int
            ) -> list[tuple[float, float]]:
    raw = layer.get("points")
    if not isinstance(raw, list) or len(raw) < 3:
        raise TemplateError(f"layer {layer['id']!r}: points must be three or more [x, y]")
    return [(_num(x, variables) * width, _num(y, variables) * height) for x, y in raw]


def _inside(x: float, y: float, polygon: list[tuple[float, float]]) -> bool:
    """Whether a point is in a polygon, edges included."""
    tolerance = 0.5
    inside = False
    for (x1, y1), (x2, y2) in zip(polygon, polygon[1:] + polygon[:1]):
        # On an edge counts as inside: text sits against the cover's own edges.
        cross = (x2 - x1) * (y - y1) - (y2 - y1) * (x - x1)
        length = ((x2 - x1) ** 2 + (y2 - y1) ** 2) ** 0.5 or 1.0
        if abs(cross) / length <= tolerance and (
            min(x1, x2) - tolerance <= x <= max(x1, x2) + tolerance
            and min(y1, y2) - tolerance <= y <= max(y1, y2) + tolerance
        ):
            return True
        if (y1 > y) != (y2 > y) and x < (x2 - x1) * (y - y1) / (y2 - y1) + x1:
            inside = not inside
    return inside


def cover_depth(polygon: list[tuple[float, float]], width: int) -> float:
    """How far down from the top edge a shape covers the cover's whole width.

    Art that starts lower than the top of the cover has to start above this.
    """
    depth: Optional[float] = None
    samples = 33
    for step in range(samples):
        x = 0.5 + (width - 1.0) * step / (samples - 1)
        crossings = sorted(
            y1 + (y2 - y1) * (x - x1) / (x2 - x1)
            for (x1, y1), (x2, y2) in zip(polygon, polygon[1:] + polygon[:1])
            if x1 != x2 and min(x1, x2) <= x < max(x1, x2)
        )
        if len(crossings) < 2 or crossings[0] > 0.5:
            return 0.0
        depth = crossings[1] if depth is None else min(depth, crossings[1])
    return depth or 0.0


# ---------------------------------------------------------------------------
# Text
# ---------------------------------------------------------------------------

class _Fonts:
    """Fonts by file, weight and size, opened once each."""

    def __init__(self, base: Path):
        self.base = Path(base)
        self._cache: dict[tuple[str, Any, float], Any] = {}

    def get(self, layer: dict[str, Any], px: float) -> Any:
        from PIL import ImageFont

        name = layer.get("font")
        if not isinstance(name, str) or not name:
            raise TemplateError(f"layer {layer['id']!r}: a text layer needs a font")
        key = (name, layer.get("weight"), round(px, 2))
        if key not in self._cache:
            path = self.base / name
            try:
                font = ImageFont.truetype(str(path), key[2])
            except OSError as exc:
                raise TemplateError(f"layer {layer['id']!r}: cannot open font {path}: {exc}") from exc
            if layer.get("weight") is not None:
                try:
                    font.set_variation_by_axes([float(layer["weight"])])
                except OSError:
                    pass  # a static font: its weight is the file
            self._cache[key] = font
        return self._cache[key]

    def metrics(self, layer: dict[str, Any], px: float) -> tuple[float, float]:
        """Ascent and descent at ``px``. Read off the font at a large size and
        scaled, because the font's own answer at ``px`` is in whole pixels and a
        layout built on that shifts with the resolution it is drawn at."""
        ascent, descent = self.get(layer, 1000.0).getmetrics()
        return ascent * px / 1000, descent * px / 1000


def _runs(text: str, small_caps: bool) -> list[tuple[str, float]]:
    """``text`` as ``(string, scale)`` runs: one, unless it is in small caps."""
    if not small_caps:
        return [(text, 1.0)]
    runs: list[tuple[str, float]] = []
    for char in text:
        scale = _SMALL_CAP_SCALE if char.islower() else 1.0
        shown = char.upper()
        if runs and runs[-1][1] == scale:
            runs[-1] = (runs[-1][0] + shown, scale)
        else:
            runs.append((shown, scale))
    return runs


def _wrap(text: str, measure: Any, limit: float) -> list[str]:
    lines: list[str] = []
    current = ""
    for word in text.split():
        attempt = f"{current} {word}" if current else word
        if current and measure(attempt) > limit:
            lines.append(current)
            current = word
        else:
            current = attempt
    return lines + [current] if current else lines


def _layer_text(layer: dict[str, Any], fields: dict[str, str]) -> Optional[str]:
    """What a text layer says for this book, or ``None`` when the book does not
    have what it would say (no author: no author line)."""
    if "field" in layer:
        return (fields.get(layer["field"]) or "").strip() or None
    raw = layer.get("text")
    if not isinstance(raw, str) or not raw:
        raise TemplateError(f"layer {layer['id']!r}: a text layer needs `field` or `text`")
    names = re.findall(r"\{(\w+)\}", raw)
    if any(not (fields.get(name) or "").strip() for name in names):
        return None
    return re.sub(r"\{(\w+)\}", lambda match: fields[match.group(1)].strip(), raw)


def _place_text(
    layer: dict[str, Any],
    text: str,
    forced: Optional[list[str]],
    variables: dict[str, Any],
    width: int,
    height: int,
    fonts: _Fonts,
    bottoms: dict[str, Optional[float]],
    polygons: dict[str, list[tuple[float, float]]],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    px = _num(layer.get("size"), variables) * height
    small_caps = bool(layer.get("small_caps"))
    base_font = fonts.get(layer, px)
    small_font = fonts.get(layer, px * _SMALL_CAP_SCALE) if small_caps else base_font

    def measure(line: str) -> float:
        return sum(
            (base_font if scale == 1.0 else small_font).getlength(part)
            for part, scale in _runs(line, small_caps)
        )

    ascent, descent = fonts.metrics(layer, px)
    spacing = float(layer.get("line_spacing", 1.0))
    line_height = (ascent + descent) * spacing

    x, top, sx, _sy = _origin(layer, variables, width, height)
    under = layer.get("below")
    if under is not None and bottoms.get(under) is not None:
        top = bottoms[under] + _num(layer.get("gap", 0), variables) * height
    if layer.get("min_top") is not None:
        top = max(top, _num(layer["min_top"], variables) * height)

    align = layer.get("align", "left")
    limit: Optional[float] = None
    if layer.get("wrap"):
        far = _num(layer.get("margin_right", 0), variables) * height
        limit = (width - x - far) if sx > 0 else (x - far)

    if forced:
        lines = [line.strip() for line in forced if line.strip()]
    else:
        lines = []
        for paragraph in text.split("\n"):
            paragraph = paragraph.strip()
            if not paragraph:
                continue
            lines.extend(_wrap(paragraph, measure, limit) if limit else [paragraph])

    shape = layer.get("fit_inside")
    if shape is not None and shape not in polygons:
        raise TemplateError(
            f"layer {layer['id']!r}: fit_inside names {shape!r}, which is not a "
            "polygon drawn before it"
        )
    margin = _num(layer.get("margin", 0), variables) * height
    placed_lines: list[dict[str, Any]] = []
    misfits: list[dict[str, Any]] = []
    for index, line in enumerate(lines):
        line_width = measure(line)
        left = x - line_width / 2 if align == "center" else (x if sx > 0 else x - line_width)
        baseline = top + index * line_height + ascent
        box = [left, baseline - ascent, left + line_width, baseline + descent]
        placed_lines.append({"text": line, "x": left, "baseline": baseline, "box": box})
        if limit is not None and line_width > limit + 0.5:
            misfits.append({
                "layer": layer["id"], "line": line,
                "problem": "is wider than the cover allows",
            })
        elif shape is not None:
            # Grown by the margin, but not past the cover: its own edges are no
            # edge of the band.
            x0, y0 = max(0.0, box[0] - margin), max(0.0, box[1] - margin)
            x1, y1 = min(float(width), box[2] + margin), min(float(height), box[3] + margin)
            corners = ((x0, y0), (x1, y0), (x0, y1), (x1, y1))
            if not all(_inside(cx, cy, polygons[shape]) for cx, cy in corners):
                misfits.append({
                    "layer": layer["id"], "line": line,
                    "problem": f"crosses the edge of {shape!r}",
                })
    bottom = top + len(lines) * line_height
    return {
        "id": layer["id"],
        "type": "text",
        "px": px,
        "small_caps": small_caps,
        "font": layer["font"],
        "weight": layer.get("weight"),
        "fill": layer.get("fill", "#000000"),
        "glow": layer.get("glow"),
        "shadow": layer.get("shadow"),
        "lines": placed_lines,
        "bottom": bottom,
    }, misfits


def _layout(
    tpl: dict[str, Any],
    variables: dict[str, Any],
    fields: dict[str, str],
    forced: dict[str, list[str]],
    width: int,
    height: int,
    fonts: _Fonts,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Every layer in pixels under one set of variables, and the lines of text
    that do not stay inside the shape they are meant to."""
    placed: list[dict[str, Any]] = []
    misfits: list[dict[str, Any]] = []
    polygons: dict[str, list[tuple[float, float]]] = {}
    bottoms: dict[str, Optional[float]] = {}
    for layer in tpl["layers"]:
        kind = layer["type"]
        if kind == "polygon":
            points = _points(layer, variables, width, height)
            polygons[layer["id"]] = points
            placed.append({"id": layer["id"], "type": kind, "points": points,
                           "fill": layer.get("fill", "#000000")})
        elif kind == "ellipse":
            placed.append({"id": layer["id"], "type": kind,
                           "box": _box(layer, variables, width, height),
                           "fill": layer.get("fill", "#000000")})
        elif kind == "image":
            if not isinstance(layer.get("src"), str):
                raise TemplateError(f"layer {layer['id']!r}: an image layer needs src")
            placed.append({"id": layer["id"], "type": kind, "src": layer["src"],
                           "box": _box(layer, variables, width, height),
                           "tint": layer.get("tint"), "mask": layer.get("mask", "alpha")})
        else:
            text = _layer_text(layer, fields)
            if text is None:
                # What sits below a line that is not there sits below what that
                # line would have sat below.
                bottoms[layer["id"]] = bottoms.get(layer.get("below"))
                continue
            field = layer.get("field")
            item, wrong = _place_text(
                layer, text, forced.get(field) if field else None,
                variables, width, height, fonts, bottoms, polygons,
            )
            bottoms[layer["id"]] = item["bottom"]
            placed.append(item)
            misfits.extend(wrong)
    return placed, misfits


# ---------------------------------------------------------------------------
# Solve
# ---------------------------------------------------------------------------

def _grow_values(grow: dict[str, Any], variables: dict[str, Any]) -> list[float]:
    name = grow["var"]
    base = _num(name, variables)
    top = max(base, _num(grow.get("max", base), variables))
    step = float(grow.get("step", 0.01))
    if step <= 0:
        raise TemplateError("fit.grow.step must be more than 0")
    values = [base]
    while values[-1] < top - 1e-9:
        values.append(min(top, values[-1] + step))
    return values


def _text_fields(template: dict[str, Any], spec: dict[str, Any]) -> dict[str, str]:
    fields = {
        key: value for key, value in (template.get("defaults") or {}).items()
        if isinstance(value, str)
    }
    fields.update({key: value for key, value in spec.items() if isinstance(value, str)})
    return fields


def _says(template: dict[str, Any], field: str) -> bool:
    """Whether some text layer of the template prints this field."""
    for layer in template["layers"]:
        if layer.get("type") != "text":
            continue
        if layer.get("field") == field or f"{{{field}}}" in str(layer.get("text") or ""):
            return True
    return False


def _forced_lines(spec: dict[str, Any]) -> dict[str, list[str]]:
    forced: dict[str, list[str]] = {}
    for key, value in spec.items():
        if key.endswith("_lines") and isinstance(value, list) and value:
            if not all(isinstance(line, str) for line in value):
                raise TemplateError(f"{key} must be a list of lines")
            forced[key[: -len("_lines")]] = value
    return forced


def solve(
    template: dict[str, Any],
    spec: dict[str, Any],
    art_size: tuple[int, int],
    *,
    height: Optional[int] = None,
) -> dict[str, Any]:
    """Where everything goes for this book and this picture, in pixels.

    ``status`` is ``ok``, or ``does_not_fit`` with the lines that would not
    stay inside their shape under the most generous thing the template allows.
    """
    tpl = _applied_template(template, spec)
    art_w, art_h = art_size
    if art_w <= 0 or art_h <= 0:
        raise TemplateError("the art has no size")
    asked = spec.get("art_top")
    if asked is not None and (isinstance(asked, bool) or not isinstance(asked, (int, float))):
        raise TemplateError("art_top must be a number: a fraction of the cover's height")
    # `art`: the cover is as wide as the picture and as tall as the picture
    # plus whatever art_top pushes it down by, so none of it is cropped.
    follows_art = tpl["aspect"] == ASPECT_ART
    if follows_art:
        if not 0 <= float(asked or 0) <= _MAX_ART_PUSH:
            raise TemplateError(
                f"with aspect {ASPECT_ART!r}, art_top is how far the picture is pushed "
                f"down under the band: 0 to {_MAX_ART_PUSH:g}"
            )
        ratio = (art_w / art_h) * (1 - float(asked or 0))
    else:
        ratio = float(tpl["aspect"][0]) / float(tpl["aspect"][1])
    out_h = int(height or min(COVER_LONG_SIDE, round(2 * art_w / ratio)))
    out_w = int(round(out_h * ratio))

    fit = tpl.get("fit") or {}
    attempts = fit.get("try") or [{}]
    if not isinstance(attempts, list) or not all(isinstance(a, dict) for a in attempts):
        raise TemplateError("fit.try must be a list of {variable: value}")
    # A variable this book sets is not the ladder's to change: asked for a
    # title size, the title is that size or it does not fit.
    pinned = set(spec.get("vars") or {})
    kept: list[dict[str, Any]] = []
    for attempt in attempts:
        free = {name: value for name, value in attempt.items() if name not in pinned}
        if free not in kept:
            kept.append(free)
    attempts = kept
    base_vars = dict(tpl["vars"])
    grow = fit.get("grow") or None
    grow_values = _grow_values(grow, base_vars) if grow else [None]
    overlap = float(fit.get("art_overlap", _DEFAULT_ART_OVERLAP)) * out_h
    hider = next(
        (layer for layer in tpl["layers"]
         if layer["type"] == "polygon" and layer.get("covers_art_top")),
        None,
    )

    def grown(value: Optional[float]) -> dict[str, Any]:
        return base_vars if value is None else {**base_vars, grow["var"]: value}

    def depth(value: Optional[float]) -> float:
        if hider is None:
            return 0.0
        return cover_depth(_points(hider, grown(value), out_w, out_h), out_w)

    # --- the art: full width, then one number for where its top edge sits.
    notes: list[str] = []
    scaled_h = out_w * art_h / art_w
    lowest = out_h - scaled_h  # its top when it stands on the bottom edge
    top = max(lowest, 0.0) if asked is None or follows_art else float(asked) * out_h
    if top < lowest - 0.5:
        notes.append(
            f"art_top raised from {float(asked):.3f} to {lowest / out_h:.3f}: any "
            "higher and the art would stop short of the bottom edge"
        )
        top = lowest
    art = {"width": float(out_w), "height": scaled_h, "left": 0.0, "top": top, "side_crop": 0.0}
    deepest = depth(grow_values[-1])
    if top > 0 and top + overlap > deepest + 0.5:
        limit = max(deepest - overlap, 0.0)
        if lowest <= limit:
            notes.append(
                f"art_top lowered from {top / out_h:.3f} to {limit / out_h:.3f}: "
                "the band cannot reach further down to hide the art's top edge"
            )
            art["top"] = top = limit
        else:
            # Too short even under the deepest band: enlarge it until it
            # reaches, and let the sides go.
            scale = (out_h - limit) / scaled_h
            art = {
                "width": out_w * scale,
                "height": out_h - limit,
                "left": -(out_w * scale - out_w) / 2,
                "top": limit,
                "side_crop": 1 - 1 / scale,
            }
            top = limit
            notes.append(
                f"the art is too wide for this cover: enlarged {scale:.2f}x and "
                f"{art['side_crop']:.0%} of its width cropped, half from each side"
            )
    need = top + overlap if top > 0 else 0.0

    fields = _text_fields(template, spec)
    forced = _forced_lines(spec)
    fonts = _Fonts(Path(template["_dir"]))

    chosen: Optional[tuple[Optional[float], dict[str, Any], list[dict[str, Any]]]] = None
    first_misfits: list[dict[str, Any]] = []
    last_misfits: list[dict[str, Any]] = []
    for value in grow_values:
        if depth(value) + 0.5 < need:
            continue
        for attempt in attempts:
            variables = {**grown(value), **attempt}
            placed, misfits = _layout(tpl, variables, fields, forced, out_w, out_h, fonts)
            if not misfits:
                chosen = (value, attempt, placed)
                break
            first_misfits = first_misfits or misfits
            last_misfits = misfits
        if chosen:
            break

    def describe(wrong: list[dict[str, Any]]) -> str:
        first = wrong[0]
        return f"{first['layer']} line {first['line']!r} {first['problem']}"

    if chosen is None:
        return {
            "status": "does_not_fit",
            "size": [out_w, out_h],
            "misfits": last_misfits,
            "notes": notes,
            "error": (
                "the text does not fit under anything the template allows: "
                + (describe(last_misfits) if last_misfits else "no band depth hides the art's top edge")
            ),
        }

    value, attempt, placed = chosen
    variables = {**grown(value), **attempt}
    reasons: list[str] = []
    if grow and value is not None and value > grow_values[0] + 1e-9:
        name = grow["var"]
        if depth(grow_values[0]) + 0.5 < need:
            reasons.append(
                f"{name} {value:.3f} (from {grow_values[0]:.3f}): the art's top edge "
                f"sits at {top / out_h:.3f} and has to be hidden"
            )
        else:
            reasons.append(
                f"{name} {value:.3f} (from {grow_values[0]:.3f}): "
                f"{describe(first_misfits)} any shallower"
            )
    if attempt and attempt is not attempts[0] and first_misfits:
        said = ", ".join(f"{k} {v:g}" for k, v in attempt.items())
        was = ", ".join(f"{k} {v:g}" for k, v in attempts[0].items())
        reasons.append(f"{said} (from {was}): {describe(first_misfits)}")

    return {
        "status": "ok",
        "size": [out_w, out_h],
        "vars": {k: variables[k] for k in sorted(variables)},
        "colors": dict(tpl["colors"]),
        "art": art,
        "art_top": top / out_h,
        "layers": placed,
        "lines": {
            item["id"]: [line["text"] for line in item["lines"]]
            for item in placed if item["type"] == "text"
        },
        "reasons": reasons,
        "notes": notes,
    }


# ---------------------------------------------------------------------------
# Render
# ---------------------------------------------------------------------------

def _flat_rgb(image: Any) -> Any:
    from PIL import Image

    if image.mode in ("RGBA", "LA", "P"):
        rgba = image.convert("RGBA")
        flat = Image.new("RGB", rgba.size, (255, 255, 255))
        flat.paste(rgba, mask=rgba.split()[-1])
        return flat
    return image if image.mode == "RGB" else image.convert("RGB")


def _clip(box: list[float], width: int, height: int, pad: float = 0.0) -> tuple[int, int, int, int]:
    return (
        max(0, int(box[0] - pad)),
        max(0, int(box[1] - pad)),
        min(width, int(box[2] + pad) + 1),
        min(height, int(box[3] + pad) + 1),
    )


def _gradient(fill: dict[str, Any], colors: dict[str, Any], size: tuple[int, int]) -> Any:
    """A top-to-bottom fade as an RGBA picture: ``stops`` are ``[position,
    colour, opacity]``, position 0 at the shape's top and 1 at its bottom."""
    from PIL import Image

    raw = fill.get("gradient")
    if not isinstance(raw, list) or len(raw) < 2:
        raise TemplateError("a gradient fill needs two or more [position, colour, opacity] stops")
    try:
        stops = sorted(
            (float(pos), parse_color(color, colors), float(alpha)) for pos, color, alpha in raw
        )
    except (TypeError, ValueError) as exc:
        if isinstance(exc, TemplateError):
            raise
        raise TemplateError("a gradient stop is [position, colour, opacity]") from exc
    width, height = size
    column = Image.new("RGBA", (1, height))
    pixels = column.load()
    for y in range(height):
        at = y / max(1, height - 1)
        lower = max((stop for stop in stops if stop[0] <= at), default=stops[0])
        upper = min((stop for stop in stops if stop[0] >= at), key=lambda stop: stop[0],
                    default=stops[-1])
        span = upper[0] - lower[0]
        mix = (at - lower[0]) / span if span > 0 else 0.0
        rgb = tuple(round(a + (b - a) * mix) for a, b in zip(lower[1], upper[1]))
        alpha = lower[2] + (upper[2] - lower[2]) * mix
        pixels[0, y] = (*rgb, round(255 * min(1.0, max(0.0, alpha))))
    return column.resize((width, height))


def _draw_shape(canvas: Any, item: dict[str, Any], colors: dict[str, Any]) -> None:
    from PIL import Image, ImageChops, ImageDraw

    if item["type"] == "polygon":
        xs, ys = [p[0] for p in item["points"]], [p[1] for p in item["points"]]
        bounds = [min(xs), min(ys), max(xs), max(ys)]
    else:
        bounds = item["box"]
    x0, y0, x1, y1 = _clip(bounds, canvas.width, canvas.height, pad=1)
    if x1 <= x0 or y1 <= y0:
        return
    n = _SUPERSAMPLE
    mask = Image.new("L", ((x1 - x0) * n, (y1 - y0) * n), 0)
    draw = ImageDraw.Draw(mask)
    if item["type"] == "polygon":
        draw.polygon([((px - x0) * n, (py - y0) * n) for px, py in item["points"]], fill=255)
    else:
        bx0, by0, bx1, by1 = item["box"]
        draw.ellipse([(bx0 - x0) * n, (by0 - y0) * n, (bx1 - x0) * n, (by1 - y0) * n], fill=255)
    mask = mask.resize((x1 - x0, y1 - y0), Image.Resampling.LANCZOS)
    fill = item["fill"]
    if isinstance(fill, dict):
        # The fade runs over the shape's own height, not the clipped part of it.
        top, bottom = round(bounds[1]), round(bounds[3])
        fade = _gradient(fill, colors, (x1 - x0, max(1, bottom - top)))
        fade = fade.crop((0, y0 - top, x1 - x0, y1 - top))
        mask = ImageChops.multiply(mask, fade.split()[-1])
        canvas.paste(fade.convert("RGB"), (x0, y0, x1, y1), mask)
    else:
        canvas.paste(parse_color(fill, colors), (x0, y0, x1, y1), mask)


def _draw_effects(
    canvas: Any,
    mask: Any,
    corner: tuple[int, int],
    color: tuple[int, int, int],
    glow: Optional[dict[str, Any]],
    shadow: Optional[dict[str, Any]],
    colors: dict[str, Any],
) -> None:
    """Shadow, then glow, then the thing itself, from one mask of its shape."""
    from PIL import Image, ImageFilter

    height = canvas.height
    region = (corner[0], corner[1], corner[0] + mask.width, corner[1] + mask.height)
    if shadow:
        offset = round(float(shadow.get("offset", 0)) * height * 0.7071)  # down and right, at 45°
        soft = Image.new("L", mask.size, 0)
        soft.paste(mask, (offset, offset))
        soft = soft.filter(ImageFilter.GaussianBlur(float(shadow.get("blur", 0)) * height / 3))
        soft = soft.point(lambda v: int(v * float(shadow.get("alpha", 0.4))))
        canvas.paste(parse_color(shadow.get("color", "#000000"), colors), region, soft)
    if glow:
        halo = mask.filter(ImageFilter.GaussianBlur(float(glow.get("radius", 0)) * height / 2))
        halo = halo.point(lambda v: min(255, int(v * 1.5)))
        canvas.paste(parse_color(glow.get("color", "#000000"), colors), region, halo)
    canvas.paste(color, region, mask)


def _effect_pad(item: dict[str, Any], height: int) -> float:
    glow, shadow = item.get("glow") or {}, item.get("shadow") or {}
    reach = max(
        float(glow.get("radius", 0)) * 2,
        float(shadow.get("offset", 0)) + float(shadow.get("blur", 0)) * 2,
    )
    return reach * height + 4


def _draw_text(canvas: Any, item: dict[str, Any], colors: dict[str, Any], fonts: _Fonts) -> None:
    from PIL import Image, ImageDraw

    if not item["lines"]:
        return
    boxes = [line["box"] for line in item["lines"]]
    bounds = [min(b[0] for b in boxes), min(b[1] for b in boxes),
              max(b[2] for b in boxes), max(b[3] for b in boxes)]
    x0, y0, x1, y1 = _clip(bounds, canvas.width, canvas.height,
                           pad=_effect_pad(item, canvas.height) + item["px"] / 2)
    if x1 <= x0 or y1 <= y0:
        return
    mask = Image.new("L", (x1 - x0, y1 - y0), 0)
    draw = ImageDraw.Draw(mask)
    base = fonts.get(item, item["px"])
    small = fonts.get(item, item["px"] * _SMALL_CAP_SCALE) if item["small_caps"] else base
    for line in item["lines"]:
        cursor = line["x"] - x0
        for part, scale in _runs(line["text"], item["small_caps"]):
            font = base if scale == 1.0 else small
            draw.text((cursor, line["baseline"] - y0), part, font=font, fill=255, anchor="ls")
            cursor += font.getlength(part)
    _draw_effects(canvas, mask, (x0, y0), parse_color(item["fill"], colors),
                  item.get("glow"), item.get("shadow"), colors)


def _draw_image(canvas: Any, item: dict[str, Any], colors: dict[str, Any], base: Path) -> None:
    from PIL import Image

    path = base / item["src"]
    try:
        with Image.open(path) as source:
            source.load()
            picture = source.copy()
    except OSError as exc:
        raise TemplateError(f"layer {item['id']!r}: cannot open {path}: {exc}") from exc
    # A tinted image is a stencil: its alpha, or how light it is, says where
    # the colour goes.
    if item["mask"] == "luminance":
        alpha = picture.convert("L")
    else:
        alpha = picture.convert("RGBA").split()[-1]
    bx0, by0, bx1, by1 = item["box"]
    scale = min((bx1 - bx0) / picture.width, (by1 - by0) / picture.height)
    size = (max(1, round(picture.width * scale)), max(1, round(picture.height * scale)))
    corner = (round((bx0 + bx1) / 2 - size[0] / 2), round((by0 + by1) / 2 - size[1] / 2))
    alpha = alpha.resize(size, Image.Resampling.LANCZOS)
    if item.get("tint"):
        fill: Any = Image.new("RGB", size, parse_color(item["tint"], colors))
    else:
        fill = _flat_rgb(picture).resize(size, Image.Resampling.LANCZOS)
    canvas.paste(fill, corner, alpha)


def render(art: Any, solved: dict[str, Any], template: dict[str, Any]) -> Any:
    """The cover: the art, then every layer in the template's order."""
    from PIL import Image

    width, height = solved["size"]
    colors = solved["colors"]
    placement = solved["art"]
    canvas = Image.new("RGB", (width, height), (255, 255, 255))
    size = (max(1, round(placement["width"])), max(1, round(placement["height"])))
    scaled = _flat_rgb(art).resize(size, Image.Resampling.LANCZOS)
    canvas.paste(scaled, (round(placement["left"]), round(placement["top"])))
    base = Path(template["_dir"])
    fonts = _Fonts(base)
    for item in solved["layers"]:
        if item["type"] in ("polygon", "ellipse"):
            _draw_shape(canvas, item, colors)
        elif item["type"] == "text":
            _draw_text(canvas, item, colors, fonts)
        else:
            _draw_image(canvas, item, colors, base)
    return canvas


# ---------------------------------------------------------------------------
# Band colours
# ---------------------------------------------------------------------------

def _deepened(hue: float, saturation: float, value: float, ink: tuple[int, int, int]
              ) -> tuple[int, int, int]:
    """A hue as a band would carry it: saturated, and dark enough to read on."""
    saturation = max(saturation, 0.7)
    value = min(max(value, 0.42), 0.6)
    while True:
        rgb = tuple(round(c * 255) for c in colorsys.hsv_to_rgb(hue, saturation, value))
        if contrast(rgb, ink) >= _MIN_CONTRAST or value <= 0.2:
            return rgb  # type: ignore[return-value]
        value *= 0.94


def suggest_band_colors(art: Any, ink: tuple[int, int, int], count: int = 4) -> list[str]:
    """Colours from the picture that the lettering reads on: its own dominant
    hues, deepened and saturated the way the house bands are. Where the picture
    has fewer hues than ``count``, darker and lighter takes on its main one."""
    from PIL import Image

    small = _flat_rgb(art).copy()
    small.thumbnail((96, 96))
    quantised = small.quantize(colors=16, method=Image.Quantize.MEDIANCUT)
    palette = quantised.getpalette() or []
    found: list[tuple[float, tuple[int, int, int], tuple[float, float, float]]] = []
    for pixels, index in quantised.getcolors() or []:
        r, g, b = palette[index * 3: index * 3 + 3]
        hue, saturation, value = colorsys.rgb_to_hsv(r / 255, g / 255, b / 255)
        if saturation < 0.15:
            continue  # a grey has no hue to deepen
        found.append((pixels * saturation, _deepened(hue, saturation, value, ink),
                      (hue, saturation, value)))
    found.sort(reverse=True)

    picked: list[tuple[int, int, int]] = []

    def take(rgb: tuple[int, int, int]) -> None:
        apart = all(sum(abs(a - b) for a, b in zip(rgb, other)) > 40 for other in picked)
        if apart and len(picked) < count:
            picked.append(rgb)

    for _score, rgb, _hsv in found:
        take(rgb)
    # Not enough hues of its own: the main one again, redder, deeper, lighter.
    for _score, _rgb, (hue, _saturation, _value) in found[:2]:
        for shift, saturation, value in ((-0.04, 0.9, 0.5), (0, 0.95, 0.44), (0, 0.8, 0.6),
                                         (-0.04, 0.9, 0.4)):
            take(_deepened((hue + shift) % 1.0, saturation, value, ink))
    return [to_hex(rgb) for rgb in picked]


def swatch_covers(
    art: Any, template: dict[str, Any], spec: dict[str, Any], colors: list[str], name: str
) -> list[tuple[dict[str, Any], Any]]:
    """The cover once per colour, as ``(the spec it was drawn with, the picture)``."""
    covers = []
    for color in colors:
        trial = merge_spec(spec, {"colors": {name: color}})
        solved = solve(template, trial, art.size, height=_SWATCH_HEIGHT)
        if solved["status"] != "ok":
            raise TemplateError(solved["error"])
        covers.append((trial, render(art, solved, template)))
    return covers


def swatch_sheet(covers: list[Any], colors: list[str]) -> Any:
    """One sheet: the covers side by side, numbered, hex underneath."""
    from PIL import Image, ImageDraw, ImageFont

    gap, label = 24, 56
    width = sum(c.width for c in covers) + gap * (len(covers) + 1)
    sheet = Image.new("RGB", (width, _SWATCH_HEIGHT + gap + label), (245, 245, 245))
    draw = ImageDraw.Draw(sheet)
    font = ImageFont.load_default(size=30)
    x = gap
    for number, (cover, color) in enumerate(zip(covers, colors), start=1):
        sheet.paste(cover, (x, gap))
        draw.text((x, gap + _SWATCH_HEIGHT + 10), f"{number}  {color}", font=font, fill=(30, 30, 30))
        x += cover.width + gap
    return sheet


def render_swatches(
    art: Any, template: dict[str, Any], spec: dict[str, Any], colors: list[str], name: str
) -> Any:
    """One sheet: the cover in each of ``colors``, side by side, hex underneath."""
    covers = swatch_covers(art, template, spec, colors, name)
    return swatch_sheet([cover for _spec, cover in covers], colors)


# ---------------------------------------------------------------------------
# The book's side: where the art is, and the command
# ---------------------------------------------------------------------------

def _current_cover(project_dir: Path) -> tuple[str, Optional[Path]]:
    for name in COVER_NAMES:
        path = images_dir(project_dir) / name
        if path.is_file():
            return name, path
    return COVER_NAMES[0], None


def kept_art(project_dir: Path) -> Optional[Path]:
    folder = cover_dir(project_dir)
    if not folder.is_dir():
        return None
    found = sorted(p for p in folder.glob("art.*") if p.is_file())
    return found[0] if found else None


def _find_art(
    project_dir: Path,
    spec: dict[str, Any],
    given: Optional[Path],
    cover_is_art: bool,
) -> dict[str, Any]:
    """Which picture to draw on, and whether it has to be copied in first.

    The one mistake to rule out is taking a cover that already has its band for
    the art, so ``images/cover.*`` is only ever taken on evidence: the ledger
    says the image pass put that very file there, or the caller says so.
    """
    kept = kept_art(project_dir)
    key, cover = _current_cover(project_dir)
    state = spec.get("state") or {}
    if given is not None:
        given = Path(given)
        if not given.is_file():
            return {"error": f"--art not found: {given}"}
        return {"source": given, "seed": True, "why": "given with --art"}
    if cover is None:
        if kept is None:
            return {"error": "this book has no cover picture yet: pass --art <file>, "
                             "or make the art with a `cover` job and apply it"}
        return {"source": kept, "seed": False, "why": "kept from an earlier compose"}

    cover_sha = ledger.sha256_file(cover)
    if cover_sha == state.get("composed_sha"):
        if kept is None:
            return {"error": f"images/{key} is a cover this command composed, and the "
                             "art it was made from is gone from cover/: pass --art <file>"}
        return {"source": kept, "seed": False, "why": "kept from an earlier compose"}
    row = ledger.current_state(project_dir).get(key)
    from_pass = bool(
        row
        and row.get("action") == ledger.ACTION_APPLY
        and not row.get("compose")
        and row.get("sha256_after") == cover_sha
    )
    if from_pass or cover_is_art:
        if kept is not None and cover_sha == state.get("art_sha"):
            return {"source": kept, "seed": False, "why": "kept from an earlier compose"}
        return {
            "source": cover, "seed": True,
            "why": f"images/{key}, which the image pass applied" if from_pass
            else f"images/{key}, on --cover-is-art",
        }
    if kept is not None:
        return {
            "source": kept, "seed": False, "why": "kept from an earlier compose",
            "warning": f"images/{key} is not the cover this command last composed; "
                       "it will be replaced on --apply",
        }
    return {"error": (
        f"cannot tell whether images/{key} is art or a finished cover: nothing in "
        "the ledger says the image pass made it. Pass --art <file> with the "
        "picture that has no lettering, or --cover-is-art if this one has none."
    )}


def _translated_title(project_dir: Path) -> Optional[str]:
    """The title in the edition's language, where the project wrote one down."""
    try:
        doc = json.loads((Path(project_dir) / "project.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(doc, dict):
        return None
    for key in ("spanish_title", "translated_title"):
        value = doc.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _spec_for_disk(spec: dict[str, Any]) -> str:
    return json.dumps(spec, ensure_ascii=False, indent=2) + "\n"


def compose(
    project_dir: Path,
    overrides: Optional[dict[str, Any]] = None,
    *,
    art: Optional[Path] = None,
    cover_is_art: bool = False,
    swatches: bool = False,
    tries: Optional[list[dict[str, Any]]] = None,
    from_draft: Optional[int] = None,
    approved_id: Optional[str] = None,
    apply: bool = False,
    dry_run: bool = False,
    template: Optional[Path] = None,
) -> dict[str, Any]:
    """Compose this book's cover into ``cover/proof.png``; with ``apply``, put
    it in ``images/``. ``overrides`` are merged into ``cover/spec.json`` and
    kept, so a change asked for once stays asked for.

    ``from_draft`` starts from the settings an earlier draft was drawn with
    instead of the spec as it stands. ``tries`` are ``{label, overrides}``: the
    cover is drawn once with each laid over the spec and kept as a draft to
    compare, and the spec is left as it was. ``approved_id`` is the draft the
    user approved on the board: ``apply`` then lands this cover only if it is
    that one.
    """
    from PIL import Image

    project_dir = Path(project_dir)
    folder = cover_dir(project_dir)
    try:
        design = load_template(template)
        spec = load_spec(project_dir)
    except TemplateError as exc:
        return {"status": "error", "error": str(exc)}
    went_back: Optional[dict[str, Any]] = None
    if from_draft is not None:
        went_back = drafts.find(project_dir, from_draft)
        if went_back is None or not isinstance(went_back.get("spec"), dict):
            on_file = [row["n"] for row in drafts.load(project_dir)]
            return {"status": "error",
                    "error": f"there is no draft {from_draft} of this cover (on file: {on_file})"}
        spec = {**went_back["spec"], **({"state": spec["state"]} if spec.get("state") else {})}
    spec = merge_spec(spec, overrides or {})

    found = _find_art(project_dir, spec, art, cover_is_art)
    if found.get("error"):
        return {"status": "error", "error": found["error"]}
    source: Path = found["source"]
    info = probe_image(source)
    if info.get("error"):
        return {"status": "error", "error": f"the art {source} cannot be read: {info['error']}"}
    warnings: list[str] = [found["warning"]] if found.get("warning") else []
    art_sha = ledger.sha256_file(source)
    design_sha = ledger.sha256_file(Path(design["_path"]))
    if went_back is not None and went_back.get("art_sha") != art_sha:
        warnings.append(
            f"draft {from_draft} was drawn on art this book no longer has: its "
            "settings are carried over to the art it has now"
        )

    meta = book_meta(project_dir)
    title_from = "spec"
    if not (spec.get("title") or "").strip():
        translated = _translated_title(project_dir)
        if translated:
            spec["title"], title_from = translated, "project (translated title)"
        else:
            spec["title"], title_from = meta["title"], "project"
            warnings.append(
                f"no title given: using the project's, {meta['title']!r}, which may be "
                "the source language's. Pass --title with the title as the cover "
                "should print it."
            )
    if "author" not in spec and meta["author"]:
        spec["author"] = meta["author"]
    # A house design from before the subtitle had a layer has nowhere to put
    # one, and would leave it off without a word.
    if (spec.get("subtitle") or "").strip() and not _says(design, "subtitle"):
        warnings.append(
            f"the subtitle is not on this cover: {Path(design['_path']).name} has no "
            f"text layer for it. Copy the `subtitle` layer and `subtitle_size` from "
            f"{EXAMPLE_TEMPLATE}, and hang the author `below` it."
        )

    with Image.open(source) as opened:
        opened.load()
        picture = opened.copy()

    # The colour the user picks per book. Until they have, the proof is drawn
    # in the first suggestion, which is not written to the spec: a guess that
    # was saved would read as their choice the next time.
    band_name = design.get("swatch_color", "band")
    drawn = spec
    try:
        ink = parse_color("$ink", design.get("colors")) if "ink" in (design.get("colors") or {}) \
            else (255, 255, 255)
        suggestions = suggest_band_colors(picture, ink)
        color_from = "spec"
        if band_name not in (spec.get("colors") or {}):
            color_from = "suggested" if suggestions else "template"
            if suggestions:
                drawn = merge_spec(spec, {"colors": {band_name: suggestions[0]}})
        solved = solve(design, drawn, picture.size)
    except TemplateError as exc:
        return {"status": "error", "error": str(exc)}
    if apply and color_from != "spec":
        return {
            "status": "error",
            "error": f"no {band_name} colour has been picked for this book: show the "
                     f"swatches, then pass --color {band_name}=#rrggbb",
            "suggested_colors": suggestions,
        }

    key, _cover = _current_cover(project_dir)
    out: dict[str, Any] = {
        "status": "ok" if solved["status"] == "ok" else "error",
        "dry_run": dry_run,
        "applied": False,
        "template": design["_path"],
        "art": {
            "from": found["why"],
            "path": str(source),
            "size": [picture.width, picture.height],
        },
        "title_from": title_from,
        "band_color": (solved.get("colors") or design.get("colors") or {}).get(band_name),
        "band_color_from": color_from,
        "suggested_colors": suggestions,
        "warnings": warnings,
    }
    if solved["status"] != "ok":
        out.update({
            "error": solved["error"],
            "misfits": solved["misfits"],
            "notes": solved["notes"],
            "instructions": (
                "Nothing was written. Shorten or re-break the title with "
                "--title-lines, or raise what the template allows with --set."
            ),
        })
        return out

    out.update({
        "size": solved["size"],
        "lines": solved["lines"],
        "vars": solved["vars"],
        "art_top": round(solved["art_top"], 4),
        "side_crop": round(solved["art"]["side_crop"], 4),
        "reasons": solved["reasons"],
        "notes": solved["notes"],
    })
    if dry_run:
        out["instructions"] = "Nothing was written. Re-run without --dry-run for the proof."
        return out

    try:
        folder.mkdir(parents=True, exist_ok=True)
        state = dict(spec.get("state") or {})
        if found["seed"]:
            kept = folder / f"art{source.suffix.lower()}"
            for old in folder.glob("art.*"):
                if old != kept:
                    old.unlink()
            if source.resolve() != kept.resolve():
                shutil.copyfile(source, kept)
            state["art_sha"] = ledger.sha256_file(kept)
            out["art"]["kept_at"] = str(kept)

        # One stamp for everything this invocation draws to compare: its set.
        set_id = time.strftime("%Y%m%d_%H%M%S")
        kept_as = {"art_sha": art_sha, "template": Path(design["_path"]).name,
                   "template_sha": design_sha}

        if swatches:
            if not suggestions:
                return {**out, "status": "error",
                        "error": "the picture has no colour to take a band from; give one with --color"}
            sheet = folder / "swatches.png"
            covers = swatch_covers(picture, design, drawn, suggestions, band_name)
            swatch_sheet([cover for _trial, cover in covers], suggestions).save(sheet)
            out["swatches"] = str(sheet)
            out["swatch_drafts"] = []
            for color, (trial, cover) in zip(suggestions, covers):
                row, _new = drafts.record(
                    project_dir, cover, kind=drafts.KIND_SWATCH, spec=trial, base=spec,
                    set_id=set_id, label=color, color=color, **kept_as,
                )
                out["swatch_drafts"].append({"draft": row["n"], "color": color})

        if tries:
            out["tried"] = []
            for attempt in tries:
                said: dict[str, Any] = {"label": attempt.get("label")}
                try:
                    trial = merge_spec(drawn, attempt.get("overrides") or {})
                    trial_solved = solve(design, trial, picture.size, height=drafts.PREVIEW_SIDE)
                    if trial_solved["status"] != "ok":
                        said.update({"status": trial_solved["status"], "error": trial_solved["error"]})
                    else:
                        row, _new = drafts.record(
                            project_dir, render(picture, trial_solved, design),
                            kind=drafts.KIND_VARIANT, spec=trial, base=spec,
                            set_id=set_id, label=attempt.get("label"), **kept_as,
                        )
                        said.update({
                            "status": "ok",
                            "draft": row["n"],
                            "path": str(drafts.preview_path(project_dir, row)),
                            "reasons": trial_solved["reasons"],
                            "notes": trial_solved["notes"],
                        })
                except TemplateError as exc:
                    said.update({"status": "error", "error": str(exc)})
                out["tried"].append(said)

        proof = folder / "proof.png"
        composed = render(picture, solved, design)
        composed.save(proof)
        out["proof"] = str(proof)
        proof_row, _new = drafts.record(
            project_dir, composed, kind=drafts.KIND_PROOF, spec=drawn, **kept_as,
        )
        out["draft"] = proof_row["n"]
        state["proof_draft"] = proof_row["n"]
        state["band"] = band_name

        if apply and approved_id is not None and proof_row["id"] != approved_id:
            spec["state"] = state
            _write_atomic(spec_path(project_dir), _spec_for_disk(spec))
            return {
                **out,
                "status": "error",
                "error": (
                    f"the cover as composed now (draft {proof_row['n']}) is not the "
                    "draft that was approved on the board: the art, the design or a "
                    "setting has changed since. Nothing was applied. Show the user "
                    "this proof and have them approve it."
                ),
            }

        if apply:
            last = ledger.current_state(project_dir).get(key)
            had_original = has_original(project_dir, key, last)
            target = images_dir(project_dir) / key
            before = probe_image(target) if target.is_file() else {}
            written, ledger_file = install_image(
                project_dir, key, proof,
                had_original=had_original,
                max_side=COVER_LONG_SIDE,
                size_before=[before["width"], before["height"]] if before.get("width") else None,
                made={
                    "candidate": None,
                    "note": None,
                    "job_id": None,
                    "mode": MODE_COVER,
                    "model": None,
                    "instruction": None,
                    "labels": {},
                    "prompt_sha": None,
                    "composite": None,
                    # What was drawn over the art, so the row reads on its own.
                    "compose": {
                        "template": Path(design["_path"]).name,
                        "lines": solved["lines"],
                        "colors": solved["colors"],
                        "vars": solved["vars"],
                        "art_top": out["art_top"],
                        "art_sha256": art_sha,
                        "draft": proof_row["n"],
                    },
                },
            )
            state["composed_sha"] = ledger.sha256_file(target)
            state["applied_draft"] = proof_row["n"]
            out.update({
                "applied": True,
                "target": str(target),
                "written_size": [written["width"], written["height"]],
                "backup": f"images_original/{key}" if had_original else None,
                "ledger_path": str(ledger_file),
            })

        spec["state"] = state
        _write_atomic(spec_path(project_dir), _spec_for_disk(spec))
        out["spec_path"] = str(spec_path(project_dir))
    except TemplateError as exc:
        return {**out, "status": "error", "error": str(exc)}

    out["instructions"] = (
        "Run `verify`, then hand back to translate-harness for `epub`."
        if apply
        else "Read the proof. Change what is wrong with one flag and re-run; when it "
        "is right, show it to the user, and re-run with --apply on their word. "
        "Every draft is on the image board's New images tab: give them its url "
        "(`board`) with the proof."
    )
    return out
