# Computer-Use Automation System

This repository implements a browser-based computer-use automation prototype with two phases:

1. **Discovery**: an LLM observes the current page and records an executable artifact.
2. **Replay**: the artifact is executed deterministically without calling the LLM again.

The current implementation is web-focused and uses Playwright for browser automation.

## 1. Setup

### Requirements
- Python 3.10+
- Playwright Chromium
- An LLM API key for discovery

### Install
```bash
conda activate interface
cd Computer-Use-Automation-System
python -m pip install -e .
python -m playwright install chromium
```

## 2. Configuration

Create a `.env` file in the repository root.

You can start from the template:

```bash
cp env.example .env
```

### Option A: OpenAI-compatible API
```bash
LLM_PROVIDER=openai_compatible
LLM_API_KEY=your_api_key_here
LLM_BASE_URL=https://api.openai.com/v1
LLM_MODEL=gpt-4o-mini
LLM_TIMEOUT_SECONDS=60
```

### Option B: Azure OpenAI
```bash
LLM_PROVIDER=azure_openai
LLM_API_KEY=your_azure_openai_key_here
LLM_BASE_URL=https://your-resource-name.openai.azure.com/
LLM_MODEL=your_deployment_name
LLM_TIMEOUT_SECONDS=60
AZURE_OPENAI_API_VERSION=2024-12-01-preview
```

Notes:
- For Azure OpenAI, `LLM_MODEL` must be the deployment name.
- Discovery requires a live LLM because the system asks the model to decide the next action.
- Replay does **not** require the LLM and can run offline once an artifact has already been generated.

## 3. Running without live services

You can run the included demos locally without any external website.

Start a local static server:

```bash
cd Computer-Use-Automation-System
python -m http.server 8000
```

Available local demo pages:
- `http://localhost:8000/examples/demo.html` — simple DOM-rich balance lookup demo
- `http://localhost:8000/examples/vision_demo.html` — weaker-DOM / more vision-oriented demo
- `http://localhost:8000/examples/complex_hitl_demo.html` — more complex multi-field demo with a manual compliance gate

Important:
- **Discovery** still needs a live LLM API.
- **Replay** can run without the LLM once you already have an artifact.

## 4. Discovery command

Basic example:

```bash
cuas discover --task "Check the balance for member 12345" --url "http://localhost:8000/examples/demo.html" --use-llm
```

Useful options:

```bash
cuas discover --task "Check the balance for member 12345" --url "http://localhost:8000/examples/demo.html" --use-llm --max-steps 12 --image-mode auto --output artifacts/member-balance-query.json
```

### Screenshot modes
- `--image-mode auto`: send screenshots only when the DOM signal is sparse or degraded
- `--image-mode always`: always send the screenshot as multimodal input
- `--image-mode never`: do not send screenshots; use only structured page context

## 5. Replay command

Replay uses a saved artifact and does not call the LLM.

```bash
cuas replay --artifact artifacts/member-balance-query.json --param memberId=12345
```

Optional headless mode:

```bash
cuas replay --artifact artifacts/member-balance-query.json --param memberId=12345 --headless
```

Optional JSON output:

```bash
cuas replay --artifact artifacts/member-balance-query.json --param memberId=12345 --json
```

## 6. Demo path

This section gives an exact end-to-end path: discover a workflow, save the artifact, then replay it.

### Demo path A: simple DOM-rich demo

Start the local server:

```bash
cd Computer-Use-Automation-System
python -m http.server 8000
```

In another terminal, run discovery:

```bash
conda activate interface
cd Computer-Use-Automation-System
cuas discover --task "Check the balance for member 12345" --url "http://localhost:8000/examples/demo.html" --use-llm --max-steps 12 --image-mode auto --output artifacts/member-balance-query.json
```

Then replay the generated artifact:

```bash
cuas replay --artifact artifacts/member-balance-query.json --param memberId=12345
```

### Demo path B: weaker-DOM / vision-assisted demo

Run discovery with screenshots always enabled:

```bash
cuas discover --task "Check the balance for member 12345" --url "http://localhost:8000/examples/vision_demo.html" --use-llm --max-steps 12 --image-mode always --output artifacts/member-balance-query.json
```

Then replay:

```bash
cuas replay --artifact artifacts/member-balance-query.json --param memberId=12345
```

## 7. Current scope and limitations

- The current system is designed around web automation with Playwright.
- The included generic observation logic is broader than a single hard-coded page, but it is still shaped by the current balance-query demo family.
- `humanApproval` is currently a minimal terminal approval gate, not a full same-session human handoff.
- Replay depends on the recorded selectors and result region captured during discovery.
- The implementation includes screenshot-based multimodal discovery, but replay itself remains deterministic and selector-driven.
