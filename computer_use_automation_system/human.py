from __future__ import annotations

from .models import Step


def request_human_approval(step: Step) -> None:
    detail = f" ({step.description})" if step.description else ""
    answer = input(f"Human approval required for step {step.id}{detail}. Approve? (yes/no): ")
    if answer.strip().lower() != "yes":
        raise RuntimeError(f"Human rejected step {step.id}")
