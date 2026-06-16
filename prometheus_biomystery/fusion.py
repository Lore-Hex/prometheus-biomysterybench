from __future__ import annotations

from collections.abc import Sequence
from typing import Any

FUSION_MODEL = "trustedrouter/fusion"
FUSION_TOOL_TYPE = "trustedrouter:fusion"

# GLM 5.2 is the requested model, but it has not been available in the current
# production entitlement. Keep the panel runnable with GLM 5.1 until 5.2 smokes.
DEFAULT_FUSION_PANEL: tuple[str, ...] = (
    "openai/gpt-5.5",
    "anthropic/claude-opus-4.8",
    "moonshotai/kimi-k2.7-code",
    "z-ai/glm-5.1",
    "minimax/minimax-m3",
    "google/gemini-3-flash-preview",
    "google/gemini-3.1-pro-preview",
)
DEFAULT_FUSION_JUDGE_MODEL = "z-ai/glm-5.1"
DEFAULT_PROMETHEUSBENCH_FUSION_SELECTION = "first_non_refusal"


def parse_model_list(raw: str | None, *, default: Sequence[str]) -> tuple[str, ...]:
    if raw is None:
        return tuple(default)
    models = tuple(part.strip() for part in raw.split(",") if part.strip())
    if not models:
        raise ValueError("model list must contain at least one model")
    return models


def fusion_parameters(
    *,
    panel: Sequence[str],
    judge_model: str,
    max_completion_tokens: int,
    selection_strategy: str | None = None,
) -> dict[str, Any]:
    params: dict[str, Any] = {
        "analysis_models": list(panel),
        "model": judge_model,
        "max_completion_tokens": max_completion_tokens,
    }
    if selection_strategy:
        params["selection_strategy"] = selection_strategy
    return params


def fusion_tool(
    *,
    panel: Sequence[str],
    judge_model: str,
    max_completion_tokens: int,
    selection_strategy: str | None = None,
) -> dict[str, Any]:
    return {
        "type": FUSION_TOOL_TYPE,
        "parameters": fusion_parameters(
            panel=panel,
            judge_model=judge_model,
            max_completion_tokens=max_completion_tokens,
            selection_strategy=selection_strategy,
        ),
    }


def fusion_plugin(
    *,
    panel: Sequence[str],
    judge_model: str,
    max_completion_tokens: int,
    selection_strategy: str | None = None,
) -> dict[str, Any]:
    return {
        "id": "fusion",
        **fusion_parameters(
            panel=panel,
            judge_model=judge_model,
            max_completion_tokens=max_completion_tokens,
            selection_strategy=selection_strategy,
        ),
    }
