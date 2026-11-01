#!/usr/bin/env python3
"""Embedded Claude agent sessions for Funkuino Studio (Claude Agent SDK).

Each session is one ``ClaudeSDKClient`` connected with no initial prompt; every
user turn is driven with ``query(text)`` followed by draining
``receive_response()`` (the connect-then-query pattern that keeps the input
stream open so the ``can_use_tool`` callback fires). Tool approvals and
AskUserQuestion prompts are escalated to the browser: the callback emits an
event and awaits an ``asyncio.Future`` that a REST endpoint resolves. Follow-up
turns are queued and run one at a time.

The manager translates SDK messages into the compact ``agent.event`` shapes the
frontend consumes and keeps a per-session ordered replay list (so a reloaded tab
can catch up via ``since=``). Nothing here logs the OAuth token; it is only ever
handed to the SDK via ``ClaudeAgentOptions.env``.

Sessions survive a restart of Studio. Each one is an append-only JSON-lines log
in ``<config dir>/agent-sessions/<id>.jsonl``: its ``{"seq", "event"}`` records
(exactly what the replay serves) plus ``{"meta": …}`` records, of which the last
one wins. The meta carries the Claude Code session id, so the first turn after a
restart reconnects with ``resume=`` and the agent remembers the conversation.
Until then a loaded session is dormant: no CLI process, nothing spent. The log
is ours rather than a view of the CLI's transcript because that transcript is
deleted after the CLI's cleanup period (30 days by default), and it does not
record what the user saw: which question card was answered how, and what a turn
cost. The config dir, not the library, because the transcripts it resumes live
in ``~/.claude`` on this machine — a copied library could show the history but
never continue it.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import secrets
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable

import espuino
from claude_agent_sdk import (AssistantMessage, ClaudeAgentOptions,
                              ClaudeSDKClient, ResultMessage, SystemMessage,
                              TextBlock, ThinkingBlock, ToolResultBlock,
                              ToolUseBlock, UserMessage, get_session_info)
from claude_agent_sdk.types import PermissionResultAllow, PermissionResultDeny

# The command dispatcher (bin/funkuino). Sessions get its directory on PATH, so
# the agent calls `funkuino <command>` — one canonical spelling that the
# permission patterns below can match regardless of cwd, which the ./ wrappers
# cannot once code and data live in different places (packaged app).
DISPATCHER = "funkuino"
# A checkout and the bundle have bin/; a pip install has the console script next
# to the interpreter. Either way the agent needs the bare *name* on PATH,
# because the permission patterns match the literal command line.
BIN_DIR = espuino.dispatcher_bin_dir()

# Bare allowlist: these are auto-approved deterministically (no classifier
# round-trip, no card). The risky set lives in ASK_RULES instead.
ALLOWED_TOOLS = [
    "Read", "Glob", "Grep", "TodoWrite",
    # The dispatcher form (bin/ is on the session PATH) is what the agent is
    # told to use; the ./ wrappers stay allowed because a checkout still has
    # them and the agent may fall back to a form it saw in a shell transcript.
    "Bash(funkuino download*)", "Bash(funkuino prepare*)",
    "Bash(funkuino covers*)",
    "Bash(./download*)", "Bash(./prepare*)",
    "Bash(./covers*)",
    "Bash(ffmpeg*)", "Bash(ffprobe*)", "Bash(ls*)", "Bash(mkdir*)", "Bash(mv*)",
    # Read-only inspection commands (same low risk as the tools above).
    "Bash(cat*)", "Bash(head*)", "Bash(tail*)", "Bash(grep*)", "Bash(find*)",
    "Bash(wc*)", "Bash(stat*)", "Bash(file*)", "Bash(du*)",
    "Bash(echo*)", "Bash(sleep*)",
    # The venv python is the workhorse (yt-dlp, mutagen probes, JSON parsing of
    # playlist metadata). Inline `-c` is arbitrary code, but the user opted for
    # auto mode anyway — a deterministic allow beats a per-call classifier trip.
    # Same reasoning as the wrappers: `funkuino python` is the cwd-independent
    # spelling, the relative one stays allowed for a checkout.
    "Bash(funkuino python*)", "Bash(.venv/bin/python*)",
]

# Machine-local allowlist extensions: a git-ignored `.agent-allow.json` at the
# repo root may hold a JSON array of additional patterns (e.g. for extra local
# wrapper scripts). Malformed or absent -> silently no extensions.
try:
    ALLOWED_TOOLS += [
        str(p) for p in
        json.loads(espuino.data_or_repo(".agent-allow.json").read_text(encoding="utf-8"))
    ]
except (OSError, ValueError):
    pass


def _extension_rules() -> tuple[list[str], list[str]]:
    """Permission patterns contributed by installed extensions.

    An extension is ``<data folder>/extensions/<name>.py``; an optional sibling
    ``<name>.permissions.json`` (``{"allow": [...], "ask": [...]}``) declares how
    its command should be gated. Shipping the rules with the extension is what
    lets a private or machine-local command exist without editing this file —
    and an extension that declares nothing simply falls through to the auto-mode
    classifier, which is the safe default.
    """
    allow: list[str] = []
    ask: list[str] = []
    for manifest in sorted((espuino.DATA_ROOT / "extensions").glob("*.permissions.json")):
        try:
            rules = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue  # a broken manifest must not take the agent down
        allow += [str(p) for p in rules.get("allow", [])]
        ask += [str(p) for p in rules.get("ask", [])]
    return allow, ask


_EXT_ALLOW, _EXT_ASK = _extension_rules()
ALLOWED_TOOLS += _EXT_ALLOW

# Ask rules: ALWAYS raise a browser card, even in auto mode — Claude Code
# evaluates permission rules deny -> ask -> mode(classifier) -> allow, so these
# beat the classifier AND the allowlist (which is why ./cards --dry-run is not
# allowlisted above: an ask rule on ./cards* would shadow it anyway).
# AskUserQuestion must be here: auto mode routes around can_use_tool, and the
# question cards only work because can_use_tool fires for that tool.
ASK_RULES = [
    "AskUserQuestion",
    # Both spellings, deliberately: missing one here does not fail loudly, it
    # silently hands the call to the auto-mode classifier, which never asks.
    "Bash(funkuino sync*)", "Bash(./sync*)",   # upload + full device listing
    "Bash(funkuino cards*)", "Bash(./cards*)",  # a real print mutates the manifest
    "Bash(rm*)", "Bash(rmdir*)", "Bash(sudo*)",
    "Bash(git push*)",
] + _EXT_ASK

# Passed to the CLI via --settings (the SDK forwards a JSON string verbatim).
SETTINGS_JSON = json.dumps({"permissions": {"ask": ASK_RULES}})

# Appended to the claude_code preset system prompt.
SYSTEM_APPEND = (
    "You are running inside Funkuino Studio. The project's commands are on your "
    "PATH as `funkuino <command>` (download, prepare, covers, sync, cards, plus "
    "any locally installed ones) — always use that form, never the ./ wrappers "
    "or an absolute path, because your working directory is the DATA folder and "
    "need not contain them. For Python use `funkuino python …` (the project "
    "venv, with the scripts importable); never `source .venv/bin/activate` and "
    "never a bare `python`. Never run a bare `funkuino sync` (a real "
    "upload to the device) unless the user explicitly asks for it. rm, sudo, "
    "git push, `funkuino sync` and `funkuino cards` always interrupt the user "
    "for approval — avoid them unless genuinely needed."
)


def _system_append() -> str:
    """SYSTEM_APPEND plus the project instructions the session needs.

    ``setting_sources=["project"]`` loads a CLAUDE.md from the session's cwd,
    which is DATA_ROOT — fine for a plain checkout (data == code), but with a
    configured data folder (and in a packaged app, where the code sits read-only
    inside the bundle) there is no CLAUDE.md there. So the code root's CLAUDE.md
    is appended explicitly in that case; the data folder may still add its own,
    which the project source picks up on top.

    A git-ignored ``CLAUDE.local.md`` is appended either way — machine-local
    tooling knowledge that must not touch the repo's CLAUDE.md.
    """
    parts = [SYSTEM_APPEND]
    if espuino.DATA_ROOT != espuino.REPO_ROOT:
        knowledge = espuino.knowledge_file()
        if knowledge:
            with contextlib.suppress(OSError):
                parts.append(knowledge.read_text(encoding="utf-8"))
    with contextlib.suppress(OSError):
        parts.append(espuino.data_or_repo("CLAUDE.local.md").read_text(encoding="utf-8"))
    return "\n\n".join(parts)
# ./sync (even --dry-run) is deliberately NOT allowlisted: it does the full
# recursive device listing outside Studio's device_lock, which would violate the
# one-storage-op-at-a-time rule. Agent ./sync calls are routed to a UI approval,
# and auto-denied while a Studio sync job holds the device (see can_use_tool).

URL_PROMPT = """The user pasted this URL into Funkuino Studio: {url}
Handle the complete intake: probe it with `funkuino download --probe <url>` \
(one call: playlist title, every entry with its duration, the first track's \
tags and the default names, as JSON; Premium-only tracks need no cookies \
for it), classify -- \
single song / music album / Hoerspiel (audiobook) / multi-story Folge -- then \
download with the correct wrapper invocation and flags per the project \
instructions (CLAUDE.md and any local additions).
Before downloading, look in files/ for the series or artist this belongs to \
and reuse its exact folder spelling, continuing its numbering (see "Matching \
the existing library" in CLAUDE.md); ask when the match or the number is \
unclear instead of creating a second folder.
The download prints where the card cover went ("cover -> ...") or a WARNING \
saying why there is none; take that as the cover check. \
Verify results: naming conventions, tags; do any needed \
renames/regrouping (multi-story Folgen per the documented convention).
When a judgment call is needed (classification unclear, naming/attribution \
choices, Folge number, intro wording), ask the user with the AskUserQuestion \
tool -- questions and options in GERMAN.
Do NOT run a real `funkuino sync` or `funkuino cards` print unless the user \
approves; you may \
suggest it at the end. Report a concise German summary of what landed where.
Communicate with the user in German."""

CHAT_PREFIX = ("[Running inside Funkuino Studio (data folder at {root}). "
               "Answer in German.]\n\n")

# Shown in a session that was mid-turn when Studio stopped. Nothing resumes on
# its own: picking a half-done download back up spends quota and touches the
# library, so the user decides with their next message.
INTERRUPTED_NOTICE = ("Studio wurde beendet, während der Agent arbeitete. "
                      "Schreib eine Nachricht, um weiterzumachen.")
FORGOTTEN_NOTICE = ("Claude hat den Verlauf dieser Session nicht mehr (Claude "
                    "Code löscht alte Verläufe). Der Agent beginnt ohne "
                    "Erinnerung an das Bisherige.")


def sessions_dir() -> Path:
    return espuino.APP_CONFIG_DIR / "agent-sessions"


def _answer_label(answers) -> str:
    """The 'resolved' line of a question card: what the user picked."""
    if not isinstance(answers, dict) or not answers:
        return "Abgebrochen"
    picked = ["/".join(map(str, v)) if isinstance(v, list) else str(v)
              for v in answers.values()]
    return "Beantwortet: " + " · ".join(picked)


def _summarize_input(tool_name: str, data: dict) -> str:
    """Compact one-line rendering of a tool call's input (Bash command, path)."""
    if tool_name == "Bash":
        return str(data.get("command", "")).strip()
    for key in ("file_path", "path", "pattern", "url", "notebook_path"):
        if data.get(key):
            return str(data[key])
    try:
        text = json.dumps(data, ensure_ascii=False)
    except (TypeError, ValueError):
        text = str(data)
    return text[:200]


def _summarize_result(block: ToolResultBlock) -> str:
    content = block.content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict):
                parts.append(str(item.get("text", "")))
            else:
                parts.append(str(item))
        content = " ".join(parts)
    text = str(content or "").strip().replace("\n", " ")
    return text[:200]


def _bash_head(command: str) -> str:
    """The executable/wrapper a Bash command runs (its first token), e.g.
    ``./sync`` or ``touch``. Falls back to the raw command if it can't split.

    Exception: for the dispatcher the risk lives in the SUBcommand, so
    ``funkuino sync …`` yields ``funkuino sync``, normalised to the basename so
    an absolute invocation compares equal. Without this, an "always allow" on a
    harmless ``funkuino covers`` would key on ``funkuino`` alone and thereby
    also always-allow ``funkuino sync``.
    """
    try:
        import shlex
        parts = shlex.split(command)
    except ValueError:
        parts = command.split()
    if not parts:
        return command.strip()
    if Path(parts[0]).name == DISPATCHER and len(parts) > 1:
        return f"{DISPATCHER} {parts[1]}"
    return parts[0]


def _pattern_for(tool_name: str, data: dict) -> str:
    """Stable key for the session's 'always allow' set. For Bash we key on the
    executable (first token) so "always" generalises across its arguments; other
    tools are keyed by tool name."""
    if tool_name == "Bash":
        return f"Bash({_bash_head(str(data.get('command', '')))})"
    return tool_name


def _is_compound_command(command: str) -> bool:
    """True if a shell command chains/pipes multiple commands. Keying "always"
    on the first token would be unsafe here (it could auto-approve an unrelated
    trailing command), so such calls never get an 'always' pattern."""
    return any(op in command for op in (";", "&&", "||", "|", "\n"))


def permission_pattern(tool_name: str, data: dict) -> str | None:
    """The pattern an 'Immer erlauben' would remember, or None when it must not
    be offered (compound/piped Bash commands). None disables the always-button."""
    if tool_name == "Bash" and _is_compound_command(str(data.get("command", ""))):
        return None
    return _pattern_for(tool_name, data)


@dataclass
class Session:
    id: str
    kind: str
    model: str
    label: str
    manager: "SessionManager"
    client: ClaudeSDKClient | None = None
    status: str = "running"
    events: list[dict] = field(default_factory=list)  # {"seq":int,"event":dict}
    queue: asyncio.Queue = field(default_factory=asyncio.Queue)
    pending: dict[str, tuple[str, asyncio.Future]] = field(default_factory=dict)
    always: set[str] = field(default_factory=set)
    worker: asyncio.Task | None = None
    progress_file: "Path | None" = None    # $FUNKUINO_PROGRESS_FILE for this session
    progress: dict | None = None            # latest intake snapshot (for reloads)
    created: float = field(default_factory=time.time)
    claude_session: str | None = None       # the CLI's session id, for resume=
    log_path: "Path | None" = None          # sessions_dir()/<id>.jsonl

    def emit(self, event: dict) -> None:
        seq = self.manager.emit(self.id, event)
        record = {"seq": seq, "event": event}
        self.events.append(record)
        self._append(record)

    # -- persistence --
    def meta(self) -> dict:
        return {"id": self.id, "kind": self.kind, "model": self.model,
                "label": self.label, "created": self.created,
                "dataDir": str(espuino.DATA_ROOT),
                "claudeSession": self.claude_session,
                "always": sorted(self.always)}

    def save_meta(self) -> None:
        self._append({"meta": self.meta()})

    def _append(self, record: dict) -> None:
        """One line per record, appended: a crash can only tear the last line,
        which the loader skips. No fsync per event — losing the last few lines
        to a power cut is acceptable for a chat history, slowing every event
        down is not."""
        if self.log_path is None:
            return
        try:
            with self.log_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        except (OSError, TypeError, ValueError) as exc:
            if not self.manager.log_warned:
                self.manager.log_warned = True
                print(f"warning: cannot save agent session {self.id}: {exc}",
                      file=sys.stderr)

    def _note_claude_session(self, claude_session) -> None:
        if claude_session and claude_session != self.claude_session:
            self.claude_session = str(claude_session)
            self.save_meta()

    def set_status(self, status: str) -> None:
        self.status = status
        self.emit({"k": "status", "status": status})

    # -- can_use_tool callback (async: it awaits a browser round-trip) --
    async def can_use_tool(self, tool_name, input_data, context):
        if tool_name == "AskUserQuestion":
            questions = input_data.get("questions", [])
            rid = secrets.token_hex(4)
            fut = self.manager.loop.create_future()
            self.pending[rid] = ("question", fut)
            self.emit({"k": "question", "requestId": rid, "questions": questions})
            self.set_status("waiting_user")
            answers = await fut
            self.pending.pop(rid, None)
            self.emit({"k": "resolved", "requestId": rid,
                       "label": _answer_label(answers)})
            self.set_status("running")
            # Original questions array MUST be passed back; answers keyed by
            # literal question text (see spec / SDK contract).
            return PermissionResultAllow(
                updated_input={"questions": questions, "answers": answers})

        # A sync while Studio already holds the device: auto-deny (a concurrent
        # device listing would wedge the SD writer) without a UI round-trip.
        if (tool_name == "Bash"
                and _bash_head(str(input_data.get("command", "")))
                in (f"{DISPATCHER} sync", "./sync")
                and self.manager.sync_active()):
            return PermissionResultDeny(message="Sync läuft bereits")

        pattern = permission_pattern(tool_name, input_data)
        if pattern is not None and pattern in self.always:
            return PermissionResultAllow(updated_input=input_data)

        rid = secrets.token_hex(4)
        fut = self.manager.loop.create_future()
        self.pending[rid] = ("permission", fut)
        self.emit({"k": "permission", "requestId": rid, "tool": tool_name,
                   "input": input_data,
                   "summary": _summarize_input(tool_name, input_data),
                   "pattern": pattern})  # null -> frontend hides "Immer erlauben"
        self.set_status("waiting_user")
        decision = await fut
        self.pending.pop(rid, None)
        self.emit({"k": "resolved", "requestId": rid, "label":
                   "Abgebrochen" if decision.get("interrupted") else
                   "Immer erlaubt" if decision.get("allow") and decision.get("always") else
                   "Erlaubt" if decision.get("allow") else "Abgelehnt"})
        self.set_status("running")
        if decision.get("allow"):
            if decision.get("always") and pattern is not None:
                self.always.add(pattern)
                self.save_meta()
            return PermissionResultAllow(updated_input=input_data)
        return PermissionResultDeny(message="Vom Nutzer abgelehnt")

    # -- turn worker --
    async def run(self) -> None:
        while True:
            item = await self.queue.get()
            if item is None:
                return
            query_text, echo_text = item
            self.set_status("running")
            self.emit({"k": "user", "text": echo_text})
            try:
                if self.client is None:
                    await self._connect()
                await self.client.query(query_text)
                async for msg in self.client.receive_response():
                    self._translate(msg)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - surface, never crash server
                self.emit({"k": "result", "error": str(exc)})
                self.set_status("error")

    async def _connect(self) -> None:
        """Start the CLI for this session: fresh, or — for a session that has
        talked before, i.e. one loaded after a restart — resuming its Claude
        Code conversation, so the agent knows what was said and done."""
        resume = self.claude_session
        if resume and not self.manager.can_resume(resume):
            self.emit({"k": "notice", "text": FORGOTTEN_NOTICE})
            resume = None
        self.client = ClaudeSDKClient(options=self.manager._options(
            self.model, self.can_use_tool, self.progress_file, resume=resume))
        try:
            await self.client.connect()
        except BaseException:
            self.client = None  # retry the connect on the next turn
            raise

    def _translate(self, msg) -> None:
        if isinstance(msg, SystemMessage):
            if msg.subtype == "init":
                self._note_claude_session(msg.data.get("session_id"))
            return  # init/other system chatter is not shown
        if isinstance(msg, AssistantMessage):
            for block in msg.content:
                if isinstance(block, TextBlock):
                    if block.text.strip():
                        self.emit({"k": "text", "text": block.text})
                elif isinstance(block, ThinkingBlock):
                    continue
                elif isinstance(block, ToolUseBlock):
                    self.emit({"k": "tool", "name": block.name,
                               "summary": _summarize_input(block.name, block.input)})
        elif isinstance(msg, UserMessage):
            for block in msg.content if isinstance(msg.content, list) else []:
                if isinstance(block, ToolResultBlock):
                    self.emit({"k": "tool_result",
                               "summary": _summarize_result(block)})
        elif isinstance(msg, ResultMessage):
            self._note_claude_session(msg.session_id)
            self.emit({"k": "result", "costUsd": msg.total_cost_usd,
                       "turns": msg.num_turns,
                       "error": msg.result if msg.is_error else None})
            self.set_status("error" if msg.is_error else "done")

    # -- external actions (called from REST handlers on the loop) --
    def post_turn(self, query_text: str, echo_text: str) -> None:
        if self.worker is None:
            self.worker = self.manager.loop.create_task(self.run())
        self.queue.put_nowait((query_text, echo_text))

    def resolve(self, request_id: str, kind: str, value) -> bool:
        entry = self.pending.get(request_id)
        if not entry or entry[0] != kind or entry[1].done():
            return False
        entry[1].set_result(value)
        return True

    async def interrupt(self) -> None:
        for _kind, fut in list(self.pending.values()):
            if not fut.done():
                fut.set_result({"allow": False, "interrupted": True}
                               if _kind == "permission" else {})
        if self.client is None:
            return
        try:
            await self.client.interrupt()
        except Exception:  # noqa: BLE001
            pass

    async def close(self) -> None:
        if self.worker:
            self.worker.cancel()
        for _kind, fut in list(self.pending.values()):
            if not fut.done():
                fut.cancel()
        if self.progress_file is not None:
            with contextlib.suppress(OSError):
                self.progress_file.unlink()
        if self.client is None:
            return
        try:
            await self.client.disconnect()
        except Exception:  # noqa: BLE001
            pass


class SessionManager:
    """Owns the live agent sessions and their SDK clients."""

    def __init__(self, token: str | None,
                 emit: Callable[[str, dict], int],
                 loop: asyncio.AbstractEventLoop,
                 sync_active: Callable[[], bool] | None = None):
        self.token = token
        self.emit = emit  # (session_id, event) -> global seq
        self.loop = loop
        # Whether a Studio sync job currently holds the device (to auto-deny
        # agent ./sync calls); defaults to "never busy" if not wired.
        self.sync_active = sync_active or (lambda: False)
        self.sessions: dict[str, Session] = {}
        self.log_warned = False  # warn once, not per event, if logs can't be written

    @property
    def available(self) -> bool:
        return bool(self.token)

    def _options(self, model: str, can_use_tool,
                 progress_file: Path | None = None,
                 resume: str | None = None) -> ClaudeAgentOptions:
        env = {
            "CLAUDE_CODE_OAUTH_TOKEN": self.token,
            # Makes the bare `funkuino …` form resolvable from any cwd. Prepended,
            # so a shell profile that appends its own entries cannot shadow it;
            # if a profile overwrites PATH wholesale the agent fails loudly with
            # "command not found" rather than quietly finding something else.
            "PATH": os.pathsep.join(
                [*([str(BIN_DIR)] if BIN_DIR else []),
                 os.environ.get("PATH", "/usr/bin:/bin")]),
            # Resolve the data folder once here instead of letting every child
            # process re-read the config file.
            "FUNKUINO_DATA_DIR": str(espuino.DATA_ROOT),
            # The CLI reads its tools' output as UTF-8. Our commands would
            # otherwise print in the locale's charset, and the agent would see
            # (and pass on) garbled file names wherever that is not UTF-8.
            "PYTHONIOENCODING": "utf-8",
        }
        if progress_file is not None:
            # ./download (and wrappers built on it) writes live progress here.
            env["FUNKUINO_PROGRESS_FILE"] = str(progress_file)
        return ClaudeAgentOptions(
            # The data folder, not the checkout: that is where the library and
            # the manifests live, and in a packaged app the code is read-only.
            cwd=str(espuino.DATA_ROOT),
            model=model,
            # Permission design (user decision 2026-07-23): "auto" + guardrails.
            # The auto-mode classifier (a separate model) silently approves or
            # DENIES routine calls — it never asks, and can_use_tool does not
            # fire for classifier-decided calls (denials surface to the agent
            # as failed tool calls). ASK_RULES above are the risk gate: ask
            # rules are evaluated before the mode, so those still raise our
            # browser cards. A bare "auto" without ask rules is NOT safe — an
            # earlier test let `rm -rf` outside the repo run with no card.
            permission_mode="auto",
            settings=SETTINGS_JSON,
            system_prompt={"type": "preset", "preset": "claude_code",
                           "append": _system_append()},
            setting_sources=["project"],  # load the repo CLAUDE.md, not user/local
            allowed_tools=ALLOWED_TOOLS,
            can_use_tool=can_use_tool,
            env=env,
            resume=resume,
        )

    @staticmethod
    def can_resume(claude_session: str) -> bool:
        """Whether the CLI still has the transcript to resume from. Resuming a
        missing one fails the whole turn; starting fresh with a notice is the
        better outcome."""
        try:
            return get_session_info(claude_session,
                                    directory=str(espuino.DATA_ROOT)) is not None
        except Exception:  # noqa: BLE001 - an SDK helper, never fatal here
            return True  # let the CLI decide; its error surfaces in the turn

    async def create(self, kind: str, model: str,
                     url: str | None = None, text: str | None = None) -> str:
        sid = secrets.token_hex(4)
        if kind == "url":
            query_text = URL_PROMPT.format(url=url or "")
            echo_text = url or ""
            label = (url or "URL")[:60]
        else:
            query_text = CHAT_PREFIX.format(root=espuino.DATA_ROOT) + (text or "")
            echo_text = text or ""
            label = (text or "Chat").strip().splitlines()[0][:60] if text else "Chat"

        session = self._new_session(sid, kind, model, label)
        with contextlib.suppress(OSError):
            session.progress_file.unlink()  # drop any stale file from a crashed run
        session.save_meta()
        self.sessions[sid] = session
        # The worker connects the CLI before the first turn (the same path a
        # session loaded after a restart takes), so a failing start shows up
        # in the transcript rather than as a failed request.
        session.post_turn(query_text, echo_text)
        return sid

    def _new_session(self, sid: str, kind: str, model: str, label: str) -> Session:
        espuino.APP_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(OSError):
            sessions_dir().mkdir(exist_ok=True)
        return Session(id=sid, kind=kind, model=model, label=label, manager=self,
                       progress_file=espuino.APP_CONFIG_DIR / f"progress-{sid}.json",
                       log_path=sessions_dir() / f"{sid}.jsonl")

    def load(self) -> None:
        """Bring back the sessions of earlier runs, dormant, oldest first.

        A session of another data folder stays on disk but is not shown: its
        conversation is about that library, and the CLI keys transcripts by
        working directory, so it could not be resumed from this one anyway.
        Whatever the previous run left open is closed here: question and
        permission cards nobody can answer any more are marked as such, and a
        session that was mid-turn says it was interrupted."""
        loaded = []
        for path in sessions_dir().glob("*.jsonl"):
            meta, records = None, []
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except OSError:
                continue
            for line in lines:
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue  # a line torn by a crash
                if not isinstance(rec, dict):
                    continue
                if isinstance(rec.get("meta"), dict):
                    meta = rec["meta"]
                elif isinstance(rec.get("seq"), int) and isinstance(rec.get("event"), dict):
                    records.append(rec)
            if (not meta or meta.get("id") != path.stem
                    or meta.get("dataDir") != str(espuino.DATA_ROOT)):
                continue
            session = self._new_session(
                path.stem, str(meta.get("kind", "chat")),
                str(meta.get("model", "sonnet")), str(meta.get("label", "Session")))
            session.created = float(meta.get("created") or path.stat().st_mtime)
            session.claude_session = meta.get("claudeSession")
            session.always = set(meta.get("always") or [])
            session.events = records
            loaded.append(session)
        for session in sorted(loaded, key=lambda s: s.created):
            self.sessions[session.id] = session
            self._settle(session)

    @staticmethod
    def _settle(session: Session) -> None:
        open_requests: list[str] = []
        status = "done"
        for rec in session.events:
            ev = rec["event"]
            if ev.get("k") in ("question", "permission"):
                open_requests.append(ev.get("requestId"))
            elif ev.get("k") == "resolved":
                with contextlib.suppress(ValueError):
                    open_requests.remove(ev.get("requestId"))
            elif ev.get("k") == "status":
                status = ev.get("status", status)
        session.status = status
        for rid in open_requests:
            session.emit({"k": "resolved", "requestId": rid, "expired": True,
                          "label": "Nicht mehr aktiv (Studio wurde beendet)"})
        if status in ("running", "waiting_user"):
            session.emit({"k": "notice", "text": INTERRUPTED_NOTICE})
            session.set_status("interrupted")

    async def remove(self, sid: str) -> bool:
        session = self.sessions.pop(sid, None)
        if session is None:
            return False
        await session.close()
        if session.log_path is not None:
            with contextlib.suppress(OSError):
                session.log_path.unlink()
        return True

    def get(self, sid: str) -> Session | None:
        return self.sessions.get(sid)

    def summaries(self) -> list[dict]:
        return [{"id": s.id, "label": s.label, "model": s.model,
                 "status": s.status, "progress": s.progress,
                 "created": s.created}
                for s in self.sessions.values()]

    def replay(self, sid: str, since: int) -> list[dict] | None:
        session = self.sessions.get(sid)
        if session is None:
            return None
        return [{"seq": e["seq"], "event": e["event"]}
                for e in session.events if e["seq"] > since]

    async def shutdown(self) -> None:
        for session in list(self.sessions.values()):
            await session.close()
        self.sessions.clear()
