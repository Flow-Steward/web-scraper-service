"""CSV beside JSON Lines (README 7.5): cells, columns, the file."""

from __future__ import annotations

import csv
import hashlib
import io
import tempfile
from pathlib import Path

from app.core.csvout import RECORD_COLUMNS, VARIANT_COLUMNS, CsvFile, cell, record_columns, record_row


def test_cells():
    assert cell(None) == "" and cell(True) == "true" and cell(False) == "false"
    assert cell(54.0) == "54.0" and cell(3) == "3"
    assert cell(["a", "b", 2]) == "a | b | 2"
    assert cell({"Fassung": "E27", "Watt": "60"}) == '{"Fassung":"E27","Watt":"60"}'
    assert cell("Mūsų lempa") == "Mūsų lempa"
    for bad in ("=1+1", "+1", "-x", "@cmd", "\tx"):
        assert cell(bad) == "'" + bad
    assert cell(-5) == "-5"  # numbers are never changed


def test_columns_and_rows():
    recs = [
        {"record_id": "rec_1", "source_url": "https://a/x", "site": "a", "template_id": "p", "mapping_version": 1, "item_index": 0,
         "extracted_at": "t", "data": {"title": "X", "price": 1.5}},
        {"record_id": "rec_2", "source_url": "https://a/y", "site": "a", "template_id": "p", "mapping_version": 1, "item_index": 1,
         "extracted_at": "t", "variant": {"index": 1, "count": 2, "source": "jsonld", "options": {"Color": "red"}},
         "data": {"title": "Y", "price": None, "specs": {"a": "b"}}},
    ]
    cols = record_columns(recs)
    assert cols == RECORD_COLUMNS + VARIANT_COLUMNS + ["data.title", "data.price", "data.specs"]
    row = dict(zip(cols, record_row(recs[1], cols)))
    assert row["variant.options"] == '{"Color":"red"}' and row["data.price"] == "" and row["data.specs"] == '{"a":"b"}'
    assert "variant.index" not in record_columns(recs[:1])


def test_file_has_bom_semicolons_and_digest():
    path = Path(tempfile.mkdtemp()) / "x.csv"
    out = CsvFile(path)
    out.write(["a", "b;c", 'q"uote'])
    out.write(["1", "line\nbreak", ""])
    info = out.close()
    raw = path.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf") and info["size_bytes"] == len(raw) and info["sha256"] == hashlib.sha256(raw).hexdigest()
    rows = list(csv.reader(io.StringIO(raw.decode("utf-8-sig"), newline=""), delimiter=";"))
    assert rows == [["a", "b;c", 'q"uote'], ["1", "line\nbreak", ""]]
    assert not path.with_name("x.csv.tmp").exists()
