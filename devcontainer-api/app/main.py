from fastapi import FastAPI, HTTPException, status, UploadFile, File, Form, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from kubernetes import client, config
from pydantic import BaseModel, validator
import uuid
import os
import logging
import re
import json
import tarfile
import tempfile
import shutil
import subprocess
import asyncio
from typing import Optional, List, Dict, Any
from datetime import datetime
import hashlib
import time

# Configure logging
logging.basicConfig(level=logging.INFO, 
                    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Initialize FastAPI app
app = FastAPI(
    title="VS Code DevContainer Manager",
    description="API for on-demand deployment of VS Code Server instances with DevContainer support",
    version="2.0.0"
)

# Add CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Initialize the Kubernetes client
try:
    config.load_incluster_config()
    logger.info("Loaded in-cluster Kubernetes configuration")
    IN_CLUSTER = True
except config.ConfigException:
    config.load_kube_config()
    logger.info("Loaded kubeconfig Kubernetes configuration")
    IN_CLUSTER = False

# Create API clients
core_v1_api = client.CoreV1Api()
apps_v1_api = client.AppsV1Api()
networking_v1_api = client.NetworkingV1Api()

# Configuration
NAMESPACE = os.environ.get("KUBERNETES_NAMESPACE", "vscode-system")
BASE_NAME = "vscode-server"
BASE_DOMAIN = os.environ.get("BASE_DOMAIN", "vscode.local")
TLS_SECRET_NAME = "vscode-server-tls"
REGISTRY = os.environ.get("REGISTRY", "localhost:32000")
DEFAULT_STORAGE_SIZE = "2Gi"
DEFAULT_SHARED_STORAGE_SIZE = "5Gi"
DEFAULT_MEMORY_REQUEST = "512Mi"
DEFAULT_MEMORY_LIMIT = "2Gi"
DEFAULT_CPU_REQUEST = "200m"
DEFAULT_CPU_LIMIT = "1000m"
DEFAULT_BASE_IMAGE = "ubuntu:22.04"  # Following devcontainer CLI best practices, use plain base images
DEVCONTAINER_BUILD_PATH = "/tmp/devcontainer-builds"

# When running in cluster, use the registry service name
PUSH_REGISTRY = REGISTRY  # Registry URL for pushing images
PULL_REGISTRY = REGISTRY  # Registry URL for pulling images

if IN_CLUSTER:
    # For MicroK8s, the registry is accessible via the node IP and port 32000
    # We need to get the node IP for pushing
    try:
        nodes = core_v1_api.list_node()
        if nodes.items:
            node_ip = None
            for address in nodes.items[0].status.addresses:
                if address.type == "InternalIP":
                    node_ip = address.address
                    break
            if node_ip:
                PUSH_REGISTRY = f"{node_ip}:32000"
                PULL_REGISTRY = "localhost:32000"  # Pods pull from localhost
                logger.info(f"Using push registry at {PUSH_REGISTRY}, pull registry at {PULL_REGISTRY}")
    except Exception as e:
        logger.error(f"Failed to get node IP: {e}")

# Path configuration
API_PATH_PREFIX = "/api"
INSTANCES_PATH_PREFIX = "/instances"

# Ensure build directory exists
os.makedirs(DEVCONTAINER_BUILD_PATH, exist_ok=True)

# Data Models
class VSCodeServerRequest(BaseModel):
    """Request model for creating a VS Code Server instance"""
    user_id: str
    storage_size: Optional[str] = DEFAULT_STORAGE_SIZE
    shared_storage_size: Optional[str] = DEFAULT_SHARED_STORAGE_SIZE
    memory_request: Optional[str] = DEFAULT_MEMORY_REQUEST
    memory_limit: Optional[str] = DEFAULT_MEMORY_LIMIT
    cpu_request: Optional[str] = DEFAULT_CPU_REQUEST
    cpu_limit: Optional[str] = DEFAULT_CPU_LIMIT
    base_image: Optional[str] = DEFAULT_BASE_IMAGE
    vscode_version: Optional[str] = "1.97.2"
    
    @validator('base_image')
    def validate_base_image(cls, v):
        if not re.match(r'^[a-zA-Z0-9][-a-zA-Z0-9_./:]*$', v):
            raise ValueError("Invalid base image format")
        return v

class DevContainerConfig(BaseModel):
    """DevContainer configuration model"""
    name: Optional[str] = None
    image: Optional[str] = None
    build: Optional[Dict[str, Any]] = None
    features: Optional[Dict[str, Any]] = None
    customizations: Optional[Dict[str, Any]] = None
    forwardPorts: Optional[List[int]] = None
    postCreateCommand: Optional[str] = None
    remoteUser: Optional[str] = None
    mounts: Optional[List[str]] = None
    
class VSCodeServerResponse(BaseModel):
    """Response model for VS Code Server instance details"""
    instance_id: str
    url: str
    access_token: str
    status: str
    base_image: str
    devcontainer_image: Optional[str] = None
    build_logs_url: Optional[str] = None

class VSCodeServerList(BaseModel):
    """Response model for listing VS Code Server instances"""
    instances: List[VSCodeServerResponse]

class BuildStatus(BaseModel):
    """Build status response"""
    instance_id: str
    status: str
    logs: Optional[str] = None
    
# Helper Functions
def generate_instance_id(user_id: str) -> str:
    """Generate a unique instance ID based on user ID and random suffix"""
    random_suffix = uuid.uuid4().hex[:8]
    return f"{user_id}-{random_suffix}"

def generate_access_token() -> str:
    """Generate a UUID-like access token for VS Code Server"""
    # VS Code Server doesn't accept hyphens in tokens, only alphanumeric and underscores
    return uuid.uuid4().hex

def generate_instance_path(instance_id: str) -> str:
    """Generate a path for the VS Code Server instance"""
    return f"{INSTANCES_PATH_PREFIX}/{instance_id}"

def ensure_shared_storage_pvc(user_id: str, storage_size: str) -> None:
    """Create a PersistentVolumeClaim for the user's shared storage if it doesn't exist"""
    pvc_name = f"{user_id}-shared"
    
    try:
        core_v1_api.read_namespaced_persistent_volume_claim(
            name=pvc_name,
            namespace=NAMESPACE
        )
        logger.info(f"Using existing shared storage PVC for user {user_id}")
        return
    except client.exceptions.ApiException as e:
        if e.status != 404:
            logger.error(f"Error checking shared storage PVC: {e}")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to check shared storage PVC: {str(e)}"
            )
    
    # Create the PVC if it doesn't exist
    pvc = client.V1PersistentVolumeClaim(
        metadata=client.V1ObjectMeta(
            name=pvc_name,
            labels={"app": BASE_NAME, "user": user_id, "type": "shared"}
        ),
        spec=client.V1PersistentVolumeClaimSpec(
            access_modes=["ReadWriteOnce"],
            resources=client.V1ResourceRequirements(
                requests={"storage": storage_size}
            )
        )
    )
    
    try:
        core_v1_api.create_namespaced_persistent_volume_claim(
            namespace=NAMESPACE,
            body=pvc
        )
        logger.info(f"Created shared storage PVC for user {user_id}")
    except client.exceptions.ApiException as e:
        logger.error(f"Error creating shared storage PVC: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to create shared storage PVC: {str(e)}"
        )

async def configure_docker_for_registry():
    """Configure Docker to accept insecure registry"""
    docker_host = os.environ.get("DOCKER_HOST", "tcp://docker-dind-service:2375")
    env = {**os.environ, "DOCKER_HOST": docker_host}
    
    # Create daemon.json config
    daemon_config = {
        "insecure-registries": [REGISTRY, "localhost:32000", f"{REGISTRY.split(':')[0]}:32000"]
    }
    
    config_json = json.dumps(daemon_config)
    
    # Try to configure Docker daemon
    try:
        # First, check if we can connect to Docker
        test_cmd = await asyncio.create_subprocess_exec(
            "docker", "info",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env
        )
        stdout, stderr = await test_cmd.communicate()
        
        if test_cmd.returncode == 0:
            logger.info("Docker daemon is accessible")
            # Check if registry is already configured
            if REGISTRY in stdout.decode() or "insecure registries" in stdout.decode().lower():
                logger.info(f"Registry {REGISTRY} appears to be configured")
        else:
            logger.warning(f"Docker info failed: {stderr.decode()}")
    except Exception as e:
        logger.error(f"Error configuring Docker: {e}")

async def build_devcontainer_image(
    instance_id: str,
    workspace_path: str,
    devcontainer_config: Optional[Dict[str, Any]] = None
) -> str:
    """Build a devcontainer image using the devcontainer CLI"""
    build_dir = os.path.join(DEVCONTAINER_BUILD_PATH, instance_id)
    os.makedirs(build_dir, exist_ok=True)
    
    # Configure Docker for insecure registry
    await configure_docker_for_registry()
    
    # Test Docker connectivity first
    docker_host = os.environ.get("DOCKER_HOST", "tcp://docker-dind-service:2375")
    env = {**os.environ, "DOCKER_HOST": docker_host}
    
    try:
        docker_test = await asyncio.create_subprocess_exec(
            "docker", "version",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env
        )
        stdout, stderr = await docker_test.communicate()
        if docker_test.returncode != 0:
            logger.error(f"Docker connectivity test failed: {stderr.decode()}")
            raise Exception("Cannot connect to Docker daemon")
        logger.info("Docker daemon is accessible")
    except Exception as e:
        logger.error(f"Docker connectivity error: {e}")
        raise Exception(f"Docker daemon not accessible: {str(e)}")
    
    try:
        # If devcontainer_config is provided, write it to the workspace
        if devcontainer_config:
            devcontainer_path = os.path.join(workspace_path, ".devcontainer")
            os.makedirs(devcontainer_path, exist_ok=True)
            with open(os.path.join(devcontainer_path, "devcontainer.json"), "w") as f:
                json.dump(devcontainer_config, f, indent=2)
        
        # Generate image name
        push_image_name = f"{PUSH_REGISTRY}/vscode-devcontainer-{instance_id}:latest"
        pull_image_name = f"{PULL_REGISTRY}/vscode-devcontainer-{instance_id}:latest"
        
        # Build the devcontainer image
        build_cmd = [
            "devcontainer", "build",
            "--workspace-folder", workspace_path,
            "--image-name", push_image_name,
            "--no-cache"
        ]
        
        logger.info(f"Building devcontainer image: {' '.join(build_cmd)}")
        
        # Run the build command
        process = await asyncio.create_subprocess_exec(
            *build_cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=build_dir,
            env=env
        )
        
        # Collect build logs
        build_logs = []
        while True:
            line = await process.stdout.readline()
            if not line:
                break
            log_line = line.decode().strip()
            build_logs.append(log_line)
            logger.info(f"Build output: {log_line}")
        
        await process.wait()
        
        if process.returncode != 0:
            raise Exception(f"Build failed with return code {process.returncode}")
        
        # For MicroK8s registry, we might need to tag and push differently
        # First, let's try to push directly
        logger.info(f"Pushing image {push_image_name} to registry")
        
        # Try multiple push strategies
        push_success = False
        push_errors = []
        
        # Strategy 1: Direct push
        push_cmd = ["docker", "push", push_image_name]
        logger.info(f"Attempting direct push: {' '.join(push_cmd)}")
        push_process = await asyncio.create_subprocess_exec(
            *push_cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=env
        )
        
        push_output = []
        while True:
            line = await push_process.stdout.readline()
            if not line:
                break
            output_line = line.decode().strip()
            push_output.append(output_line)
            logger.info(f"Push output: {output_line}")
        
        await push_process.wait()
        
        if push_process.returncode == 0:
            push_success = True
            logger.info("Successfully pushed image to registry")
        else:
            error_msg = f"Direct push failed with return code {push_process.returncode}"
            push_errors.append(error_msg)
            logger.warning(error_msg)
            
            # Strategy 2: Try retagging if localhost doesn't work
            if "localhost" in push_image_name and PUSH_REGISTRY != "localhost:32000":
                # Retag the image with the actual registry address
                retag_name = push_image_name.replace("localhost:32000", PUSH_REGISTRY)
                retag_cmd = ["docker", "tag", push_image_name, retag_name]
                logger.info(f"Retagging image: {' '.join(retag_cmd)}")
                
                retag_process = await asyncio.create_subprocess_exec(
                    *retag_cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    env=env
                )
                await retag_process.wait()
                
                if retag_process.returncode == 0:
                    # Try pushing the retagged image
                    push_cmd = ["docker", "push", retag_name]
                    logger.info(f"Pushing retagged image: {' '.join(push_cmd)}")
                    
                    push_process = await asyncio.create_subprocess_exec(
                        *push_cmd,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.STDOUT,
                        env=env
                    )
                    
                    await push_process.wait()
                    
                    if push_process.returncode == 0:
                        push_success = True
                        push_image_name = retag_name
                        logger.info(f"Successfully pushed retagged image: {retag_name}")
                    else:
                        error_msg = f"Retagged push failed with return code {push_process.returncode}"
                        push_errors.append(error_msg)
                        logger.warning(error_msg)
        
        if not push_success:
            # Log all errors
            all_errors = "\n".join(push_errors)
            logger.error(f"Failed to push image after all attempts:\n{all_errors}")
            # Instead of failing, we'll use the local image
            logger.warning("Will attempt to use the locally built image")
            # Add a note to the build logs
            build_logs.append(f"WARNING: Failed to push to registry, using local image: {push_image_name}")
        
        # Store build logs
        logs_cm = client.V1ConfigMap(
            metadata=client.V1ObjectMeta(
                name=f"{instance_id}-build-logs",
                labels={"app": BASE_NAME, "instance": instance_id}
            ),
            data={
                "logs": "\n".join(build_logs + push_output),
                "timestamp": datetime.utcnow().isoformat()
            }
        )
        
        try:
            core_v1_api.create_namespaced_config_map(
                namespace=NAMESPACE,
                body=logs_cm
            )
        except client.exceptions.ApiException:
            pass  # Ignore if already exists
        
        # Return the pull image name for deployment
        return pull_image_name
        
    finally:
        # Cleanup build directory
        if os.path.exists(build_dir):
            shutil.rmtree(build_dir)

def create_configmap(instance_id: str, access_token: str, base_image: str, 
                    devcontainer_image: Optional[str], vscode_version: str,
                    devcontainer_config: Optional[Dict[str, Any]] = None) -> None:
    """Create a ConfigMap for the VS Code Server instance"""
    
    # Prepare devcontainer configuration for VS Code
    vscode_config = {
        "extensions": [],
        "settings": {}
    }
    
    if devcontainer_config:
        # Extract VS Code specific configuration
        customizations = devcontainer_config.get("customizations", {})
        vscode_customizations = customizations.get("vscode", {})
        
        # Get extensions
        extensions = vscode_customizations.get("extensions", [])
        vscode_config["extensions"] = extensions
        
        # Get settings
        settings = vscode_customizations.get("settings", {})
        vscode_config["settings"] = settings
        
        # Also include postCreateCommand if present
        if "postCreateCommand" in devcontainer_config:
            vscode_config["postCreateCommand"] = devcontainer_config["postCreateCommand"]
    
    configmap = client.V1ConfigMap(
        metadata=client.V1ObjectMeta(
            name=f"{instance_id}-config",
            labels={"app": BASE_NAME, "instance": instance_id}
        ),
        data={
            "PORT": "8000",
            "HOST": "0.0.0.0",
            "TOKEN": access_token,
            "CLI_DATA_DIR": "/home/vscode/.vscode/cli-data",
            "USER_DATA_DIR": "/home/vscode/.vscode/user-data",
            "SERVER_DATA_DIR": "/home/vscode/.vscode/server-data",
            "EXTENSIONS_DIR": "/home/vscode/.vscode/extensions",
            "BASE_IMAGE": base_image,
            "DEVCONTAINER_IMAGE": devcontainer_image or "",
            "VSCODE_VERSION": vscode_version,
            "VSCODE_CONFIG": json.dumps(vscode_config)  # Add VS Code configuration
        }
    )
    
    try:
        core_v1_api.create_namespaced_config_map(
            namespace=NAMESPACE,
            body=configmap
        )
        logger.info(f"Created ConfigMap for instance {instance_id}")
    except client.exceptions.ApiException as e:
        logger.error(f"Error creating ConfigMap: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to create ConfigMap: {str(e)}"
        )

def create_workspace_pvc(instance_id: str, storage_size: str) -> None:
    """Create a PersistentVolumeClaim for the VS Code Server instance workspace"""
    pvc = client.V1PersistentVolumeClaim(
        metadata=client.V1ObjectMeta(
            name=f"{instance_id}-workspace",
            labels={"app": BASE_NAME, "instance": instance_id, "type": "workspace"}
        ),
        spec=client.V1PersistentVolumeClaimSpec(
            access_modes=["ReadWriteOnce"],
            resources=client.V1ResourceRequirements(
                requests={"storage": storage_size}
            )
        )
    )
    
    try:
        core_v1_api.create_namespaced_persistent_volume_claim(
            namespace=NAMESPACE,
            body=pvc
        )
        logger.info(f"Created workspace PVC for instance {instance_id}")
    except client.exceptions.ApiException as e:
        logger.error(f"Error creating workspace PVC: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to create workspace PVC: {str(e)}"
        )

def create_deployment(
    instance_id: str,
    user_id: str,
    memory_request: str, 
    memory_limit: str,
    cpu_request: str,
    cpu_limit: str,
    devcontainer_image: Optional[str],
    vscode_version: str
) -> None:
    """Create a Deployment for the VS Code Server instance"""
    
    instance_path = f"{INSTANCES_PATH_PREFIX}/{instance_id}"
    shared_pvc_name = f"{user_id}-shared"
    
    # Use devcontainer image if available, otherwise use base Ubuntu
    container_image = devcontainer_image or DEFAULT_BASE_IMAGE
    
    # VS Code installation script with extension support
    install_script = f'''#!/bin/bash
set -e

echo "=== VS Code Server Setup ==="
echo "Starting installation process..."
echo "Architecture: $(uname -m)"
echo "Home directory: $HOME"

# Ensure user exists and create if not
if ! id vscode >/dev/null 2>&1; then
    echo "Creating vscode user..."
    useradd -m -s /bin/bash -u 1000 vscode 2>/dev/null || true
fi

# Install basic dependencies if not available
if command -v apt-get >/dev/null 2>&1; then
    apt-get update >/dev/null 2>&1 || true
    apt-get install -y curl wget ca-certificates git sudo jq unzip file tar gzip >/dev/null 2>&1 || true
fi

# Define locations
INSTALL_LOCATION="/home/vscode/.local/bin"
DATA_DIR="/home/vscode/.vscode-server"
VSCODE_VERSION="{vscode_version}"

# Create directories with proper ownership
mkdir -p "$INSTALL_LOCATION"
mkdir -p "$DATA_DIR/data/Machine"
mkdir -p "$DATA_DIR/extensions"
mkdir -p /home/vscode/.vscode/cli-data
mkdir -p /home/vscode/.vscode/user-data
mkdir -p /home/vscode/.vscode/server-data
mkdir -p /home/vscode/.vscode/extensions

# Check if VS Code CLI is already installed
if [ ! -e "$INSTALL_LOCATION/code" ]; then
    echo "Installing VS Code CLI..."
    
    # Determine architecture
    if [ "$(uname -m)" = "x86_64" ]; then
        TARGET="cli-linux-x64"
    elif [ "$(uname -m)" = "aarch64" ] || [ "$(uname -m)" = "arm64" ]; then
        TARGET="cli-linux-arm64"
    else
        echo "ERROR: Unsupported architecture: $(uname -m)"
        exit 1
    fi
    
    echo "Selected target: $TARGET"
    DOWNLOAD_URL="https://update.code.visualstudio.com/${{VSCODE_VERSION}}/${{TARGET}}/stable"
    echo "Download URL: $DOWNLOAD_URL"
    
    # Download and install VS Code CLI
    echo "Downloading VS Code CLI..."
    if type curl > /dev/null 2>&1; then
        curl -L "$DOWNLOAD_URL" | tar xz -C "$INSTALL_LOCATION"
    elif type wget > /dev/null 2>&1; then
        wget -qO- "$DOWNLOAD_URL" | tar xz -C "$INSTALL_LOCATION"
    else
        echo "ERROR: Installation failed. Please install curl or wget in your container image."
        exit 1
    fi
    
    chmod +x "$INSTALL_LOCATION/code"
    echo "VS Code CLI installed successfully at: $INSTALL_LOCATION/code"
else
    echo "VS Code CLI already installed at: $INSTALL_LOCATION/code"
fi

# Set proper ownership
chown -R vscode:vscode /home/vscode /workspace /shared

# Add to PATH
export PATH="$INSTALL_LOCATION:$PATH"

# Test the VS Code CLI
echo "Testing VS Code CLI..."
if "$INSTALL_LOCATION/code" --version; then
    echo "VS Code CLI is working correctly."
else
    echo "ERROR: VS Code CLI test failed."
    exit 1
fi

# Function to download and install extension from marketplace
install_extension_from_marketplace() {{
    local extension=$1
    local publisher=$(echo "$extension" | cut -d. -f1)
    local name=$(echo "$extension" | cut -d. -f2)
    
    echo "Installing extension: $extension"
    
    # Create temp directory for download
    local temp_dir=$(mktemp -d)
    local vsix_file="$temp_dir/${{extension}}.vsix"
    
    # Use the gallery.vsassets.io URL (most reliable)
    local market_url="https://${{publisher}}.gallery.vsassets.io/_apis/public/gallery/publisher/${{publisher}}/extension/${{name}}/latest/assetbyname/Microsoft.VisualStudio.Services.VSIXPackage"
    
    echo "  Downloading from: $market_url"
    
    # Download with curl, handling both gzipped and non-gzipped responses
    if curl -L -f -H "Accept-Encoding: gzip" -o "$vsix_file" "$market_url" 2>/dev/null; then
        echo "  Download completed"
        
        # Check if it's gzipped
        if file "$vsix_file" | grep -q "gzip compressed data"; then
            echo "  File is gzipped, decompressing..."
            mv "$vsix_file" "$vsix_file.gz"
            gunzip "$vsix_file.gz" || true
        fi
        
        # Check if it's a valid VSIX (ZIP) file
        if file "$vsix_file" | grep -q -E "(Zip archive data|ZIP archive data|Java archive data)"; then
            # Extract VSIX to extensions directory
            local ext_dir="$DATA_DIR/extensions/${{publisher}}.${{name}}"
            rm -rf "$ext_dir"  # Remove if exists
            mkdir -p "$ext_dir"
            
            echo "  Extracting to: $ext_dir"
            if unzip -q -o "$vsix_file" -d "$ext_dir" 2>/dev/null; then
                # Look for package.json in different locations
                if [ -f "$ext_dir/extension/package.json" ]; then
                    # Move contents of extension folder up one level
                    mv "$ext_dir/extension/"* "$ext_dir/" 2>/dev/null || true
                    rmdir "$ext_dir/extension" 2>/dev/null || true
                fi
                
                if [ -f "$ext_dir/package.json" ]; then
                    local ext_version=$(jq -r '.version // "unknown"' "$ext_dir/package.json" 2>/dev/null)
                    echo "  ✓ Successfully installed version $ext_version"
                else
                    echo "  ✗ No package.json found in extension"
                fi
                
                # Set proper ownership
                chown -R vscode:vscode "$ext_dir"
            else
                echo "  ✗ Failed to extract VSIX"
            fi
        else
            echo "  ✗ Downloaded file is not a valid VSIX/ZIP file"
        fi
    else
        echo "  ✗ Failed to download extension"
    fi
    
    # Clean up
    rm -rf "$temp_dir"
}}

# Process VS Code configuration (extensions and settings)
echo "Processing VS Code configuration..."
if [ -n "${{VSCODE_CONFIG}}" ]; then
    echo "${{VSCODE_CONFIG}}" > /tmp/vscode_config.json
    
    # Install extensions
    extensions=$(jq -r '.extensions[]?' /tmp/vscode_config.json 2>/dev/null || echo "")
    if [ -n "$extensions" ]; then
        echo "Found extensions to install:"
        echo "$extensions" | while read -r extension; do
            echo "  - $extension"
        done
        echo ""
        
        # Install each extension
        echo "$extensions" | while read -r extension; do
            if [ -n "$extension" ]; then
                install_extension_from_marketplace "$extension"
            fi
        done
    else
        echo "No extensions to install."
    fi
    
    # Apply settings
    settings=$(jq -c '.settings' /tmp/vscode_config.json 2>/dev/null || echo "{{}}")
    if [ "$settings" != "{{}}" ] && [ "$settings" != "null" ]; then
        echo "Applying VS Code settings..."
        mkdir -p "$DATA_DIR/data/Machine"
        echo "$settings" > "$DATA_DIR/data/Machine/settings.json"
        chown -R vscode:vscode "$DATA_DIR/data"
        echo "Settings applied."
    fi
    
    # Run post-create command if present
    postCreateCommand=$(jq -r '.postCreateCommand // ""' /tmp/vscode_config.json 2>/dev/null)
    if [ -n "$postCreateCommand" ] && [ "$postCreateCommand" != "null" ] && [ "$postCreateCommand" != "" ]; then
        echo "Running post-create command: $postCreateCommand"
        su - vscode -c "cd /workspace && $postCreateCommand" || echo "Post-create command failed"
    fi
    
    rm -f /tmp/vscode_config.json
fi

# Export environment variables for vscode user
export TOKEN="${{TOKEN}}"
export CLI_DATA_DIR="${{CLI_DATA_DIR}}"
export USER_DATA_DIR="${{USER_DATA_DIR}}"
export SERVER_DATA_DIR="${{SERVER_DATA_DIR}}"
export EXTENSIONS_DIR="${{EXTENSIONS_DIR}}"

echo ""
echo "Initializing workspace..."
# Initialize workspace with a welcome file if empty
if [ -z "$(ls -A /workspace 2>/dev/null)" ]; then
    echo "Creating welcome file in empty workspace..."
    su - vscode -c "
        cat > /workspace/README.md << 'EOF'
# Welcome to VS Code DevContainer!

Your development environment is ready.

## Environment Details
- Instance ID: {instance_path}
- Base Image: $(cat /etc/os-release | grep PRETTY_NAME | cut -d= -f2 | tr -d '"')
- VS Code Version: {vscode_version}

## Installed Extensions
$(if [ -d "$DATA_DIR/extensions" ]; then
    for ext in "$DATA_DIR/extensions"/*; do
        if [ -d "$ext" ] && [ -f "$ext/package.json" ]; then
            name=$(jq -r '.displayName // .name' "$ext/package.json" 2>/dev/null)
            version=$(jq -r '.version' "$ext/package.json" 2>/dev/null)
            echo "- $name (v$version)"
        fi
    done
else
    echo "None"
fi)

## Quick Start
1. Open the file explorer on the left
2. Create new files and folders
3. Start coding!

## Storage
- **/workspace**: Your project files (instance-specific)
- **/shared**: Shared storage across all your instances
EOF
    " || echo "Failed to create welcome file"
else
    echo "Workspace already contains files."
fi

# List installed extensions
echo ""
echo "Installed extensions:"
if [ -d "$DATA_DIR/extensions" ]; then
    extension_count=0
    for ext_dir in "$DATA_DIR/extensions"/*; do
        if [ -d "$ext_dir" ] && [ -f "$ext_dir/package.json" ]; then
            ext_name=$(basename "$ext_dir")
            ext_display_name=$(jq -r '.displayName // .name // "Unknown"' "$ext_dir/package.json" 2>/dev/null)
            ext_version=$(jq -r '.version // "unknown"' "$ext_dir/package.json" 2>/dev/null)
            echo "  - $ext_name ($ext_display_name) v$ext_version"
            extension_count=$((extension_count + 1))
        fi
    done
    echo "Total: $extension_count extensions"
else
    echo "  None"
fi

echo ""
echo "Starting VS Code Server..."
echo "Server will be available at: http://localhost:8000"
echo "Instance path: {instance_path}"
echo "Token: ${{TOKEN}}"

# Start VS Code Server as vscode user with the correct parameters
exec su - vscode -c "
    export PATH='$INSTALL_LOCATION:$PATH'
    export TOKEN='${{TOKEN}}'
    export CLI_DATA_DIR='${{CLI_DATA_DIR}}'
    export USER_DATA_DIR='${{USER_DATA_DIR}}'
    export SERVER_DATA_DIR='${{SERVER_DATA_DIR}}'
    export EXTENSIONS_DIR='$DATA_DIR/extensions'
    
    echo 'Starting VS Code Server as vscode user...'
    exec '$INSTALL_LOCATION/code' serve-web \\
        --accept-server-license-terms \\
        --host 0.0.0.0 \\
        --port 8000 \\
        --connection-token '${{TOKEN}}' \\
        --server-base-path '{instance_path}' \\
        --cli-data-dir '${{CLI_DATA_DIR}}' \\
        --user-data-dir '${{USER_DATA_DIR}}' \\
        --server-data-dir '${{SERVER_DATA_DIR}}' \\
        --extensions-dir '$DATA_DIR/extensions'
"
'''
    
    deployment = client.V1Deployment(
        metadata=client.V1ObjectMeta(
            name=instance_id,
            labels={"app": BASE_NAME, "instance": instance_id, "user": user_id}
        ),
        spec=client.V1DeploymentSpec(
            replicas=1,
            selector=client.V1LabelSelector(
                match_labels={"app": BASE_NAME, "instance": instance_id}
            ),
            template=client.V1PodTemplateSpec(
                metadata=client.V1ObjectMeta(
                    labels={"app": BASE_NAME, "instance": instance_id, "user": user_id}
                ),
                spec=client.V1PodSpec(
                    containers=[
                        client.V1Container(
                            name=BASE_NAME,
                            image=container_image,
                            image_pull_policy="Always",
                            ports=[client.V1ContainerPort(container_port=8000)],
                            env_from=[
                                client.V1EnvFromSource(
                                    config_map_ref=client.V1ConfigMapEnvSource(
                                        name=f"{instance_id}-config"
                                    )
                                )
                            ],
                            volume_mounts=[
                                # Instance-specific workspace
                                client.V1VolumeMount(
                                    name="workspace",
                                    mount_path="/workspace"
                                ),
                                # Shared user storage
                                client.V1VolumeMount(
                                    name="shared",
                                    mount_path="/shared"
                                ),
                                # VS Code configuration
                                client.V1VolumeMount(
                                    name="vscode-config",
                                    mount_path="/home/vscode/.vscode"
                                )
                            ],
                            resources=client.V1ResourceRequirements(
                                requests={
                                    "memory": memory_request,
                                    "cpu": cpu_request
                                },
                                limits={
                                    "memory": memory_limit,
                                    "cpu": cpu_limit
                                }
                            ),
                            command=["/bin/bash", "-c"],
                            args=[install_script],
                            security_context=client.V1SecurityContext(
                                run_as_user=0  # Start as root to install, then switch
                            )
                        )
                    ],
                    volumes=[
                        # Instance-specific workspace
                        client.V1Volume(
                            name="workspace",
                            persistent_volume_claim=client.V1PersistentVolumeClaimVolumeSource(
                                claim_name=f"{instance_id}-workspace"
                            )
                        ),
                        # Shared user storage
                        client.V1Volume(
                            name="shared",
                            persistent_volume_claim=client.V1PersistentVolumeClaimVolumeSource(
                                claim_name=shared_pvc_name
                            )
                        ),
                        # VS Code configuration (ephemeral)
                        client.V1Volume(
                            name="vscode-config",
                            empty_dir=client.V1EmptyDirVolumeSource()
                        )
                    ]
                )
            )
        )
    )
    
    try:
        apps_v1_api.create_namespaced_deployment(
            namespace=NAMESPACE,
            body=deployment
        )
        logger.info(f"Created Deployment for instance {instance_id}")
    except client.exceptions.ApiException as e:
        logger.error(f"Error creating Deployment: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to create Deployment: {str(e)}"
        )

def create_service(instance_id: str) -> None:
    """Create a Service for the VS Code Server instance"""
    service = client.V1Service(
        metadata=client.V1ObjectMeta(
            name=f"{instance_id}-service",
            labels={"app": BASE_NAME, "instance": instance_id}
        ),
        spec=client.V1ServiceSpec(
            selector={"app": BASE_NAME, "instance": instance_id},
            ports=[
                client.V1ServicePort(
                    port=8000,
                    target_port=8000
                )
            ],
            type="ClusterIP"
        )
    )
    
    try:
        core_v1_api.create_namespaced_service(
            namespace=NAMESPACE,
            body=service
        )
        logger.info(f"Created Service for instance {instance_id}")
    except client.exceptions.ApiException as e:
        logger.error(f"Error creating Service: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to create Service: {str(e)}"
        )

def create_ingress_for_instance(instance_id: str, path_prefix: str) -> None:
    """Create an Ingress for the VS Code Server instance"""
    instance_path = f"{path_prefix}/{instance_id}"

    ingress = client.V1Ingress(
        metadata=client.V1ObjectMeta(
            name=f"{instance_id}-ingress",
            labels={"app": BASE_NAME, "instance": instance_id},
            annotations={
                "nginx.ingress.kubernetes.io/backend-protocol": "HTTP",
                "nginx.ingress.kubernetes.io/proxy-read-timeout": "3600",
                "nginx.ingress.kubernetes.io/proxy-send-timeout": "3600",
                "nginx.ingress.kubernetes.io/proxy-body-size": "0",
                "nginx.ingress.kubernetes.io/proxy-buffer-size": "128k",
                "nginx.ingress.kubernetes.io/proxy-http-version": "1.1",
                "nginx.ingress.kubernetes.io/websocket-services": f"{instance_id}-service",
                "nginx.ingress.kubernetes.io/upstream-vhost": BASE_DOMAIN,
                "nginx.ingress.kubernetes.io/configuration-snippet": """
                    more_set_headers "X-Forwarded-Host: $host";
                    more_set_headers "X-Forwarded-Proto: $scheme";
                """
            }
        ),
        spec=client.V1IngressSpec(
            tls=[
                client.V1IngressTLS(
                    hosts=[BASE_DOMAIN],
                    secret_name=TLS_SECRET_NAME
                )
            ],
            rules=[
                client.V1IngressRule(
                    host=BASE_DOMAIN,
                    http=client.V1HTTPIngressRuleValue(
                        paths=[
                            client.V1HTTPIngressPath(
                                path=instance_path,
                                path_type="Prefix",
                                backend=client.V1IngressBackend(
                                    service=client.V1IngressServiceBackend(
                                        name=f"{instance_id}-service",
                                        port=client.V1ServiceBackendPort(
                                            number=8000
                                        )
                                    )
                                )
                            )
                        ]
                    )
                )
            ]
        )
    )
    
    try:
        networking_v1_api.create_namespaced_ingress(
            namespace=NAMESPACE,
            body=ingress
        )
        logger.info(f"Created Ingress for instance {instance_id}")
    except client.exceptions.ApiException as e:
        logger.error(f"Error creating Ingress: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to create Ingress: {str(e)}"
        )

def get_instance_status(instance_id: str) -> str:
    """Get the status of a VS Code Server instance"""
    try:
        deployment = apps_v1_api.read_namespaced_deployment_status(
            name=instance_id,
            namespace=NAMESPACE
        )
        
        available_replicas = deployment.status.available_replicas
        if available_replicas is not None and available_replicas > 0:
            return "Running"
        return "Pending"
    except client.exceptions.ApiException as e:
        if e.status == 404:
            return "NotFound"
        logger.error(f"Error getting deployment status: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to get deployment status: {str(e)}"
        )

def delete_instance_resources(instance_id: str) -> None:
    """Delete all resources associated with a VS Code Server instance"""
    try:
        # Delete Ingress
        networking_v1_api.delete_namespaced_ingress(
            name=f"{instance_id}-ingress",
            namespace=NAMESPACE
        )
        logger.info(f"Deleted Ingress for instance {instance_id}")
        
        # Delete Service
        core_v1_api.delete_namespaced_service(
            name=f"{instance_id}-service",
            namespace=NAMESPACE
        )
        logger.info(f"Deleted Service for instance {instance_id}")
        
        # Delete Deployment
        apps_v1_api.delete_namespaced_deployment(
            name=instance_id,
            namespace=NAMESPACE
        )
        logger.info(f"Deleted Deployment for instance {instance_id}")
        
        # Delete ConfigMaps
        for cm_name in [f"{instance_id}-config", f"{instance_id}-build-logs"]:
            try:
                core_v1_api.delete_namespaced_config_map(
                    name=cm_name,
                    namespace=NAMESPACE
                )
                logger.info(f"Deleted ConfigMap {cm_name}")
            except client.exceptions.ApiException:
                pass
        
        # Delete instance workspace PVC
        core_v1_api.delete_namespaced_persistent_volume_claim(
            name=f"{instance_id}-workspace",
            namespace=NAMESPACE
        )
        logger.info(f"Deleted workspace PVC for instance {instance_id}")
        
    except client.exceptions.ApiException as e:
        if e.status == 404:
            logger.warning(f"Resource not found during deletion: {e}")
        else:
            logger.error(f"Error deleting resources: {e}")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to delete resources: {str(e)}"
            )

# Background task functions
async def build_and_deploy_devcontainer(build_config: Dict[str, Any]):
    """Background task to build and deploy devcontainer"""
    instance_id = build_config["instance_id"]
    
    # Update status to building
    try:
        status_cm = core_v1_api.read_namespaced_config_map(
            name=f"{instance_id}-build-status",
            namespace=NAMESPACE
        )
        status_cm.data["status"] = "building"
        core_v1_api.patch_namespaced_config_map(
            name=f"{instance_id}-build-status",
            namespace=NAMESPACE,
            body=status_cm
        )
    except Exception as e:
        logger.error(f"Error updating build status: {e}")
    
    # Create temporary workspace for building
    workspace_dir = tempfile.mkdtemp()
    try:
        # Build devcontainer image
        devcontainer_image = await build_devcontainer_image(
            instance_id,
            workspace_dir,
            build_config["devcontainer_config"]
        )
        
        # Update status to deploying
        try:
            status_cm.data["status"] = "deploying"
            core_v1_api.patch_namespaced_config_map(
                name=f"{instance_id}-build-status",
                namespace=NAMESPACE,
                body=status_cm
            )
        except:
            pass
        
        # Ensure shared storage exists
        ensure_shared_storage_pvc(build_config["user_id"], build_config["shared_storage_size"])
        
        # Create resources with devcontainer config
        create_configmap(
            instance_id, 
            build_config["access_token"], 
            DEFAULT_BASE_IMAGE, 
            devcontainer_image, 
            build_config["vscode_version"],
            build_config["devcontainer_config"]  # Pass the devcontainer config
        )
        create_workspace_pvc(instance_id, build_config["storage_size"])
        create_deployment(
            instance_id,
            build_config["user_id"],
            build_config["memory_request"], 
            build_config["memory_limit"],
            build_config["cpu_request"],
            build_config["cpu_limit"],
            devcontainer_image,
            build_config["vscode_version"]
        )
        create_service(instance_id)
        create_ingress_for_instance(instance_id, INSTANCES_PATH_PREFIX)
        
        # Update status to completed
        try:
            status_cm.data["status"] = "completed"
            core_v1_api.patch_namespaced_config_map(
                name=f"{instance_id}-build-status",
                namespace=NAMESPACE,
                body=status_cm
            )
        except:
            pass
        
    except Exception as e:
        logger.error(f"Error building devcontainer: {e}")
        # Update status to failed
        try:
            status_cm.data["status"] = "failed"
            status_cm.data["error"] = str(e)
            core_v1_api.patch_namespaced_config_map(
                name=f"{instance_id}-build-status",
                namespace=NAMESPACE,
                body=status_cm
            )
        except:
            pass
    finally:
        shutil.rmtree(workspace_dir, ignore_errors=True)
        # Clean up build status ConfigMap after some time
        await asyncio.sleep(300)  # Keep status for 5 minutes
        try:
            core_v1_api.delete_namespaced_config_map(
                name=f"{instance_id}-build-status",
                namespace=NAMESPACE
            )
        except:
            pass

async def build_and_deploy_workspace(build_config: Dict[str, Any]):
    """Background task to build and deploy workspace"""
    instance_id = build_config["instance_id"]
    
    # Update status to building
    try:
        status_cm = core_v1_api.read_namespaced_config_map(
            name=f"{instance_id}-build-status",
            namespace=NAMESPACE
        )
        status_cm.data["status"] = "building"
        core_v1_api.patch_namespaced_config_map(
            name=f"{instance_id}-build-status",
            namespace=NAMESPACE,
            body=status_cm
        )
    except Exception as e:
        logger.error(f"Error updating build status: {e}")
    
    # Extract workspace
    workspace_dir = tempfile.mkdtemp()
    devcontainer_config = None
    try:
        # Save uploaded file
        workspace_path = os.path.join(workspace_dir, "workspace.tar.gz")
        with open(workspace_path, "wb") as f:
            f.write(build_config["workspace_content"])
        
        # Extract tar.gz
        with tarfile.open(workspace_path, "r:gz") as tar:
            tar.extractall(workspace_dir)
        
        os.remove(workspace_path)
        
        # Find and read devcontainer.json
        devcontainer_json_path = None
        for root, dirs, files in os.walk(workspace_dir):
            if "devcontainer.json" in files:
                devcontainer_json_path = os.path.join(root, "devcontainer.json")
                break
        
        if not devcontainer_json_path:
            raise Exception("No devcontainer.json found in workspace")
        
        # Read devcontainer configuration
        with open(devcontainer_json_path, 'r') as f:
            devcontainer_config = json.load(f)
        
        # Build devcontainer image
        devcontainer_image = await build_devcontainer_image(
            instance_id,
            workspace_dir,
            None  # Use existing devcontainer.json from workspace
        )
        
        # Update status to deploying
        try:
            status_cm.data["status"] = "deploying"
            core_v1_api.patch_namespaced_config_map(
                name=f"{instance_id}-build-status",
                namespace=NAMESPACE,
                body=status_cm
            )
        except:
            pass
        
        # Ensure shared storage exists
        ensure_shared_storage_pvc(build_config["user_id"], build_config["shared_storage_size"])
        
        # Create resources with devcontainer config
        create_configmap(
            instance_id, 
            build_config["access_token"], 
            DEFAULT_BASE_IMAGE, 
            devcontainer_image, 
            build_config["vscode_version"],
            devcontainer_config  # Pass the extracted devcontainer config
        )
        create_workspace_pvc(instance_id, build_config["storage_size"])
        create_deployment(
            instance_id,
            build_config["user_id"],
            build_config["memory_request"], 
            build_config["memory_limit"],
            build_config["cpu_request"],
            build_config["cpu_limit"],
            devcontainer_image,
            build_config["vscode_version"]
        )
        create_service(instance_id)
        create_ingress_for_instance(instance_id, INSTANCES_PATH_PREFIX)
        
        # Update status to completed
        try:
            status_cm.data["status"] = "completed"
            core_v1_api.patch_namespaced_config_map(
                name=f"{instance_id}-build-status",
                namespace=NAMESPACE,
                body=status_cm
            )
        except:
            pass
        
    except Exception as e:
        logger.error(f"Error building workspace: {e}")
        # Update status to failed
        try:
            status_cm.data["status"] = "failed"
            status_cm.data["error"] = str(e)
            core_v1_api.patch_namespaced_config_map(
                name=f"{instance_id}-build-status",
                namespace=NAMESPACE,
                body=status_cm
            )
        except:
            pass
    finally:
        shutil.rmtree(workspace_dir, ignore_errors=True)
        # Clean up build status ConfigMap after some time
        await asyncio.sleep(300)  # Keep status for 5 minutes
        try:
            core_v1_api.delete_namespaced_config_map(
                name=f"{instance_id}-build-status",
                namespace=NAMESPACE
            )
        except:
            pass

# API Endpoints
@app.get("/", status_code=status.HTTP_200_OK)
def root():
    """Root endpoint to check if the API is running"""
    return {
        "status": "ok", 
        "service": "VS Code DevContainer Manager API",
        "version": "2.0.0"
    }

@app.post("/instances/simple", response_model=VSCodeServerResponse, status_code=status.HTTP_201_CREATED)
async def create_simple_instance(request: VSCodeServerRequest):
    """Create a simple VS Code Server instance without devcontainer"""
    instance_id = generate_instance_id(request.user_id)
    access_token = generate_access_token()
    path = generate_instance_path(instance_id)
    
    # Ensure shared storage exists
    ensure_shared_storage_pvc(request.user_id, request.shared_storage_size)
    
    # Create resources - no devcontainer config for simple instances
    create_configmap(instance_id, access_token, request.base_image, None, request.vscode_version, None)
    create_workspace_pvc(instance_id, request.storage_size)
    create_deployment(
        instance_id,
        request.user_id,
        request.memory_request, 
        request.memory_limit,
        request.cpu_request,
        request.cpu_limit,
        None,  # No devcontainer image
        request.vscode_version
    )
    create_service(instance_id)
    create_ingress_for_instance(instance_id, INSTANCES_PATH_PREFIX)
    
    url = f"https://{BASE_DOMAIN}{path}?tkn={access_token}"
    
    return VSCodeServerResponse(
        instance_id=instance_id,
        url=url,
        access_token=access_token,
        status="Creating",
        base_image=request.base_image,
        devcontainer_image=None
    )

@app.post("/instances/devcontainer", response_model=VSCodeServerResponse, status_code=status.HTTP_201_CREATED)
async def create_devcontainer_instance(
    background_tasks: BackgroundTasks,
    user_id: str = Form(...),
    devcontainer_json: UploadFile = File(...),
    storage_size: str = Form(DEFAULT_STORAGE_SIZE),
    shared_storage_size: str = Form(DEFAULT_SHARED_STORAGE_SIZE),
    memory_request: str = Form(DEFAULT_MEMORY_REQUEST),
    memory_limit: str = Form(DEFAULT_MEMORY_LIMIT),
    cpu_request: str = Form(DEFAULT_CPU_REQUEST),
    cpu_limit: str = Form(DEFAULT_CPU_LIMIT),
    vscode_version: str = Form("1.97.2")
):
    """Create a VS Code Server instance with devcontainer.json"""
    instance_id = generate_instance_id(user_id)
    access_token = generate_access_token()
    path = generate_instance_path(instance_id)
    
    # Parse devcontainer.json
    devcontainer_content = await devcontainer_json.read()
    try:
        devcontainer_config = json.loads(devcontainer_content)
    except json.JSONDecodeError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid devcontainer.json: {str(e)}"
        )
    
    # Store build configuration
    build_config = {
        "instance_id": instance_id,
        "user_id": user_id,
        "devcontainer_config": devcontainer_config,
        "storage_size": storage_size,
        "shared_storage_size": shared_storage_size,
        "memory_request": memory_request,
        "memory_limit": memory_limit,
        "cpu_request": cpu_request,
        "cpu_limit": cpu_limit,
        "vscode_version": vscode_version,
        "access_token": access_token
    }
    
    # Create initial ConfigMap to track build status
    status_cm = client.V1ConfigMap(
        metadata=client.V1ObjectMeta(
            name=f"{instance_id}-build-status",
            labels={"app": BASE_NAME, "instance": instance_id}
        ),
        data={
            "status": "queued",
            "config": json.dumps(build_config)
        }
    )
    
    try:
        core_v1_api.create_namespaced_config_map(
            namespace=NAMESPACE,
            body=status_cm
        )
    except client.exceptions.ApiException as e:
        logger.error(f"Error creating build status ConfigMap: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to create build status: {str(e)}"
        )
    
    # Start build in background
    background_tasks.add_task(
        build_and_deploy_devcontainer,
        build_config
    )
    
    url = f"https://{BASE_DOMAIN}{path}?tkn={access_token}"
    build_logs_url = f"https://{BASE_DOMAIN}{API_PATH_PREFIX}/instances/{instance_id}/build-logs"
    
    return VSCodeServerResponse(
        instance_id=instance_id,
        url=url,
        access_token=access_token,
        status="Queued",
        base_image=DEFAULT_BASE_IMAGE,
        devcontainer_image=f"{PULL_REGISTRY}/vscode-devcontainer-{instance_id}:latest",
        build_logs_url=build_logs_url
    )

@app.post("/instances/workspace", response_model=VSCodeServerResponse, status_code=status.HTTP_201_CREATED)
async def create_workspace_instance(
    background_tasks: BackgroundTasks,
    user_id: str = Form(...),
    workspace: UploadFile = File(...),
    storage_size: str = Form(DEFAULT_STORAGE_SIZE),
    shared_storage_size: str = Form(DEFAULT_SHARED_STORAGE_SIZE),
    memory_request: str = Form(DEFAULT_MEMORY_REQUEST),
    memory_limit: str = Form(DEFAULT_MEMORY_LIMIT),
    cpu_request: str = Form(DEFAULT_CPU_REQUEST),
    cpu_limit: str = Form(DEFAULT_CPU_LIMIT),
    vscode_version: str = Form("1.97.2")
):
    """Create a VS Code Server instance with a workspace folder (tar.gz)"""
    instance_id = generate_instance_id(user_id)
    access_token = generate_access_token()
    path = generate_instance_path(instance_id)
    
    # Save workspace content
    workspace_content = await workspace.read()
    
    # Store build configuration
    build_config = {
        "instance_id": instance_id,
        "user_id": user_id,
        "workspace_content": workspace_content,
        "storage_size": storage_size,
        "shared_storage_size": shared_storage_size,
        "memory_request": memory_request,
        "memory_limit": memory_limit,
        "cpu_request": cpu_request,
        "cpu_limit": cpu_limit,
        "vscode_version": vscode_version,
        "access_token": access_token
    }
    
    # Create initial ConfigMap to track build status
    status_cm = client.V1ConfigMap(
        metadata=client.V1ObjectMeta(
            name=f"{instance_id}-build-status",
            labels={"app": BASE_NAME, "instance": instance_id}
        ),
        data={
            "status": "queued",
            "config": json.dumps({k: v for k, v in build_config.items() if k != "workspace_content"})
        }
    )
    
    try:
        core_v1_api.create_namespaced_config_map(
            namespace=NAMESPACE,
            body=status_cm
        )
    except client.exceptions.ApiException as e:
        logger.error(f"Error creating build status ConfigMap: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to create build status: {str(e)}"
        )
    
    # Start build in background
    background_tasks.add_task(
        build_and_deploy_workspace,
        build_config
    )
    
    url = f"https://{BASE_DOMAIN}{path}?tkn={access_token}"
    build_logs_url = f"https://{BASE_DOMAIN}{API_PATH_PREFIX}/instances/{instance_id}/build-logs"
    
    return VSCodeServerResponse(
        instance_id=instance_id,
        url=url,
        access_token=access_token,
        status="Queued",
        base_image=DEFAULT_BASE_IMAGE,
        devcontainer_image=f"{PULL_REGISTRY}/vscode-devcontainer-{instance_id}:latest",
        build_logs_url=build_logs_url
    )

@app.get("/instances/{instance_id}/build-status")
def get_build_status(instance_id: str):
    """Get the current build status of an instance"""
    try:
        status_cm = core_v1_api.read_namespaced_config_map(
            name=f"{instance_id}-build-status",
            namespace=NAMESPACE
        )
        return {
            "instance_id": instance_id,
            "status": status_cm.data.get("status", "unknown"),
            "error": status_cm.data.get("error", None)
        }
    except client.exceptions.ApiException as e:
        if e.status == 404:
            # Check if instance exists
            try:
                core_v1_api.read_namespaced_config_map(
                    name=f"{instance_id}-config",
                    namespace=NAMESPACE
                )
                return {
                    "instance_id": instance_id,
                    "status": "completed",
                    "error": None
                }
            except:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"Instance {instance_id} not found"
                )
        raise

@app.get("/instances/{instance_id}/build-logs", response_model=BuildStatus)
def get_build_logs(instance_id: str):
    """Get build logs for an instance"""
    try:
        config_map = core_v1_api.read_namespaced_config_map(
            name=f"{instance_id}-build-logs",
            namespace=NAMESPACE
        )
        
        logs = config_map.data.get("logs", "")
        status = get_instance_status(instance_id)
        
        return BuildStatus(
            instance_id=instance_id,
            status=status,
            logs=logs
        )
    except client.exceptions.ApiException as e:
        if e.status == 404:
            # For simple instances, there are no build logs
            status = get_instance_status(instance_id)
            if status == "NotFound":
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"Instance {instance_id} not found"
                )
            else:
                return BuildStatus(
                    instance_id=instance_id,
                    status=status,
                    logs="No build logs available (simple instance)"
                )
        raise

@app.get("/instances/{instance_id}", response_model=VSCodeServerResponse)
def get_instance(instance_id: str):
    """Get details of a specific VS Code Server instance"""
    try:
        config_map = core_v1_api.read_namespaced_config_map(
            name=f"{instance_id}-config",
            namespace=NAMESPACE
        )
        
        access_token = config_map.data.get("TOKEN", "")
        base_image = config_map.data.get("BASE_IMAGE", DEFAULT_BASE_IMAGE)
        devcontainer_image = config_map.data.get("DEVCONTAINER_IMAGE", None)
        if devcontainer_image == "":
            devcontainer_image = None
            
        path = generate_instance_path(instance_id)
        url = f"https://{BASE_DOMAIN}{path}?tkn={access_token}"
        status_str = get_instance_status(instance_id)
        
        build_logs_url = None
        try:
            core_v1_api.read_namespaced_config_map(
                name=f"{instance_id}-build-logs",
                namespace=NAMESPACE
            )
            build_logs_url = f"https://{BASE_DOMAIN}{API_PATH_PREFIX}/instances/{instance_id}/build-logs"
        except:
            pass
        
        return VSCodeServerResponse(
            instance_id=instance_id,
            url=url,
            access_token=access_token,
            status=status_str,
            base_image=base_image,
            devcontainer_image=devcontainer_image,
            build_logs_url=build_logs_url
        )
    
    except client.exceptions.ApiException as e:
        if e.status == 404:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Instance {instance_id} not found"
            )
        raise

@app.delete("/instances/{instance_id}")
def delete_instance(instance_id: str):
    """Delete a VS Code Server instance"""
    status_str = get_instance_status(instance_id)
    if status_str == "NotFound":
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Instance {instance_id} not found"
        )
    
    delete_instance_resources(instance_id)
    
    return {
        "instance_id": instance_id,
        "status": "Deleted"
    }

@app.get("/health")
def health_check():
    """Health check endpoint"""
    return {"status": "healthy", "service": "vscode-devcontainer-manager"}