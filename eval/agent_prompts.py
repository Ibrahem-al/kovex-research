#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
eval/agent_prompts.py - System prompts and per-task user messages for the
Phase 4B agent experiment (eval/agent_experiment.py).

Two roles
---------
Agent A (writer): given a description of a signature change and the node id,
calls the Kovex `commit_write` MCP tool to apply it.

Agent B (reader): given a feature description, writes Python code that uses
the affected httpx API. In the Kovex condition, B also registers a read on
its dependency, polls for directives after A writes, and revises its code
if notified. In the baseline condition, B has no coordination tools and
submits its initial code as-is.

The per-task entries in TASK_SPECS embed both:
  - the natural-language brief shown to the agents, and
  - the canonical signature/return_type that Agent A is expected to commit
    (so the harness can pre-extract them and pass them through, instead of
    asking A to parse multi-line patch blocks).
"""

from __future__ import annotations

# ── Tool-calling protocol (shared) ────────────────────────────────────────────
#
# These prompts run through the Claude Code CLI in non-interactive mode
# (`claude -p`) instead of the Anthropic Python SDK, so tool use is NOT native.
# Instead, the agents emit explicit <tool_call>...</tool_call> blocks in their
# text response. The harness parses each block, dispatches the call against
# the running Kovex MCP server, and feeds the result back as a follow-up user
# message containing a matching <tool_result>...</tool_result> block.

TOOL_PROTOCOL_BLURB = """\
Tool-calling protocol (READ CAREFULLY)
--------------------------------------
You do NOT have native tool use. To call a Kovex tool, output a SINGLE
JSON object inside <tool_call>...</tool_call> tags. Format:

  <tool_call>
  {"name": "<tool_name>", "input": { ... }}
  </tool_call>

After you emit the closing </tool_call> tag, STOP GENERATING IMMEDIATELY.
Do NOT continue writing in the same turn. Specifically, do NOT:
  - emit a fake <tool_result> block (the harness will inject the real one)
  - guess what the result will be
  - emit a <python_code> block until AFTER you have seen the real result
  - emit prose describing what you will do next

In your NEXT turn, the conversation will contain the real result as a user
message of the form:

  <tool_result>
  {"name": "<tool_name>", "is_error": <bool>, "result": { ... }}
  </tool_result>

You may then issue another <tool_call> (one per turn, same rules), or emit
your final code in <python_code>...</python_code> tags.

Hard rules
----------
- ONE <tool_call> per turn, MAX. Stop at the closing </tool_call>.
- The JSON inside <tool_call> must be valid: double quotes, no trailing
  commas, no comments.
- When you are finished and ready to deliver final code, emit ONLY a
  <python_code>...</python_code> block (no <tool_call> in that turn).
- The harness extracts the LAST <python_code> block as your final answer.
"""


# ── Agent A: writer ────────────────────────────────────────────────────────────

AGENT_A_SYSTEM = """\
You are Agent A, a senior Python engineer modifying the httpx client library.
Another agent (Agent B) is writing downstream code that may depend on what you
change. The team uses Kovex, a graph-aware coordination substrate that
broadcasts your writes to dependent agents.

Your job is to apply ONE signature change to ONE function in httpx by calling
the `commit_write` Kovex tool exactly once.

""" + TOOL_PROTOCOL_BLURB + """
Calling convention for commit_write
-----------------------------------
  <tool_call>
  {"name": "commit_write",
   "input": {
     "agentId": "<from user message>",
     "nodeId":  "<from user message>",
     "patch":   {"signature": "<new declaration>",
                 "return_type": "<unchanged>"}
   }}
  </tool_call>

After the harness returns the tool_result, output ONE confirmation line:

  COMMITTED <write_id from result>

Do not output any <python_code> block. Do not call any other tool.
"""


# ── Agent B: reader ────────────────────────────────────────────────────────────

# Kovex condition: B has the full coordination toolset.
AGENT_B_KOVEX_SYSTEM = """\
You are Agent B, a Python developer building features on top of the httpx
library. Another engineer (Agent A) is also editing httpx. The team uses
Kovex, a graph-aware coordination substrate, so you can be notified when a
function you depend on has been modified.

The Kovex tools you may call are:
  - register_read   (agentId, nodeId, bodyHash)
        Tell Kovex you depend on this graph node. You will receive a
        directive if it, or any node within k=2 hops, is later written to.
  - get_node        (nodeId)
        Fetch a node's current properties (label, name, signature,
        return_type, body_hash). No registry side-effect.
  - poll_directives (agentId)
        Return and clear pending directives for your agent id. A directive
        is {writeId, writerAgent, writtenNode, triggerNode, distance,
        timestamp} and means a node you depend on was modified.
  - query_graph     (cypher)
        Execute a read-only Cypher query against the codebase graph. Optional.

""" + TOOL_PROTOCOL_BLURB + """
Two-phase protocol
------------------
PHASE 1 (initial implementation), triggered by a user message starting
"PHASE 1":
  Turn 1: emit ONE <tool_call> for register_read using the agentId,
          nodeId, and bodyHash given in the message. STOP at </tool_call>.
  Turn 2: after the real tool_result arrives, emit your code inside a
          <python_code>...</python_code> block, using the PRE-WRITE API
          signature shown in the message (the API has not been modified
          yet at this point).

PHASE 2 (revision check), triggered by a user message starting "PHASE 2":
  Turn 1: emit ONE <tool_call> for poll_directives with your agent id.
          STOP at </tool_call>.
  Turn 2 onward, depending on the poll result:
    (a) If `directives` is an empty list, your code is unaffected. Emit
        your Phase-1 code unchanged inside <python_code>...</python_code>
        and stop.
    (b) If `directives` has >= 1 entries, you MUST inspect every unique
        `writtenNode` BEFORE writing any code. Issue ONE <tool_call> for
        get_node per turn until you have a get_node result for every
        unique writtenNode value.
  Final turn: emit revised code inside <python_code>...</python_code>
    based ONLY on the signatures you observed in the get_node responses
    (read the `signature` field of the returned `node`). The revised
    code should keep the same overall feature behavior as your Phase-1
    code; the only change should be adapting to the new signature.

Critical rules for Phase 2 — these apply ONLY when there are directives:
  - You MUST call get_node before writing any code. Do not skip it.
    Even if you "remember" what httpx looks like, the codebase has been
    modified and your memory may be stale. The signature in the get_node
    response is the source of truth.
  - Do NOT invent or guess the new signature. Copy it from the
    `node.signature` field in the get_node response, verbatim.
  - Do NOT describe the new signature in prose unless you have just
    read it from a get_node response.

Implementation rules (for the <python_code> block)
--------------------------------------------------
- Keep the implementation minimal — match the feature brief, no extras.
  Do not add helper classes, retry loops, demo `if __name__ == "__main__"`
  blocks, or unused parameters.
- Do not invent imports beyond `httpx` and the symbols shown in the prompt.
- Do not use string forward-reference annotations like
  `"Union[X, Y]"` unless you have ALSO imported `Union` from `typing`.
  Prefer real annotations using imported names directly.
- Always finish each phase with exactly one <python_code>...</python_code>
  block. The harness extracts the LAST such block as your code.
"""


# Baseline condition: no MCP tools, no coordination, single phase.
AGENT_B_BASELINE_SYSTEM = """\
You are Agent B, a Python developer building features on top of the httpx
library. You are working solo against the current state of the API.

You have NO tools. Do not emit <tool_call> blocks.

Protocol
--------
You will receive one user message describing the feature and showing the
current httpx API signature. Implement the feature and emit your final
code inside <python_code>...</python_code> tags as your only output.

Rules
-----
- Emit exactly one <python_code>...</python_code> block. The harness
  extracts the LAST such block as your code.
- Keep the implementation minimal — match the feature brief, no extras.
  Do not add helper classes, retry loops, or demo blocks.
- Do not invent imports beyond `httpx` and the symbols shown in the prompt.
- Do not use string forward-reference annotations like `"Union[X, Y]"`
  unless you have ALSO imported `Union` from `typing`.
"""


# ── Per-task specifications ──────────────────────────────────────────────────
#
# For each task we expose:
#   feature_brief         : prose Agent B reads in PHASE 1 / baseline
#   pre_write_signature   : the API surface B should target initially
#   post_write_signature  : ground-truth new declaration after A's write,
#                           used to construct Agent A's commit_write patch.
#                           Also written into the prompt so any agent that
#                           inspects the node post-write sees something
#                           coherent.
#   return_type           : return type to keep in the commit_write patch
#   change_brief          : prose Agent A reads, describing the edit
#
# The node ids and source patches still come from
# eval/tasks/httpx_tasks.json — the harness joins these spec entries with
# the JSON tasks by task_id.

TASK_SPECS: dict[str, dict[str, str]] = {

    # ── T01: distance 0 — B uses AsyncClient.send(stream=True), A removes stream
    "T01": {
        "feature_brief": (
            "Write an async function `streaming_send(client, request)` that takes "
            "an httpx.AsyncClient and an httpx.Request and sends the request in "
            "streaming mode by calling `client.send(...)`. Return the resulting "
            "httpx.Response."
        ),
        "pre_write_signature": (
            "async def send(\n"
            "    self,\n"
            "    request: Request,\n"
            "    *,\n"
            "    stream: bool = False,\n"
            "    auth: AuthTypes | UseClientDefault | None = USE_CLIENT_DEFAULT,\n"
            "    follow_redirects: bool | UseClientDefault = USE_CLIENT_DEFAULT,\n"
            ") -> Response"
        ),
        "post_write_signature": (
            "async def send(\n"
            "    self,\n"
            "    request: Request,\n"
            "    *,\n"
            "    auth: AuthTypes | UseClientDefault | None = USE_CLIENT_DEFAULT,\n"
            "    follow_redirects: bool | UseClientDefault = USE_CLIENT_DEFAULT,\n"
            ") -> Response"
        ),
        "return_type": "Response",
        "change_brief": (
            "Remove the `stream: bool = False` keyword-only parameter from "
            "AsyncClient.send. The function will keep `auth` and "
            "`follow_redirects` as the only keyword-only parameters."
        ),
    },

    # ── T03: distance 1 — B uses AsyncClient.send(stream=True), registered on .request
    "T03": {
        "feature_brief": (
            "Write an async function `request_and_stream(client, url)` that "
            "uses an httpx.AsyncClient to first call `client.build_request(\"GET\", url)` "
            "and then calls `client.send(...)` to dispatch the request in streaming "
            "mode. Return the resulting httpx.Response."
        ),
        "pre_write_signature": (
            "async def send(\n"
            "    self,\n"
            "    request: Request,\n"
            "    *,\n"
            "    stream: bool = False,\n"
            "    auth: AuthTypes | UseClientDefault | None = USE_CLIENT_DEFAULT,\n"
            "    follow_redirects: bool | UseClientDefault = USE_CLIENT_DEFAULT,\n"
            ") -> Response"
        ),
        "post_write_signature": (
            "async def send(\n"
            "    self,\n"
            "    request: Request,\n"
            "    *,\n"
            "    auth: AuthTypes | UseClientDefault | None = USE_CLIENT_DEFAULT,\n"
            "    follow_redirects: bool | UseClientDefault = USE_CLIENT_DEFAULT,\n"
            ") -> Response"
        ),
        "return_type": "Response",
        "change_brief": (
            "Remove the `stream: bool = False` keyword-only parameter from "
            "AsyncClient.send. The remaining keyword-only parameters are `auth` "
            "and `follow_redirects`."
        ),
    },

    # ── T05: distance 1 — B uses AsyncClient.request(content=...), registered on .get
    "T05": {
        "feature_brief": (
            "Write an async function `get_with_body(client, url, payload)` that "
            "performs a GET request with a raw bytes body by calling "
            "`client.request(\"GET\", url, content=payload)` on an httpx.AsyncClient. "
            "Return the resulting httpx.Response."
        ),
        "pre_write_signature": (
            "async def request(\n"
            "    self,\n"
            "    method: str,\n"
            "    url: URL | str,\n"
            "    *,\n"
            "    content: RequestContent | None = None,\n"
            "    ...\n"
            ") -> Response"
        ),
        "post_write_signature": (
            "async def request(\n"
            "    self,\n"
            "    method: str,\n"
            "    url: URL | str,\n"
            "    *,\n"
            "    body: RequestContent | None = None,\n"
            "    ...\n"
            ") -> Response"
        ),
        "return_type": "Response",
        "change_brief": (
            "Rename the `content` keyword-only parameter on AsyncClient.request "
            "to `body`. The accepted type (`RequestContent | None`, default `None`) "
            "is unchanged. All other parameters remain the same."
        ),
    },

    # ── T09: distance 2 — B uses AsyncClient.send(stream=True), registered on .get
    "T09": {
        "feature_brief": (
            "Write an async function `get_streaming(client, url)` that takes an "
            "httpx.AsyncClient and a URL string, builds a GET request via "
            "`client.build_request(\"GET\", url)`, and dispatches it in streaming "
            "mode via `client.send(...)`. Return the resulting httpx.Response."
        ),
        "pre_write_signature": (
            "async def send(\n"
            "    self,\n"
            "    request: Request,\n"
            "    *,\n"
            "    stream: bool = False,\n"
            "    auth: AuthTypes | UseClientDefault | None = USE_CLIENT_DEFAULT,\n"
            "    follow_redirects: bool | UseClientDefault = USE_CLIENT_DEFAULT,\n"
            ") -> Response"
        ),
        "post_write_signature": (
            "async def send(\n"
            "    self,\n"
            "    request: Request,\n"
            "    *,\n"
            "    auth: AuthTypes | UseClientDefault | None = USE_CLIENT_DEFAULT,\n"
            "    follow_redirects: bool | UseClientDefault = USE_CLIENT_DEFAULT,\n"
            ") -> Response"
        ),
        "return_type": "Response",
        "change_brief": (
            "Remove the `stream: bool = False` keyword-only parameter from "
            "AsyncClient.send. The remaining keyword-only parameters are `auth` "
            "and `follow_redirects`."
        ),
    },

    # ── T13: distance 3 — B subclasses & overrides _send_single_request,
    #         registered on AsyncClient.send. Kovex's k=2 misses this (false negative).
    "T13": {
        "feature_brief": (
            "Write a subclass `RetryingAsyncClient` of httpx.AsyncClient that "
            "overrides the `_send_single_request` method. The override should "
            "retry the underlying call up to 3 times when an httpx.HTTPError is "
            "raised, re-raising on the third failure. Use `super()._send_single_request(...)` "
            "for the inner call. Use the imports:\n"
            "    import httpx\n"
            "    from httpx._models import Request, Response"
        ),
        "pre_write_signature": (
            "async def _send_single_request(self, request: Request) -> Response"
        ),
        "post_write_signature": (
            "async def _send_single_request(self, request: Request, deadline: float) -> Response"
        ),
        "return_type": "Response",
        "change_brief": (
            "Add a required positional parameter `deadline: float` to "
            "AsyncClient._send_single_request, immediately after `request`. "
            "Return type stays `Response`."
        ),
    },
}


# ── User-message templates ────────────────────────────────────────────────────

def agent_a_user_message(
    *,
    task_id: str,
    agent_id: str,
    node_id: str,
    write_target_name: str,
    change_brief: str,
    new_signature: str,
    return_type: str,
) -> str:
    """Construct the single user message Agent A receives."""
    return (
        f"Task {task_id}: apply the following signature change to httpx.\n\n"
        f"Target function: {write_target_name}\n"
        f"Graph node id : {node_id}\n"
        f"Your agent id : {agent_id}\n\n"
        f"Change\n------\n{change_brief}\n\n"
        f"New declaration to commit\n-------------------------\n"
        f"{new_signature}\n\n"
        f"Call commit_write now with patch.signature set to the new declaration "
        f"shown above (verbatim, including newlines), patch.return_type = "
        f"\"{return_type}\", agentId = \"{agent_id}\", nodeId = \"{node_id}\"."
    )


def agent_b_phase1_user_message(
    *,
    task_id: str,
    agent_id: str,
    registration_node_id: str,
    registration_node_name: str,
    registration_body_hash: str,
    feature_brief: str,
    pre_write_signature: str,
    condition: str,
) -> str:
    """Phase-1 user message for Agent B. Includes registration details for Kovex."""
    if condition == "kovex":
        coord_block = (
            "Coordination\n------------\n"
            f"Before writing any code, call register_read with:\n"
            f"  - agentId  = \"{agent_id}\"\n"
            f"  - nodeId   = \"{registration_node_id}\"   (corresponds to {registration_node_name})\n"
            f"  - bodyHash = \"{registration_body_hash}\"\n\n"
        )
    else:
        coord_block = ""

    return (
        f"PHASE 1 — Task {task_id}\n\n"
        f"{coord_block}"
        f"Feature\n-------\n{feature_brief}\n\n"
        f"Current httpx API signature you should target\n"
        f"---------------------------------------------\n"
        f"{pre_write_signature}\n\n"
        f"Write the implementation now and emit it inside "
        f"<python_code>...</python_code> tags."
    )


def agent_b_phase2_user_message(
    *,
    task_id: str,
    agent_id: str,
) -> str:
    """Phase-2 user message for Agent B (Kovex condition only).

    This is sent as a user message in the SAME session as Phase 1 (via
    --resume), so Agent B already has its own Phase-1 code in context and
    does not need it inlined here. The reminder line is just defensive.
    """
    return (
        f"PHASE 2 — Task {task_id}\n\n"
        f"Time has passed since you wrote your Phase-1 code (it is in this "
        f"conversation above). Another agent may have modified one of the "
        f"functions you depend on. Follow the PHASE 2 protocol from your "
        f"system prompt:\n\n"
        f"  1. First turn: call poll_directives with agentId = \"{agent_id}\". "
        f"     STOP at </tool_call>.\n"
        f"  2. If poll returns 0 directives: emit your Phase-1 code unchanged "
        f"     inside <python_code>...</python_code>.\n"
        f"  3. If poll returns >= 1 directives: for EACH unique writtenNode, "
        f"     call get_node (one per turn) BEFORE writing any code. Do not "
        f"     guess or invent the new signature.\n"
        f"  4. Final turn: emit revised code adapted to the signatures you "
        f"     observed in the get_node responses. The feature behavior "
        f"     should match your Phase-1 code; only the API call adapts."
    )
