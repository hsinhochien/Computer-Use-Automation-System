from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any

from openai import APIStatusError, AzureOpenAI, OpenAI

from .config import get_llm_settings
from .models import DiscoveryDecision, Step


def _should_include_screenshot(page_context: dict, image_mode: str) -> bool:
    if image_mode == "always":
        return True
    if image_mode == "never":
        return False
    dom_summary = page_context.get("dom_summary", [])
    experiment_flags = page_context.get("experiment_flags", {})
    if experiment_flags.get("drop_dom_summary"):
        return True
    if len(dom_summary) < 3:
        return True
    return False


class LLMClient:
    def __init__(self) -> None:
        settings = get_llm_settings()
        self.settings = settings

        if settings.provider == 'azure_openai':
            self.client = AzureOpenAI(
                api_key=settings.api_key,
                azure_endpoint=settings.base_url,
                api_version=settings.azure_api_version,
                timeout=settings.timeout_seconds,
            )
        else:
            self.client = OpenAI(
                api_key=settings.api_key,
                base_url=settings.base_url,
                timeout=settings.timeout_seconds,
            )

    def _build_messages(
        self,
        task: str,
        page_context: dict,
        action_history: list[dict[str, str | None]],
        image_mode: str,
    ) -> list[dict[str, Any]]:
        system_prompt = (
            "You are controlling a browser to complete a user task. "
            "Return exactly one safe next action as strict JSON. "
            "Use the screenshot when it is provided, especially if the DOM summary is sparse or unreliable. "
            "Prefer deterministic CSS selectors from the DOM summary when they are clearly available. "
            "Do not repeat the same action if it was already executed and the page state has not changed. "
            "If the member ID is already filled, prefer clicking the search button instead of filling again. "
            "If the balance result is visible and meaningful, prefer extractText or done. "
            "If the task is complete, return action.kind='done'. "
            "If a step is risky or uncertain, return action.kind='humanApproval'. "
            "Do not reveal secrets."
        )
        user_prompt = (
            f"Task: {task}\n"
            f"Page context: {json.dumps(page_context, ensure_ascii=False)}\n"
            f"Action history: {json.dumps(action_history, ensure_ascii=False)}\n\n"
            "Return JSON with this exact shape:\n"
            '{"thought":"...","action":{"kind":"click|fill|waitFor|extractText|assertText|goto|press|humanApproval|screenshot|done",'
            '"selector":null,"url":null,"value":null,"key":null,"outputKey":null,"expectedText":null,'
            '"description":"...","risk":"low|medium|high","sensitive":false,"continueOnError":false}}'
        )
        if _should_include_screenshot(page_context, image_mode):
            screenshot_path = page_context.get("screenshot_path")
            if screenshot_path and not str(screenshot_path).startswith("fake://"):
                image_bytes = Path(screenshot_path).read_bytes()
                image_base64 = base64.b64encode(image_bytes).decode("utf-8")
                return [
                    {"role": "system", "content": system_prompt},
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": user_prompt},
                            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{image_base64}"}},
                        ],
                    },
                ]
        return [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]

    def decide_next_action(self, task: str, page_context: dict, steps: list[Step], image_mode: str = "auto") -> DiscoveryDecision:
        action_history = [
            {
                "kind": step.kind,
                "selector": step.selector,
                "description": step.description,
                "value": None if step.sensitive else step.value,
            }
            for step in steps[-6:]
        ]
        messages = self._build_messages(task, page_context, action_history, image_mode)
        try:
            response = self.client.chat.completions.create(
                model=self.settings.model,
                messages=messages,
                temperature=0,
                response_format={"type": "json_object"},
            )
        except APIStatusError as error:
            if error.status_code == 401:
                raise RuntimeError('LLM authentication failed. Please check LLM_PROVIDER, LLM_API_KEY, LLM_BASE_URL, deployment name, and Azure API version.') from None
            if error.status_code == 404:
                raise RuntimeError('LLM endpoint/deployment not found. Please verify LLM_BASE_URL, LLM_MODEL (Azure deployment name), and AZURE_OPENAI_API_VERSION.') from None
            raise RuntimeError(f'LLM request failed with status {error.status_code}. Please verify your provider configuration.') from None
        except Exception as error:
            raise RuntimeError(f'LLM request failed: {error.__class__.__name__}') from None

        text = response.choices[0].message.content or ''
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as error:
            raise RuntimeError(f'LLM returned non-JSON content: {text[:300]}') from error
        return DiscoveryDecision.model_validate(payload)
