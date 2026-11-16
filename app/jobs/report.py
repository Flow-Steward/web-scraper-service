"""What a run did, in plain words.

* ``completeness``: target pages found, collected (pages and records), not collected by reason,
  progress "M of N" while running (N an estimate until discovery has finished), a sentence.
* ``comparison``: the previous comparable run of the same site (completed, no ``max_records``
  trial), its numbers, the change, and a warning when records fell by more than the threshold —
  worded so the operator can tell "the site has fewer products" (fewer target pages found) from
  "the scraper missed them" (found but not collected).
* ``explanation``: why the run stopped (or how it is going) and what to do, one or two sentences.
"""

from __future__ import annotations

from typing import Any

REASON_WORDS = {
    "fetch_failed": "failed to load", "not_found": "not found (404/410)", "blocked": "blocked", "incomplete": "missing required fields, or variants or a price the mapping does not read",
    "no_template": "no mapping yet", "template_failed": "mapping could not be repaired", "stopped": "not fetched before the stop",
    "robots_disallowed": "refused by robots.txt", "private_address_refused": "private address",
}
STOP_WORDS = {
    "finished": "finished", "max_records": "stopped at the record limit (max_records) as asked", "max_pages": "stopped at the page limit (max_pages)",
    "max_duration": "stopped at the time limit (max_duration)", "max_result_size": "stopped: the result reached its size limit",
    "stopped": "stopped by the operator", "blocked": "stopped: the site blocked the scraper",
    "site_unavailable": "stopped: the site was unavailable (errors or timeouts in a row after pauses)",
    "stalled": "stopped: no progress for too long (stalled; see the timeline for what was stuck)",
}
ADVICE = {
    "blocked": "Try a higher proxy tier, allow the click, or set Fetch mode = browser; check the protection trace.",
    "site_unavailable": "Try again later; if it persists, check the site in a browser and the proxy tier.",
    "max_duration": "Raise max_duration or narrow the scope (Where to walk, Only these URLs) and run again.",
    "max_result_size": "Narrow the fields or the scope, or raise the result size limit.",
    "max_pages": "Raise max_pages or narrow the scope.",
    "stalled": "Look at the timeline for the stuck pages; try again, try another fetch mode or browser, or collect the missed pages again.",
}


def _pages_collected(db, run_id: str) -> int:
    try:
        return int(db.scalar("SELECT COUNT(DISTINCT json_extract(line, '$.source_url')) FROM records WHERE run_id=?", (run_id,)) or 0)
    except Exception:  # SQLite without JSON1: one record per page
        return int(db.scalar("SELECT COUNT(*) FROM records WHERE run_id=?", (run_id,)) or 0)


def completeness(db, run_id: str, doc: dict) -> dict:
    by_reason: dict[str, int] = {}
    for row in db.all("SELECT reason, COUNT(*) AS n FROM unextracted WHERE run_id=? GROUP BY reason", (run_id,)):
        by_reason[row["reason"]] = int(row["n"])
    records = int(db.scalar("SELECT COUNT(*) FROM records WHERE run_id=?", (run_id,)) or 0)
    pages = _pages_collected(db, run_id)
    not_collected = sum(by_reason.values())
    found = pages + not_collected
    finished = doc.get("state") in ("completed", "failed")
    discovery_done = doc.get("progress", {}).get("discovery") in ("finished", "cut")
    resolved = (doc.get("resolved") or {}).get("discovery_mode")
    expected = found
    estimate = False
    if not finished:
        queued = int((doc.get("counters") or {}).get("urls_discovered", 0))
        if resolved in ("sitemap", "url_list"):
            expected = max(found, queued)
        else:
            expected = max(found, pages)
        estimate = not discovery_done or resolved not in ("sitemap", "url_list")
    reasons = ", ".join(f"{n} {REASON_WORDS.get(r, r.replace('_', ' '))}" for r, n in sorted(by_reason.items(), key=lambda kv: -kv[1]))
    sentence = (f"found {found} target page{'s' if found != 1 else ''}, collected {pages}"
                + (f" ({records} records)" if records != pages else "")
                + (f", {not_collected} not collected: {reasons}" if not_collected else ", none missing"))
    dups = int(((doc.get("counters") or {}).get("pages_by_class") or {}).get("duplicate", 0))
    if dups:
        sentence += f"; {dups} duplicate address{'es' if dups != 1 else ''} of pages already collected skipped"
    out: dict[str, Any] = {"target_pages_found": found, "pages_collected": pages, "records": records, "not_collected": not_collected,
                           "not_collected_by_reason": by_reason, "sentence": sentence}
    if not finished:
        out["progress"] = {"collected": pages, "expected": expected, "estimate": estimate,
                           "text": f"{pages} of {expected}" + (" (estimate)" if estimate else "")}
    return out


def comparison(db, run_id: str, doc: dict, current: dict, drop_share: float) -> dict | None:
    """The previous completed run of the same site that was not a ``max_records`` trial."""
    if (doc.get("config") or {}).get("max_records") or doc.get("recollect_of"):
        return None  # a trial, or a re-collect run of a few pages (README 4.10)
    from app.core.db import loads

    for row in db.all("SELECT run_id, doc FROM runs WHERE site=? AND state='completed' AND run_id != ? AND created_at < ? "
                      "AND parent_run_id IS NULL ORDER BY created_at DESC LIMIT 20", (doc["site"], run_id, doc["created_at"])):
        prev = loads(row["doc"])
        if (prev.get("config") or {}).get("max_records") or not prev.get("completeness"):
            continue
        p = prev["completeness"]
        change = {k: current[k] - p.get(k, 0) for k in ("target_pages_found", "pages_collected", "records", "not_collected")}
        out: dict[str, Any] = {"run_id": row["run_id"], "finished_at": prev.get("finished_at"),
                               "target_pages_found": p.get("target_pages_found", 0), "pages_collected": p.get("pages_collected", 0),
                               "records": p.get("records", 0), "not_collected": p.get("not_collected", 0), "change": change}
        prev_records = p.get("records", 0)
        if prev_records and current["records"] < prev_records * (1 - drop_share):
            fell = prev_records - current["records"]
            found_fell = p.get("target_pages_found", 0) - current["target_pages_found"]
            if found_fell >= fell * 0.8 and current["not_collected"] <= p.get("not_collected", 0):
                why = "fewer target pages were found: the site likely has fewer products (or the scope/discovery changed)"
            else:
                why = (f"target pages were found but not collected ({current['not_collected']} not collected, "
                       f"was {p.get('not_collected', 0)}): the scraper missed them — see the unextracted list")
            out["warning"] = (f"records fell from {prev_records} to {current['records']} (more than {int(drop_share * 100)}%) "
                              f"compared with run {row['run_id']}; {why}")
        return out
    return None


def explanation(doc: dict, current: dict, problems: list[dict]) -> dict:
    state = doc.get("state")
    if state not in ("completed", "failed"):
        prog = current.get("progress", {}).get("text")
        return {"sentence": f"Running: {prog} target pages collected so far." if prog else "Running."}
    stop = doc.get("stop_reason") or "finished"
    head = STOP_WORDS.get(stop, stop.replace("_", " "))
    sentence = head[0].upper() + head[1:] + "; " + current["sentence"] + "."
    advice = ADVICE.get(stop)
    if not advice and current["not_collected"]:
        top = max(current["not_collected_by_reason"].items(), key=lambda kv: kv[1])[0]
        advice = {
            "blocked": ADVICE["blocked"], "fetch_failed": "Run again for the failed pages (Only these URLs) or later; see the unextracted list.",
            "incomplete": "The mapping misses required fields, variants or the price on some pages: let the repair fix it, or rebuild the mapping.",
            "no_template": "Some target pages have no mapping yet: let the repair build one.",
            "template_failed": "The mapping could not be repaired: rebuild it from new samples.",
        }.get(top)
    last_problem = problems[-1]["message"] if problems else None
    if stop in ("blocked", "site_unavailable") and last_problem:
        sentence += f" {last_problem[:300]}"
    out = {"sentence": sentence[:1000]}
    if advice:
        out["advice"] = advice
    return out
