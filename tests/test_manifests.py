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
    overlay = directory / "overlays/local-cert-manager"
    shutil.copyfile(overlay / ".env.example", overlay / ".env")
    return directory


def build_manifests(directory):
    result = subprocess.run(
        ["kubectl", "kustomize", str(directory)], capture_output=True, text=True, check=True,
    )
    return list(yaml.safe_load_all(result.stdout))


def test_manifests_and_context_reasoning_settings(kustomize_tree):
    documents = build_manifests(kustomize_tree / "overlays/local")
    deployment = next(document for document in documents if document["kind"] == "Deployment")
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


def test_default_overlay_does_not_require_cert_manager(kustomize_tree):
    documents = build_manifests(kustomize_tree / "overlays/local")
    for document in documents:
        assert not document["apiVersion"].startswith("cert-manager.io/")
        assert document["kind"] != "HelmChart"

    ingress = next(document for document in documents if document["metadata"]["name"] == "llm-http")
    assert "tls" not in ingress["spec"]
    assert ingress["spec"]["entryPoints"] == ["web"]
    assert ingress["spec"]["routes"][0]["match"] == (
        'PathPrefix(`/v1/`) || Path(`/health`) || Path(`/metrics`)'
    )

    chart = next(document for document in documents if document["kind"] == "HelmChartConfig")
    values = yaml.safe_load(chart["spec"]["valuesContent"])
    assert values["ports"]["web"]["port"] == 80
    assert values["ports"]["web"]["hostPort"] == 80
    assert "--entryPoints.web.transport.respondingTimeouts.writeTimeout=0s" in values["additionalArguments"]


def test_manual_tls_overlay(kustomize_tree):
    documents = build_manifests(kustomize_tree / "overlays/local-tls")
    ingress = next(document for document in documents if document["metadata"]["name"] == "llm-https")
    assert ingress["spec"]["entryPoints"] == ["websecure"]
    assert ingress["spec"]["tls"]["secretName"] == "llm-tls"
    assert not any(document["apiVersion"].startswith("cert-manager.io/") for document in documents)
    tunnel = next(document for document in documents if document["metadata"]["name"] == "llm-tunnel")
    assert tunnel["spec"]["entryPoints"] == ["tunnel"]
    assert "tls" not in tunnel["spec"]


@pytest.mark.parametrize("overlay, expected_routes", [
    ("local", {"llm-http"}),
    ("local-tls", {"llm-http", "llm-https"}),
    ("local-http-tls", {"llm-http", "llm-https"}),
    ("local-cert-manager", {"llm-http", "llm-https"}),
    ("local-http-cert-manager", {"llm-http", "llm-https"}),
])
def test_service_exposure_options(kustomize_tree, overlay, expected_routes):
    documents = build_manifests(kustomize_tree / "overlays" / overlay)
    routes = {}
    for document in documents:
        if document["kind"] == "IngressRoute":
            routes[document["metadata"]["name"]] = document["spec"]

    assert set(routes) == expected_routes | {"llm-tunnel"}
    for route in routes.values():
        match = route["routes"][0]["match"]
        assert "Path(`/health`)" in match
        assert "Path(`/metrics`)" in match
        assert "/probe" not in match
        assert "/experiment/status" not in match
    for name in expected_routes:
        route = routes[name]
        assert route["routes"][0]["services"][0]["name"] == "gemma"
        assert route["routes"][0]["services"][0]["port"] == 8080
        if name == "llm-http":
            assert route["entryPoints"] == ["web"]
            assert "tls" not in route
            assert "Host(" not in route["routes"][0]["match"]
        else:
            assert route["entryPoints"] == ["websecure"]
            assert route["tls"]["secretName"] == "llm-tls"
            if "cert-manager" in overlay:
                assert "Host(`llm.example.com`)" in route["routes"][0]["match"]

    chart = next(document for document in documents if document["kind"] == "HelmChartConfig")
    values = yaml.safe_load(chart["spec"]["valuesContent"])
    assert values["ports"]["web"]["port"] == 80
    assert values["ports"]["websecure"]["port"] == 443
    assert routes["llm-tunnel"]["entryPoints"] == ["tunnel"]
    assert "tls" not in routes["llm-tunnel"]


@pytest.mark.parametrize("overlay", [
    "local-tls", "local-cert-manager", "local-http-tls", "local-http-cert-manager",
])
def test_tls_overlays_preserve_all_default_resources(kustomize_tree, overlay):
    default_documents = build_manifests(kustomize_tree / "overlays/local")
    documents = build_manifests(kustomize_tree / "overlays" / overlay)
    resources = {}
    for document in documents:
        metadata = document["metadata"]
        identity = (document["kind"], metadata.get("namespace"), metadata["name"])
        resources[identity] = document

    for document in default_documents:
        metadata = document["metadata"]
        identity = (document["kind"], metadata.get("namespace"), metadata["name"])
        assert resources[identity] == document


@pytest.mark.parametrize("issuer", ["letsencrypt-staging", "letsencrypt-production"])
def test_environment_replacements_and_infrastructure(kustomize_tree, issuer):
    overlay = kustomize_tree / "overlays/local-cert-manager"
    (overlay / ".env").write_text(
        "LLM_HOST=model.test.example.org\nDNS_ZONE=example.org\n"
        f"ACME_EMAIL=admin@example.org\nACME_ISSUER={issuer}\n"
    )
    documents = build_manifests(kustomize_tree / "overlays/local-cert-manager")
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
    for directory in ["overlays/cert-manager", "gpu-check"]:
        assert build_manifests(kustomize_tree / directory)


def test_model_init_cache_and_offline_serving(tmp_path, monkeypatch, capsys, kustomize_tree):
    documents = build_manifests(kustomize_tree / "overlays/local")
    deployment = next(document for document in documents if document["kind"] == "Deployment")
    pod = deployment["spec"]["template"]["spec"]
    init = pod["initContainers"][0]
    serving = pod["containers"][0]
    revision = "main"
    resolved_revision = "a" * 40
    cache_home = tmp_path / "hf-cache"
    snapshot = cache_home / "hub/models--google--gemma-4-31B-it/snapshots" / resolved_revision
    model_path = cache_home / "models/gemma-4-31B-it" / revision
    completion = snapshot / ".download-complete.json"
    download = Mock()
    api = Mock()
    api.model_info.return_value = SimpleNamespace(sha=resolved_revision)
    api.list_repo_files.return_value = ["config.json", "model.safetensors"]
    monkeypatch.setitem(sys.modules, "huggingface_hub", SimpleNamespace(
        HfApi=lambda: api,
        snapshot_download=download,
    ))
    disable_progress_bars = Mock()
    monkeypatch.setitem(sys.modules, "huggingface_hub.utils", SimpleNamespace(
        disable_progress_bars=disable_progress_bars,
    ))
    monkeypatch.setenv("HF_HOME", str(cache_home))
    monkeypatch.setenv("MODEL_REVISION", revision)
    script = compile(init["args"][0], "download-model", "exec")

    def download_snapshot(**kwargs):
        assert kwargs == {
            "repo_id": "google/gemma-4-31B-it",
            "revision": resolved_revision,
            "cache_dir": str(cache_home / "hub"),
        }
        snapshot.mkdir(parents=True, exist_ok=True)
        (snapshot / "config.json").write_text("{}")
        (snapshot / "model.safetensors").write_bytes(b"weights")
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
    assert completion.is_file()
    assert model_path.resolve() == snapshot
    output = capsys.readouterr().out
    assert "Model download: 0/2 files ready" in output
    assert "Model download: 2/2 files ready" in output
    assert f"Resolved model revision: {resolved_revision}" in output
    assert "\r" not in output
    disable_progress_bars.assert_called()

    exec(script, {})
    assert download.call_count == 1
    assert api.model_info.call_count == 2

    (snapshot / "model.safetensors").unlink()
    exec(script, {})
    assert download.call_count == 2
    assert (snapshot / "model.safetensors").is_file()

    completion.unlink()
    (snapshot / ".download-complete.tmp").write_text("interrupted marker")
    exec(script, {})
    exec(script, {})
    assert download.call_count == 3

    assert init["image"] == serving["image"] == "vllm/vllm-openai:v0.32.0"
    assert init["env"][0] == {"name": "MODEL_REVISION", "value": revision}
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
    assert arguments[:2] == ["serve", f"/data/hf-cache/models/gemma-4-31B-it/{revision}"]
    assert arguments[arguments.index("--api-key") + 1] == "test-key"

