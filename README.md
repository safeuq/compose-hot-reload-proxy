# compose-hot-reload-proxy

The [Compose Hot Reload](https://github.com/JetBrains/compose-hot-reload) MCP server (`./gradlew hotMcpServer`) only sees the app started from the folder it runs in, so its documented `.mcp.json` entry works only when the MCP client was started inside that project.

This proxy is registered once and works from any folder. It adds one tool, `use_project`, which starts the documented command in the folder you name. Every other MCP message is passed through unchanged, so you get the plugin's own tools (`status`, `reload`, `take_screenshot`, `get_semantic_tree`, `click`, …).

| | Plugin's `.mcp.json` | This proxy |
|---|---|---|
| Command | `./gradlew --no-daemon --quiet --console=plain hotMcpServer` | `python3 compose_hot_reload_proxy.py` |
| Which project | the folder the MCP client started in | `use_project(path, task)`, any time, as often as needed |
| Several projects or worktrees | one client session per folder | one registration; each session or subagent picks its own folder |

Needs Python 3.10 or newer (standard library only) and a project that applies the Compose Hot Reload Gradle plugin.

## Register

Claude Code:

```sh
claude mcp add --scope user compose-hot-reload -- python3 /path/to/compose_hot_reload_proxy.py
```

Add `-e JAVA_HOME=…` when the project needs a particular JDK for Gradle.

Any other MCP client: a stdio server with that command.

## Use

1. Start your app with hot reload from the project folder, e.g. `./gradlew hotRunAsync` (or `:desktop:hotRunAsync`).
2. Call `use_project` with the project's absolute path. Pass `task` when the plugin is applied in a module: `{"path": "/work/my-app", "task": ":desktop:hotMcpServer"}`. The default is `hotMcpServer`.
3. Use the plugin's tools. `status` answers `{"connected": true, …}` once the app is up.

Call `use_project` again to switch to another folder or worktree; the previous server is stopped.

To fix the project from the start instead, pass `--project /work/my-app` (and `--task …`), or set `COMPOSE_HOT_RELOAD_PROJECT` / `COMPOSE_HOT_RELOAD_TASK`.

## How it works

- Until a project is chosen, the tool list is `use_project` plus the plugin's tools remembered from the last run (`~/.cache/compose-hot-reload-proxy/tools.json`). Calling a plugin tool then answers "call use_project first".
- `use_project` starts `./gradlew --no-daemon --quiet --console=plain <task>` in the folder, runs the MCP handshake with it, and reads its tools. When they differ from the remembered list, the proxy sends `notifications/tools/list_changed`, and the client fetches the list again.
- The first start in a folder can take minutes (Gradle downloads and compiles); later starts take seconds. The proxy waits up to 10 minutes.
- Gradle's and the server's own output go to the proxy's stderr.

## Claude Code subagents

A subagent definition can give each subagent its own copy, so subagents working in different worktrees don't share one server:

```yaml
---
name: compose-ui
description: Changes Compose UI and checks it in the running app.
mcpServers:
  - compose-hot-reload:
      type: stdio
      command: python3
      args: ["/path/to/compose_hot_reload_proxy.py"]
---

Start the app from your worktree with `./gradlew hotRunAsync`, then call
`use_project` with the worktree's path before using the other tools.
```

## Test

```sh
python3 -m unittest test_compose_hot_reload_proxy.py
```

It runs the proxy against a fake `gradlew` in two folders: switching, passing calls through, the task argument, and the error for a folder without `gradlew`.
