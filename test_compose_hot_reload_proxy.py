"""Runs the proxy against a fake `gradlew` (a tiny MCP server) in two folders.

python3 -m unittest test_compose_hot_reload_proxy.py
"""

import json
import os
import pathlib
import queue
import subprocess
import sys
import tempfile
import threading
import unittest

PROXY = pathlib.Path(__file__).with_name("compose_hot_reload_proxy.py")

FAKE_GRADLEW = """#!{python}
import json, os, sys
task = sys.argv[-1]
for line in sys.stdin:
    message = json.loads(line)
    if "id" not in message:
        continue
    method = message["method"]
    if method == "initialize":
        result = {{"protocolVersion": message["params"]["protocolVersion"], "capabilities": {{"tools": {{}}}}, "serverInfo": {{"name": "fake"}}}}
    elif method == "tools/list":
        result = {{"tools": [{{"name": "status", "inputSchema": {{"type": "object"}}}}]}}
    elif method == "tools/call":
        result = {{"content": [{{"type": "text", "text": os.getcwd() + " " + task}}]}}
    else:
        print(json.dumps({{"jsonrpc": "2.0", "id": message["id"], "error": {{"code": -32601, "message": method}}}}), flush=True)
        continue
    print(json.dumps({{"jsonrpc": "2.0", "id": message["id"], "result": result}}), flush=True)
"""


class ProxyTest(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        root = pathlib.Path(self.folder.name)
        self.projects = []
        for name in ("first-app", "second-app"):
            project = root / name
            project.mkdir()
            gradlew = project / "gradlew"
            gradlew.write_text(FAKE_GRADLEW.format(python=sys.executable))
            gradlew.chmod(0o755)
            self.projects.append(str(project.resolve()))
        environment = dict(os.environ, XDG_CACHE_HOME=str(root / "cache"))
        self.proxy = subprocess.Popen([sys.executable, str(PROXY)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                      stderr=subprocess.DEVNULL, text=True, bufsize=1, env=environment)
        self.incoming = queue.Queue()
        threading.Thread(target=lambda: [self.incoming.put(json.loads(line)) for line in self.proxy.stdout], daemon=True).start()
        self.next_id = 0

    def tearDown(self):
        self.proxy.stdin.close()
        self.proxy.wait(timeout=10)
        self.folder.cleanup()

    def send(self, method, params=None, notification=False):
        message = {"jsonrpc": "2.0", "method": method, "params": params or {}}
        if not notification:
            self.next_id += 1
            message["id"] = self.next_id
        self.proxy.stdin.write(json.dumps(message) + "\n")
        self.proxy.stdin.flush()
        return message.get("id")

    def receive(self, request_id=None):
        """Next message; with request_id, the answer to it, collecting notifications seen on the way."""
        notifications = []
        while True:
            message = self.incoming.get(timeout=20)
            if request_id is None or message.get("id") == request_id:
                return message, notifications
            notifications.append(message.get("method"))

    def call(self, name, arguments=None):
        answer, notifications = self.receive(self.send("tools/call", {"name": name, "arguments": arguments or {}}))
        return answer["result"], notifications

    def tool_names(self):
        answer, _ = self.receive(self.send("tools/list"))
        return [tool["name"] for tool in answer["result"]["tools"]]

    def test_switches_between_projects(self):
        initialize, _ = self.receive(self.send("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}}))
        self.assertTrue(initialize["result"]["capabilities"]["tools"]["listChanged"])
        self.send("notifications/initialized", notification=True)

        self.assertEqual(self.tool_names(), ["use_project"])
        before, _ = self.call("status")
        self.assertTrue(before["isError"])

        switched, notifications = self.call("use_project", {"path": self.projects[0]})
        self.assertNotIn("isError", switched)
        if "notifications/tools/list_changed" not in notifications:
            notice, _ = self.receive()
            self.assertEqual(notice["method"], "notifications/tools/list_changed")
        self.assertEqual(self.tool_names(), ["use_project", "status"])
        first, _ = self.call("status")
        self.assertEqual(first["content"][0]["text"], f"{self.projects[0]} hotMcpServer")

        self.call("use_project", {"path": self.projects[1], "task": ":desktop:hotMcpServer"})
        second, _ = self.call("status")
        self.assertEqual(second["content"][0]["text"], f"{self.projects[1]} :desktop:hotMcpServer")

        missing, _ = self.call("use_project", {"path": self.projects[0] + "/nowhere"})
        self.assertTrue(missing["isError"])
        self.assertIn("no gradlew", missing["content"][0]["text"])


if __name__ == "__main__":
    unittest.main()
