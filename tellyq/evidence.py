"""Conservative playback evidence; requested values never substitute for observations."""

from collections.abc import Iterable
from urllib.parse import parse_qs, urlparse

from .models import Evidence, Observation


def video_id(content_id: str | None) -> str | None:
    if not content_id:
        return None
    if "://" not in content_id:
        return content_id
    url = urlparse(content_id)
    if url.hostname in {"youtube.com", "www.youtube.com", "m.youtube.com"}:
        values = parse_qs(url.query).get("v")
        return values[0] if values else None
    if url.hostname == "youtu.be":
        return url.path.strip("/")
    return None


def playback_evidence(events: Iterable[Observation], requested_id: str) -> Evidence:
    last: tuple[int, float, float] | None = None
    identity = False
    playing = False
    advancing = False
    for event in events:
        if event["kind"] != "media":
            continue
        matches = video_id(event.get("content_id")) == requested_id
        identity |= matches
        eligible = matches and event.get("player_state") == "PLAYING" and not event.get("ad_break")
        playing |= eligible
        position = event.get("position")
        timestamp = event.get("monotonic")
        session_id = event.get("media_session_id")
        if (
            not eligible
            or not isinstance(position, (int, float))
            or timestamp is None
            or session_id is None
        ):
            last = None
            continue
        if last and last[0] == session_id and timestamp - last[1] >= 1 and position > last[2]:
            advancing = True
        last = (session_id, timestamp, position)
    return {
        "identity_observed": identity,
        "playing_observed": playing,
        "advancing_position_observed": advancing,
        "receiver_playback_confirmed": identity and playing and advancing,
        "visual_confirmation": None,
    }
