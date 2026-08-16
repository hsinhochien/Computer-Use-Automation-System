from __future__ import annotations

import argparse
import json
from pathlib import Path

from .artifact_io import load_artifact, save_artifact
from .discovery import discover_task_to_artifact
from .replay import ReplayOptions, replay_artifact


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="cuas", description="Computer-Use Automation System")
    subparsers = parser.add_subparsers(dest="command")

    discover_parser = subparsers.add_parser("discover", help="Create an artifact from a natural language task")
    discover_parser.add_argument("--task", required=True, help="Natural language task")
    discover_parser.add_argument("--url", required=True, help="Start URL")
    discover_parser.add_argument("--output", default=None, help="Artifact output path")
    discover_parser.add_argument("--use-llm", action="store_true", help="Use configured LLM to drive discovery")
    discover_parser.add_argument("--max-steps", type=int, default=12, help="Maximum LLM discovery steps")
    discover_parser.add_argument("--experiment-fake-screenshot-path", action="store_true", help="Replace the screenshot path in page context with a fake path to test whether the LLM is actually using screenshots")
    discover_parser.add_argument("--experiment-drop-dom-summary", action="store_true", help="Remove the DOM summary from page context to test whether the LLM depends on it")
    discover_parser.add_argument("--image-mode", choices=["auto", "always", "never"], default="auto", help="How to provide screenshots to the LLM during discovery")

    replay_parser = subparsers.add_parser("replay", help="Replay an artifact deterministically")
    replay_parser.add_argument("--artifact", required=True, help="Path to artifact YAML/JSON")
    replay_parser.add_argument("--headless", action="store_true", help="Run browser headless")
    replay_parser.add_argument("--param", action="append", default=[], help="Parameter in key=value form")
    replay_parser.add_argument("--json", action="store_true", help="Print the redacted replay result as JSON")

    return parser


def parse_params(items: list[str]) -> dict[str, str]:
    params: dict[str, str] = {}
    for item in items:
        if "=" not in item:
            raise ValueError(f"Invalid --param value: {item}. Use key=value")
        key, value = item.split("=", 1)
        params[key] = value
    return params


def build_redacted_replay_view(result: dict, sensitive_output_names: set[str]) -> dict:
    view = dict(result)
    outputs = dict(view.get("outputs", {}))
    for key in sensitive_output_names:
        if key in outputs:
            outputs[key] = "***REDACTED***"
    view["outputs"] = outputs
    return view


def print_replay_summary(result: dict, sensitive_output_names: set[str]) -> None:
    status = result.get("status")
    artifact_id = result.get("artifactId")
    evidence = result.get("evidence", {})
    failed_step_id = result.get("failedStepId")
    business_code = result.get("businessOutcomeCode")

    if status == "success":
        print(f"Replay succeeded for artifact {artifact_id}.")
        if sensitive_output_names:
            print("Sensitive outputs were redacted from terminal output.")
        if evidence.get("screenshot"):
            print(f"Evidence screenshot: {evidence['screenshot']}")
        return

    if status == "business_outcome":
        print(f"Replay completed with business outcome for artifact {artifact_id}: {business_code}.")
        if evidence.get("screenshot"):
            print(f"Evidence screenshot: {evidence['screenshot']}")
        return

    if status == "recoverable":
        print(f"Replay encountered a recoverable condition for artifact {artifact_id} at step {failed_step_id}.")
        if result.get("message"):
            print(result["message"])
        return

    print(f"Replay failed for artifact {artifact_id} at step {failed_step_id}.")
    if result.get("message"):
        print(result["message"])
    if result.get("expected"):
        print(f"Expected: {result['expected']}")
    if result.get("observed"):
        print(f"Observed: {result['observed']}")
    if evidence.get("screenshot"):
        print(f"Evidence screenshot: {evidence['screenshot']}")


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    if args.command == "discover":
        artifact = discover_task_to_artifact(
            task=args.task,
            start_url=args.url,
            use_llm=args.use_llm,
            max_steps=args.max_steps,
            experiment_fake_screenshot_path=args.experiment_fake_screenshot_path,
            experiment_drop_dom_summary=args.experiment_drop_dom_summary,
            image_mode=args.image_mode,
        )
        output = args.output or str(Path("artifacts") / f"{artifact.artifactId}.json")
        save_artifact(output, artifact)
        print(f"Artifact saved to {output}")
        return

    if args.command == "replay":
        params = parse_params(args.param)
        artifact = load_artifact(args.artifact)
        result = replay_artifact(ReplayOptions(artifact_path=args.artifact, headless=args.headless, parameters=params))
        sensitive_output_names = {output.name for output in artifact.outputs if output.sensitive}
        terminal_view = build_redacted_replay_view(result.model_dump(mode="json"), sensitive_output_names)
        if args.json:
            print(json.dumps(terminal_view, ensure_ascii=False, indent=2))
        else:
            print_replay_summary(terminal_view, sensitive_output_names)
        return

    parser.print_help()


if __name__ == "__main__":
    main()
