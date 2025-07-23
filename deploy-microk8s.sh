#!/bin/bash

set -e

echo "=== DevContainer-Integrated VS Code Server On-Demand System Deployment ==="
echo "This script will deploy the system to MicroK8s with devcontainer support."

# Check if MicroK8s is installed
if ! command -v microk8s &> /dev/null; then
    echo "MicroK8s is not installed. Please install it first:"
    echo "sudo snap install microk8s --classic"
    exit 1
fi

# Check MicroK8s status
if ! microk8s status | grep -q "microk8s is running"; then
    echo "Starting MicroK8s..."
    microk8s start
    sleep 10
fi

# Enable required addons
echo "Enabling MicroK8s addons..."
microk8s enable dns storage ingress registry

# Wait for addons to be ready
echo "Waiting for addons to be ready..."
microk8s kubectl wait --for=condition=available --timeout=300s deployment/hostpath-provisioner -n kube-system
microk8s kubectl wait --for=condition=available --timeout=300s deployment/registry -n container-registry

# Configure kubectl alias
echo "Configuring kubectl alias..."
alias kubectl='microk8s kubectl'

# Generate TLS certificates
echo "Generating TLS certificates..."
chmod +x generate-tls-cert-microk8s.sh
./generate-tls-cert-microk8s.sh

# Get MicroK8s IP
MICROK8S_IP=$(microk8s kubectl get nodes -o jsonpath='{.items[0].status.addresses[?(@.type=="InternalIP")].address}')
echo "MicroK8s IP: $MICROK8S_IP"

# Add hosts entry
echo "Adding entries to hosts file..."
echo "You may be prompted for your password."

if grep -q "vscode.local" /etc/hosts; then
    echo "Host entry already exists. Updating..."
    sudo sed -i "s/.*vscode.local.*/$MICROK8S_IP vscode.local/" /etc/hosts
else
    echo "$MICROK8S_IP vscode.local" | sudo tee -a /etc/hosts
fi

# Build FastAPI app image
echo "Building FastAPI app image with devcontainer support..."
cd devcontainer-api
docker build -t localhost:32000/vscode-devcontainer-manager:latest -f Dockerfile .

# Push to MicroK8s registry
echo "Pushing image to MicroK8s registry..."
docker push localhost:32000/vscode-devcontainer-manager:latest

cd ..

# Create namespace
echo "Creating namespace..."
microk8s kubectl create namespace vscode-system || true

# Deploy FastAPI application
echo "Deploying FastAPI application with devcontainer support..."
microk8s kubectl apply -f devcontainer-api-k8s.yaml -n vscode-system

# Wait for deployment to be ready
echo "Waiting for deployment to be ready..."
microk8s kubectl rollout status deployment/vscode-devcontainer-manager -n vscode-system

# Display access information
echo "=== Deployment Complete ==="
echo "DevContainer Management API is available at: https://vscode.local/api"
echo "VS Code Server instances will be available at: https://vscode.local/instances/<instance-id>"
echo ""
echo "Example usage:"
echo "1. Create instance with devcontainer.json:"
echo "   curl -k -X POST https://vscode.local/api/instances/devcontainer \\"
echo "     -F 'user_id=user1' \\"
echo "     -F 'devcontainer_json=@path/to/devcontainer.json'"
echo ""
echo "2. Create instance with workspace folder:"
echo "   curl -k -X POST https://vscode.local/api/instances/workspace \\"
echo "     -F 'user_id=user1' \\"
echo "     -F 'workspace=@workspace.tar.gz'"
echo ""
echo "3. Use the client script:"
echo "   python client-devcontainer.py create-simple --user-id user1 --base-image ubuntu:22.04"