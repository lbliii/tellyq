# M1 acceptance record

Updated 2026-09-21. **M1 passed at `71a3c39f8d6df5f135ae56b4cdaf69c4a3fecc69`.**
Its foundations and second-wave code are merged. Actual `main` passed offline,
artifact, macOS/Linux CI and the fresh bounded hardware regression. Unknown ad
state remains explicit; M1 does not establish natural endings or queue advancement.
M0 is the original successful one-program playback proof; M1 makes that flow a
maintainable, explicitly verified core.

## Recorded baseline

| Checkpoint | Evidence | Meaning |
| --- | --- | --- |
| M0, 2026-09-21 | Exact Bob Ross `FozIp7Va7dY` and advancing receiver position; user confirmed visible playback and automatic TV switching; original stop observed YouTube exit | Historical proof of the playback connection; not a current TV-state or natural-ending claim |
| Foundation PRs | [Domain #7](https://github.com/lbliii/tellyq/pull/7), [storage #6](https://github.com/lbliii/tellyq/pull/6), [Cast normalization #5](https://github.com/lbliii/tellyq/pull/5), plus [plan #4](https://github.com/lbliii/tellyq/pull/4), all merged | Independently reviewed foundation components; subsequent integration is recorded below |
| Merged `main` at `a2b72a9` | Python 3.14.0, standard GIL; 251 tests; 91.2% branch-inclusive coverage; Ruff, formatting, ty 0.0.82, source/wheel build and isolated install passed | Historical automated foundation checkpoint; not acceptance of the later integration |
| Live regression on `a2b72a9` | Exact requested content and advancing positions; user confirmed Bob Ross visible; two fresh status reads did not restart playback | Start/status behavior passed the bounded regression |
| Stop in the same regression | `quit_app` returned; user confirmed return to Chromecast home screen; parser rejected idle replies without `applications`; report `unconfirmed`, saved queue `playing` | Visible stop passed. Machine verification and persisted stop state failed at this checkpoint |

Detailed reports, device identifiers, addresses and session data stay in ignored
`runtime/`. The table records sanitized outcomes only. Counts and coverage are tied
to the stated checkpoint; they are not minimum targets or reliability statistics.

## Second-wave review evidence

| Candidate | Independent evidence | Integration status |
| --- | --- | --- |
| [Stop evidence PR #8](https://github.com/lbliii/tellyq/pull/8), `a0f6219` | Python 3.14.0; `poe ci` passed 301 tests with 93.0% branch-inclusive coverage, lint/format/types, source/wheel build and isolated installation; [macOS/Linux CI passed](https://github.com/lbliii/tellyq/actions/runs/35619797156) | Merged; automated/platform and live regression passed at `71a3c39` |
| [Application PR #9](https://github.com/lbliii/tellyq/pull/9), `92bea46` | Python 3.14.0; `poe ci` passed 301 tests with 90.6% branch-inclusive coverage, lint/format/types, source/wheel build and isolated installation; [macOS/Linux CI passed](https://github.com/lbliii/tellyq/actions/runs/35620484798) | Merged; automated/platform and live regression passed at `71a3c39` |

PR #8 requires a current, timely, single-use receiver-status request match for
app-absence events, including an explicit empty application list. Omitted
`applications` also needs the validated full idle shape described in its
[design note](https://github.com/lbliii/tellyq/blob/a0f6219100239e6ace111ff1868fe8750efc7b00/docs/m1/stop-evidence.md).
Its fixtures are synthetic, based on the reported shape and public implementation.
This repairs a justified parser/transport case; it is not a new hardware result.

The [application design at `92bea46`](https://github.com/lbliii/tellyq/blob/92bea466a281007ab81b0d3e6b17e318993090ea/docs/m1/application.md)
documents report schema 1, unchanged legacy queue/session records, new snapshot
recovery and the stricter unknown-ad outcome. Scoped review addressed content-kind
round trips, revisions captured before observation, injected-store ownership,
pre-dispatch timestamps, receipts surviving post-command failures, and public
error redaction.

The coordinator combined exact commits `a0f6219` and `92bea46` without conflicts
in an isolated checkout based on `a2b72a9`. `uv run --locked poe ci` passed:
**351 tests**, **91.8% branch-inclusive coverage**, Ruff, formatting, ty, source
archive, wheel and isolated install/CLI smoke. No hardware was contacted. The
combined result closed the local offline integration check before merge; it did
not establish merged-main or live results by itself. Both code PRs subsequently passed their
macOS and Ubuntu CI jobs at the exact heads listed above. The coordinator also
installed the combined wheel in a fresh Python 3.14 environment with no
PyChromecast, Zeroconf, Milo or Chirp dependencies. Domain, application, controller,
snapshot store and Cast adapter imports passed with a socket audit and no runtime
side effects.

## Merged-main checkpoint

PR #8 merged at `110cf52`, then PR #9 merged at
`71a3c39f8d6df5f135ae56b4cdaf69c4a3fecc69`. The coordinator fast-forwarded the clean
local `main` checkout to that exact commit and reran `uv run --locked poe ci`:
**351 tests**, **91.8% branch-inclusive coverage**, Ruff, formatting, ty, source
archive, wheel and isolated install/CLI smoke all passed. The merged-main
[CI run](https://github.com/lbliii/tellyq/actions/runs/35620756804) completed
successfully for both Python 3.14 macOS and Ubuntu jobs. No hardware was contacted.

## Live regression on merged `main`

The coordinator ran the explicitly requested bounded regression on `71a3c39`.
The intended Projector receiver was idle before the test. A fresh user confirmation
established visible Bob Ross playback. Receiver messages supplied the exact
`FozIp7Va7dY` content/title and PLAYING state. Last reported positions were 23.434
seconds after start, 57.259 on the first status and 96.695 on the second status,
within the same app/media session. Both status reports had `commands: []`; no
restart occurred.

Ad state remained unknown. The expected honest result was `state: unconfirmed`,
`observed_state: playing`, and `evidence.reason: ad_unknown`. Per-status progress
flags were false because the short individual windows lacked a qualifying pair;
the fresh positions advanced across the separate reads. These observations and
user confirmation do not manufacture inactive-ad evidence or strict receiver
playback proof.

Stop returned `state: stopped`, `observed_state: stopped`, `stop_confirmed: true`,
`evidence.reason: stop_observed`, and `stop_verification_source: receiver_app_exit`.
The queue persisted `stopped`. A new snapshot-store instance recovered stop intent
with unknown current state, correctly avoiding resurrection of old playback
observations. The user freshly confirmed the visible stop: “Yes, Chromecast home
screen.” Machine stop, visible stop and persisted state therefore passed as
separate checks. M1 is accepted at the tested commit; the stricter unknown-ad
classification remains a documented limitation.

## Gates for integrated M1

Record the code commit(s), exact commands, outcome and any failures for each gate.
A result from an earlier foundation commit cannot close an integration gate.

| Gate | Required evidence | Status |
| --- | --- | --- |
| Application boundaries | One controller flow accepts backend, store and clock implementations. An offline fake and the production Cast adapter both implement the same ports. The default example program comes from the extracted program configuration, not domain policy | Merged-main offline pass: `71a3c39` |
| Shared backend contract | The same start/status/stop and receipt/evidence cases run against a fake backend and Cast with mocked transport. Additional replay covers detached observations, replacement refusal and bounded failure/timeout | Merged-main offline pass: `71a3c39` |
| Fresh evidence | Policy and application replays cover partial, stale, duplicate, reordered, foreign-target, old-generation and replaced-session messages. They cannot establish fresh progress or completion; a later delayed message cannot reclaim ownership after takeover | Merged-main offline pass: `71a3c39` |
| Ad and completion boundary | Missing ad data remains unknown. Document any separately named identity/progress observations without calling them ad-free playback or natural completion. Receipt, elapsed runtime, duration, app exit and manual stop never imply a natural ending | Merged-main offline pass: `71a3c39` |
| Stop evidence and persistence | Correlated, fresh receiver-app exit can verify stop without inventing a content-level `IDLE/CANCELED` event. Replays cover the observed missing-`applications` idle shape, malformed/partial replies, stale request correlation and replacement sessions. Uncertain stop records uncertainty instead of retaining a misleading `playing` state | Merged-main offline pass: `71a3c39` |
| CLI and wire compatibility | `discover`, `queue`, `start`, `status`, `stop` and `probe` keep their vocabulary. Reports have a documented schema version; command receipts, receiver evidence, errors and visual confirmation remain separate. Success, refusal and exception output obey the same versioned boundary | Merged-main offline pass: `71a3c39` |
| Legacy state and recovery | Existing version-1 queues and supported legacy session records read safely; migration/version behavior is documented and tested. Future/malformed records fail before device I/O. Restarted processes establish a fresh scope rather than reusing persisted monotonic times as fresh observations | Merged-main offline pass: `71a3c39` |
| Import and ownership isolation | Domain, application and storage import without Cast/Milo/Chirp and without sockets or runtime writes. Callback mutation cannot alter returned snapshots; only the adapter owns the device client | Source and dependency-absent installed-wheel isolation passed |
| Validation and artifacts | Exact integrated tree passes `uv sync --locked` and `uv run --locked poe ci`: lint, formatting, types, offline coverage suite, source/wheel build and isolated install/CLI smoke outside the checkout. No hardware access in tests | Merged-main offline pass: `71a3c39` |
| Platform CI | Python 3.14 jobs pass on macOS and Linux for final code PR heads; repeat against merged `main` before accepting it. Record job/run URLs, not only a local macOS pass | PR #8, PR #9 and merged `main` at `71a3c39` passed macOS/Linux |
| Hardware regression | An explicitly requested, bounded start/status/status/stop run on the intended receiver satisfies the separate checklist below. Retain all failures and record unknowns | Passed at `71a3c39`: fresh visible start/stop, status without restart, receiver-app exit and persisted stopped state |
| Documentation and final decision | README, architecture, roadmap, changelog and this ledger agree with implemented behavior, wire versions, limits and evidence. Record the accepted `main` commit, or name the still-open gates | Accepted at `71a3c39`; publication of this record is documentation PR #10 |

The code gates above passed on the combined candidate and were repeated on actual
merged `main` at `71a3c39`. The separate live regression above closed the hardware
gate. Automated results alone did not establish visible playback or real-receiver
stop verification.

The shared contract suite tests the Cast adapter with mocked transport. Synthetic
fixtures are labeled as such. Neither is a new real-device capture or proof of
natural completion. A protocol-compatible test double alone does not establish
that application operations actually use the production adapter correctly.

## Repeatable bounded hardware regression

The completed regression followed this checklist. Repeating it requires an
explicitly requested hardware task; it is not a CI job or a command to execute
during documentation/repository setup. Keep local reports
private and record the exact checked-out commit and dependency/interpreter versions.

1. Confirm the intended receiver by its stable saved identity and friendly name;
   verify Chromecast HDMI is connected. Read existing local queue/session state
   before changing it. Avoid interrupting an unrelated active session.
2. Queue the one existing Bob Ross example and issue one `start`. Preserve the
   command receipt separately from fresh receiver identity, state and position.
   Obtain a new user confirmation that Bob Ross is visible. Old confirmation
   cannot prove the current screen, power state or input.
3. Take two bounded `status` reads without another start/resume. Check ownership,
   fresh timestamps and advancing positions in the same media session where
   supplied. If ad state remains unknown, keep the stricter playback-proof result
   unconfirmed and record the visible/identity/progress evidence separately.
4. Issue one `stop` for the still-owned session. A Chromecast home screen is an
   expected visible result; a frozen YouTube frame would be pause, not app exit.
   Obtain separate user confirmation and machine evidence of the receiver-app
   exit. If ownership changed, refuse to stop the replacement.
5. Compare the stop report with persisted queue state: `stopped` requires valid
   evidence; otherwise persist explicit uncertainty. A later status may have
   unknown current media while the queue retains the historical stopped outcome.
   Confirm no retry relaunched content and no event marked it naturally
   `finished`. Record failure if evidence is inadequate; do not loop playback
   attempts to turn an uncertain outcome into a pass.

Use the existing bounded observation windows. This regression ends after stop;
it does not wait out an episode. An accepted stop command plus a user's home-screen
confirmation cannot by itself close the machine-verification gate.

## Scope after M1

M1 does not claim natural endings, automatic advancement, a persistent daemon,
subscription-service support, standalone TV power control, MCP, a UI or verified
free-threaded Python. Standard Python 3.14 threading remains the supported runtime.
The next milestone is **M2a**: three natural endings across at least two YouTube
titles, including a pause/resume run. M2b adds unattended queue advancement only
after those lifecycle signals are understood. See the [roadmap](ROADMAP.md).
