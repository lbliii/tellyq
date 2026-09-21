"""Additive JSON projection of historical completion, separate from fresh identity."""

from .domain.values import SessionSnapshot
from .models import CompletionFields


def completion_report(snapshot: SessionSnapshot | None) -> CompletionFields:
    completion = snapshot.completion if snapshot is not None else None
    if completion is None:
        return {}
    return {
        "completion": {
            "historical": True,
            "attribution": completion.attribution.value,
            "source": completion.source,
            "sequence": completion.sequence,
            "identity_sequence": completion.identity_sequence,
            "observed_at": completion.observed_at.isoformat(),
        }
    }
