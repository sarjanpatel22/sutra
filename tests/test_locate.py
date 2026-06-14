"""Tests for `friday.vision.locate` — pure TSV parsing + mocked OCR (no real capture)."""

from __future__ import annotations

import pytest

from friday.vision import locate
from friday.vision.locate import Element, ocr_available, parse_tsv

# tesseract TSV header it actually emits; rows are tab-separated under it.
_HEADER = "\t".join(
    "level page_num block_num par_num line_num word_num "
    "left top width height conf text".split()
)


def _row(left: int, top: int, w: int, h: int, conf: str, text: str) -> str:
    return f"5\t1\t1\t1\t1\t1\t{left}\t{top}\t{w}\t{h}\t{conf}\t{text}"


def _tsv(*rows: str) -> str:
    return "\n".join([_HEADER, *rows])


# --- parse_tsv ---------------------------------------------------------------


def test_parse_tsv_basic_rows() -> None:
    tsv = _tsv(
        _row(10, 20, 30, 40, "95.5", "Save"),
        _row(100, 200, 50, 16, "88", "File"),
    )
    els = parse_tsv(tsv)
    assert len(els) == 2
    assert els[0] == Element(text="Save", x=10, y=20, w=30, h=40, conf=95.5)
    assert els[1].text == "File"
    assert els[1].conf == 88.0


def test_parse_tsv_drops_empty_text() -> None:
    tsv = _tsv(
        _row(1, 1, 5, 5, "99", "   "),  # whitespace-only → dropped
        _row(2, 2, 5, 5, "99", "Ok"),
    )
    els = parse_tsv(tsv)
    assert [e.text for e in els] == ["Ok"]


def test_parse_tsv_drops_below_min_conf() -> None:
    tsv = _tsv(
        _row(1, 1, 5, 5, "39.9", "Low"),  # below default 40 → dropped
        _row(2, 2, 5, 5, "40.0", "Keep"),  # exactly at threshold → kept
    )
    els = parse_tsv(tsv)
    assert [e.text for e in els] == ["Keep"]


def test_parse_tsv_custom_min_conf() -> None:
    tsv = _tsv(_row(1, 1, 5, 5, "70", "Mid"))
    assert parse_tsv(tsv, min_conf=80.0) == []
    assert [e.text for e in parse_tsv(tsv, min_conf=60.0)] == ["Mid"]


def test_parse_tsv_drops_zero_dimensions() -> None:
    tsv = _tsv(
        _row(1, 1, 0, 10, "99", "ZeroW"),  # w<=0 → dropped
        _row(1, 1, 10, 0, "99", "ZeroH"),  # h<=0 → dropped
        _row(1, 1, 10, 10, "99", "Real"),
    )
    els = parse_tsv(tsv)
    assert [e.text for e in els] == ["Real"]


def test_parse_tsv_skips_malformed_rows() -> None:
    # A row whose numeric columns aren't ints/floats must be skipped, not crash.
    bad = "5\t1\t1\t1\t1\t1\tNaN\ttop\tw\th\tconf\tBad"
    short = "5\t1\t1"  # too few columns to even reach the text index
    tsv = _tsv(bad, short, _row(5, 6, 7, 8, "90", "Good"))
    els = parse_tsv(tsv)
    assert [e.text for e in els] == ["Good"]


def test_parse_tsv_empty_and_garbage() -> None:
    assert parse_tsv("") == []
    assert parse_tsv("\n\n") == []
    # Garbage without the expected header columns → no usable indices → [].
    assert parse_tsv("not\ta\treal\theader\nwith\tsome\tjunk\tdata") == []


# --- Element geometry --------------------------------------------------------


def test_element_centre_math() -> None:
    el = Element(text="x", x=10, y=20, w=30, h=40, conf=99.0)
    assert el.cx == 10 + 30 // 2  # 25
    assert el.cy == 20 + 40 // 2  # 40


def test_element_centre_odd_dimensions() -> None:
    el = Element(text="x", x=0, y=0, w=5, h=7, conf=50.0)
    assert el.cx == 2  # floor of 5//2
    assert el.cy == 3  # floor of 7//2


# --- ocr_image (mocked subprocess) -------------------------------------------


async def test_ocr_image_parses_canned_tsv(monkeypatch: pytest.MonkeyPatch) -> None:
    tsv = _tsv(_row(10, 20, 30, 40, "95", "Search")).encode("utf-8")

    async def fake_run(cmd: list[str], stdin: bytes | None = None) -> bytes:
        assert stdin == b"PNGBYTES"  # the image bytes are fed to tesseract on stdin
        return tsv

    monkeypatch.setattr(locate, "_run", fake_run)
    monkeypatch.setattr(locate, "_TESSERACT", "/usr/bin/tesseract")
    els = await locate.ocr_image(b"PNGBYTES")
    assert len(els) == 1
    assert els[0].text == "Search"
    assert (els[0].cx, els[0].cy) == (25, 40)


async def test_ocr_image_raises_without_tesseract(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(locate, "_TESSERACT", None)
    with pytest.raises(RuntimeError):
        await locate.ocr_image(b"whatever")


# --- ocr_available -----------------------------------------------------------


def test_ocr_available_true_when_both_present(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(locate, "_TESSERACT", "/usr/bin/tesseract")
    monkeypatch.setattr(locate, "_IMPORT", "/usr/bin/import")
    assert ocr_available() is True


def test_ocr_available_false_without_tesseract(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(locate, "_TESSERACT", None)
    monkeypatch.setattr(locate, "_IMPORT", "/usr/bin/import")
    assert ocr_available() is False


def test_ocr_available_false_without_import(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(locate, "_TESSERACT", "/usr/bin/tesseract")
    monkeypatch.setattr(locate, "_IMPORT", None)
    assert ocr_available() is False
