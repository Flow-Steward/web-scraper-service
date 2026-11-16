"""Units: crawl scope, images, attributes, run sentences."""

from __future__ import annotations

from app.core.schemas import validator


# ---------------------------------------------------------------- crawl scope
def test_section_pattern_and_scope():
    import re

    from app.core.scope import crawl_patterns, in_scope, section_pattern

    pat = re.compile(section_pattern("https://www.s.lt/lt/lamps-c1331"))
    for url in ("https://www.s.lt/lt/lamps-c1331", "https://s.lt/lt/lamps-c1331/sub-c1", "https://www.s.lt/lt/lamps-c1331?page=2"):
        assert pat.search(url), url
    for url in ("https://www.s.lt/lt/tools-c9", "https://www.s.lt/lt/lamps-c13310", "https://other.lt/lt/lamps-c1331", "https://www.s.lt/x-p1"):
        assert not pat.search(url), url
    assert section_pattern("https://s.lt/") is None and section_pattern(None) is None
    assert section_pattern("http://scope.test:8602/lt/lamps-c100").startswith(r"^https?://(www\.)?scope\.test:8602/lt/lamps\-c100")
    pats = crawl_patterns({"site_url": "https://s.lt/lt/a-c1", "stay_in_section": True, "crawl_include": ["/outlet/", "("]})
    assert len(pats) == 2 and in_scope(pats, "https://s.lt/outlet/x") and in_scope(pats, "https://s.lt/lt/a-c1/b") and not in_scope(pats, "https://s.lt/lt/b-c2")
    assert in_scope([], "https://anything.example/")
    ok = {"crawl_include": ["/lt/lamps-c1331"], "stay_in_section": True}
    assert not list(validator("job_config.schema.json#/$defs/job_settings").iter_errors(ok))
    assert list(validator("job_config.schema.json#/$defs/job_settings").iter_errors({"stay_in_section": "yes"}))


# ---------------------------------------------------------------- images
def test_image_transforms_and_gallery():
    from app.extraction.mapping import apply_mapping
    from app.extraction.page import Page
    from app.extraction.transforms import regex_replace, srcset_largest

    assert srcset_largest("a-300.jpg 300w, a-1200.jpg 1200w, a-800.jpg 800w") == "a-1200.jpg"
    assert srcset_largest("a.jpg, a@2x.jpg 2x") == "a@2x.jpg" and srcset_largest("") is None
    assert regex_replace("/up/lamp-300x300.jpg", r"-\d+x\d+(?=\.\w+$)", "") == "/up/lamp.jpg"
    assert regex_replace("x", "(", "") is None
    html = ("<div class='gallery'>"
            "<a href='/up/a.jpg' data-large_image='/up/a-full.jpg'><img src='/up/a-150x150.jpg' srcset='/up/a-300.jpg 300w, /up/a-1024.jpg 1024w'></a>"
            "<a href='/up/b.jpg' data-large_image='/up/b-full.jpg'><img src='/up/b-150x150.jpg'></a>"
            "<a href='/up/a.jpg' data-large_image='/up/a-full.jpg'><img src='/up/a-150x150.jpg'></a></div>")
    page = Page(f"<html><body>{html}</body></html>", "https://s.example/p/1")
    mapping = {"schema_version": 1, "template_id": "p", "match": {"url_patterns": ["/p/"]}, "fields": [
        {"name": "images", "type": "image_url", "multiple": True, "sources": [
            {"kind": "css", "selector": ".gallery a", "extract": "attribute", "attribute": "data-large_image", "transforms": [{"op": "absolute_url"}]}]},
        {"name": "images_text", "type": "text", "sources": [
            {"kind": "css", "selector": ".gallery a", "extract": "attribute", "attribute": "href", "transforms": [{"op": "absolute_url"}, {"op": "join", "separator": " | "}]}]},
        {"name": "big", "type": "image_url", "sources": [
            {"kind": "css", "selector": ".gallery img", "extract": "attribute", "attribute": "srcset", "transforms": [{"op": "srcset_largest"}, {"op": "absolute_url"}]}]},
        {"name": "thumb_full", "type": "image_url", "multiple": True, "sources": [
            {"kind": "css", "selector": ".gallery img", "extract": "attribute", "attribute": "src",
             "transforms": [{"op": "regex_replace", "pattern": "-\\d+x\\d+(?=\\.\\w+$)", "replacement": ""}, {"op": "absolute_url"}]}]}]}
    assert not list(validator("mapping.schema.json").iter_errors(mapping))
    data = apply_mapping(mapping, page).data
    assert data["images"] == ["https://s.example/up/a-full.jpg", "https://s.example/up/b-full.jpg"]  # duplicates out
    assert data["images_text"] == "https://s.example/up/a.jpg | https://s.example/up/b.jpg"
    assert data["big"] == "https://s.example/up/a-1024.jpg"
    assert data["thumb_full"] == ["https://s.example/up/a.jpg", "https://s.example/up/b.jpg"]
    assert list(validator("mapping.schema.json#/$defs/transform").iter_errors({"op": "regex_replace"}))  # pattern required


# ---------------------------------------------------------------- attributes
def test_spec_table_from_rows_and_from_additional_property():
    import json

    from app.extraction.mapping import apply_mapping
    from app.extraction.page import Page

    ld = {"@context": "https://schema.org", "@type": "Product", "name": "Wine",
          "additionalProperty": [{"@type": "PropertyValue", "name": "Vintage", "value": 2019}, {"@type": "PropertyValue", "name": "Bottle", "value": "0,75 l"}]}
    html = (f"<html><head><script type='application/ld+json'>{json.dumps(ld)}</script></head><body>"
            "<table class='specs'><tr><th>Vintage:</th><td>2019</td></tr><tr><th>Type</th><td> Red  wine </td></tr>"
            "<tr><th>Type</th><td>ignored</td></tr><tr><th></th><td>no name</td></tr></table></body></html>")
    page = Page(html, "https://s.example/wine/1")
    mapping = {"schema_version": 1, "template_id": "wine", "match": {"url_patterns": ["/wine/"]}, "fields": [
        {"name": "specs", "type": "spec_table", "sources": [{"kind": "spec_table", "row_selector": "table.specs tr", "key_selector": "th", "value_selector": "td"}]},
        {"name": "props", "type": "spec_table", "sources": [{"kind": "jsonld", "schema_type": "Product", "path": "additionalProperty"}]}]}
    assert not list(validator("mapping.schema.json").iter_errors(mapping))
    data = apply_mapping(mapping, page).data
    assert data["specs"] == {"Vintage": "2019", "Type": "Red wine"} and data["props"] == {"Vintage": "2019", "Bottle": "0,75 l"}
    assert apply_mapping(mapping, page, "selectors").data["specs"] == data["specs"]
    record = {"record_id": "rec_" + "1" * 32, "source_url": page.url, "site": "s.example", "template_id": "wine", "mapping_version": 1, "item_index": 0,
              "extracted_at": "2026-11-14T10:00:00Z", "data": data}
    assert not list(validator("record.schema.json").iter_errors(record))
    assert list(validator("mapping.schema.json#/$defs/source").iter_errors({"kind": "spec_table", "row_selector": "tr"}))
    ok = {"ai_choose_fields": True, "fields_hint": "wine: vintage, type, bottle size, taste"}
    assert not list(validator("job_config.schema.json#/$defs/job_settings").iter_errors(ok))


# ---------------------------------------------------------------- sentences
def test_explanation_sentences():
    from app.jobs.report import explanation

    cur = {"sentence": "found 10 target pages, collected 7, 3 not collected: 3 blocked", "not_collected": 3,
           "not_collected_by_reason": {"blocked": 3}, "progress": {"text": "7 of 10 (estimate)"}}
    assert explanation({"state": "running"}, cur, [])["sentence"] == "Running: 7 of 10 (estimate) target pages collected so far."
    done = explanation({"state": "completed", "stop_reason": "finished"}, cur, [])
    assert done["sentence"] == "Finished; found 10 target pages, collected 7, 3 not collected: 3 blocked." and "proxy tier" in done["advice"]
    blocked = explanation({"state": "completed", "stop_reason": "blocked"}, cur, [{"message": "90% of the last 20 pages were blocked"}])
    assert blocked["sentence"].startswith("Stopped: the site blocked the scraper;") and blocked["sentence"].endswith("90% of the last 20 pages were blocked")
