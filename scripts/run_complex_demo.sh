#!/bin/zsh

set -euo pipefail

cd "$(dirname "$0")/.."

TASK="Review the balance for member 12345. Use North America, member ID lookup mode, agent code AGENT-77, and add a case note."
URL="http://localhost:8000/examples/complex_hitl_demo.html"
OUTPUT="artifacts/complex-hitl-demo.json"

cuas discover \
  --task "$TASK" \
  --url "$URL" \
  --use-llm \
  --max-steps 15 \
  --image-mode always \
  --output "$OUTPUT"
