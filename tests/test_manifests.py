import json
import os
import shutil
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import yaml


@pytest.fixture
def kustomize_tree(tmp_path):
    directory = tmp_path / "k8s"
    shutil.copytree("k8s", directory, ignore=shutil.ignore_patterns(".env", "*.secret.yaml"))
    overlay = directory / "addons/tls"
    shutil.copyfile(overlay / ".env.example", overlay / ".env")
    return directory


def build_manifests(directory):
    result = subprocess.run(
        ["kubectl", "kustomize", str(directory)], capture_output=True, text=True, check=True,
    )
    return list(yaml.safe_load_all(result.stdout))


@pytest.mark.parametrize("overlay", ["gemma4-31b", "gemma4-26b-a4b"])
def test_manifests_and_context_reasoning_settings(kustomize_tree, overlay):
    documents = build_manifests(kustomize_tree / "overlays" / overlay)
    deployment = next(document for document in documents if document["kind"] == "Deployment")
    assert deployment["metadata"]["name"] == "vllm"
    assert deployment["spec"]["selector"]["matchLabels"] == {"app": "vllm"}
    assert deployment["spec"]["template"]["metadata"]["labels"] == {"app": "vllm"}
    pod = deployment["spec"]["template"]["spec"]
    assert pod["runtimeClassName"] == "nvidia"
    command = pod["containers"][0]["args"][0]
    assert "--max-model-len 131072" in command
    assert '"enable_thinking":true' in command
    assert "--enable-prefix-caching" in command
    assert "--max-num-seqs 1" in command
    assert "--disable-log-requests" not in command
    assert "$$" not in command
    assert [container["name"] for container in pod["containers"]] == ["vllm"]
    assert "--host 0.0.0.0 --port 8000" in command
    assert "/generation" not in command
    assert pod["containers"][0]["ports"] == [{"name": "http", "containerPort": 8000}]
    for probe in ["startupProbe", "readinessProbe"]:
        assert pod["containers"][0][probe]["httpGet"] == {"path": "/health", "port": "http"}
    assert {volume["name"] for volume in pod["volumes"]} == {"data", "shm"}
    service = next(document for document in documents if document["kind"] == "Service")
    assert service["spec"]["ports"] == [{"name": "http", "port": 8080, "targetPort": "http"}]


@pytest.mark.parametrize("overlay", [
    "gemma4-31b", "gemma4-26b-a4b", "qwen3.8-27b", "qwen3.6-35b-a3b",
])
def test_models_do_not_require_tls_addons(kustomize_tree, overlay):
    documents = build_manifests(kustomize_tree / "overlays" / overlay)
    for document in documents:
        assert not document["apiVersion"].startswith("cert-manager.io/")
        assert document["kind"] != "HelmChart"

    routes = {}
    for document in documents:
        if document["kind"] == "IngressRoute":
            routes[document["metadata"]["name"]] = document["spec"]
    assert set(routes) == {"llm-http", "llm-tunnel"}
    assert routes["llm-http"]["entryPoints"] == ["web"]
    assert routes["llm-tunnel"]["entryPoints"] == ["tunnel"]
    for route in routes.values():
        assert "tls" not in route
        assert route["routes"][0]["services"][0]["name"] == "vllm"
        assert route["routes"][0]["services"][0]["port"] == 8080
        assert route["routes"][0]["match"] == (
            'PathPrefix(`/v1/`) || Path(`/health`) || Path(`/metrics`)'
        )

    chart = next(document for document in documents if document["kind"] == "HelmChartConfig")
    values = yaml.safe_load(chart["spec"]["valuesContent"])
    assert values["ports"]["web"]["port"] == 80
    assert values["ports"]["web"]["hostPort"] == 80
    assert values["ports"]["websecure"]["port"] == 443
    assert "--entryPoints.web.transport.respondingTimeouts.writeTimeout=0s" in values["additionalArguments"]


def test_tls_addon_does_not_manage_model_resources(kustomize_tree):
    documents = build_manifests(kustomize_tree / "addons/tls")
    assert sorted(document["kind"] for document in documents) == [
        "Certificate", "ClusterIssuer", "ClusterIssuer", "ConfigMap", "IngressRoute",
    ]
    assert not any(document["kind"] == "Secret" for document in documents)
    ingress = next(document for document in documents if document["kind"] == "IngressRoute")
    assert ingress["metadata"]["name"] == "llm-https"
    assert ingress["spec"]["entryPoints"] == ["websecure"]
    assert ingress["spec"]["tls"]["secretName"] == "llm-tls"
    assert ingress["spec"]["routes"][0]["services"][0]["name"] == "vllm"
    assert ingress["spec"]["routes"][0]["services"][0]["port"] == 8080

    tls_identities = set()
    for document in documents:
        metadata = document["metadata"]
        tls_identities.add((document["kind"], metadata.get("namespace"), metadata["name"]))
    for overlay in ["gemma4-31b", "gemma4-26b-a4b", "qwen3.8-27b", "qwen3.6-35b-a3b"]:
        for document in build_manifests(kustomize_tree / "overlays" / overlay):
            metadata = document["metadata"]
            identity = (document["kind"], metadata.get("namespace"), metadata["name"])
            assert identity not in tls_identities


def test_cert_manager_install_is_separate_from_tls_resources(kustomize_tree):
    documents = build_manifests(kustomize_tree / "addons/cert-manager")
    assert len(documents) == 1
    chart = documents[0]
    assert chart["kind"] == "HelmChart"
    assert chart["metadata"]["name"] == "cert-manager"
    assert chart["spec"]["targetNamespace"] == "cert-manager"
    values = yaml.safe_load(chart["spec"]["valuesContent"])
    assert values["crds"]["enabled"] is True


@pytest.mark.parametrize("issuer", ["letsencrypt-staging", "letsencrypt-production"])
def test_environment_replacements_and_infrastructure(kustomize_tree, issuer):
    overlay = kustomize_tree / "addons/tls"
    (overlay / ".env").write_text(
        "LLM_HOST=model.test.example.org\nDNS_ZONE=example.org\n"
        f"ACME_EMAIL=admin@example.org\nACME_ISSUER={issuer}\n"
    )
    documents = build_manifests(kustomize_tree / "addons/tls")
    certificate = next(document for document in documents if document["kind"] == "Certificate")
    assert certificate["spec"]["dnsNames"] == ["model.test.example.org"]
    assert certificate["spec"]["issuerRef"]["name"] == issuer
    issuers = []
    for document in documents:
        if document["kind"] == "ClusterIssuer":
            issuers.append(document)
            assert document["spec"]["acme"]["email"] == "admin@example.org"
            assert document["spec"]["acme"]["solvers"][0]["selector"]["dnsZones"] == ["example.org"]
        if document["kind"] == "IngressRoute" and document["metadata"]["name"] == "llm-https":
            assert document["spec"]["entryPoints"] == ["websecure"]
            assert document["spec"]["tls"]["secretName"] == "llm-tls"
            assert document["spec"]["routes"][0]["match"] == (
                'Host(`model.test.example.org`) && (PathPrefix(`/v1/`) || Path(`/health`) || Path(`/metrics`))'
            )
    assert len(issuers) == 2
    assert not any(document["kind"] == "Secret" for document in documents)
    for directory in ["addons/cert-manager", "gpu-check"]:
        assert build_manifests(kustomize_tree / directory)


@pytest.mark.parametrize("overlay, model_repo", [
    ("gemma4-31b", "google/gemma-4-31B-it"),
    ("gemma4-26b-a4b", "google/gemma-4-26B-A4B-it"),
    ("qwen3.8-27b", "Qwen/Qwen3.8-27B"),
    ("qwen3.6-35b-a3b", "Qwen/Qwen3.6-35B-A3B"),
])
def test_model_init_cache_and_offline_serving(tmp_path, monkeypatch, capsys, kustomize_tree, overlay, model_repo):
    documents = build_manifests(kustomize_tree / "overlays" / overlay)
    deployment = next(document for document in documents if document["kind"] == "Deployment")
    assert deployment["metadata"]["name"] == "vllm"
    assert deployment["spec"]["selector"]["matchLabels"] == {"app": "vllm"}
    assert deployment["spec"]["template"]["metadata"]["labels"] == {"app": "vllm"}
    pod = deployment["spec"]["template"]["spec"]
    init = pod["initContainers"][0]
    serving = pod["containers"][0]
    revision = "main"
    resolved_revision = "a" * 40
    cache_home = tmp_path / "hf-cache"
    model_name = model_repo.rsplit("/", 1)[-1]
    cache_name = "models--" + model_repo.replace("/", "--")
    snapshot = cache_home / "hub" / cache_name / "snapshots" / resolved_revision
    model_path = cache_home / "models" / model_name / revision
    completion = snapshot / ".download-complete.json"
    download = Mock()
    api = Mock()
    api.model_info.return_value = SimpleNamespace(sha=resolved_revision)
    model_files = [
        "config.json", "model.safetensors.index.json",
        "model-00001-of-00002.safetensors", "model-00002-of-00002.safetensors",
    ]
    api.list_repo_files.return_value = model_files

    # A complete cache for another model must never satisfy this download.
    other_snapshot = cache_home / "hub/models--other--model/snapshots" / resolved_revision
    other_snapshot.mkdir(parents=True)
    for name in model_files:
        (other_snapshot / name).write_bytes(b"other-model")
    (other_snapshot / ".download-complete.json").write_text(json.dumps(model_files))
    monkeypatch.setitem(sys.modules, "huggingface_hub", SimpleNamespace(
        HfApi=lambda: api,
        snapshot_download=download,
    ))
    disable_progress_bars = Mock()
    monkeypatch.setitem(sys.modules, "huggingface_hub.utils", SimpleNamespace(
        disable_progress_bars=disable_progress_bars,
    ))
    monkeypatch.setenv("HF_HOME", str(cache_home))
    init_environment = {}
    for entry in init["env"]:
        if "value" in entry:
            init_environment[entry["name"]] = entry["value"]
    assert init_environment["MODEL_REVISION"] == revision
    assert init_environment["MODEL_REPO"] == model_repo
    assert init_environment["HF_XET_HIGH_PERFORMANCE"] == "1"
    monkeypatch.setenv("MODEL_REVISION", init_environment["MODEL_REVISION"])
    monkeypatch.setenv("MODEL_REPO", init_environment["MODEL_REPO"])
    script = compile(init["args"][0], "download-model", "exec")

    def download_snapshot(**kwargs):
        assert kwargs == {
            "repo_id": model_repo,
            "revision": resolved_revision,
            "cache_dir": str(cache_home / "hub"),
        }
        snapshot.mkdir(parents=True, exist_ok=True)
        for name in model_files:
            (snapshot / name).write_bytes(b"expected-model-file")
        return str(snapshot)

    download.side_effect = RuntimeError("Interrupted download")
    with pytest.raises(RuntimeError, match="Interrupted download"):
        exec(script, {})
    assert not completion.exists()

    download.side_effect = download_snapshot
    download.reset_mock()
    exec(script, {})
    assert download.call_count == 1
    assert api.list_repo_files.call_count == 2
    api.model_info.assert_called_with(model_repo, revision=revision)
    api.list_repo_files.assert_called_with(model_repo, revision=resolved_revision)
    assert json.loads(completion.read_text()) == sorted(model_files)
    assert completion.is_file()
    assert model_path.resolve() == snapshot
    output = capsys.readouterr().out
    assert "Model download: 0/4 files ready" in output
    assert "Model download: 4/4 files ready" in output
    assert f"Resolved model revision: {resolved_revision}" in output
    assert "\r" not in output
    disable_progress_bars.assert_called()

    exec(script, {})
    assert download.call_count == 1
    assert api.model_info.call_count == 2

    (snapshot / "model-00002-of-00002.safetensors").unlink()
    exec(script, {})
    assert download.call_count == 2
    assert (snapshot / "model-00002-of-00002.safetensors").is_file()

    completion.unlink()
    (snapshot / ".download-complete.tmp").write_text("interrupted marker")
    exec(script, {})
    exec(script, {})
    assert download.call_count == 3

    assert init["image"] == serving["image"] == "vllm/vllm-openai:v0.32.0"
    init_environment = {}
    for entry in init["env"]:
        init_environment[entry["name"]] = entry.get("value")
    assert init_environment["MODEL_REVISION"] == revision
    assert init_environment["MODEL_REPO"] == model_repo
    environment = {}
    for entry in serving["env"]:
        environment[entry["name"]] = entry.get("value")
    assert environment["HF_HUB_OFFLINE"] == "1"
    assert environment["TRANSFORMERS_OFFLINE"] == "1"
    assert "MODEL_REVISION" not in environment
    assert "HF_TOKEN" not in environment

    # Run the actual launch script with a stub so Bash expansion is exercised.
    binary_directory = tmp_path / "bin"
    binary_directory.mkdir()
    stub = binary_directory / "vllm"
    stub.write_text('#!/bin/bash\nprintf "%s\\n" "$@"\n')
    stub.chmod(0o755)
    command = serving["args"][0]
    process_environment = os.environ.copy()
    process_environment.pop("MODEL_REVISION", None)
    process_environment.update(environment)
    process_environment["PATH"] = f"{binary_directory}:{process_environment['PATH']}"
    process_environment["LLM_API_KEY"] = "test-key"
    result = subprocess.run(
        ["bash", "-ec", command], env=process_environment, capture_output=True, text=True, check=True,
    )
    arguments = result.stdout.splitlines()
    assert arguments[:2] == ["serve", f"/data/hf-cache/models/{model_name}/{revision}"]
    assert arguments[arguments.index("--api-key") + 1] == "test-key"
    if overlay == "gemma4-26b-a4b":
        assert arguments[arguments.index("--served-model-name") + 1] == "gemma-4-26b-a4b-it"
        assert "gemma-4-31B-it" not in command

    for container in [init, serving]:
        assert {"name": "data", "mountPath": "/data"} in container["volumeMounts"]
    data_volume = next(volume for volume in pod["volumes"] if volume["name"] == "data")
    assert data_volume["persistentVolumeClaim"]["claimName"] == "llm-data"


@pytest.mark.parametrize("overlay, tool_parser", [
    ("qwen3.8-27b", "qwen3_coder"),
    ("qwen3.6-35b-a3b", "qwen3_coder"),
])
def test_qwen_launch_settings_and_existing_routes(kustomize_tree, overlay, tool_parser):
    documents = build_manifests(kustomize_tree / "overlays" / overlay)
    deployment = next(document for document in documents if document["kind"] == "Deployment")
    command = deployment["spec"]["template"]["spec"]["containers"][0]["args"][0]
    assert f"--served-model-name {overlay}" in command
    assert "--max-model-len 131072 --max-num-seqs 1" in command
    assert f"--reasoning-parser qwen3 --tool-call-parser {tool_parser}" in command
    assert "--chat-template " not in command
    assert "gemma4" not in command
    if overlay == "qwen3.6-35b-a3b":
        assert "--language-model-only" in command
    service = next(document for document in documents if document["kind"] == "Service")
    assert service["spec"]["selector"] == deployment["spec"]["selector"]["matchLabels"]
    assert service["metadata"]["name"] == "vllm"
