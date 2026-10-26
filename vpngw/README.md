# Deploy the VPN gateway

This optional setup connects your LAN to the VM's private IP through a Nebius gateway VM running IPsec and BGP. The supplied example uses one gateway VM. It does not enable VM HA.

Run commands on your local host from the repository root. Terraform and VPNGW commands below create billable resources. Keep the VPN network state separate from `infra/`, which manages the GPU VM.

## 1. Install the gateway CLI

Use Python 3.12, `uv`, Terraform 1.6 or newer, the Nebius CLI, and `jq`. Authenticate your Nebius CLI profile for the target tenant and project, and configure Terraform authentication as described in the [main README](../README.md#2-provision-and-inspect-bootstrap). Your identity needs permission to create networking, public IP allocations, disks, and VMs. You also need an SSH key pair and SSH access to the gateway's public IP.

The gateway source checkout is expected at `vpngw/nebius-ps-services`. If it is absent, clone it first:

```sh
git clone https://github.com/nebius/nebius-ps-services.git vpngw/nebius-ps-services
```

Install into a separate environment, since the benchmark environment does not install `nebius-vpngw`:

```sh
uv venv --python 3.12 vpngw/.venv
uv pip install --python vpngw/.venv/bin/python \
  -e 'vpngw/nebius-ps-services/services/vpngw[dev]'
vpngw/.venv/bin/nebius-vpngw --version
```

Developer dependencies provide the wheel builder used when deploying from source. For release-wheel installation and advanced gateway options, see the [gateway documentation](nebius-ps-services/services/vpngw/README.md).

## 2. Create the VPC and workload subnet

The [network Terraform configuration](network/main.tf) creates a VPC, private address pool, and workload subnet. Its example ranges are:

| Purpose | CIDR |
| --- | --- |
| VPC private pool | `10.80.0.0/16` |
| Gateway subnet, created later by VPNGW | `10.80.0.0/24` |
| Gemma workload subnet | `10.80.1.0/24` |
| Remote LAN | `192.168.0.0/24` |

Choose ranges that do not overlap your LAN or other connected networks. If you change them, update `sites` in `network/main.tf` and the gateway configuration together. Replace the example project ID:

```sh
export TF_VAR_project_id=YOUR_PROJECT_ID
terraform -chdir=vpngw/network init
terraform -chdir=vpngw/network validate
terraform -chdir=vpngw/network plan -out=network.tfplan
terraform -chdir=vpngw/network apply network.tfplan
terraform -chdir=vpngw/network output -json sites

export VPNGW_NETWORK_ID="$(terraform -chdir=vpngw/network output -json sites | jq -r '.gateway.network_id')"
```

## 3. Prepare the gateway configuration and public IP

Copy the [scrubbed example](vpn.config.yaml.example) to a private working configuration. The command preserves an existing file:

```sh
cp -n vpngw/vpn.config.yaml.example vpngw/vpn.config.yaml
chmod 600 vpngw/vpn.config.yaml
```

Edit `vpngw/vpn.config.yaml` before continuing. Replace `tenant-REPLACE`, `project-REPLACE`, the documentation-only peer IP `203.0.113.1`, and `/absolute/path/to/id_ed25519` key paths with your values. Set `project_id` to the same project used by the network Terraform deployment, and confirm the region (`uk-south1` in the example). Set the remote LAN prefixes in both `connections[].remote_prefixes` and `connections[].bgp.remote_prefixes`. Set `gateway.local_prefixes` to the workload CIDR. Keep the gateway and workload in the same VPC.

The example reads `network_id` from `VPNGW_NETWORK_ID` and the PSK from `VPN_PSK`. Load the shared secret into `VPN_PSK` using your secret manager or hidden terminal input, then export it in this shell. Use the PSK agreed with the peer network administrator. Keep these variables available for subsequent VPNGW commands. The private `*.config.yaml` file is ignored by Git.

```sh
vpngw/.venv/bin/nebius-vpngw validate-config vpngw/vpn.config.yaml
vpngw/.venv/bin/nebius-vpngw prep-network \
  --local-config-file vpngw/vpn.config.yaml
```

`prep-network` prepares the dedicated gateway subnet and default-egress route table, reserves or reuses public IP allocations, and writes `gateway_group.external_ips` into the working YAML. Share that public IP with the peer network administrator.

## 4. Complete the Nebius tunnel settings

Obtain the peer's public IP, remote ASN, protected LAN prefixes, and agreed tunnel settings from the peer network administrator. In `vpngw/vpn.config.yaml`, confirm:

- `gateway.local_asn` identifies the Nebius gateway, with `65010` in the example.
- `connections[].bgp.remote_asn` identifies the peer.
- `remote_public_ip`, `psk`, and the crypto proposals match the agreed connection settings.
- `inner_cidr`, `inner_local_ip`, and `inner_remote_ip` define the tunnel addressing. The example uses `169.254.10.0/30`, with `.1` on Nebius and `.2` on the peer.
- `gateway.local_prefixes` contains the Nebius workload subnet, and the connection's remote prefixes contain only the protected remote LANs.

Check that the tunnel's APIPA range does not conflict with another tunnel. Revalidate the completed configuration before deploying:

```sh
vpngw/.venv/bin/nebius-vpngw validate-config vpngw/vpn.config.yaml
```

## 5. Deploy the gateway and publish workload routes

Preview the deployment, review the proposed resources, then apply:

```sh
vpngw/.venv/bin/nebius-vpngw apply \
  --local-config-file vpngw/vpn.config.yaml --dry-run
vpngw/.venv/bin/nebius-vpngw apply \
  --local-config-file vpngw/vpn.config.yaml
vpngw/.venv/bin/nebius-vpngw add-routes-local \
  --local-config-file vpngw/vpn.config.yaml
vpngw/.venv/bin/nebius-vpngw list-routes-local \
  --local-config-file vpngw/vpn.config.yaml
vpngw/.venv/bin/nebius-vpngw status \
  --local-config-file vpngw/vpn.config.yaml
```

For this single gateway, `add-routes-local` is required even with BGP. It installs remote LAN routes in the Nebius workload subnet's route table through the gateway's private IP. Rerun it when remote prefixes change. Review any route-table conflict before using the upstream `--swap-route-table` option. Existing gateway updates may interrupt traffic and require the CLI's displayed disruption approval.

## 6. Place Gemma in the VPN-connected subnet

Export the network override and pass it after your regular infrastructure settings:

```sh
mkdir -p rendered
terraform -chdir=vpngw/network output -raw gemma_tfvars > rendered/gemma-network.tfvars
terraform -chdir=infra init
terraform -chdir=infra plan \
  -var-file=../rendered/gemma-network.tfvars -out=gemma-vpc.tfplan
terraform -chdir=infra apply gemma-vpc.tfplan
```

Prepare `infra/terraform.tfvars` as described in the main README first. Inspect the plan carefully if a GPU VM already exists, since changing its network may replace it or reconfigure its NIC. Continue using this override on future plans and applies, or copy its values into `infra/terraform.tfvars`.

Complete application deployment in the main README. From a machine on the protected LAN, verify the private HTTP endpoint:

```sh
export LLM_BASE_URL="http://$(terraform -chdir=infra output -raw private_ip | cut -d/ -f1)"
curl --fail --show-error --silent --connect-timeout 5 --max-time 15 \
  "${LLM_BASE_URL}/health"
```

Confirm IPsec and BGP status and the Nebius return route to your LAN. The peer must also route traffic to the workload subnet. A healthy gateway service alone does not prove application connectivity. Allow TCP 80 for this HTTP check, or use your certificate hostname and TCP 443 for HTTPS.

Stopping or destroying `infra/` does not remove the VPN gateway or its network. Preserve the VPN configuration and Terraform state, and remove gateway resources before destroying the VPC when cleaning up.
