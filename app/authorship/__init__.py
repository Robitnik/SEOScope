"""Хто створив звіт/пакет (task-93). Див. план §2 (D1/D2) для контексту."""

from __future__ import annotations

from app.authorship.model import Attribution
from app.authorship.registry import RESOURCE_MODELS, resolve_model

__all__ = ["RESOURCE_MODELS", "Attribution", "resolve_model"]
