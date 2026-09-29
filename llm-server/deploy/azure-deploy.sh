#!/usr/bin/env bash
# Deploy the LLM server to Azure.
# Runs in Azure Cloud Shell (recommended: nothing to install, fast network)
# or any bash with the Azure CLI (Git Bash, WSL, Linux, macOS).
#
#   ./deploy/azure-deploy.sh vm            # (default) CPU VM, fastest; image is built ON the VM
#   ./deploy/azure-deploy.sh containerapp  # Azure Container Apps, HTTPS URL, 4 vCPU / 8 GiB max
#   ./deploy/azure-deploy.sh status        # print URL, API key and health
#   ./deploy/azure-deploy.sh logs          # VM only: tail setup + container logs
#   ./deploy/azure-deploy.sh destroy       # delete EVERYTHING (the resource group)
#
# Overridable env vars: RG, LOCATION, VM_SIZE, API_KEY, IMAGE_TAG, CA_CPU, CA_MEMORY
set -euo pipefail

CMD="${1:-vm}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STATE_FILE="$SCRIPT_DIR/.azure-state.env"
# Work from deploy/ with relative paths (keeps Windows az.cmd happy under Git Bash).
cd "$SCRIPT_DIR"

# Previous run's values (resource names, API key) win over defaults so re-runs are idempotent.
# shellcheck disable=SC1090
[ -f "$STATE_FILE" ] && source "$STATE_FILE"

RG="${RG:-rg-bup-llm}"
LOCATION="${LOCATION:-centralindia}"
VM_SIZE="${VM_SIZE:-Standard_F8s_v2}"
VM_NAME="${VM_NAME:-vm-bup-llm}"
SUFFIX="${SUFFIX:-$(openssl rand -hex 3)}"
DNS_LABEL="${DNS_LABEL:-bupllm-$SUFFIX}"
ACR_NAME="${ACR_NAME:-bupllm$SUFFIX}"
CA_ENV="${CA_ENV:-cae-bup-llm}"
CA_APP="${CA_APP:-ca-bup-llm}"
CA_CPU="${CA_CPU:-4}"
CA_MEMORY="${CA_MEMORY:-8Gi}"
IMAGE_TAG="${IMAGE_TAG:-v1}"
API_KEY="${API_KEY:-$(openssl rand -hex 24)}"
URL="${URL:-}"
TARGET="${TARGET:-}"

log()  { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m!!  %s\033[0m\n' "$*"; }

save_state() {
  cat > "$STATE_FILE" <<EOF
RG='$RG'
LOCATION='$LOCATION'
SUFFIX='$SUFFIX'
VM_NAME='$VM_NAME'
DNS_LABEL='$DNS_LABEL'
ACR_NAME='$ACR_NAME'
CA_ENV='$CA_ENV'
CA_APP='$CA_APP'
API_KEY='$API_KEY'
URL='$URL'
TARGET='$TARGET'
EOF
  chmod 600 "$STATE_FILE" 2>/dev/null || true
}

wait_healthy() {
  local url="$1" max_min="${2:-25}" code body
  log "Waiting for $url/health (image build + model load; up to ${max_min} min)"
  for ((i = 0; i < max_min * 6; i++)); do
    code=$(curl -s -o /tmp/llm-health.json -w '%{http_code}' --max-time 5 "$url/health" || true)
    body=$(cat /tmp/llm-health.json 2>/dev/null || true)
    if [ "$code" = "200" ]; then
      echo; echo "healthy: $body"; return 0
    fi
    printf '.'; [ $((i % 6)) -eq 5 ] && printf ' %d min (%s) %s\n' $(((i + 1) / 6)) "${code:-no answer}" "${body:0:120}"
    sleep 10
  done
  echo; warn "Not healthy after ${max_min} min. Check: ./deploy/azure-deploy.sh logs"; return 1
}

print_summary() {
  cat <<EOF

================ DEPLOYED ================
URL      : $URL
API key  : $API_KEY
Chatbot  : $URL/chat (paste the API key in the header)
Test UI  : $URL/ui   (paste the API key, click Connect)
Docs     : $URL/docs
Health   : curl $URL/health
Try it   :
  curl -s $URL/api/generate -H "Authorization: Bearer $API_KEY" \\
       -H "Content-Type: application/json" \\
       -d '{"model":"gemma3-1b","prompt":"Say hello in one sentence.","max_tokens":40}'
Smoke test: python3 scripts/smoke_test.py --url $URL --api-key $API_KEY
State    : $STATE_FILE
Tear down: ./deploy/azure-deploy.sh destroy
==========================================
EOF
}

ensure_login() {
  command -v az >/dev/null || { echo "Azure CLI (az) not found. Use Azure Cloud Shell or install az."; exit 1; }
  az account show -o none 2>/dev/null || az login -o none
  log "Subscription: $(az account show --query '[name, id]' -o tsv | paste -sd' ')"
}

deploy_vm() {
  TARGET=vm
  for ns in Microsoft.Compute Microsoft.Network; do az provider register -n "$ns" --wait -o none; done
  az group create -n "$RG" -l "$LOCATION" -o none
  save_state

  log "Packaging source (built on the VM, so models download inside Azure)"
  local bundle cloud_init="cloud-init.generated.yaml" rerun="vm-rerun.generated.sh" size
  bundle=$(tar -C .. --exclude='__pycache__' -czf - \
           Dockerfile .dockerignore requirements.txt models.toml app scripts | base64 | tr -d '\n')
  cat > "$cloud_init" <<EOF
#cloud-config
package_update: true
packages:
  - docker.io
write_files:
  - path: /opt/llm-server/bundle.tgz
    encoding: b64
    permissions: '0644'
    content: $bundle
  - path: /opt/llm-server/run.sh
    permissions: '0755'
    content: |
      #!/bin/bash
      set -euxo pipefail
      cd /opt/llm-server
      rm -rf src && mkdir -p src && tar -xzf bundle.tgz -C src
      systemctl enable --now docker
      docker build -t llm-server:latest src
      docker rm -f llm-server 2>/dev/null || true
      docker run -d --name llm-server --restart unless-stopped \\
        -p 80:8000 -e API_KEY='$API_KEY' llm-server:latest
runcmd:
  - [bash, -c, "/opt/llm-server/run.sh > /var/log/llm-server-setup.log 2>&1"]
EOF
  size=$(wc -c < "$cloud_init")
  echo "cloud-init size: $size bytes (Azure limit: 65535)"
  [ "$size" -lt 65535 ] || { warn "cloud-init too large; remove files from the bundle"; exit 1; }

  if az vm show -g "$RG" -n "$VM_NAME" -o none 2>/dev/null; then
    log "VM exists -> re-running setup with the current source"
    cat > "$rerun" <<EOF
#!/bin/bash
echo '$bundle' | base64 -d > /opt/llm-server/bundle.tgz
sed -i "s/API_KEY='[^']*'/API_KEY='$API_KEY'/" /opt/llm-server/run.sh
nohup bash -c '/opt/llm-server/run.sh > /var/log/llm-server-setup.log 2>&1' >/dev/null 2>&1 &
EOF
    az vm run-command invoke -g "$RG" -n "$VM_NAME" --command-id RunShellScript -o none --scripts @"$rerun"
  else
    log "Creating VM $VM_NAME ($VM_SIZE, $LOCATION)"
    az vm create -g "$RG" -n "$VM_NAME" \
      --image Ubuntu2404 --size "$VM_SIZE" \
      --admin-username azureuser --generate-ssh-keys \
      --os-disk-size-gb 64 --public-ip-sku Standard \
      --public-ip-address-dns-name "$DNS_LABEL" \
      --custom-data "$cloud_init" -o none \
    || { warn "VM create failed. If it is a quota/SKU error, retry with e.g. VM_SIZE=Standard_D4s_v5 or LOCATION=southeastasia"; exit 1; }
    az vm open-port -g "$RG" -n "$VM_NAME" --port 80 --priority 1010 -o none
  fi

  URL="http://$(az vm show -d -g "$RG" -n "$VM_NAME" --query fqdns -o tsv)"
  [ "$URL" = "http://" ] && URL="http://$(az vm show -d -g "$RG" -n "$VM_NAME" --query publicIps -o tsv)"
  save_state
  wait_healthy "$URL" 30 || true
  print_summary
}

deploy_containerapp() {
  TARGET=containerapp
  az extension add --name containerapp --upgrade -y -o none 2>/dev/null || true
  for ns in Microsoft.ContainerRegistry Microsoft.App Microsoft.OperationalInsights; do
    az provider register -n "$ns" --wait -o none
  done
  az group create -n "$RG" -l "$LOCATION" -o none
  save_state

  if ! az acr show -n "$ACR_NAME" -o none 2>/dev/null; then
    log "Creating container registry $ACR_NAME"
    az acr create -n "$ACR_NAME" -g "$RG" -l "$LOCATION" --sku Basic --admin-enabled true -o none
  fi
  local image="$ACR_NAME.azurecr.io/llm-server:$IMAGE_TAG"
  if [ "${SKIP_BUILD:-0}" != "1" ]; then
    log "Building $image in Azure (ACR Tasks; models download inside Azure, ~5-10 min)"
    az acr build -r "$ACR_NAME" -t "llm-server:$IMAGE_TAG" --platform linux/amd64 .. \
      || { warn "ACR build failed (some free/student subscriptions block ACR Tasks). Use: ./deploy/azure-deploy.sh vm"; exit 1; }
  fi

  local acr_user acr_pass
  acr_user=$(az acr credential show -n "$ACR_NAME" --query username -o tsv)
  acr_pass=$(az acr credential show -n "$ACR_NAME" --query 'passwords[0].value' -o tsv)

  if ! az containerapp env show -n "$CA_ENV" -g "$RG" -o none 2>/dev/null; then
    log "Creating Container Apps environment $CA_ENV"
    az containerapp env create -n "$CA_ENV" -g "$RG" -l "$LOCATION" -o none
  fi

  if az containerapp show -n "$CA_APP" -g "$RG" -o none 2>/dev/null; then
    log "Updating container app $CA_APP"
    az containerapp secret set -n "$CA_APP" -g "$RG" --secrets "api-key=$API_KEY" -o none
    az containerapp update -n "$CA_APP" -g "$RG" --image "$image" -o none
  else
    log "Creating container app $CA_APP ($CA_CPU vCPU / $CA_MEMORY)"
    az containerapp create -n "$CA_APP" -g "$RG" --environment "$CA_ENV" \
      --image "$image" \
      --registry-server "$ACR_NAME.azurecr.io" --registry-username "$acr_user" --registry-password "$acr_pass" \
      --ingress external --target-port 8000 \
      --cpu "$CA_CPU" --memory "$CA_MEMORY" \
      --min-replicas 1 --max-replicas 1 \
      --secrets "api-key=$API_KEY" --env-vars "API_KEY=secretref:api-key" \
      -o none
  fi

  URL="https://$(az containerapp show -n "$CA_APP" -g "$RG" --query properties.configuration.ingress.fqdn -o tsv)"
  save_state
  wait_healthy "$URL" 20 || true
  print_summary
}

case "$CMD" in
  vm)           ensure_login; deploy_vm ;;
  containerapp) ensure_login; deploy_containerapp ;;
  status)
    [ -n "$URL" ] || { echo "Nothing deployed yet (no $STATE_FILE)."; exit 1; }
    echo "target=$TARGET url=$URL api_key=$API_KEY"
    curl -s --max-time 5 "$URL/health"; echo ;;
  logs)
    ensure_login
    az vm run-command invoke -g "$RG" -n "$VM_NAME" --command-id RunShellScript \
      --scripts "tail -n 40 /var/log/llm-server-setup.log; echo ---- container ----; docker logs --tail 60 llm-server 2>&1" \
      --query 'value[0].message' -o tsv ;;
  destroy)
    ensure_login
    log "Deleting resource group $RG (everything in it)"
    az group delete -n "$RG" --yes --no-wait
    rm -f "$STATE_FILE" cloud-init.generated.yaml vm-rerun.generated.sh
    echo "Deletion started (takes a few minutes)." ;;
  *) echo "usage: $0 [vm|containerapp|status|logs|destroy]"; exit 1 ;;
esac
