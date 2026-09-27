"""Run the whole test environment on this machine, without Docker or Entra ID.

    python scripts/local/run.py [--no-browser]

Normally started by run-local.ps1 (Windows) or ``make local``, which prepare .venv
first. Starts, in order, and waits for each to be healthy:

    temporal        Temporal CLI dev server          127.0.0.1:7233 (UI http://localhost:8233)
    livekit         LiveKit server, dev keys         ws://localhost:7880
    mock-backend    fake core banking                http://localhost:8082
    agents          the six domain agents            http://localhost:8443
    orchestrator    API + Temporal worker            http://localhost:8080 (console: /console)
    token-service   voice/chat session tokens        http://localhost:8090
    master-agent    LiveKit voice agent (Azure Speech)
    test-client     the browser app                  http://localhost:8000

LiveKit and Temporal binaries are downloaded once into .local/bin. Speech keys come
from .env.local (see .env.local.example). Output is prefixed per service and also
written to .local/logs/<service>.log. Ctrl+C stops everything.
"""

from __future__ import annotations

import argparse
import io
import os
import platform
import signal
import socket
import subprocess
import sys
import tarfile
import threading
import time
import urllib.request
import webbrowser
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LOCAL = ROOT / ".local"
BIN = LOCAL / "bin"
LOGS = LOCAL / "logs"
WINDOWS = os.name == "nt"
EXE = ".exe" if WINDOWS else ""

LIVEKIT_VERSION = "1.13.7"   # same as deploy/azure/vm/docker-compose.yaml
TEMPORAL_VERSION = "1.9.1"

# Development values only (same as deploy/docker/docker-compose.yaml); never reuse them.
DEV_SECRETS = {
    "ORCH_SESSION_KEY": "8r0h0d4xJbA0jYt6oXx3T2mZq6X0qfKf7zH8vGm2b0U=",
    "ORCH_APPROVAL_KEY": "dev-approval-key-dev-approval-key-dev-approval-key",
    "ORCH_TEMPORAL_PAYLOAD_KEY": "bZ3i2Fq8m3cV9d1oQeS0tX5yL7wK4nA6hJ2gR8uT1pE=",
    "MA_DISPATCH_KEY": "dev-dispatch-key-dev-dispatch-key-dev-dispatch-key",
    "LIVEKIT_API_KEY": "devkey",
    "LIVEKIT_API_SECRET": "secret",
}
COLORS = ["36", "33", "35", "32", "34", "96", "93", "95"]


@dataclass
class Service:
    name: str
    cmd: list[str]
    port: int
    health: str = ""            # URL path to poll; empty: wait for the port only
    env: dict[str, str] = field(default_factory=dict)
    timeout_s: float = 60


def platform_asset(project: str) -> tuple[str, str]:
    osname = {"Windows": "windows", "Linux": "linux", "Darwin": "darwin"}[platform.system()]
    arch = "arm64" if platform.machine().lower() in ("arm64", "aarch64") else "amd64"
    ext = "zip" if WINDOWS else "tar.gz"
    if project == "livekit":
        return (f"https://github.com/livekit/livekit/releases/download/v{LIVEKIT_VERSION}/"
                f"livekit_{LIVEKIT_VERSION}_{osname}_{arch}.{ext}", "livekit-server" + EXE)
    return (f"https://github.com/temporalio/cli/releases/download/v{TEMPORAL_VERSION}/"
            f"temporal_cli_{TEMPORAL_VERSION}_{osname}_{arch}.{ext}", "temporal" + EXE)


def ensure_binary(project: str, version: str) -> Path:
    url, name = platform_asset(project)
    target = BIN / f"{project}-{version}" / name
    if target.exists():
        return target
    print(f"downloading {url}")
    data = urllib.request.urlopen(url, timeout=120).read()  # noqa: S310 - pinned GitHub release
    target.parent.mkdir(parents=True, exist_ok=True)
    if url.endswith(".zip"):
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            member = next(m for m in z.namelist() if m.rsplit("/", 1)[-1] == name)
            target.write_bytes(z.read(member))
    else:
        with tarfile.open(fileobj=io.BytesIO(data)) as t:
            member = next(m for m in t.getmembers() if m.name.rsplit("/", 1)[-1] == name)
            target.write_bytes(t.extractfile(member).read())  # type: ignore[union-attr]
        target.chmod(0o755)
    return target


def read_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                value = value.strip()
                value = "" if value.startswith("#") else value.split(" #", 1)[0].strip()  # inline comment
                value = value.strip('"').strip("'")
                if value:  # an empty placeholder must not hide a value set in the environment
                    values[key.strip()] = value
    return values


def speech_settings(env: dict[str, str]) -> dict[str, str]:
    """Azure Speech (as in the test environment) or Deepgram + Cartesia."""
    if env.get("AZURE_SPEECH_KEY") and env.get("AZURE_SPEECH_REGION"):
        return {"MA_CONFIG_FILE": "config/master_agent.test.yaml"}
    if env.get("DEEPGRAM_API_KEY") and env.get("CARTESIA_API_KEY"):
        return {"MA_CONFIG_FILE": "config/master_agent.yaml"}
    sys.exit(
        "No speech credentials. The master agent needs them for voice and chat sessions.\n"
        "Copy .env.local.example to .env.local and set AZURE_SPEECH_KEY and AZURE_SPEECH_REGION\n"
        "(Azure portal: the speech-<prefix>-... resource in your test resource group, 'Keys and Endpoint'),\n"
        "or DEEPGRAM_API_KEY and CARTESIA_API_KEY."
    )


def lan_ip() -> str:
    """This machine's address on its default route, or 127.0.0.1 when offline.

    LiveKit must advertise it as its ICE candidate: browsers never gather loopback
    candidates, so media to a 127.0.0.1-only server fails ("could not establish pc connection").
    """
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        try:
            s.connect(("192.0.2.1", 9))  # TEST-NET-1: selects the route, sends nothing
            return s.getsockname()[0]
        except OSError:
            return "127.0.0.1"


def port_in_use(port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


def wait_ready(svc: Service, proc: subprocess.Popen[bytes]) -> None:
    deadline = time.monotonic() + svc.timeout_s
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"{svc.name} exited with code {proc.returncode} (see .local/logs/{svc.name}.log)")
        try:
            if svc.health:
                with urllib.request.urlopen(f"http://127.0.0.1:{svc.port}{svc.health}", timeout=2) as r:  # noqa: S310
                    if r.status < 500:
                        return
            elif port_in_use(svc.port):
                return
        except OSError:
            pass
        time.sleep(0.5)
    raise RuntimeError(f"{svc.name} not ready after {svc.timeout_s:.0f}s (see .local/logs/{svc.name}.log)")


def pump(name: str, color: str, stream: io.BufferedReader, log_file: io.TextIOWrapper) -> None:
    width = 14
    for raw in iter(stream.readline, b""):
        line = raw.decode("utf-8", errors="replace").rstrip()
        log_file.write(line + "\n")
        log_file.flush()
        print(f"\033[{color}m{name:<{width}}|\033[0m {line}", flush=True)


def stop(procs: list[tuple[Service, subprocess.Popen[bytes]]]) -> None:
    for svc, proc in reversed(procs):
        if proc.poll() is None:
            if WINDOWS:  # the whole tree: the master agent runs each job in a child process
                subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True)
            else:
                proc.terminate()
    for _, proc in procs:
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--no-browser", action="store_true", help="do not open the test client")
    args = parser.parse_args()
    if WINDOWS:
        os.system("")  # noqa: S605 S607 - enables ANSI colours in the Windows console

    user_env = read_env_file(ROOT / ".env.local")
    speech = speech_settings({**os.environ, **user_env})
    livekit = ensure_binary("livekit", LIVEKIT_VERSION)
    temporal = ensure_binary("temporal", TEMPORAL_VERSION)
    py = sys.executable
    LOGS.mkdir(parents=True, exist_ok=True)

    # The voice agent's models (VAD, turn detector); the container image bakes them in.
    marker = LOCAL / "agent-files.ok"
    if not marker.exists():
        print("downloading the voice agent's model files (once)")
        # With the agent's config: it only downloads for the plugins the config imports.
        subprocess.run([py, "-m", "master_agent.agent", "download-files"], cwd=ROOT, check=True,
                       env={**os.environ, **user_env, **speech})
        marker.touch()

    node_ip = lan_ip()
    livekit_bind = ["--bind", "127.0.0.1"] + (["--bind", node_ip] if node_ip != "127.0.0.1" else [])
    urls = {"MOCK_BACKEND_URL": "http://127.0.0.1:8082"}
    services = [
        Service("temporal", [str(temporal), "server", "start-dev", "--ip", "127.0.0.1", "--ui-port", "8233",
                             "--log-level", "warn", "--db-filename", str(LOCAL / "temporal.db")], 7233, timeout_s=90),
        Service("livekit", [str(livekit), "--dev", *livekit_bind, "--node-ip", node_ip], 7880),
        Service("mock-backend", [py, "-m", "uvicorn", "mock_backend.app:app", "--host", "127.0.0.1", "--port", "8082"],
                8082, "/healthz"),
        Service("agents", [py, "-m", "uvicorn", "domain_agents.serve:app", "--host", "127.0.0.1", "--port", "8443"],
                8443, "/healthz", urls),
        Service("orchestrator", [py, "scripts/local/orchestrator_all_in_one.py"], 8080, "/readyz",
                {"ORCH_CONFIG_FILE": "config/orchestrator.local.yaml"}),
        Service("token-service", [py, "-m", "uvicorn", "token_service.app:create_app", "--factory",
                                  "--host", "127.0.0.1", "--port", "8090"], 8090, "/healthz",
                {"TS_CONFIG_FILE": "config/token_service.yaml", "TS__CELL_ID": "local",
                 "TS__ORCHESTRATOR_URL": "http://127.0.0.1:8080", "TS__LIVEKIT__URL": "ws://localhost:7880"}),
        # 8081: the agent's health server (503 until it is registered with LiveKit).
        Service("master-agent", [py, "-m", "master_agent.agent", "start"], 8081, "/",
                {**speech, "LIVEKIT_URL": "ws://127.0.0.1:7880", "MA__ORCHESTRATOR__URL": "http://127.0.0.1:8080"},
                timeout_s=120),
        Service("test-client", [py, "-m", "uvicorn", "test_client.app:create_app", "--factory",
                                "--host", "127.0.0.1", "--port", "8000"], 8000, "/healthz",
                {**urls, "ORCHESTRATOR_URL": "http://127.0.0.1:8080", "TOKEN_SERVICE_URL": "http://127.0.0.1:8090"}),
    ]
    busy = [f"{s.name} ({s.port})" for s in services if port_in_use(s.port)]
    if busy:
        sys.exit("ports already in use: " + ", ".join(busy) + ". Is the local stack already running?")

    base_env = {**os.environ, **DEV_SECRETS, **user_env, "PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8"}
    procs: list[tuple[Service, subprocess.Popen[bytes]]] = []
    signal.signal(signal.SIGINT, signal.default_int_handler)
    try:
        for i, svc in enumerate(services):
            print(f"\033[1mstarting {svc.name}\033[0m", flush=True)
            proc = subprocess.Popen(svc.cmd, cwd=ROOT, env={**base_env, **svc.env}, stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)
            procs.append((svc, proc))
            log_file = open(LOGS / f"{svc.name}.log", "w", encoding="utf-8")  # noqa: SIM115 - closed with the process
            threading.Thread(target=pump, args=(svc.name, COLORS[i % len(COLORS)], proc.stdout, log_file),
                             daemon=True).start()
            wait_ready(svc, proc)

        print("\n\033[1;32mLocal stack is up.\033[0m")
        print("  Test client    http://localhost:8000")
        print("  Command center http://localhost:8080/console")
        print("  Temporal UI    http://localhost:8233")
        print("  Mock bank API  http://localhost:8082/docs")
        print("Ctrl+C stops everything.\n", flush=True)
        if not args.no_browser:
            webbrowser.open("http://localhost:8000")
        while True:
            for svc, proc in procs:
                if proc.poll() is not None:
                    raise RuntimeError(f"{svc.name} exited with code {proc.returncode} (see .local/logs/{svc.name}.log)")
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nstopping...")
    except RuntimeError as exc:
        print(f"\n\033[1;31m{exc}\033[0m\nstopping...")
        raise SystemExit(1) from None
    finally:
        stop(procs)


if __name__ == "__main__":
    main()
