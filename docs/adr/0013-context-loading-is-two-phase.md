# 0013 Context Loading Is Two Phase

Date: 2026-05-03

Status: Accepted

Source Claims: claim #35013, claim #35017

## Context

MemoryMaster exists to provide relevant context to agent sessions. A single flat recall payload can overfit to the immediate prompt and miss broad project constraints or foundational facts.

The claims describe task briefing injection as the L1 layer for multi-layer context compilation and identify a two-phase loading approach.

## Decision

Context loading uses two phases:

1. Foundational claims are loaded first to provide broad project and system context.
2. Task briefing is injected after that to focus the session on the immediate work.

## Consequences

Agents can see durable constraints before task-specific instructions narrow attention.

Recall and briefing code should preserve the separation between foundational context and task briefing.

Future context-loading optimizations must be evaluated against both broad visibility and immediate-task relevance.
