# 0012 README Stays Concise, Handbook Holds Operator Depth

Date: 2026-04-26

Status: Accepted

Source Claims: claim #12638

## Context

The README had grown into an operator manual, mixing quick-start material with detailed guidance on hooks, dashboards, steward cycles, dream bridge, wiki engine, entity registry, OpenClaw/GitNexus, troubleshooting, performance SLOs, and agent installation.

This made the project entry point harder to scan.

## Decision

Keep the README concise and move operator-depth documentation into the handbook.

The README should orient new users and point to deeper material. The handbook should hold operational procedures and advanced system details.

## Consequences

New readers get a smaller entry point.

Operators still have a place for detailed procedures.

Future documentation should preserve this split instead of rebuilding a single long README.
