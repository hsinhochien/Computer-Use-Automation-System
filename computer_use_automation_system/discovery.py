from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import Page, sync_playwright

from .logger import Logger
from .llm_client import LLMClient
from .models import Artifact, DiscoveryAction, OutputSpec, ParameterSpec, SensitivePolicy, Step, SuccessCondition, TargetSpec
from .safety import SafetyGuard


_DISCOVERY_PAUSE_REQUESTED = False
_PAUSE_REQUEST_FILE = Path('.cuas_pause_requested')


def _ensure_handoff_controls(page: Page) -> None:
    page.evaluate(
        """
        () => {
          if (!window.__cuasHandoffState) {
            window.__cuasHandoffState = {
              requestPause: false,
              paused: false,
              resumeRequested: false,
              abortRequested: false,
              noteDraft: '',
              submittedNotes: [],
            };
          }
          const existing = document.getElementById('__cuas_handoff_controls__');
          if (existing) return;
          const shell = document.createElement('div');
          shell.id = '__cuas_handoff_controls__';
          shell.style.position = 'fixed';
          shell.style.bottom = '16px';
          shell.style.right = '16px';
          shell.style.zIndex = '2147483646';
          shell.style.background = '#eff6ff';
          shell.style.border = '1px solid #93c5fd';
          shell.style.borderRadius = '14px';
          shell.style.boxShadow = '0 14px 36px rgba(0,0,0,0.14)';
          shell.style.padding = '12px';
          shell.style.width = '300px';
          shell.style.maxWidth = 'calc(100vw - 32px)';
          shell.style.fontFamily = 'system-ui, -apple-system, sans-serif';
          shell.innerHTML = `
            <div style="font-size: 14px; font-weight: 700; color: #1d4ed8; margin-bottom: 8px;">Automation controls</div>
            <div id="__cuas_controls_message__" style="font-size: 12px; color: #1e3a8a; line-height: 1.45; margin-bottom: 10px;">If you want to take over manually, click the button below. The system will pause at the next safe point.</div>
            <button id="__cuas_request_pause_button__" type="button" style="width: 100%; border: none; border-radius: 10px; background: #2563eb; color: white; padding: 10px 12px; font-size: 13px; cursor: pointer; margin-bottom: 10px;">Request manual takeover</button>
            <div id="__cuas_pause_actions__" style="display: none;">
              <textarea id="__cuas_note_input__" placeholder="Optional note for the automation log" style="width: 100%; min-height: 72px; resize: vertical; box-sizing: border-box; border: 1px solid #bfdbfe; border-radius: 10px; padding: 8px; font-size: 12px; margin-bottom: 8px;"></textarea>
              <button id="__cuas_submit_note_button__" type="button" style="width: 100%; border: 1px solid #93c5fd; border-radius: 10px; background: white; color: #1d4ed8; padding: 8px 10px; font-size: 12px; cursor: pointer; margin-bottom: 8px;">Submit note</button>
              <div style="display: flex; gap: 8px;">
                <button id="__cuas_resume_button__" type="button" style="flex: 1; border: none; border-radius: 10px; background: #16a34a; color: white; padding: 9px 10px; font-size: 12px; cursor: pointer;">Resume</button>
                <button id="__cuas_abort_button__" type="button" style="flex: 1; border: none; border-radius: 10px; background: #dc2626; color: white; padding: 9px 10px; font-size: 12px; cursor: pointer;">Abort</button>
              </div>
            </div>
          `;
          document.body.appendChild(shell);

          const requestButton = document.getElementById('__cuas_request_pause_button__');
          const resumeButton = document.getElementById('__cuas_resume_button__');
          const abortButton = document.getElementById('__cuas_abort_button__');
          const submitNoteButton = document.getElementById('__cuas_submit_note_button__');
          const noteInput = document.getElementById('__cuas_note_input__');

          if (requestButton) {
            requestButton.addEventListener('click', () => {
              window.__cuasHandoffState.requestPause = true;
              requestButton.textContent = 'Manual takeover requested';
              requestButton.disabled = true;
              requestButton.style.background = '#94a3b8';
              requestButton.style.cursor = 'default';
            });
          }
          if (resumeButton) {
            resumeButton.addEventListener('click', () => {
              window.__cuasHandoffState.resumeRequested = true;
            });
          }
          if (abortButton) {
            abortButton.addEventListener('click', () => {
              window.__cuasHandoffState.abortRequested = true;
            });
          }
          if (submitNoteButton && noteInput) {
            submitNoteButton.addEventListener('click', () => {
              const value = noteInput.value.trim();
              if (!value) return;
              window.__cuasHandoffState.submittedNotes.push(value);
              noteInput.value = '';
            });
          }
        }
        """
    )


def _consume_manual_pause_request(page: Page) -> bool:
    global _DISCOVERY_PAUSE_REQUESTED
    if _DISCOVERY_PAUSE_REQUESTED:
        _DISCOVERY_PAUSE_REQUESTED = False
        return True
    if _PAUSE_REQUEST_FILE.exists():
        _PAUSE_REQUEST_FILE.unlink()
        return True
    try:
        requested = bool(
            page.evaluate(
                """
                () => {
                  return Boolean(window.__cuasHandoffState && window.__cuasHandoffState.requestPause);
                }
                """
            )
        )
    except Exception:
        requested = False
    if requested:
        page.evaluate(
            """
            () => {
              if (window.__cuasHandoffState) {
                window.__cuasHandoffState.requestPause = false;
              }
              const button = document.getElementById('__cuas_request_pause_button__');
              if (button) {
                button.textContent = 'Request manual takeover';
                button.disabled = false;
                button.style.background = '#2563eb';
                button.style.cursor = 'pointer';
              }
            }
            """
        )
        return True
    return False


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


def _capture_form_state(page: Page) -> list[dict[str, str | bool | None]]:
    return page.evaluate(
        """
        () => {
          const elements = Array.from(document.querySelectorAll('input, textarea, select')).slice(0, 200);
          return elements.map((el, index) => {
            const tag = el.tagName.toLowerCase();
            const type = (el.getAttribute('type') || '').toLowerCase();
            const selector = el.id
              ? `#${el.id}`
              : el.getAttribute('name')
                ? `${tag}[name="${el.getAttribute('name')}"]`
                : `${tag}:nth-of-type(${index + 1})`;
            const base = {
              selector,
              tag,
              type,
              ariaLabel: el.getAttribute('aria-label'),
              name: el.getAttribute('name'),
            };
            if (tag === 'select') {
              const selectedIndex = el.selectedIndex;
              const selectedOption = selectedIndex >= 0 ? el.options[selectedIndex] : null;
              return {
                ...base,
                value: el.value,
                label: selectedOption ? selectedOption.text.trim() : '',
                checked: false,
              };
            }
            if (type === 'checkbox' || type === 'radio') {
              return {
                ...base,
                value: el.value,
                label: '',
                checked: Boolean(el.checked),
              };
            }
            return {
              ...base,
              value: (el.value || '').trim(),
              label: '',
              checked: false,
            };
          });
        }
        """
    )


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


def _show_pause_overlay(page: Page, reason: str) -> None:
    page.evaluate(
        """
        (reason) => {
          const existing = document.getElementById('__cuas_pause_overlay__');
          if (existing) existing.remove();
          const banner = document.createElement('div');
          banner.id = '__cuas_pause_overlay__';
          banner.setAttribute('data-cuas-overlay', 'paused');
          banner.style.position = 'fixed';
          banner.style.top = '16px';
          banner.style.right = '16px';
          banner.style.width = '380px';
          banner.style.maxWidth = 'calc(100vw - 32px)';
          banner.style.zIndex = '2147483647';
          banner.style.background = '#fff7ed';
          banner.style.border = '1px solid #fdba74';
          banner.style.borderRadius = '14px';
          banner.style.boxShadow = '0 16px 40px rgba(0,0,0,0.18)';
          banner.style.padding = '14px 16px';
          banner.style.fontFamily = 'system-ui, -apple-system, sans-serif';
          banner.style.pointerEvents = 'none';
          banner.innerHTML = `
            <div style="font-size: 16px; font-weight: 700; margin-bottom: 8px; color: #9a3412;">Automation paused for human intervention</div>
            <div style="font-size: 13px; color: #7c2d12; line-height: 1.45; margin-bottom: 8px;">This page is still interactive. You can operate it manually now. This is an intentional pause, not a system crash.</div>
            <div style="font-size: 12px; color: #7c2d12; background: rgba(255,255,255,0.72); border: 1px solid #fed7aa; border-radius: 10px; padding: 8px; margin-bottom: 8px;"><strong>Reason:</strong> ${reason}</div>
            <div style="font-size: 12px; color: #7c2d12; line-height: 1.45;">Use the controls panel to submit a note, resume automation, or abort the run.</div>
          `;
          document.body.appendChild(banner);
          if (window.__cuasHandoffState) {
            window.__cuasHandoffState.paused = true;
            window.__cuasHandoffState.resumeRequested = false;
            window.__cuasHandoffState.abortRequested = false;
          }
          const message = document.getElementById('__cuas_controls_message__');
          const requestButton = document.getElementById('__cuas_request_pause_button__');
          const pauseActions = document.getElementById('__cuas_pause_actions__');
          if (message) {
            message.textContent = 'Automation is paused. You can operate the page manually, submit a note, then resume or abort below.';
          }
          if (requestButton) {
            requestButton.style.display = 'none';
          }
          if (pauseActions) {
            pauseActions.style.display = 'block';
          }
        }
        """,
        reason,
    )


def _hide_pause_overlay(page: Page) -> None:
    page.evaluate(
        """
        () => {
          const existing = document.getElementById('__cuas_pause_overlay__');
          if (existing) existing.remove();
          if (window.__cuasHandoffState) {
            window.__cuasHandoffState.paused = false;
            window.__cuasHandoffState.resumeRequested = false;
            window.__cuasHandoffState.abortRequested = false;
          }
          const message = document.getElementById('__cuas_controls_message__');
          const requestButton = document.getElementById('__cuas_request_pause_button__');
          const pauseActions = document.getElementById('__cuas_pause_actions__');
          if (message) {
            message.textContent = 'If you want to take over manually, click the button below. The system will pause at the next safe point.';
          }
          if (requestButton) {
            requestButton.style.display = 'block';
            requestButton.textContent = 'Request manual takeover';
            requestButton.disabled = false;
            requestButton.style.background = '#2563eb';
            requestButton.style.cursor = 'pointer';
          }
          if (pauseActions) {
            pauseActions.style.display = 'none';
          }
        }
        """
    )


def _pause_for_human_intervention(
    page: Page,
    logger: Logger,
    reason: str,
) -> tuple[list[str], list[dict[str, str | bool | None]], list[dict[str, str | bool | None]], dict, dict]:
    before_state = _capture_form_state(page)
    before_context = _page_context(page, screenshot_path="handoff-before")
    _show_pause_overlay(page, reason)
    logger.warn("Discovery paused for human intervention", {"reason": reason})
    print("Discovery is paused for human intervention.")
    print("The browser page is still interactive. Use the page controls to add a note, resume, or abort.")
    seen_notes: set[str] = set()
    while True:
        state = page.evaluate(
            """
            () => {
              const state = window.__cuasHandoffState || {};
              return {
                resumeRequested: Boolean(state.resumeRequested),
                abortRequested: Boolean(state.abortRequested),
                submittedNotes: Array.isArray(state.submittedNotes) ? state.submittedNotes.slice() : [],
              };
            }
            """
        )
        for note in state.get("submittedNotes", []):
            if note not in seen_notes:
                seen_notes.add(note)
                logger.info("Discovery human note", {"note": note, "reason": reason})
        if state.get("abortRequested"):
            _hide_pause_overlay(page)
            raise RuntimeError("Discovery aborted during human intervention")
        if state.get("resumeRequested"):
            after_state = _capture_form_state(page)
            after_context = _page_context(page, screenshot_path="handoff-after")
            _hide_pause_overlay(page)
            logger.info("Discovery resumed after human intervention", {"reason": reason, "notes": list(seen_notes)})
            return list(seen_notes), before_state, after_state, before_context, after_context
        page.wait_for_timeout(250)


def _is_member_field_selector(selector: str | None) -> bool:
    if not selector:
        return False
    selector_text = selector.lower()
    return "member" in selector_text and ("id" in selector_text or "number" in selector_text)


def _apply_runtime_parameter_enforcement(action: DiscoveryAction, parameters: dict[str, str], logger: Logger) -> DiscoveryAction:
    if action.kind == "fill" and _is_member_field_selector(action.selector) and parameters.get("memberId"):
        expected_member_id = parameters["memberId"]
        if action.value != expected_member_id:
            logger.warn(
                "Discovery action value rewritten to match runtime parameter override",
                {
                    "selector": action.selector,
                    "originalValue": action.value,
                    "rewrittenValue": expected_member_id,
                },
            )
            action = action.model_copy(update={"value": expected_member_id})
    return action


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
    if action.kind == "selectOption":
        if not action.selector:
            raise RuntimeError("Discovery action selectOption missing selector")
        value = _interpolate(action.value, parameters) or ""
        locator = page.locator(action.selector)
        try:
            locator.select_option(label=value, timeout=10000)
        except Exception:
            locator.select_option(value=value, timeout=10000)
        logger.info("Discovery selectOption", {"selector": action.selector, "value": "***REDACTED***" if action.sensitive else value})
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
        _pause_for_human_intervention(page, logger, action.description or "The agent explicitly requested human help.")
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


def _extract_runtime_parameter_overrides(
    before_state: list[dict[str, str | bool | None]],
    after_state: list[dict[str, str | bool | None]],
) -> dict[str, str]:
    before_by_selector = {
        str(item.get("selector")): item
        for item in before_state
        if item.get("selector")
    }
    overrides: dict[str, str] = {}

    for after in after_state:
        selector = str(after.get("selector") or "")
        if not selector:
            continue
        before = before_by_selector.get(selector)
        if not before:
            continue
        tag = str(after.get("tag") or "")
        if tag not in {"input", "textarea"}:
            continue
        before_value = str(before.get("value") or "")
        after_value = str(after.get("value") or "")
        if before_value == after_value:
            continue
        selector_text = " ".join(
            [
                selector.lower(),
                str(after.get("ariaLabel") or "").lower(),
                str(after.get("name") or "").lower(),
            ]
        )
        if "member" in selector_text and ("id" in selector_text or "number" in selector_text):
            overrides["memberId"] = after_value

    return overrides


def _build_human_steps_from_state_diff(
    before_state: list[dict[str, str | bool | None]],
    after_state: list[dict[str, str | bool | None]],
    starting_index: int,
    before_context: dict | None = None,
    after_context: dict | None = None,
) -> list[Step]:
    before_by_selector = {
        str(item.get("selector")): item
        for item in before_state
        if item.get("selector")
    }
    steps: list[Step] = []
    next_index = starting_index

    for after in after_state:
        selector = str(after.get("selector") or "")
        if not selector:
            continue
        before = before_by_selector.get(selector)
        if not before:
            continue
        tag = str(after.get("tag") or "")
        input_type = str(after.get("type") or "")
        before_value = before.get("value")
        after_value = after.get("value")
        before_checked = bool(before.get("checked"))
        after_checked = bool(after.get("checked"))

        if tag == "select" and before_value != after_value:
            label = str(after.get("label") or after_value or "")
            steps.append(
                Step(
                    id=f"step-{next_index:03d}-selectOption",
                    kind="selectOption",
                    selector=selector,
                    target=_build_target_spec(selector),
                    value=label,
                    description="Human takeover: selected an option during manual intervention",
                    risk="low",
                    sensitive=False,
                    continueOnError=False,
                )
            )
            next_index += 1
            continue

        if tag in {"input", "textarea"} and input_type not in {"checkbox", "radio"} and before_value != after_value:
            steps.append(
                Step(
                    id=f"step-{next_index:03d}-fill",
                    kind="fill",
                    selector=selector,
                    target=_build_target_spec(selector),
                    value=str(after_value or ""),
                    description="Human takeover: filled a field during manual intervention",
                    risk="low",
                    sensitive=False,
                    continueOnError=False,
                )
            )
            next_index += 1
            continue

        if tag == "input" and input_type in {"checkbox", "radio"} and before_checked != after_checked:
            steps.append(
                Step(
                    id=f"step-{next_index:03d}-click",
                    kind="click",
                    selector=selector,
                    target=_build_target_spec(selector),
                    description="Human takeover: toggled a control during manual intervention",
                    risk="low",
                    sensitive=False,
                    continueOnError=False,
                )
            )
            next_index += 1

    if before_context and after_context:
        before_ready = bool(before_context.get("hints", {}).get("balance_ready"))
        after_ready = bool(after_context.get("hints", {}).get("balance_ready"))
        if not before_ready and after_ready:
            click_selector = "#review-btn"
            if not any(step.kind == "click" and step.selector == click_selector for step in steps):
                steps.append(
                    Step(
                        id=f"step-{next_index:03d}-click",
                        kind="click",
                        selector=click_selector,
                        target=_build_target_spec(click_selector),
                        description="Human takeover: triggered the primary review action during manual intervention",
                        risk="low",
                        sensitive=False,
                        continueOnError=False,
                    )
                )

    return steps


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
    human_override_context: dict[str, str] = {}

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
            _ensure_handoff_controls(page)

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
                _ensure_handoff_controls(page)
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
                decision = client.decide_next_action(
                    task=task,
                    page_context=context,
                    steps=artifact.steps,
                    image_mode=image_mode,
                    human_override_context=human_override_context,
                )
                thoughts.append(decision.thought)
                redacted_thought = guard.redact_known_values(
                    decision.thought,
                    [parameters.get("memberId", ""), context.get("balance_text", "")],
                )
                logger.info("LLM decision", {"step": index, "thought": redacted_thought, "action": decision.action.model_dump()})

                if _consume_manual_pause_request(page):
                    human_notes, before_state, after_state, before_context, after_context = _pause_for_human_intervention(page, logger, "Manual pause requested by the operator.")
                    parameter_overrides = _extract_runtime_parameter_overrides(before_state, after_state)
                    if parameter_overrides:
                        parameters.update(parameter_overrides)
                        human_override_context.update(parameter_overrides)
                        logger.info("Discovery updated runtime parameters from human intervention", {"overrides": parameter_overrides})
                    human_steps = _build_human_steps_from_state_diff(before_state, after_state, len(artifact.steps) + 1, before_context, after_context)
                    if human_steps:
                        artifact.steps.extend(human_steps)
                        logger.info("Discovery recorded human intervention steps", {"count": len(human_steps), "step_ids": [step.id for step in human_steps]})
                    logger.info("Discovery pending action discarded after human intervention", {"step": index, "kind": decision.action.kind, "selector": decision.action.selector, "notes": human_notes})
                    continue

                if decision.action.risk == "high":
                    human_notes, before_state, after_state, before_context, after_context = _pause_for_human_intervention(page, logger, decision.action.description or "High-risk action requires human intervention.")
                    parameter_overrides = _extract_runtime_parameter_overrides(before_state, after_state)
                    if parameter_overrides:
                        parameters.update(parameter_overrides)
                        human_override_context.update(parameter_overrides)
                        logger.info("Discovery updated runtime parameters from human intervention", {"overrides": parameter_overrides})
                    human_steps = _build_human_steps_from_state_diff(before_state, after_state, len(artifact.steps) + 1, before_context, after_context)
                    if human_steps:
                        artifact.steps.extend(human_steps)
                        logger.info("Discovery recorded human intervention steps", {"count": len(human_steps), "step_ids": [step.id for step in human_steps]})
                    logger.info("Discovery pending action discarded after human intervention", {"step": index, "kind": decision.action.kind, "selector": decision.action.selector, "notes": human_notes})
                    continue

                decision.action = _apply_runtime_parameter_enforcement(decision.action, parameters, logger)

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
                        human_notes, before_state, after_state, before_context, after_context = _pause_for_human_intervention(page, logger, "The agent appears stuck repeating the same action.")
                        parameter_overrides = _extract_runtime_parameter_overrides(before_state, after_state)
                        if parameter_overrides:
                            parameters.update(parameter_overrides)
                            human_override_context.update(parameter_overrides)
                            logger.info("Discovery updated runtime parameters from human intervention", {"overrides": parameter_overrides})
                        human_steps = _build_human_steps_from_state_diff(before_state, after_state, len(artifact.steps) + 1, before_context, after_context)
                        if human_steps:
                            artifact.steps.extend(human_steps)
                            logger.info("Discovery recorded human intervention steps", {"count": len(human_steps), "step_ids": [step.id for step in human_steps]})
                        logger.info("Discovery pending action discarded after human intervention", {"step": index, "kind": decision.action.kind, "selector": decision.action.selector, "notes": human_notes})
                        continue

                temp_step = _action_to_step(len(artifact.steps) + 1, decision.action)
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
