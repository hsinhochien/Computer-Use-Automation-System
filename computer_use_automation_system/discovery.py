from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import Page, sync_playwright

from .logger import Logger
from .llm_client import LLMClient
from .models import Artifact, DiscoveryAction, OutputSpec, ParameterSpec, SensitivePolicy, Step, SuccessCondition, TargetSpec
from .safety import SafetyGuard


def _task_to_parameters(task: str) -> dict[str, str]:
    match = re.search(r"(?:member\s*)?(\d{3,})", task, re.IGNORECASE)
    return {"memberId": match.group(1)} if match else {}


def _interpolate(value: str | None, parameters: dict[str, str]) -> str | None:
    if value is None:
        return None
    result = value
    for key, item in parameters.items():
        result = result.replace(f"{{{{{key}}}}}", item)
    return result


def _page_context(
    page: Page,
    screenshot_path: str,
    experiment_fake_screenshot_path: bool = False,
    experiment_drop_dom_summary: bool = False,
) -> dict:
    dom_observation = page.evaluate(
        """
        () => {
          const nodes = Array.from(document.querySelectorAll('input, button, [role="button"], [id], [name], [data-testid], [data-role], [aria-label]')).slice(0, 80);
          const inputs = Array.from(document.querySelectorAll('input, textarea')).slice(0, 20).map((el, index) => ({
            selectorHint: el.id ? `#${el.id}` : el.getAttribute('aria-label') ? `${el.tagName.toLowerCase()}[aria-label="${el.getAttribute('aria-label')}"]` : `${el.tagName.toLowerCase()}:nth-of-type(${index + 1})`,
            value: (el.value || '').trim(),
            type: el.getAttribute('type'),
            placeholder: el.getAttribute('placeholder'),
            ariaLabel: el.getAttribute('aria-label'),
            name: el.getAttribute('name'),
          }));
          const buttons = Array.from(document.querySelectorAll('button, [role="button"], input[type="button"], input[type="submit"]')).slice(0, 20).map((el, index) => ({
            selectorHint: el.id ? `#${el.id}` : el.className ? `${el.tagName.toLowerCase()}.${String(el.className).trim().split(/\s+/).join('.')}` : `${el.tagName.toLowerCase()}:nth-of-type(${index + 1})`,
            text: (el.innerText || el.textContent || el.getAttribute('value') || '').trim().slice(0, 120),
            ariaLabel: el.getAttribute('aria-label'),
          }));
          const balanceCandidates = Array.from(document.querySelectorAll('#balance, [data-role="balance-text"], [data-testid*="balance"], [id*="balance"], [class*="balance"]')).slice(0, 10).map((el, index) => ({
            selectorHint: el.id ? `#${el.id}` : el.getAttribute('data-role') ? `[data-role="${el.getAttribute('data-role')}"]` : `${el.tagName.toLowerCase()}:nth-of-type(${index + 1})`,
            text: (el.innerText || el.textContent || '').trim().slice(0, 120),
          }));
          const visibleText = (document.body.innerText || '').trim().slice(0, 4000);
          const domSummary = nodes.map((el) => ({
            tag: el.tagName.toLowerCase(),
            id: el.id || null,
            name: el.getAttribute('name'),
            text: (el.innerText || el.textContent || '').trim().slice(0, 120),
            placeholder: el.getAttribute('placeholder'),
            type: el.getAttribute('type'),
            dataTestId: el.getAttribute('data-testid'),
            dataRole: el.getAttribute('data-role'),
            ariaLabel: el.getAttribute('aria-label'),
          }));
          return { domSummary, inputs, buttons, balanceCandidates, visibleText };
        }
        """
    )
    dom_summary = dom_observation["domSummary"]
    inputs = dom_observation["inputs"]
    buttons = dom_observation["buttons"]
    balance_candidates = dom_observation["balanceCandidates"]
    visible_text = dom_observation["visibleText"]

    member_id_value = next((item["value"] for item in inputs if item.get("value")), "")
    balance_candidate_for_selector = balance_candidates[0] if balance_candidates else None
    balance_candidate = next((item for item in balance_candidates if item.get("text") and item.get("text") != "--"), None)
    balance_text = balance_candidate["text"] if balance_candidate else ""
    result_selector = balance_candidate_for_selector["selectorHint"] if balance_candidate_for_selector else "#balance"
    search_button_visible = bool(buttons)
    primary_button_selector = buttons[0]["selectorHint"] if buttons else "button"
    member_input_selector = next((item["selectorHint"] for item in inputs if "member" in ((item.get("ariaLabel") or "") + (item.get("placeholder") or "") + (item.get("name") or "")).lower()), inputs[0]["selectorHint"] if inputs else "input")
    currency_like_match = re.search(r"(?:USD|\$)\s?\d[\d,]*(?:\.\d{2})?", visible_text)
    inferred_balance_text = balance_text or (currency_like_match.group(0) if currency_like_match else "")

    return {
        "url": page.url,
        "title": page.title(),
        "dom_summary": [] if experiment_drop_dom_summary else dom_summary,
        "screenshot_path": "fake://nonexistent-screenshot.png" if experiment_fake_screenshot_path else screenshot_path,
        "member_id_value": member_id_value,
        "balance_text": inferred_balance_text,
        "search_button_visible": search_button_visible,
        "result_selector": result_selector,
        "primary_button_selector": primary_button_selector,
        "member_input_selector": member_input_selector,
        "visible_text_excerpt": visible_text[:500],
        "experiment_flags": {
            "fake_screenshot_path": experiment_fake_screenshot_path,
            "drop_dom_summary": experiment_drop_dom_summary,
        },
        "hints": {
            "member_id_already_filled": bool(member_id_value),
            "balance_ready": bool(inferred_balance_text and inferred_balance_text != "--"),
        },
    }


def _build_target_spec(selector: str | None) -> TargetSpec | None:
    if not selector:
        return None
    reasoning = "Stable CSS ID selector captured during discovery" if selector.startswith("#") else "CSS selector captured during discovery"
    robustness = "high" if selector.startswith("#") else "medium"
    return TargetSpec(
        strategy="css",
        primary=selector,
        fallbacks=[],
        robustness=robustness,
        reasoning=reasoning,
    )


def _normalize_output_key(action: DiscoveryAction) -> str | None:
    if action.kind != "extractText":
        return action.outputKey
    selector = (action.selector or "").lower()
    output_key = action.outputKey or ""
    description = (action.description or "").lower()
    if "balance" in selector or "balance" in output_key.lower() or "balance" in description:
        return "balance"
    return action.outputKey


def _action_to_step(index: int, action: DiscoveryAction) -> Step:
    return Step(
        id=f"step-{index:03d}-{action.kind}",
        kind=action.kind,
        selector=action.selector,
        target=_build_target_spec(action.selector),
        url=action.url,
        value=action.value,
        key=action.key,
        outputKey=_normalize_output_key(action),
        expectedText=action.expectedText,
        description=action.description,
        risk=action.risk,
        sensitive=action.sensitive,
        continueOnError=action.continueOnError,
    )


def _execute_discovery_action(page: Page, action: DiscoveryAction, parameters: dict[str, str], logger: Logger) -> None:
    if action.kind == "goto":
        url = _interpolate(action.url, parameters)
        if not url:
            raise RuntimeError("Discovery action goto missing url")
        page.goto(url, wait_until="domcontentloaded", timeout=30000)
        logger.info("Discovery goto", {"url": url})
        return
    if action.kind == "click":
        if not action.selector:
            raise RuntimeError("Discovery action click missing selector")
        page.locator(action.selector).click(timeout=10000)
        logger.info("Discovery click", {"selector": action.selector})
        return
    if action.kind == "fill":
        if not action.selector:
            raise RuntimeError("Discovery action fill missing selector")
        value = _interpolate(action.value, parameters) or ""
        page.locator(action.selector).fill(value, timeout=10000)
        logger.info("Discovery fill", {"selector": action.selector, "value": "***REDACTED***" if action.sensitive else value})
        return
    if action.kind == "press":
        if not action.selector or not action.key:
            raise RuntimeError("Discovery action press missing selector/key")
        page.locator(action.selector).press(action.key, timeout=10000)
        logger.info("Discovery press", {"selector": action.selector, "key": action.key})
        return
    if action.kind == "waitFor":
        if not action.selector:
            raise RuntimeError("Discovery action waitFor missing selector")
        page.locator(action.selector).wait_for(timeout=10000)
        logger.info("Discovery waitFor", {"selector": action.selector})
        return
    if action.kind == "extractText":
        if not action.selector:
            raise RuntimeError("Discovery action extractText missing selector")
        text = (page.locator(action.selector).text_content(timeout=10000) or "").strip()
        normalized_output_key = _normalize_output_key(action)
        if normalized_output_key:
            parameters[normalized_output_key] = text
        logger.info("Discovery extractText", {"selector": action.selector, "outputKey": normalized_output_key, "value": "***REDACTED***" if action.sensitive else text})
        return
    if action.kind == "assertText":
        if not action.selector:
            raise RuntimeError("Discovery action assertText missing selector")
        actual = (page.locator(action.selector).text_content(timeout=10000) or "").strip()
        expected = _interpolate(action.expectedText, parameters) or ""
        if expected not in actual:
            raise RuntimeError(f"Discovery assertion failed: expected '{expected}' in '{actual}'")
        logger.info("Discovery assertText", {"selector": action.selector, "expected": expected})
        return
    if action.kind == "humanApproval":
        logger.warn("Discovery human approval requested", {"description": action.description, "risk": action.risk})
        answer = input(f"Discovery requests human approval: {action.description or action.kind}. Approve? (yes/no): ")
        if answer.strip().lower() != "yes":
            raise RuntimeError("Human rejected discovery action")
        return
    if action.kind == "screenshot":
        shots_dir = Path("screenshots") / "discovery"
        shots_dir.mkdir(parents=True, exist_ok=True)
        path = shots_dir / f"discovery-step-{len(list(shots_dir.glob('discovery-step-*-artifact.png')))+1:03d}-artifact.png"
        page.screenshot(path=str(path), full_page=True)
        logger.info("Discovery screenshot", {"path": str(path)})
        return
    if action.kind == "done":
        logger.info("Discovery done")
        return
    raise RuntimeError(f"Unsupported discovery action: {action.kind}")


def _is_repeated_action(steps: list[Step], action: DiscoveryAction) -> bool:
    if not steps:
        return False
    last = steps[-1]
    return (
        last.kind == action.kind
        and last.selector == action.selector
        and last.value == action.value
        and last.key == action.key
        and last.url == action.url
    )


def _has_step(steps: list[Step], kind: str, selector: str | None = None, output_key: str | None = None) -> bool:
    return any(
        step.kind == kind
        and (selector is None or step.selector == selector)
        and (output_key is None or step.outputKey == output_key)
        for step in steps
    )


def _append_finalize_steps(
    artifact: Artifact,
    page: Page,
    logger: Logger,
    experiment_fake_screenshot_path: bool = False,
    experiment_drop_dom_summary: bool = False,
) -> None:
    context = _page_context(
        page,
        screenshot_path="finalize",
        experiment_fake_screenshot_path=experiment_fake_screenshot_path,
        experiment_drop_dom_summary=experiment_drop_dom_summary,
    )
    if not context["hints"]["balance_ready"]:
        return

    result_selector = context.get("result_selector") or "#balance"
    next_index = len(artifact.steps) + 1
    finalize_steps: list[Step] = []

    if not _has_step(artifact.steps, "waitFor", selector=result_selector):
        finalize_steps.append(
            Step(
                id=f"step-{next_index:03d}-waitFor",
                kind="waitFor",
                selector=result_selector,
                target=_build_target_spec(result_selector),
                description="Wait for the balance result to appear",
                risk="low",
                sensitive=False,
                continueOnError=False,
            )
        )
        next_index += 1

    if not _has_step(artifact.steps, "extractText", selector=result_selector, output_key="balance"):
        finalize_steps.append(
            Step(
                id=f"step-{next_index:03d}-extractText",
                kind="extractText",
                selector=result_selector,
                target=_build_target_spec(result_selector),
                outputKey="balance",
                description="Extract the balance text",
                risk="low",
                sensitive=True,
                continueOnError=False,
            )
        )
        next_index += 1

    if not _has_step(artifact.steps, "screenshot"):
        finalize_steps.append(
            Step(
                id=f"step-{next_index:03d}-screenshot",
                kind="screenshot",
                description="Save a screenshot of the result",
                risk="low",
                sensitive=True,
                continueOnError=False,
            )
        )

    artifact.steps.extend(finalize_steps)

    logger.info(
        "Discovery finalize steps appended",
        {"recorded_steps_count": len(artifact.steps), "balance_text": "***REDACTED***"},
    )


def discover_task_to_artifact(
    task: str,
    start_url: str,
    use_llm: bool = False,
    max_steps: int = 12,
    experiment_fake_screenshot_path: bool = False,
    experiment_drop_dom_summary: bool = False,
    image_mode: str = "auto",
) -> Artifact:
    hostname = urlparse(start_url).hostname or ""
    parameters = _task_to_parameters(task)
    logger = Logger()

    if not use_llm:
        raise RuntimeError("This discovery mode now requires --use-llm to satisfy the assignment's discovery requirement.")

    client = LLMClient()
    thoughts: list[str] = []

    artifact = Artifact(
        artifactId="member-balance-query",
        name="Member Balance Query",
        description="LLM-driven discovered workflow",
        taskTemplate="Check the balance for member {{memberId}}",
        startUrl=start_url,
        parameters=[
            ParameterSpec(name="memberId", type="string", required=True, secret=False, description="Member identifier")
        ],
        outputs=[
            OutputSpec(name="balance", type="string", description="Member balance text", sensitive=True)
        ],
        successCondition=None,
        safety=SensitivePolicy(
            redactInputs=True,
            redactOutputs=True,
            allowedDomains=[hostname] if hostname else [],
            blockedActions=["delete", "submit_payment", "export_data", "admin_change"],
        ),
        steps=[],
    )
    guard = SafetyGuard(artifact)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        page = browser.new_page()
        try:
            guard.validate_url(start_url)
            page.goto(start_url, wait_until="domcontentloaded")

            initial_context = _page_context(
                page,
                screenshot_path="initial",
                experiment_fake_screenshot_path=experiment_fake_screenshot_path,
                experiment_drop_dom_summary=experiment_drop_dom_summary,
            )
            artifact.successCondition = SuccessCondition(
                type="text_not_equals",
                selector=initial_context.get("result_selector") or "#balance",
                expectedText="--",
                description="The result element should contain a resolved balance instead of the placeholder.",
            )

            for index in range(1, max_steps + 1):
                shots_dir = Path("screenshots") / "discovery"
                shots_dir.mkdir(parents=True, exist_ok=True)
                screenshot_path = shots_dir / f"discovery-step-{index:03d}-context.png"
                page.screenshot(path=str(screenshot_path), full_page=True)
                context = _page_context(
                    page,
                    str(screenshot_path),
                    experiment_fake_screenshot_path=experiment_fake_screenshot_path,
                    experiment_drop_dom_summary=experiment_drop_dom_summary,
                )
                logger.info(
                    "Discovery observe",
                    {
                        "step": index,
                        "url": context["url"],
                        "title": context["title"],
                        "hints": context["hints"],
                        "member_id_value": guard.redact_text(context["member_id_value"], sensitive=True),
                        "balance_text": guard.redact_text(context["balance_text"], sensitive=True),
                    },
                )
                if context["hints"]["balance_ready"]:
                    logger.info("Discovery heuristic", {"step": index, "message": "Balance appears ready; prefer extraction or completion."})
                decision = client.decide_next_action(task=task, page_context=context, steps=artifact.steps, image_mode=image_mode)
                thoughts.append(decision.thought)
                redacted_thought = guard.redact_known_values(
                    decision.thought,
                    [parameters.get("memberId", ""), context.get("balance_text", "")],
                )
                logger.info("LLM decision", {"step": index, "thought": redacted_thought, "action": decision.action.model_dump()})

                if decision.action.kind == "done":
                    break

                if _is_repeated_action(artifact.steps, decision.action):
                    logger.warn(
                        "Repeated action blocked",
                        {"step": index, "kind": decision.action.kind, "selector": decision.action.selector},
                    )
                    if context["hints"]["member_id_already_filled"] and context["search_button_visible"]:
                        decision.action = DiscoveryAction(
                            kind="click",
                            selector=context.get("primary_button_selector") or "button",
                            description="Click the primary visible action button after the member ID is already filled",
                            risk="low",
                            sensitive=False,
                            continueOnError=False,
                        )
                        logger.info("Discovery heuristic override", {"step": index, "action": decision.action.model_dump()})
                    else:
                        raise RuntimeError("Discovery got stuck repeating the same action")

                temp_step = _action_to_step(index, decision.action)
                guard.validate_step(temp_step)
                _execute_discovery_action(page, decision.action, parameters, logger)
                artifact.steps.append(temp_step)

            else:
                final_context = _page_context(
                    page,
                    str(screenshot_path),
                    experiment_fake_screenshot_path=experiment_fake_screenshot_path,
                    experiment_drop_dom_summary=experiment_drop_dom_summary,
                )
                if artifact.steps and final_context["hints"]["balance_ready"]:
                    logger.warn("Discovery max steps reached but balance is visible; finalizing artifact.")
                else:
                    raise RuntimeError("Discovery reached max steps before completion")
            _append_finalize_steps(
                artifact,
                page,
                logger,
                experiment_fake_screenshot_path=experiment_fake_screenshot_path,
                experiment_drop_dom_summary=experiment_drop_dom_summary,
            )
        finally:
            browser.close()

    if not artifact.steps:
        raise RuntimeError("Discovery completed without recording any executable steps; artifact emission aborted.")

    artifact.description = "LLM-driven discovered workflow | thoughts: " + " | ".join(thoughts[:10])
    logger.info("Artifact emission summary", {"recorded_steps_count": len(artifact.steps), "step_ids": [step.id for step in artifact.steps]})
    return artifact
