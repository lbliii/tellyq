"""Finite interpretation of the reviewed YouTube Cast player-state field.

Source: YouTube remote.js build 4fd832e7, SHA-256
08e620a0bd236bf5c0f6e72d36c122fa4340d29a678a91f6889ca1fb5b7f8d43.
This is an implementation contract, not a public stable YouTube API enum.
"""

from .domain.values import ContentPhase, ProviderEvidence
from .models import Observation

YOUTUBE_APPLICATION_ID = "233637DE"  # PyChromecast 14.0.10 config.APP_YOUTUBE.
YOUTUBE_STATE_SOURCE = "youtube.remote.4fd832e7:status.customData.playerState"


def interpret_player_state(event: Observation, *, application_id: str | None) -> ProviderEvidence:
    """Require independently fresh receiver identity and consistent standard telemetry.

    The caller must supply only freshly correlated receiver application identity.
    Positive Cast break evidence remains authoritative in the adapter regardless
    of this decoder. Every ad-family code denotes ad context, including ad endings.
    Missing break metadata alone never supplies inactive-ad evidence.
    """
    phase = ContentPhase.UNKNOWN
    if (
        application_id != YOUTUBE_APPLICATION_ID
        or type(event.get("wire_status_count")) is not int
        or event.get("wire_status_count") != 1
        or event.get("custom_player_state_shape") != "valid"
    ):
        return ProviderEvidence(YOUTUBE_STATE_SOURCE, phase)
    code = event.get("custom_player_state")
    if isinstance(code, bool) or not isinstance(code, int):
        return ProviderEvidence(YOUTUBE_STATE_SOURCE, phase)
    if code in range(1080, 1086):
        return ProviderEvidence(YOUTUBE_STATE_SOURCE, ContentPhase.AD)
    expected = {
        0: ("IDLE", ContentPhase.FINISHED),
        1: ("PLAYING", ContentPhase.PLAYING),
        2: ("PAUSED", ContentPhase.PAUSED),
        3: ("BUFFERING", ContentPhase.BUFFERING),
    }.get(code)
    if (
        expected is not None
        and event.get("player_state") == expected[0]
        and event.get("idle_reason") == ("FINISHED" if code == 0 else None)
        and event.get("wire_break_status") == "absent"
        and event.get("ad_break") is not True
    ):
        phase = expected[1]
    return ProviderEvidence(YOUTUBE_STATE_SOURCE, phase)
