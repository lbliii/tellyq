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

The repository now has offline subprocess coverage for the real initialize,
tools/list, tools/call, invalid-argument and unknown-tool protocol paths, plus
owner-command parity and owner survival after MCP exit. Full CLI/direct-Python
parity review, Milo verification against the packaged entry point, and one
supervised hardware start/status/stop run remain to be completed. Pause/resume,
planner/UI routes, hosted transports and live acceptance are outside this slice.
