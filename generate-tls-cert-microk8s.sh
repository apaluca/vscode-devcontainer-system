#!/bin/bash

# Set variables
CERT_NAME="vscode-tls"
DOMAIN="vscode.local"
NAMESPACE="vscode-system"

# Create directory for certificates
mkdir -p ./certs

# Generate a private key and self-signed certificate
openssl req -x509 -nodes -days 365 -newkey rsa:2048 \
  -keyout ./certs/$CERT_NAME.key \
  -out ./certs/$CERT_NAME.crt \
  -subj "/CN=$DOMAIN/O=VSCode DevContainer Server/C=US" \
  -addext "subjectAltName = DNS:$DOMAIN,DNS:*.vscode.local"

# Create namespace if it doesn't exist
microk8s kubectl create namespace $NAMESPACE || true

# Create Kubernetes secret from the generated certificates
microk8s kubectl create secret tls vscode-server-tls \
  --key=./certs/$CERT_NAME.key \
  --cert=./certs/$CERT_NAME.crt \
  --namespace=$NAMESPACE \
  --dry-run=client -o yaml | microk8s kubectl apply -f -

echo "TLS certificate created and stored in Kubernetes secret 'vscode-server-tls' in namespace '$NAMESPACE'"