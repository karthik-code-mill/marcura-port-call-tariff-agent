"""
LLM Output Guardrail — structured-output enforcement.

Every LLM response in this pipeline is expected to be a valid Pydantic model.
This guardrail validates raw output before it reaches downstream logic,
raising GuardrailViolationError when the schema is violated so the caller
can retry, skip, or escalate — rather than silently propagating malformed data.
"""

import logging
from typing import Any, Type, TypeVar

from pydantic import BaseModel, ValidationError

log = logging.getLogger(__name__)

M = TypeVar("M", bound=BaseModel)


class GuardrailViolationError(Exception):
    """Raised when an LLM response fails schema validation."""


class LLMOutputGuardrail:
    """Enforce that LLM outputs conform to the expected Pydantic schema."""

    def validate(self, raw: Any, model_cls: Type[M]) -> M:
        """
        Coerce and validate *raw* against *model_cls*.

        Accepts:
          - An already-validated instance of model_cls (pass-through)
          - A dict (validated via model_validate)
          - A JSON string (validated via model_validate_json)

        Raises GuardrailViolationError on schema failure so callers can
        implement retry or fallback logic without catching broad exceptions.
        """
        if isinstance(raw, model_cls):
            return raw

        try:
            if isinstance(raw, dict):
                return model_cls.model_validate(raw)
            if isinstance(raw, str):
                return model_cls.model_validate_json(raw)
            # Unexpected type — attempt dict coercion as last resort
            return model_cls.model_validate(vars(raw))
        except (ValidationError, Exception) as exc:
            log.error(
                f"[LLMOutputGuardrail] Schema violation for {model_cls.__name__}: {exc}"
            )
            raise GuardrailViolationError(
                f"LLM output does not match {model_cls.__name__}: {exc}"
            ) from exc
