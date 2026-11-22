#!/usr/bin/env python3
"""MCP proxy for Compose Hot Reload that can point at any project folder.

The Compose Hot Reload MCP server (`./gradlew hotMcpServer`) only sees the app
started from the folder it runs in. This proxy is registered once, anywhere,
and adds one tool, `use_project(path, task)`, which (re)starts that documented
command in the given folder. Every other MCP message is passed through as-is.

Standard library only. Messages are newline-delimited JSON-RPC over stdio.
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import queue
import subprocess
import sys
import threading

GRADLE_ARGUMENTS = ["--no-daemon", "--quiet", "--console=plain"]
DEFAULT_TASK = "hotMcpServer"
CHILD_START_TIMEOUT_SECONDS = 600  # a first Gradle run downloads and compiles

USE_PROJECT_TOOL = {
    "name": "use_project",
    "description": (
        "Point the Compose Hot Reload tools at a project folder. Starts "
        "`./gradlew <task>` there (default task: hotMcpServer; use e.g. "
        "':desktop:hotMcpServer' when the plugin is in a module). The other tools "
        "see the app started with hotRun/hotRunAsync from that same folder. "
        "Call it again to switch folders."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Absolute path of the Gradle project root (the folder with gradlew)."},
            "task": {"type": "string", "description": "Gradle task that starts the MCP server. Default: hotMcpServer."},
        },
        "required": ["path"],
    },
}


def cache_file() -> pathlib.Path:
    cache_root = os.environ.get("XDG_CACHE_HOME") or os.path.join(os.path.expanduser("~"), ".cache")
    return pathlib.Path(cache_root) / "compose-hot-reload-proxy" / "tools.json"


def log(text: str) -> None:
    print(f"[compose-hot-reload-proxy] {text}", file=sys.stderr, flush=True)


class Upstream:
    """The MCP client (Claude Code) on our stdin/stdout."""

    def __init__(self):
        self.write_lock = threading.Lock()

    def send(self, message: dict) -> None:
        with self.write_lock:
            sys.stdout.write(json.dumps(message) + "\n")
            sys.stdout.flush()

    def reply(self, request_id, result: dict) -> None:
        self.send({"jsonrpc": "2.0", "id": request_id, "result": result})

    def error(self, request_id, code: int, text: str) -> None:
        self.send({"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": text}})


class Child:
    """One `./gradlew <task>` MCP server process."""

    def __init__(self, path: str, task: str, upstream: Upstream, on_tools_changed):
        self.path = path
        self.task = task
        self.upstream = upstream
        self.on_tools_changed = on_tools_changed
        self.own_responses: dict[str, queue.Queue] = {}
        self.next_own_id = 0
        self.write_lock = threading.Lock()
        self.process = subprocess.Popen(
            ["./gradlew", *GRADLE_ARGUMENTS, task],
            cwd=path,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=sys.stderr,
            text=True,
            bufsize=1,
        )
        threading.Thread(target=self.read_loop, daemon=True).start()

    def send(self, message: dict) -> None:
        with self.write_lock:
            self.process.stdin.write(json.dumps(message) + "\n")
            self.process.stdin.flush()

    def request(self, method: str, params: dict, timeout: float) -> dict:
        """A request of the proxy's own; its answer is not passed upstream."""
        self.next_own_id += 1
        request_id = f"compose-hot-reload-proxy-{self.next_own_id}"
        answer = queue.Queue(maxsize=1)
        self.own_responses[request_id] = answer
        self.send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
        message = answer.get(timeout=timeout)
        if message is None:
            raise RuntimeError(f"the MCP server in {self.path} exited (its output is on stderr)")
        if "error" in message:
            raise RuntimeError(f"{method} failed: {message['error']}")
        return message["result"]

    def read_loop(self) -> None:
        for line in self.process.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                log(f"not JSON from the child, ignored: {line[:200]}")
                continue
            own = self.own_responses.pop(message.get("id"), None) if "method" not in message else None
            if own is not None:
                own.put(message)
            elif message.get("method") == "notifications/tools/list_changed":
                threading.Thread(target=self.on_tools_changed, daemon=True).start()
            else:
                self.upstream.send(message)
        for answer in self.own_responses.values():
            answer.put(None)

    def stop(self) -> None:
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.process.kill()


class Proxy:
    def __init__(self, project: str | None, task: str):
        self.upstream = Upstream()
        self.child: Child | None = None
        self.child_lock = threading.Lock()
        self.client_initialize_params: dict | None = None
        if project is None and (pathlib.Path.cwd() / "gradlew").is_file():
            project = str(pathlib.Path.cwd())  # started inside a Gradle project: use it
        self.startup_project = project
        self.startup_task = task
        self.startup_failure: str | None = None
        self.startup_finished = threading.Event()
        if project is None:
            self.startup_finished.set()
        self.child_tools = self.read_cached_tools()

    # Tool list -----------------------------------------------------------

    @staticmethod
    def read_cached_tools() -> list:
        try:
            return json.loads(cache_file().read_text())
        except (OSError, json.JSONDecodeError):
            return []

    def store_child_tools(self, tools: list) -> None:
        if tools == self.child_tools:
            return
        self.child_tools = tools
        try:
            cache_file().parent.mkdir(parents=True, exist_ok=True)
            cache_file().write_text(json.dumps(tools))
        except OSError as cache_failure:
            log(f"could not write the tool cache: {cache_failure}")
        self.upstream.send({"jsonrpc": "2.0", "method": "notifications/tools/list_changed"})

    def refresh_child_tools(self) -> None:
        child = self.child
        if child is not None:
            self.store_child_tools(child.request("tools/list", {}, timeout=60).get("tools", []))

    # Switching projects --------------------------------------------------

    def use_project(self, path: str, task: str) -> str:
        project = pathlib.Path(path).expanduser()
        if not project.is_absolute():
            raise ValueError(f"path must be absolute: {path}")
        if not (project / "gradlew").is_file():
            raise ValueError(f"no gradlew in {project}")
        with self.child_lock:
            if self.child is not None:
                self.child.stop()
                self.child = None
            log(f"starting ./gradlew {task} in {project}")
            child = Child(str(project), task, self.upstream, self.refresh_child_tools)
            try:
                child.request("initialize", self.client_initialize_params, timeout=CHILD_START_TIMEOUT_SECONDS)
                child.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
                tools = child.request("tools/list", {}, timeout=60).get("tools", [])
            except Exception:
                child.stop()
                raise
            self.child = child
        self.store_child_tools(tools)
        return f"Compose Hot Reload tools now use {project} (task {task})."

    def use_startup_project(self) -> None:
        try:
            self.use_project(self.startup_project, self.startup_task)
        except Exception as startup_failure:
            self.startup_failure = f"could not start in {self.startup_project}: {startup_failure}"
            log(self.startup_failure)
        finally:
            self.startup_finished.set()

    # Requests from the client -------------------------------------------

    def handle(self, message: dict) -> None:
        method = message.get("method")
        request_id = message.get("id")
        if method is None:
            if self.child is not None:
                self.child.send(message)  # the client answering a request the child sent it
            return
        if method == "initialize":
            self.client_initialize_params = message.get("params") or {}
            self.upstream.reply(request_id, {
                "protocolVersion": self.client_initialize_params.get("protocolVersion", "2025-06-18"),
                "capabilities": {"tools": {"listChanged": True}},
                "serverInfo": {"name": "compose-hot-reload-proxy", "version": "1.0.0"},
            })
        elif method == "notifications/initialized":
            if self.startup_project:
                threading.Thread(target=self.use_startup_project, daemon=True).start()
        elif method == "ping":
            self.upstream.reply(request_id, {})
        elif method == "tools/list":
            self.upstream.reply(request_id, {"tools": [USE_PROJECT_TOOL, *self.child_tools]})
        elif method == "tools/call" and (message.get("params") or {}).get("name") == "use_project":
            threading.Thread(target=self.call_use_project, args=(message,), daemon=True).start()
        elif self.startup_finished.is_set():
            self.forward_to_child(message)
        else:
            threading.Thread(target=self.forward_to_child, args=(message,), daemon=True).start()

    def forward_to_child(self, message: dict) -> None:
        """Passes a message on; waits first while the startup project is still starting."""
        self.startup_finished.wait(timeout=CHILD_START_TIMEOUT_SECONDS)
        child = self.child
        if child is not None and child.process.poll() is None:
            child.send(message)
            return
        method = message.get("method")
        reason = self.startup_failure or "No project selected, or its MCP server exited"
        if method == "tools/call":
            self.upstream.reply(message.get("id"), {
                "content": [{"type": "text", "text": f"{reason}: call use_project."}],
                "isError": True,
            })
        elif message.get("id") is not None:
            self.upstream.error(message["id"], -32601, f"{method} needs a project: {reason}: call use_project")

    def call_use_project(self, message: dict) -> None:
        arguments = message["params"].get("arguments") or {}
        try:
            text = self.use_project(arguments.get("path", ""), arguments.get("task") or DEFAULT_TASK)
            self.upstream.reply(message["id"], {"content": [{"type": "text", "text": text}]})
        except Exception as use_project_failure:
            self.upstream.reply(message["id"], {
                "content": [{"type": "text", "text": f"use_project failed: {use_project_failure}"}],
                "isError": True,
            })

    def run(self) -> None:
        try:
            for line in sys.stdin:
                if line.strip():
                    self.handle(json.loads(line))
        finally:
            if self.child is not None:
                self.child.stop()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--project", default=os.environ.get("COMPOSE_HOT_RELOAD_PROJECT"),
                        help="project folder to use from the start (or COMPOSE_HOT_RELOAD_PROJECT)")
    parser.add_argument("--task", default=os.environ.get("COMPOSE_HOT_RELOAD_TASK", DEFAULT_TASK),
                        help="Gradle task, default hotMcpServer (or COMPOSE_HOT_RELOAD_TASK)")
    arguments = parser.parse_args()
    Proxy(arguments.project, arguments.task).run()


if __name__ == "__main__":
    main()
