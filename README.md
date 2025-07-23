# DevContainer-Integrated VS Code Server On-Demand System

A Kubernetes-based system that provides on-demand VS Code Server instances with integrated devcontainer support, designed for MicroK8s deployment.

## Overview

This system combines the power of VS Code Server with DevContainers to provide fully-configured development environments on-demand. Key features include:

- **Dynamic VS Code Server Provisioning**: Create isolated VS Code Server instances on-demand
- **DevContainer Integration**: Build and deploy development environments using devcontainer.json specifications
- **Dual Storage Architecture**: Instance-specific workspaces and shared user storage
- **MicroK8s Optimized**: Designed for MicroK8s with built-in registry support
- **Runtime VS Code Installation**: Installs VS Code Server at container startup for flexibility

## Architecture

### Components

1. **FastAPI Management Service**: REST API for managing VS Code Server instances
2. **DevContainer CLI Integration**: Uses `@devcontainers/cli` for building development containers
3. **Docker-in-Docker**: Supports building container images within the cluster
4. **Kubernetes Resources**: Deployments, Services, Ingress, and PVCs for each instance
5. **Dual Storage System**:
   - `/workspace`: Instance-specific project data
   - `/shared`: User data persisting across all instances

### Workflow

1. User uploads a devcontainer.json or workspace folder
2. System builds a custom container image using devcontainer CLI
3. Image is pushed to MicroK8s registry
4. VS Code Server instance is deployed with the custom image
5. Instance is accessible via HTTPS with authentication token

## Prerequisites

- Ubuntu 20.04+ or compatible Linux distribution
- MicroK8s installed (`sudo snap install microk8s --classic`)
- Docker installed
- Python 3.8+ (for client script)
- Node.js 20+ (included in the API container)

## Installation

### 1. Clone the Repository

```bash
git clone https://github.com/apaluca/vscode-devcontainer-system.git
cd vscode-devcontainer-system
```

### 2. Deploy to MicroK8s

```bash
./deploy-microk8s.sh
```

This script will:
- Enable required MicroK8s addons (dns, storage, ingress, registry)
- Generate TLS certificates
- Build and push the management API image
- Deploy all Kubernetes resources
- Configure the hosts file

### 3. Verify Deployment

```bash
# Check if all pods are running
microk8s kubectl get pods -n vscode-system

# Check ingress
microk8s kubectl get ingress -n vscode-system
```

## Usage

### Using the Client Script

The `client-devcontainer.py` script provides an easy way to interact with the API.

#### Create a Simple Instance (No DevContainer)

```bash
python client-devcontainer.py create-simple \
  --user-id user1 \
  --base-image ubuntu:22.04
```

#### Create Instance with DevContainer.json

First, create a sample workspace or use the provided examples:

```bash
# Option 1: Use simple devcontainer.json
python client-devcontainer.py create-devcontainer \
  --user-id user1 \
  --devcontainer-json ./samples/simple-devcontainer.json

# Option 2: Create and use Python workspace
python client-devcontainer.py create-workspace \
  --user-id user1 \
  --workspace-dir ./samples/python
```

#### Get Instance Details

```bash
python client-devcontainer.py get --instance-id user1-abc123
```

#### View Build Logs

```bash
python client-devcontainer.py build-logs --instance-id user1-abc123
```

#### Delete Instance

```bash
python client-devcontainer.py delete --instance-id user1-abc123
```

### Using the REST API Directly

#### Create Instance with DevContainer

```bash
curl -k -X POST https://vscode.local/api/instances/devcontainer \
  -F 'user_id=user1' \
  -F 'devcontainer_json=@path/to/devcontainer.json'
```

#### Create Instance with Workspace

```bash
# Create tar.gz of workspace
tar -czf workspace.tar.gz -C /path/to/workspace .

# Upload
curl -k -X POST https://vscode.local/api/instances/workspace \
  -F 'user_id=user1' \
  -F 'workspace=@workspace.tar.gz'
```

## DevContainer Support

### DevContainer Best Practices

This system follows the patterns from the official devcontainer CLI examples:

1. **Use Plain Base Images**: Start with standard images like `ubuntu:22.04` rather than pre-built devcontainer images
2. **Build Custom Dockerfiles**: Create your own Dockerfiles with necessary dependencies
3. **Leverage DevContainer Features**: Use features to add development tools on top of your base image
4. **Runtime VS Code Installation**: VS Code Server is installed when the container starts, not baked into the image

### Supported Features

- **Base Images**: Any Docker image as base
- **DevContainer Features**: Full support for features from the devcontainers/features repository
- **Customizations**: VS Code extensions and settings
- **Build Configuration**: Dockerfile-based builds
- **Post-Create Commands**: Automated setup commands

### Example DevContainer Configurations

#### Python Development
```json
{
  "name": "Python Dev",
  "build": {
    "dockerfile": "Dockerfile"
  },
  "features": {
    "ghcr.io/devcontainers/features/python:1": {
      "version": "3.12",
      "installJupyterlab": true
    }
  },
  "customizations": {
    "vscode": {
      "extensions": ["ms-python.python", "ms-python.vscode-pylance"]
    }
  }
}
```

#### Node.js with Custom Dockerfile
```json
{
  "name": "Node.js Custom",
  "build": {
    "dockerfile": "Dockerfile",
    "context": ".."
  },
  "features": {
    "ghcr.io/devcontainers/features/node:1": {"version": "18"}
  }
}
```

## Storage Architecture

### Instance-Specific Storage (`/workspace`)
- Unique to each VS Code Server instance
- Contains project files and code
- Deleted when instance is removed
- Default size: 2Gi (configurable)

### Shared User Storage (`/shared`)
- Persists across all instances for a user
- Ideal for:
  - Personal configuration files
  - Shared libraries or tools
  - Data that needs to persist
- Default size: 5Gi (configurable)
- Created once per user, reused across instances

## API Reference

### Endpoints

- `POST /instances/simple` - Create a simple VS Code Server instance
- `POST /instances/devcontainer` - Create instance with devcontainer.json
- `POST /instances/workspace` - Create instance with workspace folder
- `GET /instances/{instance_id}` - Get instance details
- `GET /instances/{instance_id}/build-logs` - Get build logs
- `DELETE /instances/{instance_id}` - Delete instance
- `GET /health` - Health check

### Instance Response Format

```json
{
  "instance_id": "user1-abc123",
  "url": "https://vscode.local/instances/user1-abc123?tkn=...",
  "access_token": "uuid-token",
  "status": "Running",
  "base_image": "ubuntu:22.04",
  "devcontainer_image": "localhost:32000/vscode-devcontainer-user1-abc123:latest",
  "build_logs_url": "https://vscode.local/api/instances/user1-abc123/build-logs"
}
```

## Configuration

### Environment Variables

Set in `devcontainer-api-k8s.yaml`:

- `KUBERNETES_NAMESPACE`: Namespace for VS Code instances (default: `vscode-system`)
- `BASE_DOMAIN`: Base domain for access (default: `vscode.local`)
- `REGISTRY`: Container registry URL (default: `localhost:32000`)

### Resource Limits

Default limits (configurable per instance):

- Memory: 512Mi - 2Gi
- CPU: 200m - 1000m
- Workspace Storage: 2Gi
- Shared Storage: 5Gi

## Troubleshooting

### Check System Status

```bash
# Check all pods
microk8s kubectl get pods -n vscode-system -o wide

# Check logs of management API
microk8s kubectl logs -n vscode-system deployment/vscode-devcontainer-manager

# Check Docker-in-Docker status
microk8s kubectl logs -n vscode-system daemonset/docker-dind
```

### Common Issues

1. **Build Failures**: Check Docker-in-Docker logs and ensure registry is accessible
2. **Instance Not Starting**: Verify resource limits and check pod events
3. **Cannot Access Instance**: Ensure ingress is configured and TLS certificates are valid
4. **Storage Issues**: Check PVC status and available storage

### Debug Commands

```bash
# Get detailed instance information
microk8s kubectl describe deployment -n vscode-system <instance-id>

# Check PVC status
microk8s kubectl get pvc -n vscode-system

# View build logs
python client-devcontainer.py build-logs --instance-id <instance-id>
```

## Security Considerations

- Each instance has a unique access token
- TLS encryption for all communications
- Instances run with non-root users (after initial setup)
- Network isolation between instances
- Resource limits prevent resource exhaustion

## Contributing

1. Fork the repository
2. Create a feature branch
3. Make your changes
4. Test with MicroK8s
5. Submit a pull request

## License

This project is licensed under the MIT License.

## Acknowledgments

- Built on the DevContainer CLI from Microsoft
- Inspired by cloud-based development environments
- Designed for educational and development purposes