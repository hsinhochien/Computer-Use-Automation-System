# Report

## 1. Architecture

### Overview
The system is organized as a two-phase computer-use workflow:

1. **Discovery**: an LLM observes the current browser state and proposes one next action at a time.
2. **Replay**: the discovered workflow is executed deterministically from a saved artifact, without calling the LLM.

This split is the core architectural decision. Discovery is intentionally adaptive and model-driven, while replay is intentionally rigid and automation-driven.

### Main components
- **CLI (`cli.py`)**: exposes `discover` and `replay` commands.
- **Discovery (`discovery.py`)**: drives the browser with Playwright, captures page context, asks the LLM for the next action, validates and records steps, and emits an artifact.
- **LLM client (`llm_client.py`)**: sends structured prompts to the configured LLM and, depending on `--image-mode`, can also send the current screenshot as multimodal image input.
- **Models (`models.py`)**: defines the typed schema for artifacts, steps, safety settings, outputs, and replay results.
- **Replay (`replay.py`)**: loads an artifact and executes it deterministically with Playwright.
- **Safety (`safety.py`)**: enforces allowed domains, blocked actions, and masking/redaction behavior.
- **Artifact I/O (`artifact_io.py`)**: persists and loads artifacts.

### Key design decisions

#### A. Discovery and replay are separated
This design keeps model uncertainty in discovery and keeps replay deterministic. Once a workflow is discovered, replay does not need the LLM and should be repeatable as long as the UI surface remains compatible.

**Trade-off:**
- Advantage: replay is cheaper, faster, and easier to reason about.
- Cost: discovery must capture enough structure up front to make replay reliable later.

#### B. Hybrid perception instead of DOM-only prompting
During discovery, the LLM can consume:
- structured page context derived from the DOM,
- generic page hints inferred from the visible page state,
- and optionally the actual screenshot as multimodal input.

This was chosen because a pure DOM-only prompt is brittle for weaker or less explicit surfaces, while always sending screenshots is more expensive and slower.

**Trade-off:**
- Advantage: `--image-mode auto` allows a practical balance between cost and perception quality.
- Cost: the current generic observation logic is still task-shaped by the balance-query demos and is not yet a general surface adapter.

#### C. Generic observation, but still task-shaped
The observation layer no longer depends only on the original `#member-id`, `#search-btn`, and `#balance` selectors. It now extracts more generic structures such as inputs, buttons, balance-like candidates, and visible text.

**Trade-off:**
- Advantage: the system now works across both the simple demo and the weaker-DOM vision demo.
- Cost: the heuristics still clearly reflect the current task family (member balance lookup) and are not yet fully task-agnostic.

#### D. Artifact-first execution contract
The artifact is the contract between discovery and replay. It records the workflow structure, target selectors, output contract, safety policy, and success condition.

**Trade-off:**
- Advantage: easy to inspect, replay, and debug.
- Cost: if the artifact captures weak selectors or overly task-specific assumptions, replay quality degrades.

#### E. Real same-session HITL handoff during discovery
The current implementation now supports a real same-session human handoff during discovery. An in-browser control panel lets the operator request manual takeover, pause automation at a safe boundary, operate the same live page session, submit notes, and resume or abort from the same browser surface.

A key design choice is that human takeover invalidates the pending automation action. After resume, discovery does not execute the stale pre-handoff action; instead it re-observes the current page state and asks the LLM for a fresh next step.

**Trade-off:**
- Advantage: this creates a real control-transfer seam and avoids overwriting human edits with stale pending actions.
- Cost: the system must explicitly model pause/resume state and handle mixed human/agent authorship of the final artifact.

#### F. Human action capture is minimal but replayable
The current system captures human-applied form changes during handoff by taking a page form-state snapshot before pause and another before resume, diffing them, and converting the changes into replayable artifact steps.

This currently works best for:
- text inputs and textareas → `fill`
- selects → `selectOption`
- some toggles → simplified `click`

**Trade-off:**
- Advantage: this satisfies the assignment's requirement to record what the human did in a minimal but real way, and lets replay reproduce common manual edits.
- Cost: this is not a full event-level recorder, so richer button sequences and transient interaction traces are not captured exactly.

---

## 2. Artifact schema

### Overview
The artifact is a typed JSON/YAML-compatible object designed to serve as a stable replay contract.

Main top-level fields:
- `version`
- `artifactId`
- `name`
- `description`
- `taskTemplate`
- `createdAt`
- `startUrl`
- `parameters`
- `outputs`
- `successCondition`
- `safety`
- `steps`

### Why the schema is shaped this way

#### A. `taskTemplate`
This captures the discovered workflow at the intent level, not only as a concrete recording. It makes the artifact reusable across different runtime parameters such as `memberId`.

#### B. `parameters`
Parameters describe runtime inputs separately from the discovered step sequence. During replay, values are injected through `--param key=value`.

This separation is important because it keeps replay deterministic while still allowing controlled variation in input values.

#### C. `outputs`
Outputs define the data contract that replay is expected to produce. In the current task family, the main output is `balance`.

A key shaping decision was to normalize balance extraction output keys to `balance`, even if the LLM proposes variants such as `balance_text`. This avoids duplicate or inconsistent output contracts in the artifact.

#### D. `successCondition`
The success condition is stored explicitly instead of being inferred only during replay. This makes the replay contract self-contained.

The selector is not hard-coded only to `#balance`; it is inferred from the observed result region for the current surface. This allows the same schema to work for both the DOM-rich demo and the weaker-DOM vision demo.

#### E. `safety`
Safety is artifact-local, not only runtime-global. This means the allowed domains, masking policy, and blocked action classes travel with the artifact.

#### F. `steps`
Each step is strongly typed and includes:
- `id`
- `kind`
- `selector`
- `target`
- `url`
- `value`
- `key`
- `outputKey`
- `expectedText`
- `risk`
- `sensitive`
- `continueOnError`
- `description`

This shape was chosen to support both:
- human readability,
- and deterministic execution.

With the current HITL implementation, `steps` may include both agent-generated steps and human-generated replayable steps inferred from manual intervention. The artifact therefore acts not only as an agent plan, but also as a merged execution record across automation and operator handoff.

#### G. `target`
`target` stores selector strategy metadata separately from the step kind. Right now the system uses CSS selectors, but the schema leaves room for future selector strategies and fallback logic.

### Schema trade-offs
- The schema is clear and replay-friendly.
- It is still task-specialized in practice because outputs and success criteria are shaped by the balance-query demo family.
- `fallbacks` are currently present in the schema but not yet populated meaningfully, so selector robustness is still limited.

---

## 3. Determinism & error handling

### How replay is made deterministic

Replay does not call the LLM. Instead, it:
1. loads the artifact,
2. opens the saved `startUrl`,
3. interpolates runtime parameters into step values if needed,
4. executes the recorded steps in order,
5. evaluates the stored success condition,
6. returns a structured replay result.

Determinism comes from the fact that replay uses a fixed step list rather than generating actions dynamically.

### Mechanisms supporting determinism

#### A. Typed step kinds
Replay only executes a known set of step kinds (`goto`, `click`, `fill`, `selectOption`, `press`, `waitFor`, `extractText`, `assertText`, `screenshot`, etc.). This limits ambiguity.

#### B. Recorded selectors and target metadata
Discovery records concrete selectors into the artifact. Replay consumes those selectors directly instead of re-deriving them from the UI or asking the model again.

#### C. Explicit success condition
Replay checks an artifact-defined success condition rather than relying only on step completion. This protects against false positives where all actions ran but the business result did not appear.

#### D. Parameter contract
Replay parameters are passed explicitly through `--param key=value`, which keeps runtime input substitution controlled and inspectable.

### Runtime errors and exceptional states

The system currently handles runtime failures in a practical but still prototype-oriented way.

#### A. Playwright execution failures
If a selector is missing, a click fails, a text assertion fails, or a timeout occurs, replay surfaces that as a failed or recoverable result depending on the step configuration and failure context.

#### B. Business result validation
Replay distinguishes between “the steps executed” and “the workflow actually succeeded” by evaluating `successCondition` after step execution.

#### C. Continue-on-error support
The schema includes `continueOnError`, which allows some steps to be marked as non-fatal. This provides a limited form of recovery policy.

#### D. Repeated-action protection during discovery
Discovery includes a repeated-action check to avoid getting stuck in simple loops. If the same action is proposed again with no apparent change, the system can block it and use a heuristic override or enter human handoff.

#### E. Human-triggered same-session handoff
Discovery supports a real operator-triggered handoff. The operator can request manual takeover from the in-browser control panel, automation pauses on the same live session, and the operator can resume or abort without leaving the browser surface.

A key correctness rule is that resuming from handoff discards the pending pre-handoff action and forces discovery to re-observe the current page state before continuing.

#### F. Agent-triggered escalation is partial
The current prototype has only basic agent-triggered escalation support. It can enter human handoff when:
- the model returns `humanApproval`,
- the action is marked high risk,
- or repeated-action stuck detection fires.

This is intentionally partial rather than a full escalation policy engine.

### UI drift handling
The current implementation handles UI drift only partially.

What helps today:
- storing concrete selectors in the artifact,
- storing target metadata,
- generic result-region inference for the current demo family,
- and using multimodal discovery when DOM signals are weak.

What is still missing:
- populated fallback selectors,
- stronger selector robustness policies,
- adaptive replay-time re-grounding,
- explicit drift taxonomy,
- full event-level capture of human actions during handoff.

So the system has some drift resistance, but it is not yet a full drift-tolerant replay engine.

### Error-handling trade-offs
- The replay path is deterministic and simple to debug.
- The system has a clear success contract and output contract.
- Error handling is still narrower than a production-grade automation system because the runtime taxonomy is not yet rich enough (for example, no explicit member-not-found or session-expired business outcome model).

### Summary
The current design prioritizes:
- deterministic replay,
- inspectable artifacts,
- and a clean separation between adaptive discovery and fixed execution.

The main trade-off is that robustness still depends heavily on selector quality and on task-shaped observation heuristics established during discovery.
