import json
import os
from pathlib import Path
import subprocess

import pytest


@pytest.mark.parametrize("profile, api_model, tokenizer", [
    ("gemma4-31b", "gemma-4-31b-it", "google/gemma-4-31B-it"),
    ("gemma4-26b-a4b", "gemma-4-26b-a4b-it", "google/gemma-4-26B-A4B-it"),
    ("qwen3.8-27b", "qwen3.8-27b", "Qwen/Qwen3.8-27B"),
    ("qwen3.6-35b-a3b", "qwen3.6-35b-a3b", "Qwen/Qwen3.6-35B-A3B"),
])
def test_benchmark_model_selection(tmp_path, profile, api_model, tokenizer):
    stub = tmp_path / "uv"
    stub.write_text('#!/bin/bash\nprintf "%s\\n" "$@"\n')
    stub.chmod(0o755)
    environment = os.environ.copy()
    environment.update(
        PATH=f"{tmp_path}:{environment['PATH']}",
        LLM_BASE_URL="http://example.test",
        LLM_API_KEY="dummy-test-key",
        INPUT_TOKENS="128",
        OUTPUT_TOKENS="256",
        THINKING="false",
    )
    script = Path("scripts/benchmark-model.sh").resolve()
    result = subprocess.run(
        ["bash", str(script), profile], cwd=tmp_path, env=environment,
        capture_output=True, text=True, check=True,
    )
    arguments = result.stdout.splitlines()
    assert arguments[arguments.index("--model") + 1] == api_model
    assert arguments[arguments.index("--tokenizer") + 1] == tokenizer
    assert arguments[arguments.index("--output-artifact-dir") + 1].startswith(f"results/{profile}/")
    inputs = json.loads(arguments[arguments.index("--extra-inputs") + 1])
    assert inputs["chat_template_kwargs"]["enable_thinking"] is False


@pytest.mark.parametrize("profile", ["qwen3-8b", "qwen3-30b-a3b"])
def test_removed_benchmark_profiles_are_rejected(profile):
    result = subprocess.run(
        ["bash", "scripts/benchmark-model.sh", profile], capture_output=True, text=True,
    )
    assert result.returncode == 2
    assert "Usage:" in result.stderr
