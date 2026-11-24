"""Bounded execution of configurable URL policy patterns, with Python re syntax.

Only URL policy matching uses this module; extraction transforms are separate.
Budgets surround synchronous work only and must never span a network await.
"""

from __future__ import annotations

import hashlib
import re
import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from functools import lru_cache, wraps

import regex

from app.core.errors import ApiError, field_error

SEARCH_SECONDS = 0.020
URL_SECONDS = 0.050
BATCH_SECONDS = 2.0
# A successful search must not fail because this process was descheduled while
# another browser/process used the CPU. The engine retains its own timeout;
# wrapper allowances charge only this thread's matching work.
_clock = time.thread_time
_url_budget: ContextVar[float | None] = ContextVar("url_regex_budget", default=None)
_batch_budget: ContextVar[float | None] = ContextVar("url_regex_batch_budget", default=None)


class UrlPatternError(ApiError):
    def __init__(self, field: str, pattern: str, code: str) -> None:
        identifier = hashlib.sha256(pattern.encode()).hexdigest()[:12]
        detail = "exceeded its execution budget" if code == "pattern_timeout" else "is not a valid Python regular expression"
        message = f"URL pattern {field} ({identifier}) {detail}"
        super().__init__("validation_failed", message, fields=[field_error(field, code, message)])
        self.pattern_code = code


@contextmanager
def _budget(variable, seconds):
    token = variable.set(seconds) if variable.get() is None else None
    try:
        yield
    finally:
        if token is not None:
            variable.reset(token)


def url_pattern_budget():
    return _budget(_url_budget, URL_SECONDS)


@dataclass
class UrlPatternBatch:
    """Carry matching allowance across synchronous pieces before the next await."""

    remaining: float = field(default_factory=lambda: BATCH_SECONDS)


@contextmanager
def url_pattern_batch(batch: UrlPatternBatch | None = None):
    if batch is None:
        with _budget(_batch_budget, BATCH_SECONDS):
            yield
        return
    token = _batch_budget.set(batch.remaining) if _batch_budget.get() is None else None
    try:
        yield
    finally:
        if token is not None:
            batch.remaining = _batch_budget.get()
            _batch_budget.reset(token)


def url_policy(function):
    @wraps(function)
    def bounded(*args, **kwargs):
        with url_pattern_budget():
            return function(*args, **kwargs)
    return bounded


def url_batch(function):
    @wraps(function)
    def bounded(*args, **kwargs):
        with url_pattern_batch():
            return function(*args, **kwargs)
    return bounded


@lru_cache(maxsize=1024)
def _compile(pattern: str, flags: int):
    # VERSION0 plus standard-library validation retains the documented syntax;
    # regex-only constructs must not become accepted API configuration.
    re.compile(pattern, flags)
    translated = regex.VERSION0
    for python_flag, engine_flag in (
        (re.ASCII, regex.ASCII), (re.IGNORECASE, regex.IGNORECASE),
        (re.MULTILINE, regex.MULTILINE), (re.DOTALL, regex.DOTALL),
        (re.VERBOSE, regex.VERBOSE), (re.UNICODE, regex.UNICODE),
    ):
        if flags & python_flag:
            translated |= engine_flag
    return regex.compile(pattern, translated)


@dataclass(frozen=True)
class UrlPattern:
    pattern: str
    flags: int
    field: str
    _compiled: object

    def search(self, value: str):
        with url_pattern_budget():
            batch = _batch_budget.get()
            remaining = min(SEARCH_SECONDS, _url_budget.get(), batch if batch is not None else float("inf"))
            if remaining <= 0:
                raise UrlPatternError(self.field, self.pattern, "pattern_timeout")
            started = _clock()
            try:
                match = self._compiled.search(value, timeout=remaining)
            except TimeoutError as exc:
                raise UrlPatternError(self.field, self.pattern, "pattern_timeout") from exc
            finally:
                elapsed = max(0.0, _clock() - started)
                _url_budget.set(_url_budget.get() - elapsed)
                if batch is not None:
                    _batch_budget.set(batch - elapsed)
            # Even a successful match must not turn an expired budget into success.
            if elapsed > remaining:
                raise UrlPatternError(self.field, self.pattern, "pattern_timeout")
            return match


def compile_patterns(patterns, field: str = "url_pattern") -> list[UrlPattern]:
    out = []
    for index, raw in enumerate(patterns):
        if isinstance(raw, UrlPattern):
            out.append(raw)
            continue
        pattern = raw.pattern if isinstance(raw, re.Pattern) else raw
        flags = raw.flags if isinstance(raw, re.Pattern) else 0
        location = f"{field}[{index}]"
        if not isinstance(pattern, str):
            raise UrlPatternError(location, str(pattern), "pattern_invalid")
        try:
            compiled = _compile(pattern, flags)
        except (re.error, regex.error, TypeError, ValueError, OverflowError, RecursionError) as exc:
            raise UrlPatternError(location, str(pattern), "pattern_invalid") from exc
        out.append(UrlPattern(pattern, flags, location, compiled))
    return out


def matches_any(patterns, value: str) -> bool:
    with url_pattern_budget():
        return any(pattern.search(value) for pattern in compile_patterns(patterns))
