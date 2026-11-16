"""CSV beside JSON Lines (contract README 7.5): one row per record, flat columns.

* UTF-8 with a byte order mark, so Excel reads the text as UTF-8;
* separator ``;`` (Excel in most European locales splits on ``;``, and decimal commas never
  collide with it); fields quoted when they hold ``;``, a quote or a line break; lines end CRLF;
* columns: the record's own fields, ``variant.*`` only when a record has a variant,
  ``price_status``/``price_max``/``regular_price``/``on_sale``/``currency_from_default`` only when a record has a price
  status, then ``data.<field>`` in the mappings' field order;
* values: ``null`` empty, booleans ``true``/``false``, numbers as in JSON (dot decimal), lists
  joined with `` | ``, objects (spec tables, variant options) as JSON text;
* a text value starting with ``=``, ``+``, ``-``, ``@``, a tab or a carriage return gets a
  leading ``'`` so a spreadsheet does not run it as a formula (numbers are never changed).
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
from pathlib import Path
from typing import Any, Iterable

SEPARATOR = ";"
LIST_JOIN = " | "
RECORD_COLUMNS = ["record_id", "source_url", "requested_url", "source_key", "site", "template_id", "mapping_version", "item_index", "extracted_at"]
VARIANT_COLUMNS = ["variant.index", "variant.count", "variant.source", "variant.id", "variant.options"]
PRICE_COLUMNS = ["price_status", "price_max", "regular_price", "on_sale", "currency_from_default"]  # when a record has a price status
FORMULA_START = ("=", "+", "-", "@", "\t", "\r")


def cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return json.dumps(value)
    if isinstance(value, dict):
        return _text(json.dumps(value, ensure_ascii=False, separators=(",", ":")))
    if isinstance(value, list):
        return _text(LIST_JOIN.join(cell(v) if not isinstance(v, str) else v for v in value))
    return _text(str(value))


def _text(text: str) -> str:
    return "'" + text if text.startswith(FORMULA_START) else text


def record_columns(records: Iterable[dict]) -> list[str]:
    """Header for these records: own fields, variant columns if any, data fields in first-seen order."""
    data_fields: dict[str, None] = {}
    has_variant = has_price = False
    for rec in records:
        if rec.get("variant"):
            has_variant = True
        if rec.get("price_status"):
            has_price = True
        for name in rec.get("data") or {}:
            data_fields.setdefault(name, None)
    return (RECORD_COLUMNS + (VARIANT_COLUMNS if has_variant else []) + (PRICE_COLUMNS if has_price else [])
            + [f"data.{n}" for n in data_fields])


def record_row(rec: dict, columns: list[str]) -> list[str]:
    out = []
    variant = rec.get("variant") or {}
    data = rec.get("data") or {}
    for col in columns:
        if col.startswith("data."):
            out.append(cell(data.get(col[5:])))
        elif col.startswith("variant."):
            out.append(cell(variant.get(col[8:])))
        else:
            out.append(cell(rec.get(col)))
    return out


class CsvFile:
    """Writes rows to ``path`` (through a ``.tmp`` file), counting bytes and the SHA-256."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.tmp = path.with_name(path.name + ".tmp")
        self.fh = open(self.tmp, "wb")
        self.sha = hashlib.sha256()
        self.size = 0
        self.rows = 0
        self._put("﻿")

    def _put(self, text: str) -> None:
        data = text.encode("utf-8")
        self.fh.write(data)
        self.sha.update(data)
        self.size += len(data)

    def write(self, row: list[str]) -> None:
        buf = io.StringIO()
        csv.writer(buf, delimiter=SEPARATOR, lineterminator="\r\n", quoting=csv.QUOTE_MINIMAL).writerow(row)
        self._put(buf.getvalue())

    def close(self) -> dict:
        self.fh.flush()
        os.fsync(self.fh.fileno())
        self.fh.close()
        os.replace(self.tmp, self.path)
        return {"file_name": self.path.name, "size_bytes": self.size, "sha256": self.sha.hexdigest()}

    def abort(self) -> None:
        try:
            self.fh.close()
        finally:
            self.tmp.unlink(missing_ok=True)
