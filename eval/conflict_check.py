#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
eval/conflict_check.py - Semantic conflict detector (Definition 2).

Definition 2 (from Phase 2 paper)
-----------------------------------
A semantic conflict between Agent A (writer) and Agent B (reader) exists if:
  1. Agent A committed an observable write to node N  (body_hash changed)
  2. Agent B had a registered read on N (or a k-hop neighbor) before the write
  3. Agent B generated code C_B after its read registration
  4. Valid(C_B, pre_write_state) == True  AND
     Valid(C_B, post_write_state) == False

This script operationalizes condition 4:
  - Patches the target httpx file to the pre-write signature, runs mypy -> must pass
  - Patches the target httpx file to the post-write signature, runs mypy -> must fail
  - The mypy failure must reference the patched function name

Usage
-----
    python eval/conflict_check.py [--kovex-root PATH] [--results-file PATH]
    python eval/conflict_check.py --results-file eval/results/baseline_<ts>.json
"""

from __future__ import annotations

import argparse
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
from datetime import datetime, timezone
from pathlib import Path

# Force UTF-8 stdout on Windows.
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

# ── Signatures ─────────────────────────────────────────────────────────────

# Original httpx signature (pre-write state)
PRE_WRITE_SIG = (
    "    def send(\n"
    "        self,\n"
    "        request: Request,\n"
    "        *,\n"
    "        stream: bool = False,\n"
    "        auth: AuthTypes | UseClientDefault | None = USE_CLIENT_DEFAULT,\n"
    "        follow_redirects: bool | UseClientDefault = USE_CLIENT_DEFAULT,\n"
    "    ) -> Response:"
)

# Post-write signature (after Agent A's commit_write patch)
POST_WRITE_SIG = (
    "    def send(\n"
    "        self,\n"
    "        request: Request,\n"
    "        *,\n"
    "        auth: AuthTypes = None,\n"
    "        follow_redirects: bool = False,\n"
    "    ) -> Response:"
)

# Async version (same pattern, used in AsyncClient)
PRE_WRITE_ASYNC_SIG = (
    "    async def send(\n"
    "        self,\n"
    "        request: Request,\n"
    "        *,\n"
    "        stream: bool = False,\n"
    "        auth: AuthTypes | UseClientDefault | None = USE_CLIENT_DEFAULT,\n"
    "        follow_redirects: bool | UseClientDefault = USE_CLIENT_DEFAULT,\n"
    "    ) -> Response:"
)

POST_WRITE_ASYNC_SIG = (
    "    async def send(\n"
    "        self,\n"
    "        request: Request,\n"
    "        *,\n"
    "        auth: AuthTypes = None,\n"
    "        follow_redirects: bool = False,\n"
    "    ) -> Response:"
)

# ── Agent B's code (same fixture as baseline_harness.py) ──────────────────

# This code is valid against the pre-write signature (stream is available)
# and becomes invalid after the write removes the `stream` parameter.
AGENT_B_CODE = textwrap.dedent("""\
    import httpx

    def retry_send(client: httpx.Client, request: httpx.Request, retries: int = 3) -> httpx.Response:
        \"\"\"Retry wrapper using pre-write Client.send signature.\"\"\"
        for attempt in range(retries):
            try:
                # 'stream' keyword exists in pre-write signature; removed post-write.
                response = client.send(request, stream=False, auth=None)
                response.raise_for_status()
                return response
            except httpx.HTTPStatusError:
                if attempt == retries - 1:
                    raise
        raise RuntimeError("unreachable")
""")

# ── Helpers ────────────────────────────────────────────────────────────────

def _find_mypy() -> list[str]:
    """Return command list to invoke mypy."""
    for name in ("mypy", "mypy.exe"):
        found = shutil.which(name)
        if found:
            return [found]
    # Fallback: run as a module via the current Python interpreter
    try:
        subprocess.run(
            [sys.executable, "-m", "mypy", "--version"],
            check=True, capture_output=True
        )
        return [sys.executable, "-m", "mypy"]
    except subprocess.CalledProcessError:
        pass
    raise RuntimeError(
        "mypy not found. Install it: pip install mypy"
    )


def _run_mypy_in_sandbox(
    mypy: list[str],
    agent_b_code: str,
    httpx_src: Path,
    apply_post_write: bool,
) -> tuple[bool, str]:
    """Legacy single-task sandbox using the hardcoded Client.send patches."""
    return _run_mypy_sandboxed(
        mypy, agent_b_code, httpx_src,
        source_patch={
            "file": "_client.py",
            "old_text": PRE_WRITE_SIG,
            "new_text": POST_WRITE_SIG,
        } if apply_post_write else None,
        extra_patches=[
            {"file": "_client.py", "old_text": PRE_WRITE_ASYNC_SIG, "new_text": POST_WRITE_ASYNC_SIG},
        ] if apply_post_write else None,
    )


def _run_mypy_sandboxed(
    mypy: list[str],
    agent_b_code: str,
    httpx_src: Path,
    source_patch: dict | None,
    extra_patches: list[dict] | None = None,
) -> tuple[bool, str]:
    """
    Copy httpx source to a temp dir, apply an arbitrary source_patch (if given),
    write agent_b_code, run mypy with --no-site-packages.

    source_patch: {"file": relative path inside httpx/, "old_text": str, "new_text": str}
    extra_patches: additional patches to apply after source_patch.
    Returns (passed: bool, output: str).  Temp dir is cleaned up on return.
    """
    tmp = Path(tempfile.mkdtemp())
    try:
        shutil.copytree(str(httpx_src), str(tmp / "httpx"))

        if source_patch:
            target = tmp / "httpx" / source_patch["file"]
            # Normalize CRLF → LF on both sides so patches authored with \n
            # match files checked out with \r\n on Windows.
            text     = target.read_text(encoding="utf-8").replace("\r\n", "\n")
            old_text = source_patch["old_text"].replace("\r\n", "\n")
            new_text = source_patch["new_text"].replace("\r\n", "\n")
            if old_text not in text:
                raise ValueError(
                    f"source_patch.old_text not found in {source_patch['file']}.\n"
                    f"First 60 chars: {old_text[:60]!r}"
                )
            text = text.replace(old_text, new_text, 1)
            target.write_text(text, encoding="utf-8")

        for patch in (extra_patches or []):
            target   = tmp / "httpx" / patch["file"]
            text     = target.read_text(encoding="utf-8").replace("\r\n", "\n")
            old_text = patch["old_text"].replace("\r\n", "\n")
            new_text = patch["new_text"].replace("\r\n", "\n")
            if old_text in text:
                text = text.replace(old_text, new_text, 1)
                target.write_text(text, encoding="utf-8")

        agent_b_file = tmp / "agent_b_code.py"
        agent_b_file.write_text(agent_b_code, encoding="utf-8")

        result = subprocess.run(
            [*mypy, "--ignore-missing-imports", "--no-error-summary",
             "--no-site-packages", str(agent_b_file)],
            capture_output=True, text=True, cwd=str(tmp),
        )
        output = result.stdout + result.stderr
        return result.returncode == 0, output
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ── Public API for the experiment loop ────────────────────────────────────────

def check_task_conflict(
    agent_b_code: str,
    httpx_pkg: Path,
    source_patch: dict,
    mypy: list[str] | None = None,
) -> dict:
    """
    Operationalize Definition 2 condition 4 for an arbitrary task.

    source_patch: {"file": relative path inside httpx pkg,
                   "old_text": original text, "new_text": patched text}

    Returns a dict with pre_write_valid, post_write_valid, conflict_classified,
    mypy_pre, mypy_post.
    """
    if mypy is None:
        mypy = _find_mypy()

    try:
        pre_ok, pre_out = _run_mypy_sandboxed(mypy, agent_b_code, httpx_pkg,
                                               source_patch=None)
        post_ok, post_out = _run_mypy_sandboxed(mypy, agent_b_code, httpx_pkg,
                                                 source_patch=source_patch)
        conflict_classified = pre_ok and (not post_ok)
        return {
            "pre_write_valid":    pre_ok,
            "post_write_valid":   post_ok,
            "conflict_classified": conflict_classified,
            "mypy_pre":           pre_out.strip(),
            "mypy_post":          post_out.strip(),
            "error":              None,
        }
    except Exception as exc:
        return {
            "pre_write_valid":    None,
            "post_write_valid":   None,
            "conflict_classified": False,
            "mypy_pre":           "",
            "mypy_post":          "",
            "error":              str(exc),
        }


# ── Main check ─────────────────────────────────────────────────────────────

def run_conflict_check(kovex_root: Path, results_dir: Path, baseline_results: dict | None = None) -> dict:
    results: dict = {
        "timestamp":       datetime.now(timezone.utc).isoformat(),
        "scenario":        "conflict_check_definition2",
        "passed":          False,
        "pre_write_valid": None,
        "post_write_valid": None,
        "conflict_classified": False,
        "mypy_pre_output":  None,
        "mypy_post_output": None,
        "error":           None,
    }

    httpx_pkg = kovex_root / "httpx" / "httpx"

    if not (httpx_pkg / "_client.py").exists():
        results["error"] = f"httpx source not found at {httpx_pkg}"
        return results

    mypy = _find_mypy()
    print(f"mypy: {mypy}")
    print(f"Agent B code: {len(AGENT_B_CODE)} chars")

    try:
        # ── Step 1: run mypy against PRE-write snapshot ─────────────────────
        print("\n[1] Running mypy on Agent B code against PRE-write state (expect: PASS) ...")
        pre_ok, pre_out = _run_mypy_in_sandbox(mypy, AGENT_B_CODE, httpx_pkg, apply_post_write=False)
        results["pre_write_valid"]  = pre_ok
        results["mypy_pre_output"]  = pre_out
        print(f"    mypy result: {'PASS' if pre_ok else 'FAIL'}")
        if pre_out.strip():
            for line in pre_out.strip().splitlines()[:8]:
                print(f"      {line}")

        # ── Step 2: run mypy against POST-write snapshot ─────────────────────
        print("\n[2] Running mypy on Agent B code against POST-write state (expect: FAIL) ...")
        post_ok, post_out = _run_mypy_in_sandbox(mypy, AGENT_B_CODE, httpx_pkg, apply_post_write=True)
        results["post_write_valid"]  = post_ok
        results["mypy_post_output"]  = post_out
        print(f"    mypy result: {'PASS' if post_ok else 'FAIL'}")
        if post_out.strip():
            for line in post_out.strip().splitlines()[:8]:
                print(f"      {line}")

        # ── Definition 2 check ───────────────────────────────────────────────
        # Condition 4: Valid(C_B, pre) == True  AND  Valid(C_B, post) == False
        conflict_classified = pre_ok and (not post_ok)

        # Optionally verify the failure references Client.send or AsyncClient.send
        failure_traces_to_node = bool(
            re.search(r"(Client\.send|AsyncClient\.send|unexpected keyword|no parameter named)", post_out, re.I)
        ) if not post_ok else False

        results["conflict_classified"]     = conflict_classified
        results["failure_traces_to_node"]  = failure_traces_to_node

        print("\nDefinition 2 assertions:")
        checks = {
            "C_B valid against pre-write state  (mypy PASS)":  pre_ok,
            "C_B invalid against post-write state (mypy FAIL)": not post_ok,
            "Failure traces to patched send() node":            failure_traces_to_node,
        }
        for label, ok in checks.items():
            print(f"  {'PASS' if ok else 'FAIL'}  {label}")

        results["passed"] = all(checks.values())

    except Exception as exc:
        results["error"] = str(exc)
        print(f"\nERROR: {exc}", file=sys.stderr)

    # ── Save results ──────────────────────────────────────────────────────
    results_dir.mkdir(parents=True, exist_ok=True)
    ts   = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    dest = results_dir / f"conflict_check_{ts}.json"
    dest.write_text(json.dumps(results, indent=2))
    print(f"\nResults -> {dest}")

    banner = "PASS" if results["passed"] else "FAIL"
    print(f"\n{'='*44}\n  ConflictCheck: {banner}\n{'='*44}\n")
    return results


# ── Entry point ────────────────────────────────────────────────────────────

if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Kovex semantic conflict check (Definition 2)")
    ap.add_argument(
        "--kovex-root", type=Path,
        default=Path(__file__).resolve().parent.parent,
        help="Kovex repo root (default: parent of eval/)",
    )
    ap.add_argument(
        "--results-file", type=Path, default=None,
        help="Optional: path to a baseline_harness results JSON for context",
    )
    args = ap.parse_args()

    root        = args.kovex_root.resolve()
    results_dir = root / "eval" / "results"

    baseline_data: dict | None = None
    if args.results_file:
        baseline_data = json.loads(args.results_file.read_text())

    sys.exit(0 if run_conflict_check(root, results_dir, baseline_data)["passed"] else 1)
