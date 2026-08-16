from __future__ import annotations

from urllib.parse import urlparse

from .models import Artifact, Step


class SafetyGuard:
    def __init__(self, artifact: Artifact) -> None:
        self.artifact = artifact

    def validate_url(self, url: str) -> None:
        allowed_domains = self.artifact.safety.allowedDomains
        if not allowed_domains:
            return
        hostname = urlparse(url).hostname or ""
        allowed = any(hostname == domain or hostname.endswith(f".{domain}") for domain in allowed_domains)
        if not allowed:
            raise RuntimeError(f"Blocked navigation to non-allowed domain: {hostname}")

    def validate_step(self, step: Step) -> None:
        blocked = set(self.artifact.safety.blockedActions)
        if step.kind == "humanApproval":
            return
        description = (step.description or "").lower()
        if "delete" in description and "delete" in blocked:
            raise RuntimeError(f"Blocked dangerous step: {step.id}")
        if "payment" in description and "submit_payment" in blocked:
            raise RuntimeError(f"Blocked payment-related step: {step.id}")
        if "export" in description and "export_data" in blocked:
            raise RuntimeError(f"Blocked export-related step: {step.id}")
        if "admin" in description and "admin_change" in blocked:
            raise RuntimeError(f"Blocked admin-change step: {step.id}")

    def mask_value(self, step: Step, value: str | None) -> str | None:
        if value is None:
            return None
        if step.sensitive and self.artifact.safety.redactInputs:
            return "***REDACTED***"
        return value

    def redact_text(self, value: str | None, sensitive: bool = True) -> str | None:
        if value is None:
            return None
        if sensitive and (self.artifact.safety.redactInputs or self.artifact.safety.redactOutputs):
            return "***REDACTED***"
        return value

    def redact_known_values(self, text: str, values: list[str]) -> str:
        redacted = text
        for value in values:
            if value:
                redacted = redacted.replace(value, "***REDACTED***")
        return redacted
