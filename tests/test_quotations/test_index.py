"""The EPUB reader, on a tiny made-up EPUB in the NBLA's markup."""

import json
import zipfile

import pytest

from src.quotations.index import (
    IndexError_,
    build_index,
    load_index,
    plain,
    write_index,
)

OPF = """<?xml version="1.0"?><package><metadata>
<dc:title>Biblia de Prueba</dc:title></metadata></package>"""

# Made-up verses in the NBLA's markup: a poetry chapter with a heading, a footnote
# anchor and a divine-name span; a prose chapter with small caps and a ver3 marker.
POETRY = """<html><body>
<p class="ctfm">Salmo 1</p>
<p class="ahaft2">Un título</p>
<p class="cs">Salmo de prueba.</p>
<p class="sl"><span class="ver2" id="v19001001">1</span>El <span>SEÑOR</span> es mi luz<a class="enref" href="x">a</a>,</p>
<p class="sl">Nada temeré.</p>
<p class="slf"><span class="ver2" id="v19001002">2</span>Junto al río me guía;</p>
<p class="sll">En Jerusalén <i class="calibre9">me</i> canta.</p>
</body></html>"""

PROSE = """<html><body>
<p class="calibre8"><span class="ver" id="v44001001">1</span>Y dijo Pedro: <span>D</span><span class="sm1">ICE EL</span> <span>S</span><span class="sm1">EÑOR</span>, <span class="sm1">QUE HACE TODO</span>. <span class="ver3" id="v44001002"><a id="GBS.1.01" class="calibre2"></a>2</span>Luego dijo* que fue a Jerusalén<sup>1</sup>.</p>
<p class="pf">Y Jerusalén se alegró en Jerusalén con Jerusalén.</p>
<p class="bl">Selah Una nota de glosario</p>
</body></html>"""


@pytest.fixture
def epub(tmp_path):
    path = tmp_path / "prueba.epub"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("content.opf", OPF)
        zf.writestr("OEBPS/a.xhtml", POETRY)
        zf.writestr("OEBPS/b.xhtml", PROSE)
    return path


def test_poetry_line_starts_are_kept_as_markers(epub):
    doc = build_index(epub, version="prueba")
    verses = doc["verses"]
    assert verses["19:001:001"] == "\nEl SEÑOR es mi luz,\nNada temeré."
    assert verses["19:001:002"] == "\nJunto al río me guía;\nEn Jerusalén me canta."
    assert plain(verses["19:001:001"]) == "El SEÑOR es mi luz, Nada temeré."


def test_small_caps_read_as_lower_case_and_headings_dropped(epub):
    verses = build_index(epub, version="prueba")["verses"]
    assert verses["44:001:001"] == "Y dijo Pedro: Dice el Señor, que hace todo."
    # ver3 counts, a page anchor inside the number does not hide the verse, the
    # historic-present "*" goes, a later prose paragraph continues the verse, and
    # the glossary line does not.
    assert verses["44:001:002"] == (
        "Luego dijo que fue a Jerusalén. Y Jerusalén se alegró en Jerusalén con Jerusalén."
    )
    assert "Salmo de prueba" not in json.dumps(verses, ensure_ascii=False)


def test_index_records_title_hash_and_names(epub):
    doc = build_index(epub, version="prueba")
    assert doc["title"] == "Biblia de Prueba"
    assert len(doc["epub_sha256"]) == 64
    assert "jerusalén" in doc["proper_words"]


def test_missing_index_is_an_error(tmp_path):
    with pytest.raises(IndexError_, match="index --epub"):
        load_index("nbla", path=tmp_path / "nbla.json")


def test_changed_epub_is_an_error(epub, tmp_path):
    target = write_index(build_index(epub, version="prueba"), path=tmp_path / "prueba.json")
    assert load_index("prueba", path=target).get("19:001:001")
    with zipfile.ZipFile(epub, "a") as zf:
        zf.writestr("OEBPS/c.xhtml", "<html></html>")
    with pytest.raises(IndexError_, match="has changed"):
        load_index("prueba", path=target)


def test_wrong_version_is_an_error(epub, tmp_path):
    target = write_index(build_index(epub, version="prueba"), path=tmp_path / "nbla.json")
    with pytest.raises(IndexError_, match="not 'nbla'"):
        load_index("nbla", path=target)
