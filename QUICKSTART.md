# Quick Start Guide

Get your DevContainer-integrated VS Code Server system running in minutes!

## Prerequisites Check

```bash
# Check MicroK8s
microk8s version || echo "Please install MicroK8s: sudo snap install microk8s --classic"

# Check Docker
docker --version || echo "Please install Docker"

# Check Python
python3 --version || echo "Please install Python 3.8+"
```

## 1. Initial Setup (5 minutes)

```bash
# Add your user to microk8s group (logout/login required after)
sudo usermod -a -G microk8s $USER
sudo chown -f -R $USER ~/.kube
newgrp microk8s

# Clone and enter directory
git clone <repository-url>
cd vscode-devcontainer-system
```

## 2. Deploy the System (10 minutes)

```bash
# Make scripts executable
chmod +x deploy-microk8s.sh generate-tls-cert-microk8s.sh create-sample-workspace.sh

# Deploy everything
./deploy-microk8s.sh
```

## 3. Create Your First Instance

### Option A: Simple Ubuntu Instance (1 minute)
```bash
python client-devcontainer.py create-simple --user-id myuser
```

### Option B: Python Development Environment (3 minutes)
```bash
# Create Python sample workspace
chmod +x create-python-sample.sh
./create-python-sample.sh

# Deploy it
python client-devcontainer.py create-workspace \
  --user-id myuser \
  --workspace-dir ./samples/python
```

### Option C: Full Workspace with DevContainer (5 minutes)
```bash
# Create sample workspace
./create-sample-workspace.sh

# Deploy it
python client-devcontainer.py create-workspace \
  --user-id myuser \
  --workspace-dir ./sample-workspace
```

## 4. Access Your Instance

1. Get the instance details:
```bash
python client-devcontainer.py get --instance-id <your-instance-id>
```

2. Copy the Access URL and open in your browser
3. Use the provided access token when prompted

## 5. Monitor Build Progress (for DevContainer instances)

```bash
# Watch build logs
python client-devcontainer.py build-logs --instance-id <your-instance-id>
```

## Common Commands

```bash
# List all pods
microk8s kubectl get pods -n vscode-system

# Check API logs
microk8s kubectl logs -n vscode-system deployment/vscode-devcontainer-manager

# Delete an instance
python client-devcontainer.py delete --instance-id <your-instance-id>

# Check storage usage
microk8s kubectl get pvc -n vscode-system
```

## Tips

1. **First Run**: The first DevContainer build may take longer as base images are downloaded
2. **Browser**: Use Chrome or Edge for best VS Code Server compatibility
3. **Storage**: Shared storage at `/shared` persists across instances
4. **Extensions**: Extensions installed in one instance don't affect others

## Troubleshooting

If VS Code Server doesn't load:
1. Wait 30-60 seconds for the instance to fully start
2. Check instance status: `microk8s kubectl get pod -n vscode-system <instance-id>-<hash>`
3. Verify ingress is working: `curl -k https://vscode.local/api/`

## Next Steps

- Read the full [README.md](README.md) for detailed documentation
- Explore different devcontainer configurations
- Create custom base images for your team
- Set up persistent development environments