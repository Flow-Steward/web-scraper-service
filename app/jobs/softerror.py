"""Answers with status 200 that are not the page (contract README 4.16).

Servers under load answer 200 with an empty or truncated body, a "try again later" /
maintenance / database-error page, or a bare error template; the right page comes on the second
or third try. Such an answer is retried like a server error and, after the retries, the page is
``fetch_failed`` with "unusable answer: …" — never ``incomplete`` (which would count towards a
mapping repair) and never a listing whose links are silently lost.

The judgement per answer: an (almost) empty body; HTML cut off before ``</body></html>`` when
the pages of its URL shape normally end with them; error or maintenance wording in the title or
the first heading of a page that is small or has little text; a body far smaller than the usual
page of its URL shape; and (judged after extraction) a page of a URL shape that was always a
target page that now has none of its template's marks.
"""

from __future__ import annotations

import re
from collections import deque
from dataclasses import dataclass, field

ERROR_WORDS = re.compile(
    r"service (temporarily )?unavailable|temporarily unavailable|try again later|please try again|under maintenance|maintenance mode"
    r"|scheduled maintenance|we.ll be (right )?back|be back soon|database (connection )?error|error establishing a database connection"
    r"|internal server error|bad gateway|gateway time-?out|server (is )?(busy|overloaded)|too many (requests|connections)|\b(error|http) 50[0234]\b|\b50[234] (service|bad|gateway)"
    r"|something went wrong|an error (has )?occurred"
    r"|wartungsarbeiten|wartungsmodus|später (noch einmal|erneut)|vorübergehend nicht (verfügbar|erreichbar)|wir sind gleich zurück"
    r"|service indisponible|en maintenance|réessayer plus tard|réessayez plus tard"
    r"|mantenimiento|inténtelo (de nuevo )?más tarde|servicio no disponible|manutenzione|riprova più tardi|servizio non disponibile"
    r"|przerwa techniczna|spróbuj (ponownie )?później|technická odstávka|zkuste to později"
    r"|techniniai darbai|bandykite vėliau|paslauga laikinai neprieinama|tehniskie darbi|mēģiniet vēlāk|pakalpojums īslaicīgi nav pieejams"
    r"|tehnilised tööd|proovige hiljem|технические работы|попробуйте (позже|ещё раз позже)|сервис временно недоступен|ошибка базы данных"
    r"|технічні роботи|спробуйте пізніше",
    re.I)
TAG = re.compile(r"<[^>]+>")
SCRIPTS = re.compile(r"<(script|style|noscript)\b.*?</\1\s*>", re.I | re.S)
TITLE = re.compile(r"<title[^>]*>(.*?)</title", re.I | re.S)
H1 = re.compile(r"<h1[^>]*>(.*?)</h1", re.I | re.S)
REASONS = ("empty", "truncated", "error_page", "too_small", "lost_template")


@dataclass
class ShapeStats:
    """What normal pages of one URL shape look like: sizes, whether they end with the closing tags,
    whether they were target pages."""

    sizes: deque = field(default_factory=lambda: deque(maxlen=30))
    pages: int = 0
    closed: int = 0
    targets: int = 0
    others: int = 0

    def add(self, size: int, closed: bool, target: bool | None) -> None:
        self.pages += 1
        self.sizes.append(size)
        self.closed += 1 if closed else 0
        if target is True:
            self.targets += 1
        elif target is False:
            self.others += 1

    @property
    def median(self) -> float:
        s = sorted(self.sizes)
        return float(s[len(s) // 2]) if s else 0.0


def _text(fragment: str) -> str:
    return " ".join(TAG.sub(" ", fragment or "").split())


def is_closed(html: str) -> bool:
    tail = (html or "")[-4000:].lower()
    return "</html>" in tail or "</body>" in tail


def judge(html: str, stats: ShapeStats | None) -> tuple[str, str] | None:
    """(reason, plain words) when the answer is not usable, else None."""
    body = html or ""
    stripped = body.strip()
    head = stripped[:200000]
    visible = _text(SCRIPTS.sub(" ", head))
    if not visible or len(stripped) < 50:
        return "empty", f"an empty page ({len(stripped)} characters, no text)"
    known = stats is not None and stats.pages >= 3
    m_title, m_h1 = TITLE.search(head), H1.search(head)
    title = _text(m_title.group(1)) if m_title else ""
    h1 = _text(m_h1.group(1)) if m_h1 else ""
    if ERROR_WORDS.search(f"{title} {h1}"):
        small = known and stats.median and len(body) < 0.5 * stats.median
        if small or len(visible) < 1500:
            return "error_page", f"an error or maintenance page (\"{(h1 or title)[:100]}\")"
    if len(visible) < 20 and not re.search(r"<(body|div|p|main|h1|table|ul|article|section|a)\b", head[:50000], re.I):
        return "empty", f"a nearly empty page ({len(stripped)} characters, no page structure)"
    if not is_closed(body):
        if known and stats.closed >= 0.8 * stats.pages:
            return "truncated", f"the HTML is cut off ({len(body)} characters, no closing </body></html>; pages like it end with them)"
        if not known and not stripped.endswith(">"):
            return "truncated", f"the HTML is cut off ({len(body)} characters, it stops in the middle of the text)"
    if known and stats.pages >= 5 and stats.median >= 5000 and len(body) < 0.1 * stats.median:
        return "too_small", f"{len(body)} characters against a usual {int(stats.median)} for pages like it"
    return None


def lost_template(stats: ShapeStats | None, page_class: str) -> tuple[str, str] | None:
    """A URL shape that was always a target page (5 or more, none other) answered without its template's marks."""
    if stats is None or page_class not in ("not_target", "no_template"):
        return None
    if stats.targets >= 5 and stats.others == 0:
        return "lost_template", f"none of the template's marks on a kind of page that was a target page {stats.targets} times"
    return None
