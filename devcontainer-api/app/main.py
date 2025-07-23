from fastapi import FastAPI, HTTPException, status, UploadFile, File, Form
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
except config.ConfigException:
    config.load_kube_config()
    logger.info("Loaded kubeconfig Kubernetes configuration")

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
    return str(uuid.uuid4())

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

async def build_devcontainer_image(
    instance_id: str,
    workspace_path: str,
    devcontainer_config: Optional[Dict[str, Any]] = None
) -> str:
    """Build a devcontainer image using the devcontainer CLI"""
    build_dir = os.path.join(DEVCONTAINER_BUILD_PATH, instance_id)
    os.makedirs(build_dir, exist_ok=True)
    
    try:
        # If devcontainer_config is provided, write it to the workspace
        if devcontainer_config:
            devcontainer_path = os.path.join(workspace_path, ".devcontainer")
            os.makedirs(devcontainer_path, exist_ok=True)
            with open(os.path.join(devcontainer_path, "devcontainer.json"), "w") as f:
                json.dump(devcontainer_config, f, indent=2)
        
        # Generate image name
        image_name = f"{REGISTRY}/vscode-devcontainer-{instance_id}:latest"
        
        # Build the devcontainer image
        build_cmd = [
            "devcontainer", "build",
            "--workspace-folder", workspace_path,
            "--image-name", image_name,
            "--no-cache"
        ]
        
        logger.info(f"Building devcontainer image: {' '.join(build_cmd)}")
        
        # Run the build command
        process = await asyncio.create_subprocess_exec(
            *build_cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=build_dir
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
        
        # Push the image to registry
        push_cmd = ["docker", "push", image_name]
        push_process = await asyncio.create_subprocess_exec(
            *push_cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT
        )
        
        await push_process.wait()
        
        if push_process.returncode != 0:
            raise Exception("Failed to push image to registry")
        
        # Store build logs
        logs_cm = client.V1ConfigMap(
            metadata=client.V1ObjectMeta(
                name=f"{instance_id}-build-logs",
                labels={"app": BASE_NAME, "instance": instance_id}
            ),
            data={
                "logs": "\n".join(build_logs),
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
        
        return image_name
        
    finally:
        # Cleanup build directory
        if os.path.exists(build_dir):
            shutil.rmtree(build_dir)

def create_configmap(instance_id: str, access_token: str, base_image: str, 
                    devcontainer_image: Optional[str], vscode_version: str) -> None:
    """Create a ConfigMap for the VS Code Server instance"""
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
            "VSCODE_VERSION": vscode_version
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
    
    # VS Code installation script
    install_script = f"""
    # Ensure user exists
    if ! id vscode >/dev/null 2>&1; then
        useradd -m -s /bin/bash -u 1000 vscode
    fi
    
    # Install dependencies if not in devcontainer image
    if ! command -v code >/dev/null 2>&1; then
        if command -v apt-get >/dev/null 2>&1; then
            apt-get update && apt-get install -y curl wget ca-certificates git sudo
        fi
        
        # Determine architecture
        if [ "$(uname -m)" = "x86_64" ]; then
            export TARGET='cli-linux-x64'
        elif [ "$(uname -m)" = "aarch64" ] || [ "$(uname -m)" = "arm64" ]; then
            export TARGET='cli-linux-arm64'
        else
            echo "Unsupported architecture: $(uname -m)"
            exit 1
        fi
        
        # Install VS Code CLI
        wget -qO- "https://update.code.visualstudio.com/{vscode_version}/${{TARGET}}/stable" | tar xvz -C /usr/bin/
        chmod +x /usr/bin/code
    fi
    
    # Set up directories
    mkdir -p /home/vscode/.vscode
    chown -R vscode:vscode /home/vscode /workspace /shared
    
    # Run VS Code Server as vscode user
    exec su - vscode -c 'code serve-web --accept-server-license-terms --host 0.0.0.0 --port 8000 \
        --connection-token "$TOKEN" --server-base-path {instance_path} \
        --cli-data-dir "$CLI_DATA_DIR" --user-data-dir "$USER_DATA_DIR" \
        --server-data-dir "$SERVER_DATA_DIR" --extensions-dir "$EXTENSIONS_DIR"'
    """
    
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
                "nginx.ingress.kubernetes.io/use-regex": "true"
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
                                path=f"{instance_path}(/.*)?",
                                path_type="ImplementationSpecific",
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
    
    # Create resources
    create_configmap(instance_id, access_token, request.base_image, None, request.vscode_version)
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
    
    # Create temporary workspace for building
    workspace_dir = tempfile.mkdtemp()
    try:
        # Build devcontainer image
        devcontainer_image = await build_devcontainer_image(
            instance_id,
            workspace_dir,
            devcontainer_config
        )
        
        # Ensure shared storage exists
        ensure_shared_storage_pvc(user_id, shared_storage_size)
        
        # Create resources
        create_configmap(instance_id, access_token, DEFAULT_BASE_IMAGE, devcontainer_image, vscode_version)
        create_workspace_pvc(instance_id, storage_size)
        create_deployment(
            instance_id,
            user_id,
            memory_request, 
            memory_limit,
            cpu_request,
            cpu_limit,
            devcontainer_image,
            vscode_version
        )
        create_service(instance_id)
        create_ingress_for_instance(instance_id, INSTANCES_PATH_PREFIX)
        
    finally:
        shutil.rmtree(workspace_dir, ignore_errors=True)
    
    url = f"https://{BASE_DOMAIN}{path}?tkn={access_token}"
    build_logs_url = f"https://{BASE_DOMAIN}{API_PATH_PREFIX}/instances/{instance_id}/build-logs"
    
    return VSCodeServerResponse(
        instance_id=instance_id,
        url=url,
        access_token=access_token,
        status="Building",
        base_image=DEFAULT_BASE_IMAGE,
        devcontainer_image=devcontainer_image,
        build_logs_url=build_logs_url
    )

@app.post("/instances/workspace", response_model=VSCodeServerResponse, status_code=status.HTTP_201_CREATED)
async def create_workspace_instance(
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
    
    # Extract workspace
    workspace_dir = tempfile.mkdtemp()
    try:
        # Save uploaded file
        workspace_path = os.path.join(workspace_dir, "workspace.tar.gz")
        with open(workspace_path, "wb") as f:
            content = await workspace.read()
            f.write(content)
        
        # Extract tar.gz
        with tarfile.open(workspace_path, "r:gz") as tar:
            tar.extractall(workspace_dir)
        
        os.remove(workspace_path)
        
        # Find devcontainer.json
        devcontainer_json_path = None
        for root, dirs, files in os.walk(workspace_dir):
            if "devcontainer.json" in files:
                devcontainer_json_path = os.path.join(root, "devcontainer.json")
                break
        
        if not devcontainer_json_path:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="No devcontainer.json found in workspace"
            )
        
        # Build devcontainer image
        devcontainer_image = await build_devcontainer_image(
            instance_id,
            workspace_dir,
            None  # Use existing devcontainer.json from workspace
        )
        
        # Ensure shared storage exists
        ensure_shared_storage_pvc(user_id, shared_storage_size)
        
        # Create resources
        create_configmap(instance_id, access_token, DEFAULT_BASE_IMAGE, devcontainer_image, vscode_version)
        create_workspace_pvc(instance_id, storage_size)
        create_deployment(
            instance_id,
            user_id,
            memory_request, 
            memory_limit,
            cpu_request,
            cpu_limit,
            devcontainer_image,
            vscode_version
        )
        create_service(instance_id)
        create_ingress_for_instance(instance_id, INSTANCES_PATH_PREFIX)
        
    finally:
        shutil.rmtree(workspace_dir, ignore_errors=True)
    
    url = f"https://{BASE_DOMAIN}{path}?tkn={access_token}"
    build_logs_url = f"https://{BASE_DOMAIN}{API_PATH_PREFIX}/instances/{instance_id}/build-logs"
    
    return VSCodeServerResponse(
        instance_id=instance_id,
        url=url,
        access_token=access_token,
        status="Building",
        base_image=DEFAULT_BASE_IMAGE,
        devcontainer_image=devcontainer_image,
        build_logs_url=build_logs_url
    )

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
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Build logs for instance {instance_id} not found"
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