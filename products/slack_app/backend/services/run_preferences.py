"""Which model a Slack-triggered run actually uses.

One precedence chain, resolved in one place: a model named in the mention itself
("use fable for this one") beats the personal row, which beats the workspace row,
which falls back to the Slack default. `slack_settings` owns the personal-vs-workspace
half; this module owns the ends — the default underneath and the mention override on
top — so no caller has to assemble a triple by hand.

The triple is not three independent values. The runtime adapter follows from the
model, and which reasoning efforts exist depends on that pair, so every result is
built through `_coherent_preferences` rather than field by field. A model named in a
mention additionally has to be in the live catalogue (`model_catalogue`) — the same
set the App Home picker offers — so an override can never select something the picker
itself would refuse.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from products.slack_app.backend.services.model_catalogue import (
    ModelChoice,
    available_model_choices,
    filter_unsupported_effort,
    runtime_adapter_for,
)
from products.slack_app.backend.services.slack_settings import AIPreferences, resolve_ai_preferences

if TYPE_CHECKING:
    from posthog.models.integration import Integration

# What a Slack run uses when neither the user nor the workspace has pinned a model.
# Chosen here rather than left to the agent server so the App Home card and the run
# itself agree on what "unset" means.
SLACK_DEFAULT_MODEL = "claude-opus-5"


class ModelOverride(Protocol):
    """What a mention asked for. Structural so the Temporal payload satisfies it
    without this module importing anything from `posthog/temporal/`."""

    model: str | None
    reasoning_effort: str | None


class RunDefaults(Protocol):
    """The triple a resolver downstream of Slack would supply on its own. Structural so
    the tasks product's `ResolvedAIRunConfig` satisfies it without an import here, and
    read-only so a frozen dataclass qualifies."""

    @property
    def runtime_adapter(self) -> str | None: ...

    @property
    def model(self) -> str | None: ...

    @property
    def reasoning_effort(self) -> str | None: ...


def _coherent_preferences(
    model: str | None,
    reasoning_effort: str | None,
    *,
    fallback_runtime_adapter: str | None,
) -> AIPreferences:
    """Assemble the one self-consistent triple for a model.

    The runtime adapter is derived rather than passed in, and an effort the pair
    doesn't support is dropped. `fallback_runtime_adapter` covers a model the tasks
    catalogue no longer lists, where a stored adapter is the best information left.
    """
    runtime_adapter = runtime_adapter_for(model) or fallback_runtime_adapter
    effort = filter_unsupported_effort(
        runtime_adapter, model, reasoning_effort.strip().lower() if reasoning_effort else None
    )
    return AIPreferences(runtime_adapter=runtime_adapter, model=model, reasoning_effort=effort)


def resolve_run_preferences(
    integration: Integration,
    slack_user_id: str | None,
    *,
    override: ModelOverride | None = None,
    default_model: str | None = SLACK_DEFAULT_MODEL,
    deferred_default: RunDefaults | None = None,
) -> AIPreferences:
    """Resolve the full chain for one Slack-triggered run.

    A model named in the mention replaces the pair outright: an effort saved against
    the previous model must not ride along onto a different one. An effort named on its
    own applies to whichever model the run was already going to use. Either can be
    absent, and a request we can't honour — a model that isn't on offer, an effort the
    model doesn't support — leaves the run on its saved preferences.

    `default_model` is what the run falls back to once nothing in Slack applies. Passing
    `None` yields an empty result, which is meaningful to the caller: it defers the choice
    to whatever resolves the run downstream — the project or user default — rather than
    pinning a model here and putting those defaults out of reach. `deferred_default` is
    the triple that resolver would supply, needed only to make sense of an effort the
    mention named on its own.

    Note that `resolve_ai_preferences` yields nothing at all for a workspace that
    hasn't enabled `slack-app-home`, so there the chain is the fallback plus
    whatever the mention asked for.
    """
    override_model = override.model if override else None
    override_effort = override.reasoning_effort if override else None

    if override_model:
        # Only a model named in the mention needs the catalogue, and the saved rows
        # can't influence the result, so neither is read on the other paths.
        choice = find_model_choice(override_model, available_model_choices())
        if choice is not None:
            return _coherent_preferences(choice.model, override_effort, fallback_runtime_adapter=choice.runtime_adapter)

    saved = resolve_ai_preferences(integration, slack_user_id)
    base = _coherent_preferences(
        saved.model or default_model,
        saved.reasoning_effort,
        fallback_runtime_adapter=saved.runtime_adapter,
    )
    if not override_effort:
        return base

    # An effort has to be validated against a model, and deferring leaves none here. So an
    # effort-only mention resolves against what the downstream resolver would have chosen
    # anyway — a run at a different effort is not the run it would have produced, so there
    # is nothing left to defer.
    target = base
    if base.model is None and deferred_default is not None and deferred_default.model:
        target = _coherent_preferences(
            deferred_default.model,
            deferred_default.reasoning_effort,
            fallback_runtime_adapter=deferred_default.runtime_adapter,
        )

    # An effort this model can't do is dropped by `_coherent_preferences`; falling back
    # to `base` rather than to the stripped result means an impossible ask leaves the
    # run alone — still deferring where it was deferring — instead of quietly clearing
    # the saved effort as well.
    requested = _coherent_preferences(target.model, override_effort, fallback_runtime_adapter=target.runtime_adapter)
    return requested if requested.reasoning_effort else base


def find_model_choice(model: str | None, choices: tuple[ModelChoice, ...]) -> ModelChoice | None:
    """Match a requested model id against the catalogue, case-insensitively."""
    if not model:
        return None
    normalized = model.strip().lower()
    return next((c for c in choices if c.model.lower() == normalized), None)


__all__ = [
    "SLACK_DEFAULT_MODEL",
    "find_model_choice",
    "resolve_run_preferences",
]
