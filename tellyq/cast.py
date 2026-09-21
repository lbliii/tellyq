"""Cast transport. Commands run on the caller; receiver events use a thread-safe queue."""

from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from queue import Empty, Queue
from threading import Event
from time import monotonic
from typing import Protocol
from uuid import UUID

import pychromecast
from pychromecast.controllers import BaseController
from pychromecast.controllers.youtube import YouTubeController
from pychromecast.models import CastInfo
from zeroconf import Zeroconf

from .cast_messages import media_observation as media_observation
from .cast_messages import normalize_message
from .models import Device, Observation

MEDIA = "urn:x-cast:com.google.cast.media"
RECEIVER = "urn:x-cast:com.google.cast.receiver"
VIDEO_ID = "FozIp7Va7dY"


def now() -> str:
    return datetime.now(UTC).isoformat()


def device_dict(info: CastInfo) -> Device:
    return {
        "uuid": str(info.uuid),
        "name": info.friendly_name,
        "model": info.model_name,
        "type": info.cast_type,
    }


@contextmanager
def discovery(seconds: float = 12) -> Iterator[tuple[pychromecast.CastBrowser, Zeroconf]]:
    zconf = Zeroconf()
    browser = pychromecast.CastBrowser(pychromecast.SimpleCastListener(lambda *_: None), zconf)
    try:
        browser.start_discovery()
        Event().wait(seconds)
        yield browser, zconf
    finally:
        browser.stop_discovery()
        zconf.close()


class HasUUID(Protocol):
    @property
    def uuid(self) -> UUID: ...


def select_device[T: HasUUID](devices: Iterable[T], device_id: str) -> T:
    """An exact UUID is required even when only one receiver is discovered."""
    wanted = UUID(device_id)
    matches = [device for device in devices if device.uuid == wanted]
    if len(matches) != 1:
        raise ValueError("The selected device was not uniquely discovered; run discover again.")
    return matches[0]


class Observer(BaseController):
    """Passive listener alongside PyChromecast's standard handlers."""

    def __init__(self, namespace: str, events: Queue[Observation]) -> None:
        super().__init__(namespace, target_platform=namespace == RECEIVER)
        self.events = events

    def receive_message(self, _message: object, _data: object) -> bool:
        value = normalize_message(_data)
        if value is None:
            return False
        # The parser creates a new record containing only immutable scalars;
        # later mutation of a library-owned message cannot alter queued evidence.
        self.events.put({"observed_at": now(), "monotonic": monotonic(), **value})
        return True


class Connection:
    def __init__(self, info: CastInfo, zconf: Zeroconf) -> None:
        self.events: Queue[Observation] = Queue()
        self.cast = pychromecast.get_chromecast_from_cast_info(
            info, zconf, tries=2, retry_wait=1, timeout=8
        )
        self.cast.register_handler(Observer(MEDIA, self.events))
        self.cast.register_handler(Observer(RECEIVER, self.events))
        self.youtube = YouTubeController(timeout=10)
        self.cast.register_handler(self.youtube)

    def drain(self) -> list[Observation]:
        result: list[Observation] = []
        while True:
            try:
                result.append(self.events.get_nowait())
            except Empty:
                return result

    def observe(self, seconds: float) -> list[Observation]:
        end = monotonic() + seconds
        while monotonic() < end:
            self.cast.socket_client.receiver_controller.update_status()
            if self.cast.media_controller.is_active:
                self.cast.media_controller.update_status()
            Event().wait(min(2, max(0, end - monotonic())))
        return self.drain()


@contextmanager
def connect(device_id: str, discovery_seconds: float = 12) -> Iterator[tuple[Connection, Device]]:
    with discovery(discovery_seconds) as (browser, zconf):
        info = select_device(list(browser.devices.values()), device_id)
        connection = Connection(info, zconf)
        try:
            connection.cast.wait(timeout=15)
            yield connection, device_dict(info)
        finally:
            if connection.cast.socket_client.is_alive():
                connection.cast.disconnect(timeout=10)
