"""Read current Hook identities through the native Codex app-server protocol."""
import json
import os
import queue
import subprocess
import threading


def list_hooks(command, home, cwd):
    process = subprocess.Popen([command, "app-server"], env={**os.environ, "CODEX_HOME": str(home)},
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    responses = queue.Queue()
    def read():
        try:
            for line in process.stdout:
                if len(line) > 4 * 1024 * 1024:
                    responses.put(None)
                    return
                responses.put(json.loads(line))
        finally:
            responses.put(None)
    reader = threading.Thread(target=read, daemon=True)
    reader.start()
    try:
        for number, method, params in [
            (1, "initialize", {"clientInfo": {"name": "pennix-hook-state", "version": "1"},
                                "capabilities": {"experimentalApi": True}}),
            (2, "hooks/list", {"cwds": [str(cwd)]}),
        ]:
            process.stdin.write(json.dumps({"id": number, "method": method, "params": params}) + "\n")
            process.stdin.flush()
            for _ in range(100):
                response = responses.get(timeout=30)
                if response is None:
                    raise RuntimeError("Codex app-server 未返回 Hook 状态")
                if response.get("id") == number:
                    if "error" in response:
                        raise RuntimeError("Codex app-server 拒绝 Hook 状态查询")
                    break
            else:
                raise RuntimeError("Codex app-server 响应超过核验上限")
            if number == 1:
                process.stdin.write('{"method":"initialized"}\n')
                process.stdin.flush()
        return response["result"]["data"]
    finally:
        process.stdin.close()
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        reader.join(timeout=5)
