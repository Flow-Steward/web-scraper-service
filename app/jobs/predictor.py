"""Our rendering-type predictor: no detection runs, browser per host when needed."""

from __future__ import annotations

from typing import TYPE_CHECKING

from crawlee.crawlers._adaptive_playwright._rendering_type_predictor import (
    RenderingTypePrediction,
    RenderingTypePredictor,
)

from app.core.urls import host_of

if TYPE_CHECKING:
    from app.jobs.runner import JobRunner


class JobPredictor(RenderingTypePredictor):
    """``detection_probability_recommendation`` is always 0, so a page is never fetched twice
    to learn; a host goes to the browser after a challenge or when the browser repeatedly
    extracted pages the HTTP fetch could not."""

    def __init__(self, runner: "JobRunner") -> None:
        super().__init__()
        self.runner = runner

    def predict(self, request) -> RenderingTypePrediction:
        mode = self.runner.config["fetch_mode"]
        if mode == "browser" and self.runner.browser:
            return RenderingTypePrediction("client only", 0)
        if mode == "http" or not self.runner.browser:
            return RenderingTypePrediction("static", 0)
        try:
            host = host_of(request.url)
        except ValueError:
            return RenderingTypePrediction("static", 0)
        if host in (self.runner.rt.get("browser_hosts") or []):
            return RenderingTypePrediction("client only", 0)
        static_ok, browser_wins = (self.runner.rt.get("render_stats") or {}).get(host, [0, 0])
        if browser_wins >= 2 and browser_wins > static_ok:
            return RenderingTypePrediction("client only", 0)
        return RenderingTypePrediction("static", 0)

    def store_result(self, request, rendering_type) -> None:  # no learning runs
        return None
