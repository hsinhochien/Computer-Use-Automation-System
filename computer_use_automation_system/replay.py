from __future__ import annotations

from pathlib import Path
import re

from playwright.sync_api import Page, sync_playwright

from .artifact_io import load_artifact
from .human import request_human_approval
from .logger import Logger
from .models import ReplayResult, Step, SuccessCondition
from .safety import SafetyGuard


class ReplayOptions:
    def __init__(self, artifact_path: str, headless: bool = False, parameters: dict[str, str] | None = None) -> None:
        self.artifact_path = artifact_path
        self.headless = headless
        self.parameters = parameters or {}


def interpolate(value: str | None, parameters: dict[str, str]) -> str | None:
    if value is None:
        return None

    def replace(match: re.Match[str]) -> str:
        key = match.group(1)
        return parameters.get(key, "")

    return re.sub(r"\{\{\s*([\w.-]+)\s*\}\}", replace, value)


def run_step(page: Page, step: Step, parameters: dict[str, str], logger: Logger, guard: SafetyGuard) -> None:
    guard.validate_step(step)

    if step.kind == "goto":
        url = interpolate(step.url, parameters)
        if not url:
            raise RuntimeError(f"Step {step.id} missing url")
        guard.validate_url(url)
        logger.info("Navigating", {"stepId": step.id, "url": url})
        page.goto(url, wait_until="domcontentloaded", timeout=step.timeoutMs or 30000)
        return

    if step.kind == "click":
        if not step.selector:
            raise RuntimeError(f"Step {step.id} missing selector")
        logger.info("Click", {"stepId": step.id, "selector": step.selector})
        page.locator(step.selector).click(timeout=step.timeoutMs or 10000)
        return

    if step.kind == "fill":
        if not step.selector:
            raise RuntimeError(f"Step {step.id} missing selector")
        value = interpolate(step.value, parameters) or ""
        logger.info(
            "Fill",
            {"stepId": step.id, "selector": step.selector, "value": guard.mask_value(step, value)},
        )
        page.locator(step.selector).fill(value, timeout=step.timeoutMs or 10000)
        return

    if step.kind == "press":
        if not step.selector or not step.key:
            raise RuntimeError(f"Step {step.id} missing selector or key")
        logger.info("Press", {"stepId": step.id, "selector": step.selector, "key": step.key})
        page.locator(step.selector).press(step.key, timeout=step.timeoutMs or 10000)
        return

    if step.kind == "waitFor":
        if not step.selector:
            raise RuntimeError(f"Step {step.id} missing selector")
        logger.info("WaitFor", {"stepId": step.id, "selector": step.selector})
        page.locator(step.selector).wait_for(timeout=step.timeoutMs or 10000)
        return

    if step.kind == "extractText":
        if not step.selector:
            raise RuntimeError(f"Step {step.id} missing selector")
        logger.info("ExtractText", {"stepId": step.id, "selector": step.selector, "outputKey": step.outputKey})
        text = (page.locator(step.selector).text_content(timeout=step.timeoutMs or 10000) or "").strip()
        if step.outputKey:
            parameters[step.outputKey] = text
        logger.info(
            "Extracted",
            {"stepId": step.id, "outputKey": step.outputKey, "value": "***REDACTED***" if step.sensitive else text},
        )
        return

    if step.kind == "assertText":
        if not step.selector:
            raise RuntimeError(f"Step {step.id} missing selector")
        expected = interpolate(step.expectedText, parameters) or ""
        actual = (page.locator(step.selector).text_content(timeout=step.timeoutMs or 10000) or "").strip()
        logger.info(
            "AssertText",
            {"stepId": step.id, "expected": expected, "actual": "***REDACTED***" if step.sensitive else actual},
        )
        if expected not in actual:
            raise RuntimeError(f"Assertion failed at {step.id}: expected '{expected}' in '{actual}'")
        return

    if step.kind == "humanApproval":
        logger.warn(
            "Awaiting human approval",
            {"stepId": step.id, "description": step.description, "risk": step.risk},
        )
        request_human_approval(step)
        return

    if step.kind == "screenshot":
        screenshots_dir = Path("screenshots") / "replay"
        screenshots_dir.mkdir(parents=True, exist_ok=True)
        path = screenshots_dir / f"replay-{step.id}.png"
        logger.info("Screenshot", {"stepId": step.id, "path": str(path)})
        page.screenshot(path=str(path), full_page=True)
        return

    if step.kind == "done":
        logger.info("Done marker reached", {"stepId": step.id})
        return

    raise RuntimeError(f"Unsupported step kind: {step.kind}")


def _evaluate_success_condition(page: Page, condition: SuccessCondition | None) -> tuple[bool, str | None, str | None]:
    if condition is None:
        return True, None, None

    actual = (page.locator(condition.selector).text_content(timeout=10000) or "").strip()
    if condition.type == "text_not_equals":
        return actual != condition.expectedText, f"text != {condition.expectedText}", actual
    if condition.type == "text_contains":
        return condition.expectedText in actual, f"text contains {condition.expectedText}", actual
    return False, "unknown success condition", actual


def replay_artifact(options: ReplayOptions) -> ReplayResult:
    artifact = load_artifact(options.artifact_path)
    logger = Logger()
    guard = SafetyGuard(artifact)
    parameters = dict(options.parameters)
    evidence: dict[str, str] = {}

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=options.headless)
        page = browser.new_page()
        try:
            guard.validate_url(artifact.startUrl)
            page.goto(artifact.startUrl, wait_until="domcontentloaded")
            for step in artifact.steps:
                try:
                    run_step(page, step, parameters, logger, guard)
                    if step.kind == "screenshot":
                        evidence["screenshot"] = str((Path("screenshots") / "replay" / f"replay-{step.id}.png"))
                except Exception as error:
                    logger.error("Step failed", {"stepId": step.id, "error": str(error)})
                    if step.continueOnError:
                        return ReplayResult(
                            status="recoverable",
                            artifactId=artifact.artifactId,
                            outputs={key: parameters[key] for key in [output.name for output in artifact.outputs] if key in parameters},
                            message="Recoverable condition encountered and step marked continueOnError.",
                            failedStepId=step.id,
                            observed=str(error),
                            evidence=evidence,
                        )
                    return ReplayResult(
                        status="failure",
                        artifactId=artifact.artifactId,
                        outputs={key: parameters[key] for key in [output.name for output in artifact.outputs] if key in parameters},
                        message="Replay failed.",
                        failedStepId=step.id,
                        observed=str(error),
                        evidence=evidence,
                    )

            success, expected, observed = _evaluate_success_condition(page, artifact.successCondition)
            declared_outputs = {key: parameters[key] for key in [output.name for output in artifact.outputs] if key in parameters}

            if success:
                return ReplayResult(
                    status="success",
                    artifactId=artifact.artifactId,
                    outputs=declared_outputs,
                    message="Replay completed successfully.",
                    expected=expected,
                    observed=guard.redact_text(observed, sensitive=True),
                    evidence=evidence,
                )

            if observed in {"", "--", "USD 0"}:
                return ReplayResult(
                    status="business_outcome",
                    artifactId=artifact.artifactId,
                    outputs=declared_outputs,
                    businessOutcomeCode="no_result_or_empty_balance",
                    message="Replay completed but the business result indicates no usable balance was returned.",
                    expected=expected,
                    observed=guard.redact_text(observed, sensitive=True),
                    evidence=evidence,
                )

            return ReplayResult(
                status="failure",
                artifactId=artifact.artifactId,
                outputs=declared_outputs,
                message="Replay ended without satisfying the declared success condition.",
                expected=expected,
                observed=guard.redact_text(observed, sensitive=True),
                evidence=evidence,
            )
        finally:
            browser.close()
