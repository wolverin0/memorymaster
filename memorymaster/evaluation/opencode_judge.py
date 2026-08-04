"""Evaluation aliases for the shared bounded OpenCode OAuth client."""

from memorymaster.core.opencode_client import (
    OpenCodeClient as OpenCodeJudge,
    OpenCodeClientError as OpenCodeJudgeError,
    OpenCodeClientResult as OpenCodeJudgeResult,
)

__all__ = ["OpenCodeJudge", "OpenCodeJudgeError", "OpenCodeJudgeResult"]
