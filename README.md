# Gemma 4 vLLM benchmarks

Deploy one reasoning-enabled vLLM server in a Nebius region (`uk-south1` by default), then run AIPerf and OpenCode directly against its OpenAI-compatible API. Measure individual HTTP round trips separately using `/health`.

The repository creates deployment artifacts. Running the infrastructure commands below provisions billable resources.

For private access from your LAN, follow the [VPN gateway deployment instructions](vpngw/README.md) before provisioning the GPU VM. The optional VPN setup creates a separate VPC and workload subnet, then deploys a gateway VM using `nebius-vpngw`.

## What runs where

| Component | Location | Managed by |
|---|---|---|
| Spot B300 VM, boot and data disks, tunnel and IAM | Nebius | Terraform, Nebius provider only |
| Single-node k3s with embedded containerd and tunnel agent | GPU VM | Cloud-init |
| NVIDIA device plugin | k3s | Cloud-init, using the repository manifest |
| cert-manager, vLLM and ingress | k3s | YAML and `kubectl apply` |
| AIPerf, OpenCode and HTTP round-trip checks | Your local host | `uv run`, OpenCode and curl |

We use single-node K3s and its packaged Helm controller installs charts through Kubernetes Jobs. The optional cert-manager overlay installs it via the `HelmChart` CRD. We configure the bundled Traefik chart using `HelmChartConfig` YAML.

## 1. Environment, configuration, and setup

Local dependencies required:

- python 3.12
- uv
- terraform
- Nebius CLI
- kubectl (optional, for local manifest tests)
- jq
- npm
- rsync

Run the following to prepare the uv environment and base template files:

```sh
uv venv --python 3.12
uv sync --extra test --extra benchmark --locked

cp infra/terraform.tfvars.example infra/terraform.tfvars

# Optional cert-manager configuration. Preserve an existing .env.
cp -n k8s/overlays/local-cert-manager/.env.example k8s/overlays/local-cert-manager/.env
```

Create a tenant and project in Nebius.

In `terraform.tfvars`, populate project/network/subnet IDs and your SSH public key.

e.g.
```hcl
project_id     = "project-MY_PROJECT_ID"
network_id     = "vpcnetwork-MY-VPC_NETWORK_ID"
subnet_id      = "vpcsubnet-MY_VPC_SUBNET_ID"
ssh_public_key = "<my ssh public key>"
```

Optionally, add the VM platform and its resource preset in `infra/terraform.tfvars`. The defaults are:

```hcl
platform = "gpu-b300-sxm"
preset   = "1gpu-24vcpu-346gb"
```

Choose a GPU platform and a preset it supports in your region. Bootstrap checks NVIDIA driver and runtime availability.

The default provisions a spot VM that follows market spot prices. Supply `spot_pricing_policy_id` to use an existing maximum-price policy instead. Check your available quota and available machine preset before applying:

```sh
nebius compute platform list --parent-id YOUR_PROJECT_ID
nebius compute image list-public --region uk-south1
```

This example uses the `ubuntu24.04-cuda13.0` image. Nebius documents driver 580.x, CUDA 13.0 and Container Toolkit in that image. The resolved image ID is a Terraform output. Set `image_id` to that ID for subsequent reproducibility. [Image documentation](https://docs.nebius.com/compute/storage/boot-disk-images)

## 2. Provision and inspect bootstrap

Authenticate the Nebius Terraform provider using your normal environment/profile. Follow the provider's [authentication guide](https://docs.nebius.com/terraform-provider/authentication).

```sh
terraform -chdir=infra init
terraform -chdir=infra plan -out=deployment.tfplan
terraform -chdir=infra apply deployment.tfplan
terraform -chdir=infra output
```

Cloud-init runs once the VM boots up and performs these steps in order:

1. Create a user `ubuntu` with your SSH key.
2. Mount the secondary disk at `/srv/llm` using `/dev/disk/by-id/virtio-llm-data`. Disk size is configured in `boot_disk_size_gib` and `data_disk_size_gib` in `infra/terraform.tfvars`, the defaults are 100 GiB Network SSD for boot and 279 GiB (93 × 3) non-replicated Network SSD for data. 
   - We use the non replicated disk for higher IOPS and bandwidth than the standard Network SSD and store container images and model data here. The bandwidth scales with the size of the disk, you can experiment with it to improve model download and load times.
   - On first boot, bootstrap creates an EXT4 filesystem on the blank data disk and records the mount in `/etc/fstab` for subsequent reboots.
3. Run `nvidia-smi` to check GPU availability, verifies NVIDIA driver 580 or newer, and checks Container Toolkit and the NVIDIA runtime.
4. Install basic host tools (`curl`, `ca-certificates`, `jq`, `rsync`). 
   - Bind `/var/lib/docker` and `/var/lib/containerd` to directories on the data disk. Initial container data is copied with services stopped. Original directories remain as `.boot-backup` directories for operator review.
5. Install k3s with embedded containerd, private node addressing, secrets encryption, restrictive kubeconfig permissions, and data under `/srv/llm/k3s` on the data disk.
6. Check node readiness and NVIDIA RuntimeClass registration, apply the NVIDIA device plugin manifest, and wait for its DaemonSet to finish rolling out. 
7. Install tunnel agent v1.2.0 using the release archive and published SHA256 checksum. Create a restricted systemd service and configures VM metadata authentication. Its HTTP target is `127.0.0.1:8088`.
8. Write status and version logs.

You can view the bootstrap status in the log

```sh
ssh ubuntu@PUBLIC_IP 'sudo tail -100 /var/log/llm-bootstrap/bootstrap.log'
```

To view the nebius tunnel installation progress and status, run the following:

```sh
ssh ubuntu@PUBLIC_IP 'sudo journalctl -u nebius-tunnel-agent -n 30 --no-pager'
nebius tunnel get --id YOUR_TUNNEL_ID
```

## 3. Copy deployment files and SSH into the VM

From your local host, sync only the Kubernetes deployment manifests to the VM. This includes any local overlay `.env` and Secret files under `k8s/`, so the VM can apply the configuration you prepared. The command preserves the directory layout and restricts access to the deployment directory to the SSH user. Files that exist only on the VM are retained.

Rsync must be installed on both machines. Bootstrap installs it on new VMs. For an older VM without it, run `sudo apt-get update && sudo apt-get install -y rsync` in an SSH session first.

```sh
export SSH_TARGET=ubuntu@<VM_IP>
ssh "$SSH_TARGET" 'umask 077 && mkdir -p ~/llm-latency-test && chmod 700 ~/llm-latency-test'
rsync -av --relative \
  --exclude='.DS_Store' --exclude='._*' --exclude='__pycache__/' \
  k8s/ "${SSH_TARGET}:/home/ubuntu/llm-latency-test/"
ssh "$SSH_TARGET"
```

Once connected, enter the deployment directory and check the node:

```sh
cd ~/llm-latency-test
export KUBECONFIG="$HOME/.kube/config"
kubectl get nodes
```

Run all commands in steps 4 and 5 inside this SSH session. If you change deployment source on your local machine, remember to copy them over before applying it on the VM.

## 4. Prepare the application configuration

On the VM, run these commands from `~/llm-latency-test`. Preview the default overlay:

```sh
kubectl kustomize k8s/overlays/local
```

The deployment uses the published `vllm/vllm-openai:v0.32.0` image.

Create a namespace in Kubernetes to hold the inference server.

```sh
kubectl apply -f k8s/base/00-namespace.yaml
```

Create an application Secret. Populate it with a random API key and a Hugging Face token if access requires one.

```sh
cp -n k8s/secrets/secrets.yaml.example k8s/secrets/credentials.secret.yaml
chmod 600 k8s/secrets/credentials.secret.yaml
# Edit k8s/secrets/credentials.secret.yaml before continuing.
kubectl apply -f k8s/secrets/credentials.secret.yaml
```

## 5. Deploy HTTP, then optionally add HTTPS

Continue in the same VM SSH session, from `~/llm-latency-test`. Verify GPU access before starting the model. Delete a previous completed check so that a repeat deployment runs the check again:

```sh
kubectl -n kube-system rollout status daemonset/nvidia-device-plugin --timeout=300s
kubectl -n llm-latency delete job gpu-check --ignore-not-found
kubectl apply -k k8s/gpu-check
kubectl -n llm-latency wait --for=condition=complete job/gpu-check --timeout=300s
kubectl -n llm-latency logs job/gpu-check
kubectl -n llm-latency delete job gpu-check
```

### Choose service exposure

By default, we will deploy the vLLM service and expose it in plaintext HTTP. Optionally, you can choose to terminate TLS connections in the included Traefik ingress controller.  Both routes forward to the same `gemma:8080` service, which targets vLLM directly on pod port 8000.

| Overlay | Service routes | Certificate source |
| --- | --- | --- |
| `k8s/overlays/local` (default) | `llm-http` on HTTP port 80 | None |
| `k8s/overlays/local-tls` | HTTP on port 80 plus `llm-https` on HTTPS port 443 | Existing `llm-tls` Secret |
| `k8s/overlays/local-cert-manager` | HTTP on port 80 plus `llm-https` on HTTPS port 443 | cert-manager |

The TLS overlays include `local` and add the HTTPS route without changing `llm-http`. They work when applied after `local` or on a fresh deployment with the required certificate dependencies. There is no automatic HTTP-to-HTTPS redirect. `local-http-tls` and `local-http-cert-manager` remain aliases for `local-tls` and `local-cert-manager` respectively.

The `llm-tunnel` route remains on loopback port 8088 in every overlay. Its external HTTPS URL and certificate are managed by Nebius.

Cleanup is manual. Applying `local` after a TLS overlay leaves the existing HTTPS route in place. To remove that route, run `kubectl -n llm-latency delete ingressroute llm-https --ignore-not-found`. Certificate resources are retained. 

Copy the updated `k8s` tree to the VM before applying an overlay so that it uses the current manifests.

### Deploy and verify HTTP

Apply the HTTP overlay after the GPU check succeeds. Wait for the model before sending benchmark traffic.

```sh
export KUSTOMIZE_OVERLAY=k8s/overlays/local
kubectl -n llm-latency get secret llm-secrets
kubectl apply -k "$KUSTOMIZE_OVERLAY"
```

This step may take a long time as it pulls the vllm serving container (~10 GB), then downloads the model weights from Huggingface in an init container (~60 GB), then loads the model weights into GPU memory.  Once the model weights are cached, it will just reuse them so a second start will be faster. You can validate that the data directory is bound, then monitor the deployment is complete by watching the logs:

```sh
kubectl -n llm-latency get pods,pvc,ingressroute
kubectl -n llm-latency logs deployment/gemma -c vllm --tail=80
```

The application serves HTTP on port 80. Use an `http://` client URL. HTTP sends the API key and request/response content unencrypted, so use it on a trusted network or through the VPN. Allow TCP port 80 through any firewall used for direct HTTP access. The Nebius tunnel continues to use its public HTTPS URL.

If you're okay with plaintext HTTP, you can proceed to step 6 with this deployment. To add HTTPS, continue below in the same SSH session.

### Optional: add cert-manager and HTTPS

This upgrade reuses the running application, model cache, storage and application Secret. Apply `local-cert-manager` to add HTTPS alongside the existing HTTP route. The HTTPS route requires certificate issuance to finish. HTTP remains available throughout issuance.

First install cert-manager through K3s's built-in [Helm controller](https://docs.k3s.io/add-ons/helm). Its pinned chart includes the namespace, CRDs and RBAC. Wait for all three deployments before creating issuers or certificates:

```sh
kubectl get crd helmcharts.helm.cattle.io
kubectl apply -k k8s/overlays/cert-manager
for deployment in cert-manager cert-manager-webhook cert-manager-cainjector; do
  kubectl -n cert-manager wait --for=create "deployment/$deployment" --timeout=300s
  kubectl -n cert-manager rollout status "deployment/$deployment" --timeout=300s
done
```

In this example, we configure certificate issuance through Cloudflare with DNS-01 authorization. Prepare a Cloudflare token scoped to Zone DNS Edit and Zone Read for the selected zone, then populate and apply its Secret:

```sh
cp -n k8s/secrets/cloudflare.secret.yaml.example k8s/secrets/cloudflare.secret.yaml
chmod 600 k8s/secrets/cloudflare.secret.yaml
# Edit k8s/secrets/cloudflare.secret.yaml before continuing.
kubectl apply -f k8s/secrets/cloudflare.secret.yaml
```

Prepare the certificate settings on the VM. Preserve an existing `.env`:

```sh
cp -n k8s/overlays/local-cert-manager/.env.example k8s/overlays/local-cert-manager/.env
# Edit .env: LLM_HOST, DNS_ZONE, ACME_EMAIL and ACME_ISSUER.
```

Set `LLM_HOST` to the hostname clients will use, `DNS_ZONE` to its Cloudflare zone and `ACME_EMAIL` to your ACME contact email. Start with `ACME_ISSUER=letsencrypt-staging` to validate DNS-01. Allow TCP port 443 through any firewall used for direct HTTPS access.

Now apply the selected overlay. It includes the application resources, adds issuers and a Certificate, and creates `llm-https` using the `llm-tls` Secret and `LLM_HOST` hostname:

```sh
export KUSTOMIZE_OVERLAY=k8s/overlays/local-cert-manager
kubectl kustomize "$KUSTOMIZE_OVERLAY"
kubectl apply -k "$KUSTOMIZE_OVERLAY"
kubectl -n llm-latency wait --for=condition=Ready certificate/llm-tls --timeout=600s
kubectl -n llm-latency get certificate llm-tls
kubectl -n llm-latency get secret llm-tls
```

Staging certificates are untrusted by clients. After staging issuance succeeds, set `ACME_ISSUER=letsencrypt-production` in the same `.env`, then apply it again:

```sh
kubectl apply -k "$KUSTOMIZE_OVERLAY"
kubectl -n llm-latency wait --for=condition=Ready certificate/llm-tls --timeout=600s
```

Wait until the replacement Certificate is Ready and the endpoint presents a trusted production certificate before testing. On your local host, set `LLM_BASE_URL` to `https://` using the `LLM_HOST` hostname for direct HTTPS access. The Nebius tunnel retains its own external URL.

The HTTPS application route uses Traefik's [IngressRoute TLS configuration](https://doc.traefik.io/traefik/reference/routing-configuration/kubernetes/crd/http/ingressroute/).

### Optional: add HTTPS with your own certificate

If you already have a trusted certificate, you can upgrade the HTTP deployment directly. Place its certificate chain and private key on the VM, then run:

```sh
kubectl -n llm-latency create secret tls llm-tls \
  --cert=/path/to/fullchain.pem --key=/path/to/privkey.pem \
  --dry-run=client -o yaml | kubectl apply -f -
export KUSTOMIZE_OVERLAY=k8s/overlays/local-tls
kubectl apply -k "$KUSTOMIZE_OVERLAY"
```

Allow TCP port 443 and use `https://` client URLs with a hostname covered by the certificate. Applying `local-tls` retains the HTTP route. Any route cleanup is manual.

## 6. Connect directly to vLLM

Leave the VM's SSH session and run the remaining commands from your local repository:

```sh
export LLM_BASE_URL="http://$(terraform -chdir=infra output -raw private_ip | cut -d/ -f1)"
# For public HTTP access, use public_ip instead of private_ip above.
export OPENAI_BASE_URL="${LLM_BASE_URL}/v1"
```

`LLM_BASE_URL` is an origin such as `http://10.80.1.8`, with no path. The private IP requires a working VPN route. For public access, use the public IP. For HTTPS, use the certificate hostname. For tunnel access, use the actual Nebius tunnel URL. Keep `LLM_BASE_URL` free of a trailing slash and `/v1`. Reset `OPENAI_BASE_URL` after changing it.

Load `LLM_API_KEY` into your local environment securely. It must match the `api-key` in the deployed `llm-secrets` Secret. Check health, model discovery and Prometheus metrics:

```sh
curl --fail --show-error --silent "${LLM_BASE_URL}/health"
curl --fail --show-error --silent \
  --header "Authorization: Bearer ${LLM_API_KEY}" \
  "${LLM_BASE_URL}/v1/models"
curl --fail --show-error --silent "${LLM_BASE_URL}/metrics"
```

The path is `client → Traefik → vLLM`. vLLM listens on the pod network at port 8000. Traefik exposes `/v1/`, `/health` and `/metrics`. The vLLM API requires the key. In the pinned vLLM version, `/health` and `/metrics` are unauthenticated. Metrics are available to clients that can reach the configured ingress. [vLLM metrics](https://docs.vllm.ai/en/latest/usage/metrics/)

## 7. Run AIPerf

AIPerf generates synthetic prompts, measures client latency and throughput, and scrapes vLLM's `/metrics` endpoint automatically.  Here is an example that runs 20 requests with 8192 input tokens and up to 4096 output tokens after 5 warmup requests.

```sh
uv run --no-sync aiperf profile \
  --model gemma-4-31b-it \
  --endpoint-type chat \
  --url "$LLM_BASE_URL" \
  --api-key "$LLM_API_KEY" \
  --streaming \
  --concurrency 1 \
  --warmup-request-count 5 \
  --request-count 20 \
  --synthetic-input-tokens-mean 8192 \
  --synthetic-input-tokens-stddev 0 \
  --output-tokens-mean 4096 \
  --output-tokens-stddev 0 \
  --use-legacy-max-tokens \
  --tokenizer google/gemma-4-31B-it \
  --tokenizer-revision main \
  --apply-chat-template \
  --random-seed 42 \
  --network-latency-automatic \
  --extra-inputs '{"temperature":0,"seed":42,"chat_template_kwargs":{"enable_thinking":true},"stream_options":{"include_usage":true}}'
```

This requests approximately 8192 input tokens and an output allowance of 4096 tokens, including reasoning. Actual output can finish earlier. Change the input-token mean to 128 or 8192 for other prompt sizes. The tokenizer revision must match the model revision configured in the deployment. The first run may download the tokenizer locally.

AIPerf prints TTFT, inter-token latency and completion latency measured from the client. Prometheus statistics describe vLLM's internal operation and are not paired per-request transport timings. Keep OpenCode and other model users idle during a benchmark.

The `--network-latency-automatic` option collects TCP-handshake RTT samples during the run. AIPerf preserves raw timings and emits a network RTT summary and separate `network_adjusted_*` metrics. The adjustment subtracts the mean TCP RTT. That should give an idea of the impact of the network.

Here is an example output.  You can see the avg Request Latency vs the avg Network Adjusted Request Latency gives an estimate of the impact of the network.  in this case ~110ms out of the full 20s request, since I ran this against a vLLM running in UK from North America.

```sh
                                                    NVIDIA AIPerf | LLM Metrics                                                     
┏━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━┳━━━━━━━━━━━┳━━━━━━━━━━━┳━━━━━━━━━━━┳━━━━━━━━━━━┳━━━━━━━━━━━┳━━━━━━━━━━┓
┃                                        Metric ┃       avg ┃       min ┃       max ┃       p99 ┃       p90 ┃       p50 ┃      std ┃
┡━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━╇━━━━━━━━━━━╇━━━━━━━━━━━╇━━━━━━━━━━━╇━━━━━━━━━━━╇━━━━━━━━━━━╇━━━━━━━━━━┩
│                      Time to First Token (ms) │    200.46 │    196.24 │    210.92 │    210.10 │    203.20 │    200.20 │     3.40 │
│          Network-Adjusted Time to First Token │     90.34 │     86.12 │    100.80 │     99.98 │     93.08 │     90.08 │     3.40 │
│                                          (ms) │           │           │           │           │           │           │          │
│                     Time to Second Token (ms) │     12.12 │      3.07 │     17.57 │     17.20 │     14.40 │     12.43 │     2.98 │
│               Time to First Output Token (ms) │  8,283.82 │  5,168.04 │ 11,977.19 │ 11,885.99 │  9,949.23 │  8,306.14 │ 1,658.83 │
│   Network-Adjusted Time to First Output Token │  8,173.70 │  5,057.92 │ 11,867.07 │ 11,775.87 │  9,839.11 │  8,196.02 │ 1,658.83 │
│                                          (ms) │           │           │           │           │           │           │          │
│                          Request Latency (ms) │ 19,254.05 │ 10,423.87 │ 27,601.65 │ 26,947.62 │ 24,016.63 │ 20,384.82 │ 4,203.24 │
│              Network-Adjusted Request Latency │ 19,143.93 │ 10,313.75 │ 27,491.53 │ 26,837.50 │ 23,906.51 │ 20,274.70 │ 4,203.24 │
│                                          (ms) │           │           │           │           │           │           │          │
│                              Network RTT (ms) │    110.12 │    110.12 │    110.12 │    110.12 │    110.12 │    110.12 │     0.00 │
│                          Decode Duration (ms) │ 19,053.59 │ 10,226.62 │ 27,400.99 │ 26,746.86 │ 23,819.57 │ 20,183.36 │ 4,203.78 │
│                      Inter Token Latency (ms) │     13.03 │     13.01 │     13.04 │     13.04 │     13.03 │     13.03 │     0.01 │
│              Output Token Throughput Per User │     76.77 │     76.66 │     76.85 │     76.84 │     76.79 │     76.77 │     0.03 │
│                             (tokens/sec/user) │           │           │           │           │           │           │          │
│ E2E Output Token Throughput (tokens/sec/user) │     75.97 │     75.31 │     76.23 │     76.22 │     76.16 │     76.05 │     0.21 │
│               Output Sequence Length (tokens) │  1,463.65 │    785.00 │  2,104.00 │  2,053.84 │  1,829.20 │  1,550.50 │   322.56 │
│                Input Sequence Length (tokens) │  8,179.00 │  8,179.00 │  8,179.00 │  8,179.00 │  8,179.00 │  8,179.00 │     0.00 │
│          Output Token Throughput (tokens/sec) │     75.95 │       N/A │       N/A │       N/A │       N/A │       N/A │      N/A │
│           Input Token Throughput (tokens/sec) │    424.42 │       N/A │       N/A │       N/A │       N/A │       N/A │      N/A │
│             Request Throughput (requests/sec) │      0.05 │       N/A │       N/A │       N/A │       N/A │       N/A │      N/A │
│          Completed Requests (Success + Error) │     20.00 │       N/A │       N/A │       N/A │       N/A │       N/A │      N/A │
│                                    (requests) │           │           │           │           │           │           │          │
│                        Request Error Rate (%) │      0.00 │       N/A │       N/A │       N/A │       N/A │       N/A │      N/A │
│                      Request Count (requests) │     20.00 │       N/A │       N/A │       N/A │       N/A │       N/A │      N/A │
└───────────────────────────────────────────────┴───────────┴───────────┴───────────┴───────────┴───────────┴───────────┴──────────┘
```

## 8. Run OpenCode v2

Run OpenCode v2 on your local machine with the model served remotely by vLLM. The example pins `@opencode/cli` to version `2.1.4` and installs it under the `.local/opencode-v2/` directory. [OpenCode v2 installation](https://opencode.ai/v2/docs/)

The maintained [OpenCode v2 configuration](config/opencode.example.json) connects directly to `${OPENAI_BASE_URL}` and reads the key from `LLM_API_KEY`. It uses v2 `providers`, `settings`, and ordered `permissions` fields. The `nebius` provider uses the built-in OpenAI-compatible package, with model limits and tool support defined explicitly. [OpenCode v2 providers](https://opencode.ai/v2/docs/providers/)

Set `LLM_BASE_URL`, `OPENAI_BASE_URL` and `LLM_API_KEY` in this terminal as shown in step 6 before launching OpenCode. Stop AIPerf while trying OpenCode.

```sh
mkdir -p .local
npm install --prefix .local/opencode-v2 --no-audit --no-fund @opencode/cli@2.1.4
.local/opencode-v2/node_modules/.bin/opencode --version
export OPENCODE_CONFIG="$PWD/config/opencode.example.json"
.local/opencode-v2/node_modules/.bin/opencode --standalone
```

The interactive command reads `nebius/gemma-4-31b-it` from the example configuration. `--standalone` starts a private OpenCode server with this terminal's environment, so the endpoint and key are available without configuring a shared background service. [OpenCode v2 CLI](https://opencode.ai/v2/docs/cli/)

The configuration advertises a 131,072-token context window and a 16,384-token output budget. Reasoning and tool calling are enabled. It allows file reads and edits, and limits shell tools to `python -m pytest` and `git diff`. OpenCode itself does not require Python or uv. For the Python coding fixture, optionally prefix the launch command with `uv run --no-sync` so its test tool can use the prepared Python environment.

For one noninteractive coding task:

```sh
.local/opencode-v2/node_modules/.bin/opencode run --standalone \
  --model nebius/gemma-4-31b-it \
  'Inspect the code and explain the bug before making changes.'
```

Use a disposable copy of `fixtures/coding` for a repeatable coding exercise. Its calculator intentionally contains a failing multiplication test. OpenCode's interaction time includes model calls, tool execution and local processing, so it is different from an individual API request's latency.

## 9. Measure individual round trips

Probe `/health` to measure a small HTTP request without invoking model generation. This records every request separately in CSV. Each curl process opens a fresh connection, so the total includes connection setup, TLS for HTTPS, ingress handling and the health response:

```sh
mkdir -p results
printf 'sample,http_status,dns_s,tcp_connected_s,tls_ready_s,first_byte_s,total_s,remote_ip\n' \
  > results/roundtrip.csv
for sample in $(seq 1 20); do
  printf '%s,' "$sample" >> results/roundtrip.csv
  curl --fail --show-error --silent --output /dev/null \
    --connect-timeout 5 --max-time 15 \
    --write-out '%{http_code},%{time_namelookup},%{time_connect},%{time_appconnect},%{time_starttransfer},%{time_total},%{remote_ip}\n' \
    "${LLM_BASE_URL}/health" >> results/roundtrip.csv || break
  sleep 0.2
done
cat results/roundtrip.csv
```

All timing fields are seconds measured from the start of that request. `total_s` is the complete HTTP round trip. `first_byte_s` includes server response delay. `tcp_connected_s` includes DNS and TCP setup. `tls_ready_s` is zero for HTTP. These are cumulative timings, not separate durations to add together. A failed request stops the loop and may leave a failed sample in the CSV.

For ICMP RTT to a directly reachable VM IP, run:

```sh
ping -c 20 "$(terraform -chdir=infra output -raw private_ip | cut -d/ -f1)"
```

ICMP RTT excludes HTTP and model work and can be blocked independently of HTTP. For a Nebius tunnel URL, HTTP and TCP checks measure the exposed relay path. ICMP to an edge hostname does not measure the complete path to vLLM. Health timings and AIPerf generation timings answer different questions. Neither should be subtracted from the other as an exact model-processing measurement.

## 10. Validate and stop

Local tests use the uv environment. Manifest tests need local `kubectl` for offline Kustomize builds. Network integration tests bind temporary localhost sockets:

```sh
uv run --no-sync python -m pytest -q
terraform -chdir=infra validate
```

To inspect the serving pod on the VM:

```sh
kubectl -n llm-latency get pods,services,ingressroutes
kubectl -n llm-latency logs deployment/gemma -c vllm --tail=80
```

Collect AIPerf artifacts and any server logs you need before stopping or destroying resources. Stop through Terraform rather than an operating-system shutdown:

```sh
terraform -chdir=infra apply -var='stopped=true'
```

To remove all managed infrastructure, including both disks and the model cache:

```sh
terraform -chdir=infra destroy
```

Export required data before destroying either disk. Stopped VMs can still incur storage and IP costs. Preserve `infra/terraform.tfstate*`, `infra/terraform.tfvars`, local environment files and Kubernetes Secrets when cleaning generated output.

