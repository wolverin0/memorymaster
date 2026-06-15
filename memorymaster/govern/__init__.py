"""Governance layer: steward cycle, LLM steward, resolvers, lifecycle jobs.

P2 restructure subpackage. Hosts the deterministic steward, the LLM steward
and its budget guard, the scheduler daemon, feedback/quality scoring,
conflict/auto resolvers, candidate dedupe, claim verification, contradiction
probing, verbatim cleanup, and the reliability job modules (``govern.jobs``).
"""
