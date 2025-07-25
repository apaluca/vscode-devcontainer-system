#!/usr/bin/env python3
"""
VS Code DevContainer Manager Client Script

This script provides command-line interaction with the VS Code DevContainer Manager API.
"""

import argparse
import json
import os
import sys
import requests
import tarfile
import tempfile
import time
from typing import Dict, Any, Optional
import urllib3

# Disable SSL warnings for self-signed certificates
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# Default API endpoint
DEFAULT_API_URL = "https://vscode.local/api"
DEFAULT_BASE_IMAGE = "ubuntu:22.04"

def parse_args():
    """Parse command-line arguments"""
    parser = argparse.ArgumentParser(description="VS Code DevContainer Manager Client")
    
    subparsers = parser.add_subparsers(dest="command", help="Command to execute")
    
    # Create simple instance command
    create_parser = subparsers.add_parser("create-simple", help="Create a simple VS Code Server instance")
    create_parser.add_argument("--user-id", required=True, help="User ID")
    create_parser.add_argument("--storage", default="2Gi", help="Workspace storage size (default: 2Gi)")
    create_parser.add_argument("--shared-storage", default="5Gi", help="Shared storage size (default: 5Gi)")
    create_parser.add_argument("--memory-request", default="512Mi", help="Memory request (default: 512Mi)")
    create_parser.add_argument("--memory-limit", default="2Gi", help="Memory limit (default: 2Gi)")
    create_parser.add_argument("--cpu-request", default="200m", help="CPU request (default: 200m)")
    create_parser.add_argument("--cpu-limit", default="1000m", help="CPU limit (default: 1000m)")
    create_parser.add_argument("--base-image", default=DEFAULT_BASE_IMAGE, 
                             help=f"Base Docker image (default: {DEFAULT_BASE_IMAGE})")
    create_parser.add_argument("--vscode-version", default="1.97.2", 
                             help="VS Code Server version (default: 1.97.2)")
    
    # Create devcontainer instance command
    devcontainer_parser = subparsers.add_parser("create-devcontainer", 
                                              help="Create VS Code Server with devcontainer.json")
    devcontainer_parser.add_argument("--user-id", required=True, help="User ID")
    devcontainer_parser.add_argument("--devcontainer-json", required=True, 
                                   help="Path to devcontainer.json file")
    devcontainer_parser.add_argument("--storage", default="2Gi", help="Workspace storage size (default: 2Gi)")
    devcontainer_parser.add_argument("--shared-storage", default="5Gi", help="Shared storage size (default: 5Gi)")
    devcontainer_parser.add_argument("--memory-request", default="512Mi", help="Memory request (default: 512Mi)")
    devcontainer_parser.add_argument("--memory-limit", default="2Gi", help="Memory limit (default: 2Gi)")
    devcontainer_parser.add_argument("--cpu-request", default="200m", help="CPU request (default: 200m)")
    devcontainer_parser.add_argument("--cpu-limit", default="1000m", help="CPU limit (default: 1000m)")
    devcontainer_parser.add_argument("--vscode-version", default="1.97.2", 
                                   help="VS Code Server version (default: 1.97.2)")
    
    # Create workspace instance command
    workspace_parser = subparsers.add_parser("create-workspace", 
                                           help="Create VS Code Server with workspace folder")
    workspace_parser.add_argument("--user-id", required=True, help="User ID")
    workspace_parser.add_argument("--workspace-dir", required=True, 
                                help="Path to workspace directory containing devcontainer.json")
    workspace_parser.add_argument("--storage", default="2Gi", help="Workspace storage size (default: 2Gi)")
    workspace_parser.add_argument("--shared-storage", default="5Gi", help="Shared storage size (default: 5Gi)")
    workspace_parser.add_argument("--memory-request", default="512Mi", help="Memory request (default: 512Mi)")
    workspace_parser.add_argument("--memory-limit", default="2Gi", help="Memory limit (default: 2Gi)")
    workspace_parser.add_argument("--cpu-request", default="200m", help="CPU request (default: 200m)")
    workspace_parser.add_argument("--cpu-limit", default="1000m", help="CPU limit (default: 1000m)")
    workspace_parser.add_argument("--vscode-version", default="1.97.2", 
                                help="VS Code Server version (default: 1.97.2)")
    
    # Get instance command
    get_parser = subparsers.add_parser("get", help="Get details of a VS Code Server instance")
    get_parser.add_argument("--instance-id", required=True, help="Instance ID")
    
    # Get build logs command
    logs_parser = subparsers.add_parser("build-logs", help="Get build logs for an instance")
    logs_parser.add_argument("--instance-id", required=True, help="Instance ID")
    
    # Get build status command
    status_parser = subparsers.add_parser("build-status", help="Get build status for an instance")
    status_parser.add_argument("--instance-id", required=True, help="Instance ID")
    
    # Delete instance command
    delete_parser = subparsers.add_parser("delete", help="Delete a VS Code Server instance")
    delete_parser.add_argument("--instance-id", required=True, help="Instance ID")
    
    # Global options
    parser.add_argument("--api-url", default=DEFAULT_API_URL, help=f"API URL (default: {DEFAULT_API_URL})")
    parser.add_argument("--no-wait", action="store_true", help="Don't wait for build to complete")
    
    return parser.parse_args()

def make_api_request(method: str, url: str, data: Optional[Dict[str, Any]] = None, 
                    files: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Make an API request to the VS Code DevContainer Manager"""
    try:
        if method.upper() == "GET":
            response = requests.get(url, params=data, verify=False)
        elif method.upper() == "POST":
            if files:
                response = requests.post(url, data=data, files=files, verify=False)
            else:
                response = requests.post(url, json=data, verify=False)
        elif method.upper() == "DELETE":
            response = requests.delete(url, verify=False)
        else:
            raise ValueError(f"Unsupported HTTP method: {method}")
        
        response.raise_for_status()
        return response.json()
    
    except requests.exceptions.RequestException as e:
        print(f"Error making API request: {e}")
        if hasattr(e, "response") and e.response is not None:
            try:
                error_data = e.response.json()
                print(f"API Error: {error_data.get('detail', 'Unknown error')}")
            except ValueError:
                print(f"API Error: {e.response.text}")
        sys.exit(1)

def wait_for_build(api_url: str, instance_id: str, max_wait: int = 300) -> bool:
    """Wait for build to complete"""
    print(f"\nWaiting for build to complete...")
    start_time = time.time()
    last_status = None
    
    while time.time() - start_time < max_wait:
        try:
            response = requests.get(
                f"{api_url}/instances/{instance_id}/build-status",
                verify=False
            )
            if response.status_code == 404:
                # Build status not found, might be completed
                return True
            
            data = response.json()
            status = data.get("status", "unknown")
            
            if status != last_status:
                print(f"\nBuild status: {status}")
                last_status = status
            else:
                print(".", end="", flush=True)
            
            if status == "completed":
                print("\nBuild completed successfully!")
                return True
            elif status == "failed":
                print(f"\nBuild failed: {data.get('error', 'Unknown error')}")
                return False
            
            time.sleep(5)
        except Exception as e:
            print(f"\nError checking build status: {e}")
            time.sleep(5)
    
    print(f"\nBuild timeout after {max_wait} seconds")
    return False

def create_simple_instance(args):
    """Create a simple VS Code Server instance"""
    data = {
        "user_id": args.user_id,
        "storage_size": args.storage,
        "shared_storage_size": args.shared_storage,
        "memory_request": args.memory_request,
        "memory_limit": args.memory_limit,
        "cpu_request": args.cpu_request,
        "cpu_limit": args.cpu_limit,
        "base_image": args.base_image,
        "vscode_version": args.vscode_version
    }
    
    response = make_api_request("POST", f"{args.api_url}/instances/simple", data)
    
    print("VS Code Server instance created successfully!")
    print(f"Instance ID: {response['instance_id']}")
    print(f"Base Image: {response['base_image']}")
    print(f"Access URL: {response['url']}")
    print(f"Access Token: {response['access_token']}")
    print(f"Status: {response['status']}")
    
    return response

def create_devcontainer_instance(args):
    """Create a VS Code Server instance with devcontainer.json"""
    # Read devcontainer.json file
    if not os.path.exists(args.devcontainer_json):
        print(f"Error: devcontainer.json file not found: {args.devcontainer_json}")
        sys.exit(1)
    
    with open(args.devcontainer_json, 'r') as f:
        devcontainer_content = f.read()
    
    # Validate JSON
    try:
        json.loads(devcontainer_content)
    except json.JSONDecodeError as e:
        print(f"Error: Invalid JSON in devcontainer.json: {e}")
        sys.exit(1)
    
    data = {
        "user_id": args.user_id,
        "storage_size": args.storage,
        "shared_storage_size": args.shared_storage,
        "memory_request": args.memory_request,
        "memory_limit": args.memory_limit,
        "cpu_request": args.cpu_request,
        "cpu_limit": args.cpu_limit,
        "vscode_version": args.vscode_version
    }
    
    files = {
        "devcontainer_json": ("devcontainer.json", devcontainer_content, "application/json")
    }
    
    response = make_api_request("POST", f"{args.api_url}/instances/devcontainer", data, files)
    
    print("VS Code Server instance with devcontainer created successfully!")
    print(f"Instance ID: {response['instance_id']}")
    print(f"DevContainer Image: {response.get('devcontainer_image', 'Building...')}")
    print(f"Access URL: {response['url']}")
    print(f"Access Token: {response['access_token']}")
    print(f"Status: {response['status']}")
    if response.get('build_logs_url'):
        print(f"Build Logs: {response['build_logs_url']}")
    
    # Wait for build unless --no-wait is specified
    if not args.no_wait:
        if wait_for_build(args.api_url, response['instance_id']):
            # Get updated instance details
            instance_response = get_instance(args)
            print("\nInstance is ready!")
            print(f"Access URL: {instance_response['url']}")
    
    return response

def create_workspace_instance(args):
    """Create a VS Code Server instance with a workspace folder"""
    # Check if workspace directory exists
    if not os.path.isdir(args.workspace_dir):
        print(f"Error: Workspace directory not found: {args.workspace_dir}")
        sys.exit(1)
    
    # Check for devcontainer.json
    devcontainer_paths = [
        os.path.join(args.workspace_dir, ".devcontainer", "devcontainer.json"),
        os.path.join(args.workspace_dir, ".devcontainer.json")
    ]
    
    found_devcontainer = False
    for path in devcontainer_paths:
        if os.path.exists(path):
            found_devcontainer = True
            break
    
    if not found_devcontainer:
        print("Error: No devcontainer.json found in workspace directory")
        print("Checked paths:")
        for path in devcontainer_paths:
            print(f"  - {path}")
        sys.exit(1)
    
    # Create tar.gz of workspace
    with tempfile.NamedTemporaryFile(suffix='.tar.gz', delete=False) as tmp_file:
        try:
            with tarfile.open(tmp_file.name, 'w:gz') as tar:
                tar.add(args.workspace_dir, arcname='.')
            
            # Prepare data and files
            data = {
                "user_id": args.user_id,
                "storage_size": args.storage,
                "shared_storage_size": args.shared_storage,
                "memory_request": args.memory_request,
                "memory_limit": args.memory_limit,
                "cpu_request": args.cpu_request,
                "cpu_limit": args.cpu_limit,
                "vscode_version": args.vscode_version
            }
            
            with open(tmp_file.name, 'rb') as f:
                files = {
                    "workspace": ("workspace.tar.gz", f, "application/gzip")
                }
                
                response = make_api_request("POST", f"{args.api_url}/instances/workspace", data, files)
        finally:
            os.unlink(tmp_file.name)
    
    print("VS Code Server instance with workspace created successfully!")
    print(f"Instance ID: {response['instance_id']}")
    print(f"DevContainer Image: {response.get('devcontainer_image', 'Building...')}")
    print(f"Access URL: {response['url']}")
    print(f"Access Token: {response['access_token']}")
    print(f"Status: {response['status']}")
    if response.get('build_logs_url'):
        print(f"Build Logs: {response['build_logs_url']}")
    
    # Wait for build unless --no-wait is specified
    if not args.no_wait:
        if wait_for_build(args.api_url, response['instance_id']):
            # Get updated instance details
            instance_response = get_instance(args)
            print("\nInstance is ready!")
            print(f"Access URL: {instance_response['url']}")
    
    return response

def get_instance(args):
    """Get details of a VS Code Server instance"""
    response = make_api_request("GET", f"{args.api_url}/instances/{args.instance_id}")
    
    print(f"VS Code Server instance details:")
    print(f"Instance ID: {response['instance_id']}")
    print(f"Base Image: {response['base_image']}")
    if response.get('devcontainer_image'):
        print(f"DevContainer Image: {response['devcontainer_image']}")
    print(f"Access URL: {response['url']}")
    print(f"Access Token: {response['access_token']}")
    print(f"Status: {response['status']}")
    if response.get('build_logs_url'):
        print(f"Build Logs: {response['build_logs_url']}")
    
    return response

def get_build_logs(args):
    """Get build logs for an instance"""
    response = make_api_request("GET", f"{args.api_url}/instances/{args.instance_id}/build-logs")
    
    print(f"Build logs for instance {response['instance_id']}:")
    print(f"Status: {response['status']}")
    print("\nLogs:")
    print("-" * 80)
    if response.get('logs'):
        print(response['logs'])
    else:
        print("No logs available yet.")
    print("-" * 80)
    
    return response

def get_build_status(args):
    """Get build status for an instance"""
    response = make_api_request("GET", f"{args.api_url}/instances/{args.instance_id}/build-status")
    
    print(f"Build status for instance {args.instance_id}:")
    print(f"Status: {response['status']}")
    if response.get('error'):
        print(f"Error: {response['error']}")
    
    return response

def delete_instance(args):
    """Delete a VS Code Server instance"""
    response = make_api_request("DELETE", f"{args.api_url}/instances/{args.instance_id}")
    
    print(f"VS Code Server instance {args.instance_id} has been deleted")
    
    return response

def main():
    """Main function"""
    args = parse_args()
    
    if args.command == "create-simple":
        create_simple_instance(args)
    elif args.command == "create-devcontainer":
        create_devcontainer_instance(args)
    elif args.command == "create-workspace":
        create_workspace_instance(args)
    elif args.command == "get":
        get_instance(args)
    elif args.command == "build-logs":
        get_build_logs(args)
    elif args.command == "build-status":
        get_build_status(args)
    elif args.command == "delete":
        delete_instance(args)
    else:
        print("Please specify a command. Use --help for more information.")
        sys.exit(1)

if __name__ == "__main__":
    main()