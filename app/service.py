"""The service container: settings, storage, journals, workers."""

from __future__ import annotations

import logging
from pathlib import Path

from app.core.db import Database
from app.core.secrets import SecretStore
from app.events.delivery import DeliveryWorker
from app.events.journal import EventJournal
from app.extraction.samples_store import SampleStore
from app.extraction.store import MappingStore
from app.jobs.manager import JobManager
from app.settings import Settings

log = logging.getLogger("wss")


class Service:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        data = Path(settings.data_dir)
        data.mkdir(parents=True, exist_ok=True)
        self.db = Database(data / "service.sqlite3")
        self.secrets = SecretStore(self.db, settings.secrets_key)
        self.mappings = MappingStore(self.db)
        self.samples = SampleStore(self.db, data, settings.samples_hours)
        self.delivery = DeliveryWorker(self)
        self.events = EventJournal(self.db, settings.claim_timeout_seconds, on_new=self.delivery.wake)
        self.jobs = JobManager(self)
        self.ready = False
        self.shutting_down = False

    async def start(self) -> None:
        await self.delivery.start()
        await self.jobs.start()
        self.ready = True

    async def shutdown(self) -> None:
        self.shutting_down = True
        await self.jobs.shutdown()
        await self.delivery.shutdown()
        self.db.close()


_service: Service | None = None


def get_service() -> Service:
    if _service is None:
        raise RuntimeError("service not started")
    return _service


def set_service(service: Service | None) -> None:
    global _service
    _service = service
