# M1 execution plan: maintainable playback core

Updated 2026-09-21. Foundations and second-wave code are merged into `main` at
`71a3c39`. This wave started from `a2b72a9`. The original YouTube playback proof is M0. M1 finishes the core
boundaries and stop verification before M2 adds continuous monitoring and queue
advancement. Its [acceptance record](M1-ACCEPTANCE.md) tracks separate source, CI,
artifact and hardware gates. **M1 passed at `71a3c39`**, including the fresh
bounded hardware regression; natural endings and advancement remain M2 work.

## Delivery strategy

Each agent works in an isolated worktree on a branch from the same `main` commit
and opens an independent PR with **base `main`**. No stacked PRs or imports from
another agent's unmerged branch. Independent branches prevent shared edits, but
combined review and testing still resolve semantic integration issues.

The coordinator owns combined verification and shared project configuration.
Do not add dependencies, move the package into `src/`, or add a persistent runner
in this wave. The acceptance agent owns shared documentation; implementation
agents keep their design notes in separate files. No task controls the TV unless
hardware work is explicitly requested.

## Wave 1: merged foundations

The first wave branched from `2f0e99b`, where 23 baseline tests passed. Its contracts
were deliberately usable without changing the existing controller all at once.

| Stream | Merged PR / source | Delivered boundaries | Independent validation |
| --- | --- | --- | --- |
| A: Domain | [#7](https://github.com/lbliii/tellyq/pull/7), `41a55aa`; `tellyq/domain/`, `tests/domain/`, [design](m1/domain.md) | Frozen/slotted values, backend/store/clock ports, pure correlated evidence policy and dependency isolation | 78 tests; Ruff/ty; preflight; source and installed core imports without Cast; macOS/Linux CI |
| B: Persistence | [#6](https://github.com/lbliii/tellyq/pull/6), `a94fd40`; state/clock/program modules, [design](m1/storage.md) | Validated version-1 queue and legacy sessions, explicit future-version errors, atomic private writes, process ownership, extracted program defaults | 140 tests; Ruff/ty; preflight; compatibility and failure injection; macOS/Linux CI |
| C: Cast normalization | [#5](https://github.com/lbliii/tellyq/pull/5), `62d3e00`; Cast parser/observer, synthetic fixtures, [design](m1/cast.md) | Detached defensive observations, unknown fields/ad state preserved, offline wire replay, parser imports without Cast | 79 tests; Ruff/ty; preflight; fixture source-archive inclusion; macOS/Linux CI |

The [planning PR #4](https://github.com/lbliii/tellyq/pull/4) is also merged.
Coordinator review resolved an expired progress anchor, a session-bootstrap port
dependency cycle, overflowing JSON numbers and malformed receiver messages being
mistaken for app exit. Those foundation changes did not yet connect the controller
to every new contract; the second wave below completed that integration.

A temporary combination passed 251 tests and full packaging checks. After merge,
`main` at `a2b72a9` also passed `uv run --locked poe ci`: **251 tests**, **91.2%**
branch-inclusive coverage, Ruff, formatting, ty 0.0.82, source/wheel builds and
isolated installation. The supported runtime was Python 3.14.0 with the GIL.

A separately requested live regression passed visible start and two status reads
without restarting Bob Ross. Stop visibly returned to the Chromecast home screen,
but the parser rejected idle receiver replies without `applications`, so the
report was `unconfirmed` and the queue still said `playing`. This is a software
verification/persistence gap; the expected visible stop is app exit, not pause.

## Wave 2: independently delivered workstreams

| Task | Branch / owned files | Required outcome |
| --- | --- | --- |
| D2/D3/D4: Application and contract integration | [Merged PR #9](https://github.com/lbliii/tellyq/pull/9), `codex/m1-application`; application/controller/adapters, JSON report contracts and associated offline tests; `docs/m1/application.md` | Inject backend/store/clock, use domain policy with fresh owned scopes, preserve legacy state and CLI names, version report output, exercise the same fake/Cast backend contracts and application scenarios |
| S1: Receiver idle/stop evidence | [Merged PR #8](https://github.com/lbliii/tellyq/pull/8), `codex/m1-stop-evidence`; Cast parser/observer and associated tests/fixtures; `docs/m1/stop-evidence.md` | Recognize a justified, fresh correlated idle reply while refusing malformed, partial or stale replies; preserve unknown fields and prevent false stop confirmation |
| E1: Acceptance and documentation | `codex/m1-acceptance`; README and shared docs | Correct stale foundation status, record the failed machine-stop checkpoint honestly, review contracts and keep commit-specific acceptance gates and remaining limits visible |
| E2: Combined review | Coordinator; isolated integration worktree | Review independent PRs, resolve cross-boundary mismatches, run exact combined code through `poe ci` and installed artifacts, inspect macOS/Linux CI and record results |

D2/D3 and the shared D4 contract suite stay with one application owner because
those tests exercise the same changing interfaces. S1 stays independent of
application policy. The coordinator integrates only completed code for verification;
each PR continues to target `main` directly. No agent merges its own PR or marks
M1 complete because its independent suite passes.

The stop-evidence candidate at `a0f6219` independently passed `poe ci`: 301 tests,
93.0% branch-inclusive coverage, lint/format/types, build and isolated install.
Its [macOS/Linux CI](https://github.com/lbliii/tellyq/actions/runs/35619797156) also passed.
[PR #8](https://github.com/lbliii/tellyq/pull/8) was independent of the application
candidate. The application candidate at `92bea46` also passed `poe ci`: 301 tests, 90.6%
branch-inclusive coverage and all build/install checks.

The coordinator combined exact stop/application commits `a0f6219` + `92bea46`
without conflicts on `a2b72a9`. Full `poe ci` passed **351 tests** with **91.8%**
branch-inclusive coverage, Ruff/format/ty, source/wheel builds and isolated
install/CLI smoke. Both code PRs passed macOS and Ubuntu CI;
[PR #9 run](https://github.com/lbliii/tellyq/actions/runs/35620484798). The combined
installed wheel also imported the core without Cast/Milo/Chirp or socket/runtime
side effects.

PR #8 merged at `110cf52`; PR #9 merged at `71a3c39`. The coordinator pulled actual
`main` at `71a3c39` and reran full `poe ci`: **351 tests**, **91.8%** coverage and
all build/install checks passed. Its
[macOS/Linux CI run](https://github.com/lbliii/tellyq/actions/runs/35620756804)
also passed. The explicitly requested hardware rerun then passed on that commit:
visible exact-title playback, two statuses without restart, machine-verified app
exit, user-confirmed Chromecast home screen and persisted stopped state. Ad state
remained unknown, so strict playback proof stayed unconfirmed rather than
inventing inactive-ad evidence. M1 is accepted at `71a3c39`.

## Decisions that integration must preserve

- Content IDs are opaque and provider-specific. The domain does not parse YouTube
  URLs or contain the Bob Ross ID. Use the extracted example program at the
  application boundary.
- Scope observations by stable target, content/session, connection generation,
  sequence and a fresh monotonic start boundary. UTC timestamps describe records;
  monotonic times from a prior process cannot become current evidence after restart.
- Keep command receipts, observed playback and historical user confirmation
  separate. Status never launches or resumes playback. Uncertain effects require
  inspection, not an automatic repeated start.
- Cast has no verified no-ad signal. Preserve active/unknown ad state. The domain
  policy's stricter playback/completion proof cannot be made to pass by turning
  missing ad fields into false. Any weaker identity/progress evidence needs its
  own honest label and must not establish natural completion.
- Receiver-app exit is stop evidence only with valid ownership, freshness and
  correlation. Do not fabricate content-level `IDLE/CANCELED`. A missing field in
  an arbitrary message is not proof that the receiver has no running app.
- Keep version-1 queues and supported legacy sessions readable. Document changes
  to the on-disk format and report schema; reject future/malformed versions before
  device I/O. An uncertain stop must not preserve misleading `playing` state.
- Validate unknown input before returning typed values. Do not use blanket type
  ignores, success defaults or raw network exception payloads in public errors.
- Fixtures use synthetic identifiers and state their provenance. Ordinary tests
  block sockets/DNS and never discover or control a receiver.

## Definition of done

Each PR explains the concrete behavior change, validation and limits. Code changes
run `uv run --locked poe check`; package/dependency changes also run preflight.
The final combined tree runs `uv run --locked poe ci`, including installed-wheel
behavior. Final PR heads need macOS/Linux CI; merged `main` needs its own check.
The [acceptance record](M1-ACCEPTANCE.md) supplies the detailed replay, wire/state,
isolation, stop and bounded hardware criteria.

After integration, the explicitly requested supervised start/status/status/stop
regression verified visible behavior and machine stop independently. No natural
ending is required for M1. M2a owns full lifecycle experiments with ads, pauses,
buffering and natural endings; M2b owns automatic advancement. MCP, subscription
adapters, UI, a daemon and free-threaded support claims are outside this wave.

## Progress ledger

- [x] M0 playback connection and repository quality tooling established.
- [x] Domain, persistence and Cast foundation PRs reviewed and merged into `main`.
- [x] Combined foundation checks and actual merged-main checks passed.
- [x] Live foundation regression recorded, including the failed stop-verification gate.
- [x] Dispatch independent second-wave application, stop-evidence and acceptance agents.
- [x] D2/D3: controller integration, state compatibility and versioned report candidates reviewed.
- [x] D4: shared fake/Cast contracts and end-to-end offline scenarios pass together.
- [x] S1: conservative idle/stop evidence and persistence fix pass offline regression tests.
- [x] E2 local: exact combined code, source/wheel artifacts and isolated install pass.
- [x] E2 platform: both final code PR heads pass macOS/Linux CI; installed core imports without Cast.
- [x] Merge both code PRs; full local and macOS/Linux CI checks pass on actual `main` at `71a3c39`.
- [x] Run the explicitly requested hardware regression on merged `main` at `71a3c39`.
- [x] E1: record the accepted commit `71a3c39` and close M1; M2a is next.
- [x] Publish the acceptance record: [documentation PR #10](https://github.com/lbliii/tellyq/pull/10) merged at `8a480ea`.
