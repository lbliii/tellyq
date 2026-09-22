# M3 local MCP foundation

TellyQ's optional MCP interface is a Milo 0.4.3 stdio server. It exposes
exactly three tools: `start`, `status`, and `stop`. Each tool calls the same
`service_cli.owner_command` boundary used by the foreground-owner CLI, so the
MCP path cannot discover a receiver, create another Cast connection, or fall
back to direct playback.

Start the foreground owner first with a private copy of the service manifest,
then start the MCP process with the owner's private runtime selected at process
startup. Replace the receiver UUID with the value from `discover` and choose a
new queue ID; the example uses the already-tested three-item service shape.

```sh
# Terminal 1, from the TellyQ checkout.
mkdir -p runtime/mcp-owner
cp examples/native-session.json runtime/mcp-session.json
# Edit runtime/mcp-session.json: set queue_id and target.device_id.
uv run --locked tellyq --runtime "$PWD/runtime/mcp-owner" serve \
  --manifest "$PWD/runtime/mcp-session.json"
```

```sh
# Terminal 2, from any working directory; use the absolute checkout path.
TELLYQ_OWNER_RUNTIME="/absolute/path/to/tellyq/runtime/mcp-owner" \
  uv --directory "/absolute/path/to/tellyq" run --locked --extra mcp tellyq-mcp --mcp
```

A generic MCP client configuration is equivalent to:

```json
{
  "mcpServers": {
    "tellyq": {
      "command": "uv",
      "args": [
        "--directory", "/absolute/path/to/tellyq", "run", "--locked",
        "--extra", "mcp", "tellyq-mcp", "--mcp"
      ],
      "env": {"TELLYQ_OWNER_RUNTIME": "/absolute/path/to/runtime/mcp-owner"}
    }
  }
}
```

`TELLYQ_OWNER_RUNTIME` must be an absolute path. The MCP caller cannot choose
or change it through a tool argument. The configured runtime must belong to an
already-running foreground owner; a missing or unavailable owner produces a
machine-readable `owner_unavailable` tool result and never starts direct
playback. The MCP process has no shutdown hook for the owner, so ending the
stdio process leaves that separate owner and its monitoring lifecycle running.

`start` returns the owner's accepted request/ticket identity. An accepted
ticket is an acknowledgement of command handling, not proof that the receiver
is playing. `status` returns the owner's timestamped snapshot and evidence;
inspect its state and evidence fields before claiming playback. `stop` submits
the owner's existing guarded stop and retains its uncertain-outcome behavior.

The advertised MCP output schema is intentionally a generic JSON object at the
Milo boundary. Milo 0.4.3 cannot faithfully render TellyQ's recursive IPC
aliases and nullable nested evidence into JSON Schema; the actual structured
payload remains the versioned typed contract in `tellyq/models.py`.

## Bounded command-parity batch

The M3 batch after PR #44 keeps the existing foreground-owner CLI and the
three-tool MCP surface. `service_cli.owner_tool_command` now owns the shared
start/status/stop names, command-ID validation, safe structured error codes,
and dispatch to `owner_command`. The direct Python caller receives the typed
`MCPResult`; MCP sends that result unchanged. The owner CLI uses the same
dispatch and retains its existing successful `IPCResponse` JSON. On a local
validation or transport failure it emits the shared structured error. Owner
defaults remain in one place: stable queue-derived start ID and a new stop ID
unless the caller supplies one. Reusing an explicit ID preserves the owner's
retry/ticket semantics. An unavailable owner remains uncertain; the result
directs the caller to inspect status before retrying.

Offline regression coverage now compares CLI and direct Python outcomes,
malformed IDs, retries, safe error redaction and unavailable-owner behavior.
The real stdio tests cover initialize, tools/list, tools/call, invalid arguments,
unknown tools, clean protocol stdout, and owner survival after MCP exit. The
isolated wheel smoke checks the installed `tellyq-mcp` entry point's stdio
handshake and tool list, then runs Milo 0.4.3 `verify` against a shim importing
the installed wheel. Milo's verifier requires a module-level `CLI` instance;
the packaged entry point builds its instance after startup runtime selection.
The shim supplies that instance without changing production startup behavior.

The next offline batch shares `OwnerToolSpec` metadata for the three commands.
It selects CLI help and `--command-id` availability, Milo registration names,
descriptions, and MCP hints from the same definitions. Milo continues to infer
the input schema from the registered Python handler signature; the spec selects
the handler shape so `status` has no command ID and `start`/`stop` do. Tests
compare both surfaces to the shared metadata. The `device` override remains a
legacy CLI-only safeguard; MCP keeps its startup-fixed owner runtime and does
not accept a device or runtime argument. The generic object output schema remains
the honest Milo 0.4.3 boundary described above.

The standard `tellyq` CLI now accepts `--owner` on `start`, `status`, and `stop`.
For example, `tellyq --runtime /absolute/owner/runtime status --owner` requires
the foreground owner and prints the same `MCPResult` envelope as direct Python
dispatch and MCP, including command-ID validation and unavailable-owner errors.
If the owner is absent, it returns `owner_unavailable` without trying direct
playback. This explicit mode is useful for clients that require one contract and
must never fall back to the legacy controller. The ordinary commands retain
their existing automatic owner selection and historical success JSON.

Remaining M3 gates: the legacy direct-playback CLI commands still use their
historical controller path when no foreground owner is selected, and
pause/resume, probe, queue, discover, ticket, shutdown and serve are outside
the three-tool shared definition. A full CLI migration would need an explicit
decision on those commands' owner routing and compatibility, followed by
broader parity and schema tests. Do not claim the complete M3 command-definition
acceptance gate from this slice. A supervised hardware start/status/stop run
through an actual MCP client has now passed its machine-side checks, and the
user separately confirmed visible playback and the home screen after stop. See
the [second MCP checkpoint](M3-MCP-SECOND-CHECKPOINT.md) for the evidence and
remaining acceptance limits.

## Prepared supervised MCP checkpoint

`scripts/checkpoint_mcp.py` is an opt-in MCP client for an already-running
foreground owner. It uses the installed `tellyq-mcp --mcp` entry point and
records protocol responses in a new exclusive directory under ignored
`runtime/`. `trace` reads status only. `start --live` first checks that the
owner's complete queue and target match the selected private manifest. It
records a start intent and receipt, closes the MCP process, reopens it to read
fresh receiver evidence, then sends a guarded stop and waits for observed stop.
An acknowledgement alone never sets either observation flag. After an accepted
or uncertain start, the runner attempts guarded stop even if observation fails
or the operator interrupts. A definite start rejection causes no stop, so the
checkpoint cannot cancel preexisting playback. Consult the private journal
when any command outcome is uncertain. Closing MCP does not shut down the owner.

For the later explicitly requested live session, copy
`examples/mcp-checkpoint.json` into `runtime/`, fill in a fresh queue ID and
the discovered receiver UUID, and start the foreground owner with that exact
private manifest. From the repository root, choose a new output directory and
record the tested Git revision:

```sh
uv run --locked --extra mcp python scripts/checkpoint_mcp.py trace \
  --runtime runtime/mcp-owner --manifest runtime/mcp-checkpoint.json \
  --output runtime/mcp-trace-1 --code-revision REVISION

uv run --locked --extra mcp python scripts/checkpoint_mcp.py start --live \
  --runtime runtime/mcp-owner --manifest runtime/mcp-checkpoint.json \
  --output runtime/mcp-live-1 --code-revision REVISION
```

The default start observation window is 75 seconds, followed by up to 30
seconds for stop evidence. Both are bounded options; the runner stops early
when it sees the required evidence. The private summary
reports command acknowledgement, receiver playback evidence, stop observation,
and owner survival separately. Its generated `visual_confirmation` and
`live_acceptance` fields are null and false; a separate review records the
user's visible TV confirmation and checks the journal. The offline test uses a
fake Unix owner and real Milo stdio; it never controls a receiver. The first supervised
run and its timing limitation are recorded in [the first M3 MCP
checkpoint](M3-MCP-CHECKPOINT.md). The second supervised run passed its timed
machine-side checks and the user confirmed visible playback and the home
screen after stop; see [its record](M3-MCP-SECOND-CHECKPOINT.md).
