# Working on TellyQ

Read [the MVP](docs/MVP.md), [architecture](docs/ARCHITECTURE.md) and
[roadmap](docs/ROADMAP.md) first. The working playback experiment is the baseline;
the full domain/adapter/runner design is still incremental work.

## Development environment

Use standard CPython 3.14 on macOS or Linux and uv 0.12.4 or newer. CI pins uv
0.12.4 so automatic Python provisioning includes the stable 3.14 release. Windows is
not supported by the current `fcntl` process lock. Free-threaded 3.14 is a future
compatibility lane, not an established support claim.

From the repository root:

```sh
uv sync --locked
uv run --locked poe check
uv run --locked poe preflight
```

`uv.lock` is the authoritative dependency lock, including development tools and
the build backend. Commit it with dependency changes. `uv sync --locked` refuses
stale configuration. If a sandbox needs a project-local cache, prefix uv commands
with `UV_CACHE_DIR=runtime/uv-cache`.

## Checks

| Task | What it checks |
| --- | --- |
| `poe lint` / `poe format-check` | Ruff correctness/style rules and formatting |
| `poe typecheck` | ty over the package, tests and development scripts; warnings fail |
| `poe test` | pytest, including the original unittest regressions; sockets and DNS are blocked |
| `poe test-cov` | Branch coverage report and ignored `coverage.xml` |
| `poe build` | Source archive, then a wheel built from that archive using the locked backend |
| `poe smoke` | Archive contents, clean wheel install, import behavior and CLI behavior outside the checkout |
| `poe check` | Lint, formatting, types and tests |
| `poe preflight` | Check, build and installation smoke |
| `poe ci` | Same gates with coverage reporting, on macOS and Linux |

Run these through `uv run --locked`. Use `poe lint-fix` and `poe format` for
intentional fixes. Coverage initially reports gaps without a percentage gate:
hardware transport coverage must be meaningful, not inflated with call-count tests.

Optional local commit hooks use the locked Ruff/ty tools:

```sh
uv run --locked prek install
uv run --locked prek run --all-files
```

CI remains authoritative whether hooks are installed or not. Hook checks do not
rewrite files. CI runs no live playback, discovery, publishing or deployment.
Install smoke may download locked packages into a temporary environment.

## Engineering conventions

- Annotate package functions and model JSON records explicitly. Keep dynamic data
  at Cast/JSON boundaries; validate it before promoting it to domain values.
  Avoid blanket type/lint suppressions. `py.typed` ships with the package.
- Keep policy free of I/O. Add small protocols when a real adapter and test double
  need a shared contract. Use immutable values for new cross-thread snapshots and
  explicit owners/queues for mutable clients.
- Keep CLI rendering separate from controller operations. A command returning
  successfully never proves playback. Unknown values remain unknown; elapsed
  duration never implies completion.
- Add behavior tests for changed contracts and failure paths. Use sanitized,
  synthetic identifiers; block real networking in the normal suite. Live tests
  require a task that explicitly requests them and separate verification evidence.
- Avoid imports that open sockets, discover devices or create files. The installed
  CLI writes `runtime/` under the working directory, so run it from the checkout
  for consistent local state. Keep that directory, credentials and logs out of Git.
- Change docs with behavior. Record user-visible changes in `CHANGELOG.md`. Keep
  Milo/MCP and Chirp integration at their planned milestones; no unused framework
  dependencies or generic utility package are needed for this experiment.

## Dependency changes

Update the requirement in `pyproject.toml`, run `uv lock`, inspect the resolved
changes, then run preflight. To deliberately refresh a package already allowed by
the requirement, use `uv lock --upgrade-package PACKAGE`. Ruff and ty have explicit
version pins so new rule/format behavior is reviewed. Other tools are pinned by
the lock. Keep PyChromecast at the hardware-verified version until an upgrade has
its own compatibility evidence. Automated update proposals never establish that
evidence by themselves.

Builds contain no device state. There is no release/publish workflow; packaging
checks only establish that the local artifacts install correctly.
