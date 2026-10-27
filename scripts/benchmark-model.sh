#!/usr/bin/env bash
set -euo pipefail

profile="${1:-}"
served_model="$profile"
case "$profile" in
  gemma4-31b|gemma-4-31b-it)
    profile=gemma4-31b
    served_model=gemma-4-31b-it
    tokenizer=google/gemma-4-31B-it
    ;;
  gemma4-26b-a4b|gemma-4-26b-a4b-it)
    profile=gemma4-26b-a4b
    served_model=gemma-4-26b-a4b-it
    tokenizer=google/gemma-4-26B-A4B-it
    ;;
  qwen3.6-35b-a3b)
    tokenizer=Qwen/Qwen3.6-35B-A3B
    ;;
  qwen3.8-27b)
    tokenizer=Qwen/Qwen3.8-27B
    ;;
  *)
    echo 'Usage: bash scripts/benchmark-model.sh {gemma4-31b|gemma4-26b-a4b|qwen3.6-35b-a3b|qwen3.8-27b}' >&2
    exit 2
    ;;
esac

: "${LLM_BASE_URL:?Set LLM_BASE_URL to the deployed server origin}"
: "${LLM_API_KEY:?Set LLM_API_KEY to the server API key}"
input_tokens="${INPUT_TOKENS:-8192}"
output_tokens="${OUTPUT_TOKENS:-4096}"
thinking="${THINKING:-true}"
if [[ "$thinking" != true && "$thinking" != false ]]; then
  echo 'THINKING must be true or false' >&2
  exit 2
fi
for token_count in "$input_tokens" "$output_tokens"; do
  if [[ ! "$token_count" =~ ^[1-9][0-9]*$ ]]; then
    echo 'INPUT_TOKENS and OUTPUT_TOKENS must be positive integers' >&2
    exit 2
  fi
done
if (( input_tokens + output_tokens > 131072 )); then
  echo 'Input plus output allowance must fit the shared 131072-token context' >&2
  exit 2
fi

run_id="$(date -u +%Y%m%dT%H%M%SZ)-$$"
artifact_dir="results/${profile}/in${input_tokens}-out${output_tokens}-thinking${thinking}/${run_id}"
extra_inputs="{\"temperature\":0,\"seed\":42,\"chat_template_kwargs\":{\"enable_thinking\":${thinking}},\"stream_options\":{\"include_usage\":true}}"

# Run from the repository root so uv and results resolve consistently.
cd "$(dirname "$0")/.."
exec uv run --no-sync aiperf profile \
  --model "$served_model" \
  --endpoint-type chat \
  --url "$LLM_BASE_URL" \
  --api-key "$LLM_API_KEY" \
  --streaming \
  --concurrency 1 \
  --warmup-request-count 5 \
  --request-count 20 \
  --synthetic-input-tokens-mean "$input_tokens" \
  --synthetic-input-tokens-stddev 0 \
  --output-tokens-mean "$output_tokens" \
  --output-tokens-stddev 0 \
  --use-legacy-max-tokens \
  --tokenizer "$tokenizer" \
  --tokenizer-revision "${TOKENIZER_REVISION:-main}" \
  --apply-chat-template \
  --random-seed 42 \
  --network-latency-automatic \
  --output-artifact-dir "$artifact_dir" \
  --extra-inputs "$extra_inputs"
