"""Check the built artifacts and install the wheel away from the source checkout."""

import json
import os
import subprocess
import sys
import tarfile
import tomllib
import zipfile
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]


def run(*args: str, cwd: Path = ROOT) -> str:
    try:
        result = subprocess.run(
            args, cwd=cwd, check=True, text=True, capture_output=True, timeout=120
        )
    except subprocess.CalledProcessError as exc:
        print(exc.stderr, file=sys.stderr)
        raise
    return result.stdout


def check_members(members: list[str]) -> None:
    for member in members:
        parts = Path(member).parts
        if {"runtime", ".venv", "logs", ".git"}.intersection(parts):
            raise RuntimeError(f"Private directory included in distribution: {member}")
        if any(part.startswith(".env") or part.endswith(".log") for part in parts):
            raise RuntimeError(f"Private file included in distribution: {member}")
    if not any(member.endswith("tellyq/py.typed") for member in members):
        raise RuntimeError("The distribution is missing its typing marker.")


def check_source_fixtures(members: list[str], root: Path = ROOT) -> None:
    """Require all reviewed fixture formats in the source archive, across suites."""
    expected = {
        path.relative_to(root).as_posix()
        for path in (root / "tests" / "fixtures").rglob("*")
        if path.is_file() and path.suffix in {".json", ".jsonl", ".md"}
    }
    # An sdist has one project/version directory above its repository paths.
    included = {Path(*Path(member).parts[1:]).as_posix() for member in members}
    if missing := expected - included:
        raise RuntimeError(f"Source distribution is missing reviewed fixtures: {sorted(missing)}")


def main() -> None:
    metadata = tomllib.loads((ROOT / "pyproject.toml").read_text())
    version = metadata["project"]["version"]
    wheel = ROOT / "dist" / f"tellyq-{version}-py3-none-any.whl"
    sdist = ROOT / "dist" / f"tellyq-{version}.tar.gz"
    with zipfile.ZipFile(wheel) as archive:
        check_members(archive.namelist())
    with tarfile.open(sdist) as archive:
        members = archive.getnames()
        check_members(members)
        check_source_fixtures(members)

    # A relative cache path must keep pointing at the checkout when cwd changes.
    if cache := os.environ.get("UV_CACHE_DIR"):
        os.environ["UV_CACHE_DIR"] = str(Path(cache).resolve())
    with TemporaryDirectory(prefix="tellyq-install-") as directory:
        work = Path(directory)
        environment = work / "venv"
        python = environment / "bin" / "python"
        cli = environment / "bin" / "tellyq"
        mcp_cli = environment / "bin" / "tellyq-mcp"
        requirements = work / "requirements.txt"
        mcp_requirements = work / "requirements-mcp.txt"
        run("uv", "export", "--locked", "--no-dev", "--no-emit-project", "-o", str(requirements))
        run("uv", "venv", "--python", sys.executable, str(environment))
        run(
            "uv",
            "pip",
            "install",
            "--python",
            str(python),
            "--require-hashes",
            "--no-deps",
            "-r",
            str(requirements),
        )
        run("uv", "pip", "install", "--python", str(python), "--no-deps", str(wheel))
        run("uv", "pip", "check", "--python", str(python))
        run(
            str(python),
            "-I",
            "-c",
            """
import sys
from pathlib import Path

def reject_network(event, args):
    if event.startswith("socket."):
        raise RuntimeError(f"Import attempted networking: {event}")

sys.addaudithook(reject_network)
import tellyq
import tellyq.__main__
import tellyq.cast
import tellyq.controller
import tellyq.evidence
import tellyq.models
import tellyq.state
assert "site-packages" in str(tellyq.__file__)
assert not Path("runtime").exists(), "Imports created runtime state"
assert tellyq.state.RUNTIME == Path.cwd() / "runtime"
""",
            cwd=work,
        )
        help_output = run(str(cli), "--help", cwd=work)
        if not all(
            command in help_output for command in ("discover", "queue", "start", "status", "stop")
        ):
            raise RuntimeError("Installed CLI is missing a command.")
        if (work / "runtime").exists():
            raise RuntimeError("CLI help created runtime state.")
        output = run(
            str(cli), "queue", "--device", "00000000-0000-4000-8000-000000000001", cwd=work
        )
        report = json.loads(output)
        if report["state"] != "queued" or report["commands"]:
            raise RuntimeError(
                "Installed queue command did not return a queue without device commands."
            )
        if not (work / "runtime" / "queue.json").is_file():
            raise RuntimeError("Installed CLI did not save state under the working directory.")

        # The base wheel remains usable without Milo; install only the optional
        # extra before exercising the separately packaged stdio entry point.
        run(
            "uv",
            "export",
            "--locked",
            "--no-dev",
            "--extra",
            "mcp",
            "--no-emit-project",
            "-o",
            str(mcp_requirements),
        )
        run(
            "uv",
            "pip",
            "install",
            "--python",
            str(python),
            "--require-hashes",
            "--no-deps",
            "-r",
            str(mcp_requirements),
        )
        if not mcp_cli.is_file():
            raise RuntimeError("The optional wheel install did not provide tellyq-mcp.")
        mcp_environment = os.environ.copy()
        mcp_environment["TELLYQ_OWNER_RUNTIME"] = str(work / "missing-owner")
        handshake = subprocess.run(
            [str(mcp_cli), "--mcp"],
            cwd=work,
            env=mcp_environment,
            input=(
                '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}\n'
                '{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}\n'
            ),
            capture_output=True,
            check=True,
            text=True,
            timeout=30,
        )
        responses = [json.loads(line) for line in handshake.stdout.splitlines() if line]
        if len(responses) != 2 or any(response.get("jsonrpc") != "2.0" for response in responses):
            raise RuntimeError("Installed tellyq-mcp emitted an invalid stdio handshake.")
        if {tool["name"] for tool in responses[1]["result"]["tools"]} != {
            "start",
            "status",
            "stop",
        }:
            raise RuntimeError("Installed tellyq-mcp exposed an unexpected tool set.")
        # Milo verify needs a module-level CLI object and executes a Python
        # file. Use a tiny shim against the isolated installed wheel while the
        # handshake above exercises the wheel's actual console entry point.
        verify_target = work / "verify_mcp.py"
        verify_target.write_text(
            "from pathlib import Path\n"
            "from tellyq.mcp import build_cli\n"
            "cli = build_cli(Path('missing-owner').resolve())\n"
            "if __name__ == '__main__':\n"
            "    cli.run()\n"
        )
        run(str(environment / "bin" / "milo"), "verify", str(verify_target), cwd=work)
    print(
        "Wheel/sdist contents, isolated imports, CLI help/local queue, optional tellyq-mcp handshake and Milo verify passed."
    )


if __name__ == "__main__":
    main()
