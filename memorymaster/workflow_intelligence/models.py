"""Typed normalized records stored by Workflow Intelligence."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class SessionRecord:
    session_id: str
    external_id: str
    provider: str
    session_kind: str
    project_scope: str = "global"
    root_session_hash: str = ""
    parent_session_hash: str = ""
    model: str = ""
    branch: str = ""
    worktree: str = ""
    started_at: str = ""
    ended_at: str = ""
    initial_request_excerpt: str = ""
    task_category: str = "unknown"
    deep_parsed: bool = False


@dataclass(frozen=True, slots=True)
class TurnRecord:
    turn_id: str
    session_id: str
    ordinal: int
    role: str
    excerpt: str
    timestamp: str
    byte_start: int
    byte_end: int
    is_a2a: bool = False


@dataclass(frozen=True, slots=True)
class ActionRecord:
    action_id: str
    session_id: str
    turn_id: str
    ordinal: int
    kind: str
    name: str
    status: str = "unknown"
    command_family: str = ""
    byte_start: int = 0
    byte_end: int = 0
    metadata_json: str = "{}"


@dataclass(frozen=True, slots=True)
class FeedbackRecord:
    feedback_id: str
    session_id: str
    turn_id: str
    kind: str
    theme: str
    excerpt: str
    confidence: float
    user_origin: bool = True

