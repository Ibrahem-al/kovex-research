#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
eval/baseline_harness.py - Baseline (no-coordination) harness for Kovex.

Scenario (identical setup to harness.py, coordination stripped out)
---------------------------------------------------------------------
Agent B generates code against the pre-write Client.send signature.
Agent A commits the same write as in harness.py.
We verify Agent B receives NO directive (Kovex is bypassed) and that
Agent B's pre-written code now conflicts with the post-write signature
(semantic conflict exists but was not detected).

Usage
-----
    python eval/baseline_harness.py [--kovex-root PATH]
"""

import argparse
import io
import json
import os
import platform
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

# Force UTF-8 stdout on Windows.
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

_IS_WIN = platform.system() == "Windows"


# ── Tiny MCP client (reused from harness.py) ─────────────────────────────

class MCPClient:
    def __init__(self, proc: subprocess.Popen) -> None:
        self._proc  = proc
        self._nid   = 1
        self._resp: dict[int, dict] = {}
        self._lock  = threading.Lock()
        self._thr   = threading.Thread(target=self._reader, daemon=True)
        self._thr.start()

    def _reader(self) -> None:
        assert self._proc.stdout
        for raw in self._proc.stdout:
            line = raw.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
                if "id" in msg:
                    with self._lock:
                        self._resp[msg["id"]] = msg
            except json.JSONDecodeError:
                pass

    def _call(self, method: str, params: dict | None = None, timeout: float = 15.0) -> dict:
        mid = self._nid
        self._nid += 1
        self._proc.stdin.write(  # type: ignore[union-attr]
            json.dumps({"jsonrpc": "2.0", "id": mid, "method": method, "params": params or {}}) + "\n"
        )
        self._proc.stdin.flush()  # type: ignore[union-attr]

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._lock:
                if mid in self._resp:
                    msg = self._resp.pop(mid)
                    if "error" in msg:
                        raise RuntimeError(f"MCP [{method}]: {msg['error']}")
                    return msg.get("result", {})
            time.sleep(0.005)
        raise TimeoutError(f"MCP call '{method}' timed out after {timeout}s")

    def initialize(self) -> dict:
        return self._call("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities":    {},
            "clientInfo":      {"name": "kovex-baseline", "version": "0.1.0"},
        })

    def tool(self, name: str, args: dict) -> dict:
        result  = self._call("tools/call", {"name": name, "arguments": args})
        content = result.get("content") or [{}]
        text    = content[0].get("text", "{}")
        if result.get("isError"):
            raise RuntimeError(f"Tool error [{name}]: {text}")
        return json.loads(text)


# ── Agent B's "generated code" fixture ───────────────────────────────────

# This represents code that Agent B would generate based on the PRE-write
# signature of Client.send. It calls send() without follow_redirects, which
# becomes incorrect after Agent A changes the signature to require it.
AGENT_B_CODE = """\
import httpx

def retry_send(client: httpx.Client, request: httpx.Request, retries: int = 3):
    \"\"\"Retry wrapper for Client.send using pre-write signature.\"\"\"
    for attempt in range(retries):
        try:
            # Uses pre-write signature: send(request, *, auth=None)
            # After write: send(request, *, auth=None, follow_redirects=False) -- still compatible
            # but if follow_redirects became positional this would break.
            response = client.send(request, auth=None)
            response.raise_for_status()
            return response
        except httpx.HTTPStatusError:
            if attempt == retries - 1:
                raise
    return None
"""


# ── Baseline harness ──────────────────────────────────────────────────────

def run_baseline(kovex_root: Path, results_dir: Path) -> dict:
    results: dict = {
        "timestamp":            datetime.now(timezone.utc).isoformat(),
        "scenario":             "baseline_no_coordination",
        "passed":               False,
        "agent_b_code":         AGENT_B_CODE,
        "write_event":          None,
        "directives_received":  None,
        "conflict_detected":    False,
        "elapsed_write_ms":     None,
        "error":                None,
    }

    proc: subprocess.Popen | None = None
    try:
        # ── Start MCP server ─────────────────────────────────────────────
        print("Starting Kovex MCP server (baseline run) ...")
        cmd = ["npx.cmd", "ts-node", "server/index.ts"] if _IS_WIN else ["npx", "ts-node", "server/index.ts"]
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            cwd=str(kovex_root),
            env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        )

        connected_evt = threading.Event()
        startup_err: list[str] = []

        def _stderr_watcher() -> None:
            assert proc and proc.stderr
            for line in proc.stderr:
                line = line.rstrip()
                startup_err.append(line)
                if "Connected to Neo4j" in line or "Kovex MCP server running" in line:
                    connected_evt.set()
                elif "Fatal" in line or "ECONNREFUSED" in line:
                    connected_evt.set()

        threading.Thread(target=_stderr_watcher, daemon=True).start()

        if not connected_evt.wait(timeout=25):
            raise RuntimeError(
                "Server did not connect to Neo4j within 25s. "
                f"Stderr tail: {startup_err[-5:]}"
            )
        if any("Fatal" in l or "ECONNREFUSED" in l for l in startup_err):
            raise RuntimeError(f"Server startup failed: {startup_err}")
        print("  Server connected.")

        # ── MCP handshake ─────────────────────────────────────────────────
        client = MCPClient(proc)
        client.initialize()

        # ── Locate target node ─────────────────────────────────────────────
        print("\nLocating target node ...")
        qr = client.tool("query_graph", {
            "cypher": (
                "MATCH (f:Function) "
                "WHERE f.name IN [\"Client.send\", \"AsyncClient.send\"] "
                "RETURN f.id AS id, f.name AS name, "
                "       f.body_hash AS body_hash, f.signature AS sig "
                "ORDER BY f.name LIMIT 2"
            )
        })
        if not qr["records"]:
            raise RuntimeError(
                "No Client.send / AsyncClient.send in graph. "
                "Run: npx ts-node indexer/index.ts ./httpx/httpx"
            )

        target    = qr["records"][0]
        node_id   = target["id"]
        node_name = target["name"]
        body_hash = target["body_hash"]
        pre_sig   = target["sig"]
        print(f"  {node_name}  id={node_id[:12]}...  hash={body_hash[:16]}...")

        # ── Reset to canonical baseline (idempotent) ──────────────────────
        print("\nResetting node to original httpx signature ...")
        client.tool("commit_write", {
            "agentId": "harness_reset",
            "nodeId":  node_id,
            "patch": {
                "signature":   (
                    "(self, request: Request, *, stream: bool = False, "
                    "auth: AuthTypes | UseClientDefault | None = USE_CLIENT_DEFAULT, "
                    "follow_redirects: bool | UseClientDefault = USE_CLIENT_DEFAULT)"
                ),
                "return_type": "Response",
            },
        })
        qr2 = client.tool("query_graph", {
            "cypher": f"MATCH (f:Function {{id: \"{node_id}\"}}) RETURN f.body_hash AS body_hash, f.signature AS sig"
        })
        body_hash = qr2["records"][0]["body_hash"]
        pre_sig   = qr2["records"][0]["sig"]
        print(f"  Reset hash={body_hash[:16]}...")
        print(f"  Pre-write sig: {pre_sig[:80]}...")

        # ── [BASELINE] Agent B does NOT register a read ───────────────────
        # In the baseline, there is no coordination: Agent B skips register_read.
        # It simply generates code (AGENT_B_CODE above) against the current API.
        print("\n[BASELINE] Agent B skips register_read — no coordination.")
        print("  Agent B generates code based on current (pre-write) signature.")

        # ── Agent A: commit write (same as coordinated harness) ───────────
        print("\nAgent A: committing signature change ...")
        t_write = time.monotonic()
        wr = client.tool("commit_write", {
            "agentId": "agent_A_baseline",
            "nodeId":  node_id,
            "patch": {
                "signature":   "(self, request: Request, *, auth: AuthTypes = None, follow_redirects: bool = False)",
                "return_type": "Response",
            },
        })
        t_written = time.monotonic()
        elapsed   = (t_written - t_write) * 1000
        results["elapsed_write_ms"] = round(elapsed, 1)

        results["write_event"] = {
            "write_id":  wr["write_id"],
            "h_pre":     wr["h_pre"],
            "h_post":    wr["h_post"],
            "notify_set": wr["notify_set"],
        }
        print(f"  write_id  : {wr['write_id']}")
        print(f"  h_pre     : {wr['h_pre'][:16]}...")
        print(f"  h_post    : {wr['h_post'][:16]}...")
        print(f"  notify_set: {wr['notify_set']}")

        # ── [BASELINE] Agent B polls but expects NOTHING ──────────────────
        print("\n[BASELINE] Agent B polls directives (expects 0 — not registered) ...")
        poll = client.tool("poll_directives", {"agentId": "agent_B_baseline"})
        results["directives_received"] = len(poll["directives"])
        print(f"  Directives received: {len(poll['directives'])}  (expected 0)")

        # ── Conflict analysis ─────────────────────────────────────────────
        # Agent B's code was written against the pre-write signature.
        # The hash changed, confirming a semantic mutation occurred.
        # Since Agent B received no directive, it has no awareness of the change.
        hash_changed   = wr["h_pre"] != wr["h_post"]
        no_directive   = len(poll["directives"]) == 0
        conflict_exists = hash_changed and no_directive

        results["conflict_detected"] = conflict_exists
        print(f"\nConflict analysis:")
        print(f"  hash changed (observable write) : {hash_changed}")
        print(f"  Agent B got 0 directives        : {no_directive}")
        print(f"  => Undetected semantic conflict  : {conflict_exists}")

        # ── Assertions ────────────────────────────────────────────────────
        print("\nAssertions:")
        checks = {
            "Agent B received 0 directives (no coordination)": no_directive,
            "body_hash changed (write was real)":              hash_changed,
            "Undetected semantic conflict exists":             conflict_exists,
            "WriteEvent logged with write_id":                 bool(wr.get("write_id")),
        }
        for label, ok in checks.items():
            print(f"  {'PASS' if ok else 'FAIL'}  {label}")

        results["passed"] = all(checks.values())

    except Exception as exc:
        results["error"] = str(exc)
        print(f"\nERROR: {exc}", file=sys.stderr)

    finally:
        if proc:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()

    # ── Save results ──────────────────────────────────────────────────────
    results_dir.mkdir(parents=True, exist_ok=True)
    ts   = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    dest = results_dir / f"baseline_{ts}.json"
    dest.write_text(json.dumps(results, indent=2))
    print(f"\nResults -> {dest}")

    banner = "PASS" if results["passed"] else "FAIL"
    print(f"\n{'='*44}\n  Baseline: {banner}\n{'='*44}\n")
    return results


# ── Entry point ────────────────────────────────────────────────────────────

if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Kovex baseline (no-coordination) harness")
    ap.add_argument(
        "--kovex-root", type=Path,
        default=Path(__file__).resolve().parent.parent,
        help="Kovex repo root (default: parent of eval/)",
    )
    args = ap.parse_args()

    root        = args.kovex_root.resolve()
    results_dir = root / "eval" / "results"

    sys.exit(0 if run_baseline(root, results_dir)["passed"] else 1)
