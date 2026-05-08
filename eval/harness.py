#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
eval/harness.py -- Kovex evaluation harness.

Two modes
---------
Default (Phase 4 experiment loop):
    Runs every task in eval/tasks/httpx_tasks.json as both a Kovex-coordinated
    and a baseline (no-coordination) scenario.  Saves one JSON per task to
    eval/results/<task_id>_result.json.

Demo (--demo):
    The original two-agent scenario against AsyncClient.send / Client.send.

Usage
-----
    python eval/harness.py                        # Phase 4 loop (all tasks)
    python eval/harness.py --task t03             # single task from the loop
    python eval/harness.py --demo                 # original demo
    python eval/harness.py --kovex-root PATH
"""

import argparse
import hashlib
import io
import json
import os
import platform
import shutil
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Generator

# Force UTF-8 stdout on Windows.
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

# Ensure eval/ is on sys.path so we can import conflict_check.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from conflict_check import _find_mypy, check_task_conflict  # noqa: E402

_IS_WIN = platform.system() == "Windows"


# ── MCP client ────────────────────────────────────────────────────────────────

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
            json.dumps({"jsonrpc": "2.0", "id": mid, "method": method,
                        "params": params or {}}) + "\n"
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
            "clientInfo":      {"name": "kovex-harness", "version": "0.1.0"},
        })

    def tool(self, name: str, args: dict) -> dict:
        result  = self._call("tools/call", {"name": name, "arguments": args})
        content = result.get("content") or [{}]
        text    = content[0].get("text", "{}")
        if result.get("isError"):
            raise RuntimeError(f"Tool error [{name}]: {text}")
        return json.loads(text)


# ── Server lifecycle ──────────────────────────────────────────────────────────

@contextmanager
def _server(kovex_root: Path) -> Generator[MCPClient, None, None]:
    """Start the MCP server, yield an initialized MCPClient, then shut it down."""
    cmd = (["npx.cmd", "ts-node", "server/index.ts"] if _IS_WIN
           else ["npx", "ts-node", "server/index.ts"])
    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, bufsize=1, cwd=str(kovex_root),
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
    )
    connected = threading.Event()
    stderr_lines: list[str] = []

    def _watch_stderr() -> None:
        assert proc.stderr
        for line in proc.stderr:
            line = line.rstrip()
            stderr_lines.append(line)
            if "Connected to Neo4j" in line or "Kovex MCP server running" in line:
                connected.set()
            elif "Fatal" in line or "ECONNREFUSED" in line:
                connected.set()

    threading.Thread(target=_watch_stderr, daemon=True).start()
    try:
        if not connected.wait(timeout=25):
            raise RuntimeError(
                f"Server did not connect within 25s. Tail: {stderr_lines[-5:]}"
            )
        if any("Fatal" in l or "ECONNREFUSED" in l for l in stderr_lines):
            raise RuntimeError(f"Server startup failed: {stderr_lines}")
        client = MCPClient(proc)
        client.initialize()
        yield client
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


# ── Helpers ───────────────────────────────────────────────────────────────────

def _resolve_node(client: MCPClient, name: str) -> tuple[str, str, str, str]:
    """Return (id, body_hash, signature, return_type) for the first Function node with the given name."""
    qr = client.tool("query_graph", {
        "cypher": (
            f"MATCH (f:Function {{name: \"{name}\"}}) "
            "RETURN f.id AS id, f.body_hash AS body_hash, "
            "       f.signature AS signature, f.return_type AS return_type LIMIT 1"
        )
    })
    if not qr["records"]:
        raise RuntimeError(f"Function node not found in graph: {name!r}")
    rec = qr["records"][0]
    return (
        rec["id"],
        rec["body_hash"] or "",
        rec.get("signature") or "",
        rec.get("return_type") or "",
    )


def _resolve_node_by_id(client: MCPClient, node_id: str) -> tuple[str, str, str]:
    """Return (body_hash, signature, return_type) for the Function node with the given id."""
    qr = client.tool("query_graph", {
        "cypher": (
            f"MATCH (f:Function {{id: \"{node_id}\"}}) "
            "RETURN f.body_hash AS body_hash, "
            "       f.signature AS signature, f.return_type AS return_type LIMIT 1"
        )
    })
    if not qr["records"]:
        raise RuntimeError(f"Function node not found in graph: id={node_id!r}")
    rec = qr["records"][0]
    return (
        rec["body_hash"] or "",
        rec.get("signature") or "",
        rec.get("return_type") or "",
    )


def _reset_node(client: MCPClient, node_id: str, patch: dict) -> str:
    """Apply patch to reset node to canonical pre-write state; return new body_hash."""
    client.tool("commit_write", {
        "agentId": "harness_reset",
        "nodeId":  node_id,
        "patch":   patch,
    })
    qr = client.tool("query_graph", {
        "cypher": f"MATCH (f {{id: \"{node_id}\"}}) RETURN f.body_hash AS h"
    })
    return qr["records"][0]["h"]


def _git_reset_httpx(kovex_root: Path) -> None:
    """Reset httpx/ source tree to last committed state.

    httpx/ is its own git clone (untracked in the outer kovex_research repo),
    so the reset must run from inside httpx/ rather than the kovex root.
    """
    subprocess.run(
        ["git", "checkout", "--", "."],
        cwd=str(kovex_root / "httpx"), check=True, capture_output=True,
    )


def _banner(label: str, passed: bool) -> None:
    status = "PASS" if passed else "FAIL"
    print(f"\n{'='*44}\n  {label}: {status}\n{'='*44}")


# ── Single-task experiment ────────────────────────────────────────────────────

def run_task_experiment(
    client: MCPClient,
    task: dict,
    kovex_root: Path,
    mypy: list[str],
    results_dir: Path,
    canonical_state: dict[str, tuple[str, str]],
) -> dict:
    """
    Run one task from httpx_tasks.json as both Kovex and Baseline scenarios.
    Returns the combined result dict and saves it to results_dir.

    canonical_state maps write_target_id -> (signature, return_type) captured
    at experiment start, before any commit_write calls. Used to build orig_patch
    so successive tasks targeting the same node still reset to canonical state
    rather than the post-write state of an earlier task.
    """
    tid              = task["task_id"]
    write_name       = task["write_target_name"]
    b_name           = task["b_registration_name"]
    src_patch        = task["source_patch"]
    agent_b_code     = task["agent_b_code"]
    expect_notify    = task["expected_kovex_notifies"]
    expect_dist      = task["expected_distance"]
    conflict_type    = task.get("conflict_type", task.get("group", "unknown"))

    # Convert JSON's source_patch keys (target_file/old_code/new_code) to the
    # shape conflict_check.check_task_conflict expects (file/old_text/new_text).
    # The 'file' must be relative to the inner httpx package, so strip any
    # leading "httpx/" repo-prefix from target_file.
    cc_target_file = src_patch["target_file"]
    if cc_target_file.startswith("httpx/"):
        cc_target_file = cc_target_file[len("httpx/"):]
    cc_source_patch = {
        "file":     cc_target_file,
        "old_text": src_patch["old_code"],
        "new_text": src_patch["new_code"],
    }

    print(f"\n{'─'*55}")
    print(f"  Task {tid}: {task['description'][:70]}")
    print(f"  write target: {write_name}  |  conflict_type: {conflict_type}")
    print(f"{'─'*55}")

    result: dict = {
        "task_id":          tid,
        "write_target_name": write_name,
        "conflict_type":    conflict_type,
        "timestamp":        datetime.now(timezone.utc).isoformat(),
        "kovex":            None,
        "baseline":         None,
        "error":            None,
    }

    try:
        # Use the canonical IDs from the JSON; multiple Function nodes can share
        # the same name (e.g. when the indexer was run with different roots),
        # so name-only lookup is non-deterministic. The JSON pins specific IDs.
        write_id = task["write_target_id"]
        b_reg_id = task["b_registration_id"]
        # Pull (sig, ret) from the cache captured at experiment start, *not* from
        # the live graph: by the time later tasks run, the graph has been mutated
        # by earlier commit_writes, so a fresh _resolve_node_by_id would return
        # the post-write state of an earlier task as if it were canonical.
        write_sig, write_ret = canonical_state[write_id]

        # Derive Neo4j patches from source_patch.
        # commit_write recomputes Function body_hash as sha256(signature + '\x00' + return_type)
        # server-side, so we can't pass body_hash directly. Instead, encode a hash of new_code
        # into the signature so the resulting body_hash is deterministically derived from new_code
        # and reliably differs from the canonical state.
        new_code      = src_patch.get("new_code", "")
        new_code_hash = hashlib.sha256(new_code.encode("utf-8")).hexdigest()

        orig_patch  = {"signature": write_sig, "return_type": write_ret}
        write_patch = {
            "signature":   f"kovex_write::{new_code_hash}::{new_code}",
            "return_type": write_ret,
        }

        # ── Kovex scenario ────────────────────────────────────────────────
        print(f"\n  [Kovex] Resetting {write_name} to pre-write state ...")
        pre_hash = _reset_node(client, write_id, orig_patch)

        print(f"  [Kovex] Agent B registers read on {b_name}  hash={pre_hash[:12]}...")
        client.tool("register_read", {
            "agentId":  f"agent_B_{tid}",
            "nodeId":   b_reg_id,
            "bodyHash": pre_hash,
        })

        print(f"  [Kovex] Agent A commits write to {write_name} ...")
        t0 = time.monotonic()
        wr = client.tool("commit_write", {
            "agentId": f"agent_A_{tid}",
            "nodeId":  write_id,
            "patch":   write_patch,
        })
        poll = client.tool("poll_directives", {"agentId": f"agent_B_{tid}"})
        elapsed_ms = (time.monotonic() - t0) * 1000

        print(f"  [Kovex] Directives: {len(poll['directives'])}  ({elapsed_ms:.0f} ms)")
        for d in poll["directives"]:
            print(f"    writeId={str(d.get('writeId','?'))[:8]}...  "
                  f"triggerNode={str(d.get('triggerNode','?'))[:12]}...  "
                  f"dist={d.get('distance','?')}")

        print(f"  [Kovex] Conflict check (mypy) ...")
        _git_reset_httpx(kovex_root)
        cc_kovex = check_task_conflict(agent_b_code, kovex_root / "httpx" / "httpx",
                                       cc_source_patch, mypy)

        kov_checks: dict[str, bool] = {
            "h_pre != h_post":          wr["h_pre"] != wr["h_post"],
            "WriteEvent has write_id":  bool(wr.get("write_id")),
        }
        if expect_notify:
            kov_checks["Agent B got >= 1 directive"] = len(poll["directives"]) >= 1
            kov_checks[f"Directive distance == {expect_dist}"] = any(
                d.get("distance") == expect_dist for d in poll["directives"]
            )
            kov_checks["Notified within 500 ms"] = elapsed_ms < 500
        kov_checks["Conflict classified"] = bool(cc_kovex["conflict_classified"])

        result["kovex"] = {
            "scenario":        f"kovex_{write_name}",
            "passed":          all(kov_checks.values()),
            "checks":          {k: bool(v) for k, v in kov_checks.items()},
            "elapsed_notify_ms": round(elapsed_ms, 1),
            "directives":      poll["directives"],
            "write_event":     {
                "write_id":   wr["write_id"],
                "h_pre":      wr["h_pre"],
                "h_post":     wr["h_post"],
                "notify_set": wr["notify_set"],
            },
            "conflict_check":  cc_kovex,
        }
        kov_pass = result["kovex"]["passed"]
        print(f"  [Kovex] checks: "
              + "  ".join(f"{'OK' if v else 'FAIL'} {k}" for k, v in kov_checks.items()))

        # Deregister Agent B before moving on so its read does not leak into
        # later tasks' notify_set computations (the registry is in-memory and
        # persists across all tasks within a single MCP server lifetime).
        try:
            client.tool("deregister_read", {
                "agentId": f"agent_B_{tid}",
                "nodeId":  b_reg_id,
            })
        except Exception as e:
            print(f"  WARNING: deregister_read failed for {tid}: {e}", file=sys.stderr)

        # ── Baseline scenario ─────────────────────────────────────────────
        print(f"\n  [Baseline] Resetting {write_name} ...")
        _reset_node(client, write_id, orig_patch)
        # Agent B does NOT register a read — no coordination.

        print(f"  [Baseline] Agent A commits write (no B registration) ...")
        t1 = time.monotonic()
        wr2 = client.tool("commit_write", {
            "agentId": f"agent_A_base_{tid}",
            "nodeId":  write_id,
            "patch":   write_patch,
        })
        poll2 = client.tool("poll_directives", {"agentId": f"agent_B_base_{tid}"})
        elapsed2 = (time.monotonic() - t1) * 1000

        print(f"  [Baseline] Conflict check (mypy) ...")
        _git_reset_httpx(kovex_root)
        cc_base = check_task_conflict(agent_b_code, kovex_root / "httpx" / "httpx",
                                      cc_source_patch, mypy)

        base_checks: dict[str, bool] = {
            "h_pre != h_post":                          wr2["h_pre"] != wr2["h_post"],
            "Agent B got 0 directives (no coordination)": len(poll2["directives"]) == 0,
            "Undetected semantic conflict":             (
                wr2["h_pre"] != wr2["h_post"] and len(poll2["directives"]) == 0
            ),
            "Conflict classified by mypy":              bool(cc_base["conflict_classified"]),
        }

        result["baseline"] = {
            "scenario":       f"baseline_{write_name}",
            "passed":         all(base_checks.values()),
            "checks":         {k: bool(v) for k, v in base_checks.items()},
            "directives":     poll2["directives"],
            "write_event":    {
                "write_id":   wr2["write_id"],
                "h_pre":      wr2["h_pre"],
                "h_post":     wr2["h_post"],
                "notify_set": wr2["notify_set"],
            },
            "conflict_check": cc_base,
        }
        base_pass = result["baseline"]["passed"]
        print(f"  [Baseline] checks: "
              + "  ".join(f"{'OK' if v else 'FAIL'} {k}" for k, v in base_checks.items()))

        overall = kov_pass and base_pass
        print(f"\n  Task {tid}: {'PASS' if overall else 'FAIL'}  "
              f"(kovex={'PASS' if kov_pass else 'FAIL'}  "
              f"baseline={'PASS' if base_pass else 'FAIL'})")

    except Exception as exc:
        result["error"] = str(exc)
        print(f"\n  ERROR in task {tid}: {exc}", file=sys.stderr)

    # Restore source after last conflict check
    try:
        _git_reset_httpx(kovex_root)
    except Exception:
        pass

    # Save per-task result
    results_dir.mkdir(parents=True, exist_ok=True)
    dest = results_dir / f"{tid}_result.json"
    dest.write_text(json.dumps(result, indent=2))
    print(f"  Result -> {dest}")

    return result


# ── Phase 4 experiment loop ───────────────────────────────────────────────────

def run_experiment(
    kovex_root: Path,
    results_dir: Path,
    task_filter: str | None = None,
) -> dict:
    """
    Main Phase 4 loop: iterate all tasks, run Kovex + Baseline for each.
    Returns a summary dict.
    """
    tasks_path = kovex_root / "eval" / "tasks" / "httpx_tasks.json"
    data = json.loads(tasks_path.read_text(encoding="utf-8"))
    tasks: list[dict] = data["tasks"]

    if task_filter:
        tasks = [t for t in tasks if t["task_id"].upper() == task_filter.upper()]
        if not tasks:
            raise ValueError(f"Task {task_filter!r} not found in {tasks_path}")

    mypy = _find_mypy()
    print(f"mypy: {mypy}")
    print(f"Running {len(tasks)} task(s) ...")

    # Verify the httpx layout we'll be patching / git-resetting against.
    httpx_repo = kovex_root / "httpx"
    httpx_pkg  = httpx_repo / "httpx"
    print(f"  httpx repo dir   : {httpx_repo}  (exists={httpx_repo.exists()})")
    print(f"  httpx package dir: {httpx_pkg}  (exists={httpx_pkg.exists()})")
    if not (httpx_pkg / "_client.py").exists():
        print(f"  WARNING: {httpx_pkg / '_client.py'} not found — conflict_check will fail",
              file=sys.stderr)

    task_results: list[dict] = []
    with _server(kovex_root) as client:
        print("  MCP server connected.")

        # Capture canonical (signature, return_type) for every unique write
        # target *before* any commit_write runs. This snapshot is what each
        # task's orig_patch resets to, immune to later in-loop mutations.
        unique_write_ids = sorted({t["write_target_id"] for t in tasks})
        canonical_state: dict[str, tuple[str, str]] = {}
        for nid in unique_write_ids:
            _, sig, ret = _resolve_node_by_id(client, nid)
            canonical_state[nid] = (sig, ret)
        print(f"  Canonical state cached for {len(canonical_state)} write target(s).")

        for task in tasks:
            # Ensure httpx source is clean at start of each task
            try:
                _git_reset_httpx(kovex_root)
            except Exception as e:
                print(f"  WARNING: git reset failed: {e}", file=sys.stderr)

            tr = run_task_experiment(
                client, task, kovex_root, mypy, results_dir, canonical_state,
            )
            task_results.append(tr)

    # Final git reset
    try:
        _git_reset_httpx(kovex_root)
    except Exception:
        pass

    # Summary
    passed = [r for r in task_results if r.get("error") is None
              and (r.get("kovex") or {}).get("passed")
              and (r.get("baseline") or {}).get("passed")]
    errored = [r for r in task_results if r.get("error")]

    summary = {
        "timestamp":    datetime.now(timezone.utc).isoformat(),
        "total_tasks":  len(task_results),
        "passed":       len(passed),
        "failed":       len(task_results) - len(passed),
        "errored":      len(errored),
        "task_results": [
            {
                "id":             r["task_id"],
                "write_target":   r["write_target_name"],
                "kovex_pass":     (r.get("kovex") or {}).get("passed"),
                "baseline_pass":  (r.get("baseline") or {}).get("passed"),
                "error":          r.get("error"),
            }
            for r in task_results
        ],
    }

    ts   = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    dest = results_dir / f"experiment_summary_{ts}.json"
    results_dir.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(summary, indent=2))

    print(f"\n{'='*55}")
    print(f"  Phase 4 summary: {len(passed)}/{len(task_results)} tasks fully passed")
    for r in task_results:
        kp = (r.get("kovex") or {}).get("passed")
        bp = (r.get("baseline") or {}).get("passed")
        err = r.get("error")
        status = "ERROR" if err else ("PASS" if kp and bp else "FAIL")
        print(f"    {r['task_id']:4}  {r['write_target_name']:<45}  {status}")
    print(f"  Summary -> {dest}")
    print(f"{'='*55}\n")
    return summary


# ── Original demo (--demo) ────────────────────────────────────────────────────

def run_harness(kovex_root: Path, results_dir: Path) -> dict:
    """
    Original two-agent demo: Agent B reads AsyncClient.send,
    Agent A writes it, we assert B is notified within 500 ms.
    """
    results: dict = {
        "timestamp":         datetime.now(timezone.utc).isoformat(),
        "scenario":          None,   # filled in after node lookup
        "passed":            False,
        "elapsed_notify_ms": None,
        "write_event":       None,
        "directives":        [],
        "error":             None,
    }

    with _server(kovex_root) as client:
        print("  Server connected to Neo4j.")
        info = client.initialize()
        print(f"  Initialized: {info.get('serverInfo', {})}")

        # Locate target node
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
        print(f"  {node_name}  id={node_id[:12]}...  hash={body_hash[:16]}...")

        # Fix: use actual node name in scenario label
        results["scenario"] = f"agent_A_writes_{node_name}"

        # Reset to canonical baseline
        print(f"\nResetting {node_name} to original httpx signature ...")
        pre_hash = _reset_node(client, node_id, {
            "signature": (
                "(self, request: Request, *, stream: bool = False, "
                "auth: AuthTypes | UseClientDefault | None = USE_CLIENT_DEFAULT, "
                "follow_redirects: bool | UseClientDefault = USE_CLIENT_DEFAULT)"
            ),
            "return_type": "Response",
        })
        print(f"  Reset hash={pre_hash[:16]}...")

        # Agent B: register read
        print(f"\nAgent B: registering read on {node_name}")
        client.tool("register_read", {
            "agentId":  "agent_B",
            "nodeId":   node_id,
            "bodyHash": pre_hash,
        })
        read_list = client.tool("list_reads", {"agentId": "agent_B"})
        print(f"  ReadSet size: {len(read_list['reads'])}")

        # Agent A: commit write
        print("\nAgent A: committing signature change ...")
        t_write = time.monotonic()
        wr = client.tool("commit_write", {
            "agentId": "agent_A",
            "nodeId":  node_id,
            "patch": {
                "signature":   "(self, request: Request, *, auth: AuthTypes = None, "
                               "follow_redirects: bool = False)",
                "return_type": "Response",
            },
        })

        # Agent B: poll directives
        print("\nAgent B: polling directives ...")
        poll    = client.tool("poll_directives", {"agentId": "agent_B"})
        elapsed = (time.monotonic() - t_write) * 1000

        results["elapsed_notify_ms"] = round(elapsed, 1)
        results["directives"]        = poll["directives"]
        results["write_event"]       = {
            "write_id":   wr["write_id"],
            "h_pre":      wr["h_pre"],
            "h_post":     wr["h_post"],
            "notify_set": wr["notify_set"],
        }

        print(f"  write_id : {wr['write_id']}")
        print(f"  h_pre    : {wr['h_pre'][:16]}...")
        print(f"  h_post   : {wr['h_post'][:16]}...")
        print(f"  notify_set: {wr['notify_set']}")
        print(f"\n  Received {len(poll['directives'])} directive(s)  ({elapsed:.0f} ms)")
        for d in poll["directives"]:
            print(f"    writeId={str(d.get('writeId','?'))[:8]}...  "
                  f"triggerNode={str(d.get('triggerNode','?'))[:12]}...  "
                  f"dist={d.get('distance','?')}")

        checks = {
            "Agent B received >= 1 directive":              len(poll["directives"]) >= 1,
            f"Notification within 500 ms ({elapsed:.0f} ms)": elapsed < 500,
            "body_hash changed (h_pre != h_post)":         wr["h_pre"] != wr["h_post"],
            "WriteEvent has a write_id":                   bool(wr.get("write_id")),
        }
        print("\nAssertions:")
        for label, ok in checks.items():
            print(f"  {'PASS' if ok else 'FAIL'}  {label}")
        results["passed"] = all(checks.values())

    results_dir.mkdir(parents=True, exist_ok=True)
    ts   = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    dest = results_dir / f"harness_{ts}.json"
    dest.write_text(json.dumps(results, indent=2))
    print(f"\nResults -> {dest}")
    _banner("Harness", results["passed"])
    return results


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Kovex evaluation harness")
    ap.add_argument(
        "--kovex-root", type=Path,
        default=Path(__file__).resolve().parent.parent,
        help="Kovex repo root (default: parent of eval/)",
    )
    ap.add_argument(
        "--demo", action="store_true",
        help="Run the original single-scenario demo instead of the Phase 4 loop",
    )
    ap.add_argument(
        "--task", default=None,
        help="Run only this task ID from the experiment loop (e.g. t03)",
    )
    args = ap.parse_args()
    root        = args.kovex_root.resolve()
    results_dir = root / "eval" / "results"

    if args.demo:
        sys.exit(0 if run_harness(root, results_dir)["passed"] else 1)
    else:
        summary = run_experiment(root, results_dir, task_filter=args.task)
        sys.exit(0 if summary["failed"] == 0 and summary["errored"] == 0 else 1)
