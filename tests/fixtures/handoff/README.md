# Sanitized historical handoff excerpts

These three JSONL fixtures derive from the private M2a completion checkpoint
journals on 2026-09-21, captured on reviewed candidate `2599345`. They are sanitized
observations from past live runs, not new hardware evidence, full-run captures,
raw Cast frames, or handoff acceptance. The source journals and their private
command chronology remain under ignored runtime state.

The existing checkpoint projection selected the begin record and every raw
observation window in source order, preserving actual window return times and all
observations, then passed them through `CaptureSanitizer`. The excerpts here pass
that projection through a fresh sanitizer and retain the begin record plus four
complete windows before the first terminal, its complete window, and four after:

| File | Retained projection record sequences |
| --- | --- |
| `run-a1.jsonl` | 0, 65–73 |
| `run-b.jsonl` | 0, 39–47 |
| `run-a2.jsonl` | 0, 55–63 |

No event inside a selected window is omitted or resequenced. Times, gaps, states,
positions, anonymous identity, wire shapes, unknown ad flags and finite numeric
provider codes retain their recorded values and relationships. Earlier launch,
pause/resume/control activity and later cleanup are omitted; no end record is
fabricated. `entire_run_observed` remains false and
`external_control_unobserved` remains true. Saved evidence conclusions are absent.

An ephemeral HMAC key per fixture pseudonymizes device, content, app-session and
media-session identities consistently within that fixture. Keys and mappings are
not committed. The public YouTube application ID `233637DE` remains because it is
required by the source-qualified provider contract. Wall clocks move to a 2000
baseline; monotonic clocks are relative to the original begin record. Names,
titles, URLs, credentials, arbitrary custom data and original identifiers are
absent. Re-exporting changes pseudonyms without changing equality relationships.

Offline tests start a read-only Cast adapter and lifecycle tracker at the excerpt
boundary. Four pre-terminal windows establish fresh scoped identity and progress
locally; this does not assert continuous observation of omitted history. Replay
recomputes the ending and release diagnostic rather than trusting saved flags.
A1 and B retain completion then veto the position rewind under same identity;
A2 retains completion and vetoes different content before later ad context. The
numeric code 5 stays unknown. Synthetic guard and queue tests live separately;
the committed observed fixtures do not add inactive-ad flags or alter identities.
