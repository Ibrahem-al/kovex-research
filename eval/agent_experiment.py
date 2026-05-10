#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
eval/agent_experiment.py - Phase 4B: real Claude agents over Kovex.

Two Claude agents collaborate on the indexed httpx codebase through the
Kovex MCP server.

Authentication
--------------
Each agent turn is a non-interactive `claude -p` invocation, so calls go
through the user's logged-in Claude Code session (no ANTHROPIC_API_KEY
needed). See `--bare` notes in `claude --help`: we do NOT use --bare so
OAuth/keychain auth still applies.

Tool plumbing
-------------
The Claude Code CLI's native MCP integration spawns a fresh server
subprocess per invocation, which would wipe Kovex's in-memory registry
between Agent A and Agent B and break the coordination protocol. To keep
the registry shared across the whole experiment, this script:

  - Spawns ONE Kovex stdio MCP server up front.
  - Connects to it via the existing MCPClient JSON-RPC client.
  - Runs each agent turn through `claude -p` with --system-prompt set so
    the model emits text-format `<tool_call>...</tool_call>` blocks
    instead of trying to use native tools.
  - Parses each <tool_call>, dispatches against the shared MCPClient, and
    sends the result back as a follow-up user message wrapped in a
    `<tool_result>...</tool_result>` block.
  - Continues the conversation by re-invoking `claude -p` with the FULL
    history rendered as a single transcript (no --resume; --no-session-
    persistence keeps disk state clean).

After both agents have run for a (task, condition, run_idx) triple, we
extract Agent B's last <python_code>...</python_code> block and run mypy
against the post-write httpx source (using the existing _run_mypy_sandboxed
helper from conflict_check.py). The post-write mypy outcome is the primary
metric: if it passes, B avoided the semantic conflict; if it fails, B
emitted code that breaks under the new API.

Usage
-----
    python eval/agent_experiment.py
    python eval/agent_experiment.py --tasks T01,T03 --runs 1
    python eval/agent_experiment.py --conditions kovex
    python eval/agent_experiment.py --model claude-sonnet-4-6
"""

from __future__ import annotations

import argparse
import io
import json
import os
import platform
import re
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Generator

import psutil

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).resolve().parent))
from conflict_check import _find_mypy, _run_mypy_sandboxed  # noqa: E402
from agent_prompts import (  # noqa: E402
    AGENT_A_SYSTEM,
    AGENT_B_BASELINE_SYSTEM,
    AGENT_B_KOVEX_SYSTEM,
    TASK_SPECS,
    agent_a_user_message,
    agent_b_phase1_user_message,
    agent_b_phase2_user_message,
)

_IS_WIN = platform.system() == "Windows"

DEFAULT_MODEL = "claude-sonnet-4-6"
DEFAULT_TASKS = ["T01", "T03", "T05", "T09", "T13"]
DEFAULT_RUNS = 3
DEFAULT_CONDITIONS = ["kovex", "baseline"]

# Hard cap on agent turns per phase. Each turn is one `claude -p` call.
# Generous ceiling — Phase 1 needs ~2, Phase 2 needs ~3-4 typically.
MAX_AGENT_TURNS = 8

# Per-call timeout for `claude -p`. Sonnet typically returns in a few seconds
# but the cold cache miss + Kovex MCP roundtrip can push it higher.
CLI_CALL_TIMEOUT_S = 120

# All built-in Claude Code tools we want to deny so the model focuses on the
# experiment's text-format tool protocol. The CLI's --tools "" form would do
# this too, but PowerShell mangles empty-string args, so we deny by name.
DISALLOWED_BUILTIN_TOOLS = ",".join([
    "Bash", "Edit", "Write", "Read", "Grep", "Glob", "Task", "Agent",
    "WebFetch", "WebSearch", "NotebookEdit", "KillBash", "BashOutput",
    "SlashCommand", "ExitPlanMode", "TodoWrite", "MultiEdit",
])


# ── MCP client (stdio JSON-RPC, same shape as harness.py) ────────────────────

class MCPClient:
    """Minimal JSON-RPC client for the Kovex stdio MCP server."""

    def __init__(self, proc: subprocess.Popen) -> None:
        self._proc = proc
        self._nid = 1
        self._resp: dict[int, dict] = {}
        self._lock = threading.Lock()
        self._thr = threading.Thread(target=self._reader, daemon=True)
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
            json.dumps({
                "jsonrpc": "2.0", "id": mid, "method": method,
                "params": params or {},
            }) + "\n"
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
            "capabilities": {},
            "clientInfo": {"name": "kovex-agent-experiment", "version": "0.1.0"},
        })

    def tool(self, name: str, args: dict) -> dict:
        result = self._call("tools/call", {"name": name, "arguments": args})
        content = result.get("content") or [{}]
        text = content[0].get("text", "{}")
        if result.get("isError"):
            raise RuntimeError(f"Tool error [{name}]: {text}")
        return json.loads(text)


@contextmanager
def _server(kovex_root: Path) -> Generator[MCPClient, None, None]:
    """Spawn the Kovex MCP server as a subprocess and yield an initialized client."""
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
                f"MCP server did not start within 25s. Tail: {stderr_lines[-5:]}"
            )
        if any("Fatal" in l or "ECONNREFUSED" in l for l in stderr_lines):
            raise RuntimeError(f"Server startup failed: {stderr_lines}")
        client = MCPClient(proc)
        client.initialize()
        yield client
    finally:
        # Windows: npx.cmd spawns a node grandchild that holds file handles.
        # Enumerate descendants BEFORE terminating the parent — once the
        # parent dies, children get reparented and we lose the link.
        try:
            descendants = psutil.Process(proc.pid).children(recursive=True)
        except psutil.NoSuchProcess:
            descendants = []
        proc.terminate()
        for child in descendants:
            try:
                child.terminate()
            except psutil.NoSuchProcess:
                pass
        _, still = psutil.wait_procs(descendants, timeout=3)
        for child in still:
            try:
                child.kill()
            except psutil.NoSuchProcess:
                pass
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


# ── Helpers ──────────────────────────────────────────────────────────────────

def _resolve_node_by_id(client: MCPClient, node_id: str) -> tuple[str, str, str]:
    qr = client.tool("query_graph", {
        "cypher": (
            f"MATCH (f:Function {{id: \"{node_id}\"}}) "
            "RETURN f.body_hash AS body_hash, "
            "       f.signature AS signature, "
            "       f.return_type AS return_type LIMIT 1"
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


def _reset_node(client: MCPClient, node_id: str, sig: str, ret: str) -> str:
    client.tool("commit_write", {
        "agentId": "agent_experiment_reset",
        "nodeId":  node_id,
        "patch":   {"signature": sig, "return_type": ret},
    })
    qr = client.tool("query_graph", {
        "cypher": f"MATCH (f {{id: \"{node_id}\"}}) RETURN f.body_hash AS h"
    })
    return qr["records"][0]["h"]


def _drain_directives(client: MCPClient, agent_id: str) -> None:
    try:
        client.tool("poll_directives", {"agentId": agent_id})
    except Exception:
        pass


def _git_reset_httpx(kovex_root: Path) -> None:
    subprocess.run(
        ["git", "checkout", "--", "."],
        cwd=str(kovex_root / "httpx"), check=True, capture_output=True,
    )


# ── Tool-call extraction (text protocol) ─────────────────────────────────────

_TOOL_CALL_RE = re.compile(
    r"<tool_call>\s*(\{.*?\})\s*</tool_call>",
    flags=re.DOTALL | re.IGNORECASE,
)
_PYTHON_CODE_RE = re.compile(
    r"<python_code>\s*(.*?)\s*</python_code>",
    flags=re.DOTALL | re.IGNORECASE,
)


def extract_tool_call(text: str) -> tuple[dict | None, str]:
    """
    Return (parsed_first_tool_call, truncated_text).

    truncated_text is `text` cut off immediately after the closing
    </tool_call> tag of the first tool_call. If the model continued
    generating after the tag (a common failure mode where the model
    hallucinates a <tool_result> and final code in the same turn), that
    extra content is discarded so we can splice in the REAL tool_result.

    Returns (None, text) if no tool_call is present.
    """
    m = _TOOL_CALL_RE.search(text)
    if not m:
        return None, text
    truncated = text[: m.end()]
    raw = m.group(1).strip()
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError as exc:
        return {"_parse_error": str(exc), "_raw": raw}, truncated
    if not isinstance(obj, dict) or "name" not in obj:
        return ({"_parse_error": "tool_call JSON missing 'name' key", "_raw": raw},
                truncated)
    obj.setdefault("input", {})
    return obj, truncated


def extract_python_code(text: str) -> str | None:
    """Pull the LAST <python_code>...</python_code> block from a string."""
    matches = _PYTHON_CODE_RE.findall(text)
    if not matches:
        return None
    return matches[-1].strip()


_MYPY_LINE_RE = re.compile(r"^(?P<file>[^:\s][^:]*?):(?P<line>\d+):", re.MULTILINE)


def split_mypy_errors_by_file(mypy_output: str) -> dict[str, list[str]]:
    """
    Group mypy output lines by source filename. Lines that do not start with
    a 'file:line:' prefix (e.g. summary lines, mypy notes that wrap) are
    attached to the previous file they were reported for, or grouped under
    "" if no file context exists yet.
    """
    by_file: dict[str, list[str]] = {}
    current = ""
    for line in mypy_output.splitlines():
        m = _MYPY_LINE_RE.match(line)
        if m:
            current = m.group("file")
        by_file.setdefault(current, []).append(line)
    return by_file


def agent_code_failed_post_write(mypy_post_output: str) -> bool:
    """
    True iff Agent B's code (agent_b_code.py in the sandbox) has any line
    flagged in the post-write mypy output. Errors in the patched source
    file (httpx/_client.py) are an artifact of the partial source_patch —
    the patch removes a parameter from the function HEADER but leaves the
    function body referencing it, plus any internal callers — and they
    happen for every Agent B input. They do NOT reflect Agent B's correctness.
    """
    by_file = split_mypy_errors_by_file(mypy_post_output)
    for fname, lines in by_file.items():
        if "agent_b_code.py" in fname:
            # Any 'error:' line attached to agent_b_code.py is a real failure.
            if any(": error:" in ln for ln in lines):
                return True
    return False


# ── Allowed tool names per role ──────────────────────────────────────────────

ALLOWED_TOOLS_AGENT_A: set[str] = {"commit_write"}
ALLOWED_TOOLS_AGENT_B_KOVEX: set[str] = {
    "register_read", "poll_directives", "get_node", "query_graph",
}


# ── claude CLI invocation ────────────────────────────────────────────────────

def _claude_call(
    *,
    user_message: str,
    system_prompt: str | None,
    model: str,
    resume_session_id: str | None = None,
    cli_extra_args: list[str] | None = None,
) -> dict:
    """
    Invoke `claude -p` once and return the parsed JSON result.

    If resume_session_id is given, --resume is used to continue an existing
    conversation; in that case we must NOT pass --system-prompt (the system
    prompt was set when the session was created and persists across resumes;
    re-passing it has no effect but is wasted argv length).

    Sessions DO persist to disk so --resume works. We do not pass
    --no-session-persistence — that flag explicitly forbids resume.
    """
    args = [
        _claude_call._cmd,  # type: ignore[attr-defined]
        "-p",
        "--output-format", "json",
        "--model", model,
        "--disallowedTools", DISALLOWED_BUILTIN_TOOLS,
        # No MCP config: tool calls are text-format and dispatched harness-side.
    ]
    if resume_session_id is not None:
        args.extend(["--resume", resume_session_id])
    else:
        if system_prompt is None:
            raise ValueError(
                "system_prompt is required for the first call (no resume)."
            )
        args.extend(["--system-prompt", system_prompt])
    if cli_extra_args:
        args.extend(cli_extra_args)

    try:
        completed = subprocess.run(
            args,
            input=user_message,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=CLI_CALL_TIMEOUT_S,
        )
    except FileNotFoundError as exc:
        raise RuntimeError(f"`claude` CLI not on PATH: {exc}") from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(
            f"claude -p timed out after {CLI_CALL_TIMEOUT_S}s"
        ) from exc

    if completed.returncode != 0:
        raise RuntimeError(
            f"claude -p exited with code {completed.returncode}.\n"
            f"stderr: {completed.stderr.strip()[-2000:]}\n"
            f"stdout (first 2k): {completed.stdout[:2000]}"
        )

    stdout = completed.stdout.strip()
    if not stdout:
        raise RuntimeError(
            f"claude -p produced no stdout. stderr: {completed.stderr.strip()[-2000:]}"
        )

    # `--output-format json` returns a single JSON object on the last
    # non-empty line of stdout. Filter out any stderr-style chatter that
    # leaked to stdout (rare).
    last_line = stdout.splitlines()[-1].strip()
    try:
        data = json.loads(last_line)
    except json.JSONDecodeError as exc:
        # Try the entire stdout as a JSON blob (single-line case).
        try:
            data = json.loads(stdout)
        except json.JSONDecodeError:
            raise RuntimeError(
                f"Could not parse claude -p JSON output: {exc}\n"
                f"Last line was: {last_line[:500]}"
            )
    return data


_claude_call._cmd = "claude"  # type: ignore[attr-defined]


def _resolve_claude_cmd() -> None:
    """
    Pick the right claude binary. Python's subprocess.run on Windows does NOT
    apply PATHEXT search to argv[0] (that's a shell behavior), so we must
    pass an explicit filename. Prefer .exe → .cmd → bare in that order.
    """
    from shutil import which
    for candidate in ("claude.exe", "claude.cmd", "claude"):
        path = which(candidate)
        if path:
            _claude_call._cmd = path  # type: ignore[attr-defined]
            return
    raise RuntimeError(
        "Could not find `claude` CLI on PATH. "
        "Install Claude Code, or check `where.exe claude` / `which claude`."
    )


# ── Agent loop (text-format tool calls) ──────────────────────────────────────

def run_agent_loop(
    *,
    system_prompt: str,
    initial_user_message: str,
    allowed_tools: set[str],
    mcp: MCPClient,
    model: str,
    role_label: str,
    resume_session_id: str | None = None,
    max_turns: int = MAX_AGENT_TURNS,
) -> dict:
    """
    Drive a single agent through a multi-turn tool-use loop using
    `claude -p` and `--resume` for session continuity.

    First turn:
      - If resume_session_id is None: starts a fresh session with
        --system-prompt; subsequent turns --resume <captured session_id>.
      - If resume_session_id is given (e.g. Phase 2 resumes Phase 1's
        session): the first user message is sent into that session via
        --resume so Agent B has its own prior code in context.

    Returns a dict including `final_session_id` so the caller can chain
    later phases / agents off this run.
    """
    transcript: list[dict] = []
    tool_calls_log: list[dict] = []
    final_text = ""
    current_session_id = resume_session_id  # may be None on first turn
    final_session_id: str | None = resume_session_id

    next_user_message = initial_user_message

    for turn_idx in range(max_turns):
        try:
            result = _claude_call(
                user_message=next_user_message,
                system_prompt=system_prompt if current_session_id is None else None,
                model=model,
                resume_session_id=current_session_id,
            )
        except Exception as exc:
            transcript.append({
                "turn": turn_idx,
                "error": f"claude -p call failed: {exc}",
            })
            return {
                "turns":             turn_idx,
                "transcript":        transcript,
                "tool_calls":        tool_calls_log,
                "final_text":        final_text,
                "final_session_id":  final_session_id,
                "error":             str(exc),
            }

        assistant_text = (result.get("result") or "").strip()
        stop_reason = result.get("stop_reason")
        cost_usd = result.get("total_cost_usd")
        usage = result.get("usage") or {}
        session_id = result.get("session_id")

        # Update tracking. After the first call we have a session_id; we
        # keep using it for --resume on every subsequent turn.
        if session_id:
            current_session_id = session_id
            final_session_id = session_id

        # Detect tool_call up front so we can truncate hallucinated trailing
        # content (common failure: model emits the tool_call AND a fake
        # <tool_result> AND final code, all in one turn).
        tool_call, truncated_text = extract_tool_call(assistant_text)
        recorded_text = truncated_text if tool_call is not None else assistant_text

        transcript.append({
            "turn":          turn_idx,
            "role":          "assistant",
            "text":          recorded_text,
            "raw_full_text": (
                assistant_text if recorded_text != assistant_text else None
            ),
            "stop_reason":   stop_reason,
            "cost_usd":      cost_usd,
            "session_id":    session_id,
            "input_tokens":  usage.get("input_tokens"),
            "output_tokens": usage.get("output_tokens"),
        })

        # Only treat THIS turn's text as the final answer if the model did
        # NOT also issue a tool_call. A tool-call turn is by definition not
        # the final answer; any code in it was generated against a fake
        # tool_result the model hallucinated and must be discarded.
        if tool_call is None and assistant_text.strip():
            final_text = assistant_text

        if tool_call is None:
            break

        # Validate / dispatch the tool call.
        if "_parse_error" in tool_call:
            next_user_message = (
                f"<tool_result>\n"
                f'{{"name": "?", "is_error": true, '
                f'"result": "tool_call JSON parse error: '
                f'{tool_call["_parse_error"]}"}}\n'
                f"</tool_result>\n\n"
                f"Please re-issue the tool_call with valid JSON."
            )
            tool_calls_log.append({
                "turn": turn_idx, "name": "?", "input": None,
                "is_error": True, "result_preview": tool_call["_parse_error"],
                "elapsed_ms": 0.0,
            })
            continue

        name = tool_call["name"]
        tool_input = tool_call.get("input") or {}
        if name not in allowed_tools:
            next_user_message = (
                f"<tool_result>\n"
                f'{{"name": "{name}", "is_error": true, '
                f'"result": "tool {name!r} is not allowed for this role; '
                f'allowed tools: {sorted(allowed_tools)}"}}\n'
                f"</tool_result>"
            )
            tool_calls_log.append({
                "turn": turn_idx, "name": name, "input": tool_input,
                "is_error": True, "result_preview": "not allowed",
                "elapsed_ms": 0.0,
            })
            continue

        t0 = time.monotonic()
        try:
            tool_result = mcp.tool(name, tool_input)
            is_err = False
            payload = json.dumps(tool_result, default=str)
        except Exception as exc:
            tool_result = {"error": str(exc)}
            is_err = True
            payload = json.dumps(tool_result)
        elapsed_ms = (time.monotonic() - t0) * 1000

        tool_calls_log.append({
            "turn":           turn_idx,
            "name":           name,
            "input":          tool_input,
            "is_error":       is_err,
            "result_preview": payload[:1500],
            "elapsed_ms":     round(elapsed_ms, 2),
        })

        # If the model also emitted <python_code> in the same turn as the
        # tool_call (a protocol violation), our truncation discarded it.
        # Tell the model so it knows to re-emit its final answer rather
        # than referring to "the code above" (which is no longer there).
        discarded_code_hint = ""
        raw = transcript[-1].get("raw_full_text") or ""
        if raw and _PYTHON_CODE_RE.search(raw):
            discarded_code_hint = (
                "\n\n(Note: your previous turn included a <python_code> block "
                "in the same turn as a <tool_call>, which is a protocol "
                "violation — that code was discarded. Now that you have the "
                "real tool_result, please emit your final code in a fresh "
                "<python_code>...</python_code> block when you are ready.)"
            )

        next_user_message = (
            f"<tool_result>\n"
            f'{{"name": "{name}", "is_error": {str(is_err).lower()}, '
            f'"result": {payload}}}\n'
            f"</tool_result>"
            f"{discarded_code_hint}"
        )

    return {
        "turns":            len(transcript),
        "transcript":       transcript,
        "tool_calls":       tool_calls_log,
        "final_text":       final_text,
        "final_session_id": final_session_id,
        "error":            None,
    }


# ── Per-trial driver ─────────────────────────────────────────────────────────

def run_one_trial(
    *,
    mcp: MCPClient,
    model: str,
    task: dict,
    spec: dict,
    canonical: dict[str, tuple[str, str]],
    condition: str,
    run_idx: int,
    kovex_root: Path,
    mypy: list[str],
) -> dict:
    tid = task["task_id"]
    write_id_node = task["write_target_id"]
    b_reg_node = task["b_registration_id"]
    b_reg_name = task["b_registration_name"]
    write_target_name = task["write_target_name"]

    agent_a_id = f"agentA_{tid}_{condition}_r{run_idx}"
    agent_b_id = f"agentB_{tid}_{condition}_r{run_idx}"

    print(f"\n  ── trial: task={tid}  condition={condition}  run={run_idx}  ──")

    # Reset write target to its canonical PRE-WRITE signature. We use the
    # spec's pre_write_signature (not the live graph value) because the
    # graph state may be polluted by post-write residue from earlier runs:
    # commit_writes persist in Neo4j and there is no rollback. Driving the
    # reset from the spec guarantees h_pre != h_post when Agent A applies
    # the post-write signature.
    canonical_sig = spec["pre_write_signature"]
    canonical_ret = spec["return_type"]
    pre_hash = _reset_node(mcp, write_id_node, canonical_sig, canonical_ret)

    if b_reg_node == write_id_node:
        b_reg_hash = pre_hash
    else:
        # B's registration node may itself be another task's write target.
        # If we have a pre_write_signature for it in the canonical map, use
        # that for the reset. Otherwise its current graph state is fine —
        # bodyHash is stored as metadata in the registry and not checked
        # during BFS notification, so a mismatch is cosmetic only.
        b_sig, b_ret = canonical[b_reg_node]
        b_reg_hash = _reset_node(mcp, b_reg_node, b_sig, b_ret)
    _drain_directives(mcp, agent_b_id)

    trial: dict = {
        "task_id":           tid,
        "condition":         condition,
        "run_idx":           run_idx,
        "model":             model,
        "agent_a_id":        agent_a_id,
        "agent_b_id":        agent_b_id,
        "write_target_name": write_target_name,
        "b_reg_name":        b_reg_name,
        "expected_distance": task["expected_distance"],
        "expected_kovex_notifies": task["expected_kovex_notifies"],
        "timestamp":         datetime.now(timezone.utc).isoformat(),
        "phase1":            None,
        "agent_a":           None,
        "phase2":            None,
        "final_code":        None,
        "mypy":              None,
        "error":             None,
    }

    try:
        # ── Phase 1: Agent B initial implementation ───────────────────────
        b_phase1_msg = agent_b_phase1_user_message(
            task_id=tid,
            agent_id=agent_b_id,
            registration_node_id=b_reg_node,
            registration_node_name=b_reg_name,
            registration_body_hash=b_reg_hash,
            feature_brief=spec["feature_brief"],
            pre_write_signature=spec["pre_write_signature"],
            condition=condition,
        )
        if condition == "kovex":
            b_system = AGENT_B_KOVEX_SYSTEM
            allowed_b = ALLOWED_TOOLS_AGENT_B_KOVEX
        else:
            b_system = AGENT_B_BASELINE_SYSTEM
            allowed_b = set()  # baseline B has no tools

        print(f"    [Phase 1] Agent B ({condition}) generating ...")
        phase1 = run_agent_loop(
            system_prompt=b_system,
            initial_user_message=b_phase1_msg,
            allowed_tools=allowed_b,
            mcp=mcp,
            model=model,
            role_label="agentB_phase1",
        )
        trial["phase1"] = phase1
        phase1_code = extract_python_code(phase1["final_text"]) or ""

        # ── Agent A: commit the signature change ──────────────────────────
        a_msg = agent_a_user_message(
            task_id=tid,
            agent_id=agent_a_id,
            node_id=write_id_node,
            write_target_name=write_target_name,
            change_brief=spec["change_brief"],
            new_signature=spec["post_write_signature"],
            return_type=spec["return_type"],
        )
        print(f"    [Agent A] committing change to {write_target_name} ...")
        agent_a = run_agent_loop(
            system_prompt=AGENT_A_SYSTEM,
            initial_user_message=a_msg,
            allowed_tools=ALLOWED_TOOLS_AGENT_A,
            mcp=mcp,
            model=model,
            role_label="agentA",
            max_turns=4,
        )
        trial["agent_a"] = agent_a

        # ── Phase 2 (Kovex only): Agent B revises if notified ─────────────
        # Resume Phase 1's session so Agent B has its own Phase-1 code in
        # context and doesn't need to be told what to revise.
        final_code = phase1_code
        if condition == "kovex":
            b_phase2_msg = agent_b_phase2_user_message(
                task_id=tid, agent_id=agent_b_id,
            )
            phase1_session_id = phase1.get("final_session_id")
            if not phase1_session_id:
                print(
                    f"    WARNING: no Phase 1 session_id; Phase 2 will "
                    f"start fresh.", file=sys.stderr,
                )
            print(f"    [Phase 2] Agent B polling for directives ...")
            phase2 = run_agent_loop(
                system_prompt=b_system,
                initial_user_message=b_phase2_msg,
                allowed_tools=allowed_b,
                mcp=mcp,
                model=model,
                role_label="agentB_phase2",
                resume_session_id=phase1_session_id,
            )
            trial["phase2"] = phase2
            phase2_code = extract_python_code(phase2["final_text"])
            if phase2_code:
                final_code = phase2_code

        trial["final_code"] = final_code

        # ── Mypy check on the final code against POST-write httpx state ──
        cc_target_file = task["source_patch"]["target_file"]
        if cc_target_file.startswith("httpx/"):
            cc_target_file = cc_target_file[len("httpx/"):]
        source_patch = {
            "file":     cc_target_file,
            "old_text": task["source_patch"]["old_code"],
            "new_text": task["source_patch"]["new_code"],
        }

        if not final_code.strip():
            trial["mypy"] = {
                "agent_code_pre_passed":  None,
                "agent_code_post_passed": False,
                "raw_pre_passed":         None,
                "raw_post_passed":        False,
                "error": "Agent B produced no code (no <python_code>...</python_code> block).",
                "pre_write_output":  "",
                "post_write_output": "",
            }
        else:
            try:
                _git_reset_httpx(kovex_root)
                pre_ok, pre_out = _run_mypy_sandboxed(
                    mypy, final_code, kovex_root / "httpx" / "httpx",
                    source_patch=None,
                )
                post_ok, post_out = _run_mypy_sandboxed(
                    mypy, final_code, kovex_root / "httpx" / "httpx",
                    source_patch=source_patch,
                )
                # The "raw" booleans are mypy's verdict on the whole sandbox
                # (incl. patched _client.py). The post_write source patch
                # leaves dangling references in _client.py for every task,
                # so raw_post is always False — useless as a per-agent metric.
                # The agent_code_* booleans are the real signal: did any
                # mypy error attach to agent_b_code.py?
                pre_agent_failed = agent_code_failed_post_write(pre_out)
                post_agent_failed = agent_code_failed_post_write(post_out)
                trial["mypy"] = {
                    "agent_code_pre_passed":  not pre_agent_failed,
                    "agent_code_post_passed": not post_agent_failed,
                    "raw_pre_passed":         pre_ok,
                    "raw_post_passed":        post_ok,
                    "pre_write_output":       pre_out.strip(),
                    "post_write_output":      post_out.strip(),
                    "error": None,
                }
            except Exception as exc:
                trial["mypy"] = {
                    "agent_code_pre_passed":  None,
                    "agent_code_post_passed": False,
                    "raw_pre_passed":         None,
                    "raw_post_passed":        False,
                    "pre_write_output":  "",
                    "post_write_output": "",
                    "error": str(exc),
                }

        m = trial["mypy"] or {}
        print(
            f"    -> agent_code pre={m.get('agent_code_pre_passed')!s:<5}  "
            f"post={m.get('agent_code_post_passed')!s:<5}  "
            f"phase1_chars={len(phase1_code)}  final_chars={len(final_code)}"
        )

    except Exception as exc:
        trial["error"] = str(exc)
        print(f"    ERROR: {exc}", file=sys.stderr)

    return trial


# ── Experiment orchestration ─────────────────────────────────────────────────

def run_experiment(
    *,
    kovex_root: Path,
    results_dir: Path,
    task_ids: list[str],
    runs: int,
    conditions: list[str],
    model: str,
) -> dict:
    _resolve_claude_cmd()

    mypy = _find_mypy()
    print(f"mypy : {mypy}")
    print(f"model: {model}")
    print(f"tasks: {task_ids}  conditions: {conditions}  runs each: {runs}")

    tasks_path = kovex_root / "eval" / "tasks" / "httpx_tasks.json"
    data = json.loads(tasks_path.read_text(encoding="utf-8"))
    all_tasks = {t["task_id"]: t for t in data["tasks"]}
    missing = [tid for tid in task_ids if tid not in all_tasks]
    if missing:
        raise ValueError(f"Tasks not found in JSON: {missing}")
    missing_specs = [tid for tid in task_ids if tid not in TASK_SPECS]
    if missing_specs:
        raise ValueError(
            f"Tasks missing prompt specs in agent_prompts.TASK_SPECS: {missing_specs}"
        )

    selected = [all_tasks[tid] for tid in task_ids]

    httpx_pkg = kovex_root / "httpx" / "httpx"
    if not (httpx_pkg / "_client.py").exists():
        raise RuntimeError(f"httpx source not found at {httpx_pkg}")

    results_dir.mkdir(parents=True, exist_ok=True)
    all_trials: list[dict] = []
    started_at = datetime.now(timezone.utc)

    with _server(kovex_root) as mcp:
        print("MCP server connected.")

        # Build a per-node "pre-write" signature map. Priority:
        #   1) If the node is a write_target in some task, use that task's
        #      spec.pre_write_signature (immune to graph pollution).
        #   2) Otherwise (read-only nodes B registers on), fall back to
        #      the live graph value — bodyHash is metadata only.
        spec_pre_for_node: dict[str, tuple[str, str]] = {}
        for t in selected:
            spec = TASK_SPECS[t["task_id"]]
            spec_pre_for_node[t["write_target_id"]] = (
                spec["pre_write_signature"], spec["return_type"]
            )

        unique_nodes = sorted(
            {t["write_target_id"] for t in selected}
            | {t["b_registration_id"] for t in selected}
        )
        canonical: dict[str, tuple[str, str]] = {}
        for nid in unique_nodes:
            if nid in spec_pre_for_node:
                canonical[nid] = spec_pre_for_node[nid]
            else:
                _, sig, ret = _resolve_node_by_id(mcp, nid)
                canonical[nid] = (sig, ret)
        print(
            f"Cached canonical state for {len(canonical)} node(s) "
            f"({sum(1 for n in unique_nodes if n in spec_pre_for_node)} from spec)."
        )

        for task in selected:
            tid = task["task_id"]
            spec = TASK_SPECS[tid]

            try:
                _git_reset_httpx(kovex_root)
            except Exception as exc:
                print(f"  warn: git reset failed for {tid}: {exc}", file=sys.stderr)

            for condition in conditions:
                for run_idx in range(runs):
                    trial = run_one_trial(
                        mcp=mcp, model=model,
                        task=task, spec=spec, canonical=canonical,
                        condition=condition, run_idx=run_idx,
                        kovex_root=kovex_root, mypy=mypy,
                    )
                    all_trials.append(trial)

                    out_path = results_dir / (
                        f"{tid}_{condition}_run{run_idx}.json"
                    )
                    out_path.write_text(
                        json.dumps(trial, indent=2, default=str),
                        encoding="utf-8",
                    )

    try:
        _git_reset_httpx(kovex_root)
    except Exception:
        pass

    # ── Summary ──────────────────────────────────────────────────────────
    by_key: dict[tuple[str, str], list[dict]] = {}
    for t in all_trials:
        by_key.setdefault((t["task_id"], t["condition"]), []).append(t)

    summary_rows = []
    for (tid, cond), trials in sorted(by_key.items()):
        n = len(trials)
        n_pass = sum(
            1 for t in trials
            if (t.get("mypy") or {}).get("agent_code_post_passed") is True
        )
        n_err = sum(1 for t in trials if t.get("error"))
        summary_rows.append({
            "task_id":                       tid,
            "condition":                     cond,
            "runs":                          n,
            "agent_code_post_pass":          n_pass,
            "agent_code_post_pass_rate":    (n_pass / n) if n else 0.0,
            "errored":                       n_err,
        })

    summary = {
        "started_at":   started_at.isoformat(),
        "finished_at":  datetime.now(timezone.utc).isoformat(),
        "model":        model,
        "tasks":        task_ids,
        "conditions":   conditions,
        "runs_per_cell": runs,
        "n_trials":     len(all_trials),
        "summary":      summary_rows,
    }

    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    summary_path = results_dir / f"agent_experiment_summary_{ts}.json"
    summary_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")

    print("\n" + "=" * 64)
    print("  Phase 4B agent experiment summary")
    print("  " + "-" * 60)
    for row in summary_rows:
        print(
            f"    {row['task_id']:4} {row['condition']:8} "
            f"pass={row['agent_code_post_pass']}/{row['runs']}  "
            f"({row['agent_code_post_pass_rate']*100:5.1f}%)  "
            f"errored={row['errored']}"
        )
    print(f"  Summary -> {summary_path}")
    print("=" * 64 + "\n")
    return summary


# ── CLI ──────────────────────────────────────────────────────────────────────

def _split_csv(s: str) -> list[str]:
    return [x.strip() for x in s.split(",") if x.strip()]


def main() -> int:
    ap = argparse.ArgumentParser(description="Phase 4B Kovex agent experiment")
    ap.add_argument(
        "--kovex-root", type=Path,
        default=Path(__file__).resolve().parent.parent,
        help="Kovex repo root (default: parent of eval/)",
    )
    ap.add_argument(
        "--results-dir", type=Path, default=None,
        help="Where per-run JSONs go (default: <kovex-root>/eval/agent_results)",
    )
    ap.add_argument(
        "--tasks", default=",".join(DEFAULT_TASKS),
        help=f"Comma-separated task ids (default: {','.join(DEFAULT_TASKS)})",
    )
    ap.add_argument(
        "--runs", type=int, default=DEFAULT_RUNS,
        help=f"Number of runs per (task, condition) cell (default: {DEFAULT_RUNS})",
    )
    ap.add_argument(
        "--conditions", default=",".join(DEFAULT_CONDITIONS),
        help=f"Comma-separated conditions (default: {','.join(DEFAULT_CONDITIONS)})",
    )
    ap.add_argument(
        "--model", default=DEFAULT_MODEL,
        help=f"Anthropic model id (default: {DEFAULT_MODEL})",
    )
    args = ap.parse_args()

    kovex_root = args.kovex_root.resolve()
    results_dir = (
        args.results_dir.resolve() if args.results_dir
        else kovex_root / "eval" / "agent_results"
    )

    summary = run_experiment(
        kovex_root=kovex_root,
        results_dir=results_dir,
        task_ids=_split_csv(args.tasks),
        runs=args.runs,
        conditions=_split_csv(args.conditions),
        model=args.model,
    )

    return 0 if summary["n_trials"] > 0 else 1


if __name__ == "__main__":
    sys.exit(main())
