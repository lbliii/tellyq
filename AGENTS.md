# TellyQ project guidance

TellyQ is the app name. Cueby is its TV-programming companion.

Read README.md and docs/MVP.md before implementing. docs/RESEARCH.md records researched options and material limitations.

- Preserve the smallest MVP: one real YouTube program, one existing Chromecast, a local Python controller, and explicit playback verification.
- The user wants software API/MCP control. Do not introduce synthetic voice control or custom hardware as the first route.
- Prove the playback connection before building UI, recommendation logic, cross-service scheduling, or an MCP wrapper.
- Keep commands and observed results distinct. Never report successful playback based only on an accepted command or app launch.
- Do not infer completion from nominal runtime; account for ads, pauses, and buffering.
- Keep credentials, pairing material, local device addresses, and runtime logs out of Git. Use runtime/ for local state.
- Do not expand repository setup into a live TV playback test unless that work is requested in the active task.

## Development checks

- Use Python 3.14 and the committed `uv.lock`; install with `uv sync --locked`.
- Run `uv run --locked poe check` for code changes and `poe preflight` for packaging/dependency changes.
- Keep package functions annotated and JSON contracts in `tellyq/models.py`; do not silence whole modules to pass ty or Ruff.
- Normal pytest runs block sockets and DNS. Live receiver checks belong in explicitly requested hardware work, never CI.
- See CONTRIBUTING.md for local hooks, coverage, packaging and dependency updates.
