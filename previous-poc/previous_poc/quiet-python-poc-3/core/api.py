#!/usr/bin/env python3
"""
Minimal HTTP-like API executor.

Maps OpenAPI operationIds to handler commands and executes them using the
framework command runner. Designed for local tooling and tests.
"""

import sys
import os
import json
import yaml
import argparse
import re
from contextlib import contextmanager
from pathlib import Path
from core.command import run_command

def load_yaml(filepath):
    with open(filepath, 'r') as f:
        return yaml.safe_load(f)

def match_path_to_operation(api_spec, method, request_path):
    """Return (spec_path, operation, path_params) for a method and path."""
    method = method.lower()
    
    for spec_path, path_item in api_spec.get("paths", {}).items():
        if method not in path_item:
            continue
            
        # Convert OpenAPI path to regex by replacing {param} with named groups
        pattern = spec_path
        param_names = re.findall(r'\{([^}]+)\}', spec_path)
        for param_name in param_names:
            pattern = pattern.replace(f"{{{param_name}}}", f"(?P<{param_name}>[^/]+)")
        
        # Add start and end anchors
        pattern = f"^{pattern}$"
        
        # Try to match
        match = re.match(pattern, request_path)
        if match:
            path_params = match.groupdict()
            return spec_path, path_item[method], path_params
    
    return None, None, None

def extract_handler_command(operation_id):
    """Extract (handler, command) from operationId like 'handler.command'."""
    if '.' not in operation_id:
        raise ValueError(f"Invalid operationId format: {operation_id}")
    
    parts = operation_id.split('.', 1)
    return parts[0], parts[1]

def prepare_command_input(operation, path_params, query_params, body_data):
    """Merge path params, query params and body into a single dict."""
    input_data = {}
    
    # Add path parameters
    if path_params:
        input_data.update(path_params)
    
    # Add query parameters
    if query_params:
        for key, value in query_params.items():
            if isinstance(value, list):
                input_data[key] = value[0] if value else None
            else:
                input_data[key] = value
    
    # Add body data
    if body_data:
        input_data.update(body_data)
    
    return input_data

def format_response(result, method, status_code=200):
    """Format a command result into a simple HTTP-like response dict."""
    response = {
        "status": status_code,
        "headers": {
            "Content-Type": "application/json"
        }
    }
    
    # Check if handler uses new api_response convention
    if isinstance(result, dict) and 'api_response' in result:
        # Use the explicit api_response
        response["body"] = result['api_response']
        
        # Include newEvents if they exist
        if 'newEvents' in result:
            response["body"]['newEvents'] = result['newEvents']
    elif isinstance(result, dict):
        # Remove internal fields from response but keep newEvents
        body = {k: v for k, v in result.items() if k not in ['db', 'newlyCreatedEvents']}
        
        # If handler wants to format response, let it do so
        # Otherwise return the cleaned result as-is
        response["body"] = body if body else result
    else:
        response["body"] = result
    
    return response

@contextmanager
def _temp_env(**pairs):
    """Temporarily set environment variables inside a context."""
    old = {}
    for k, v in pairs.items():
        old[k] = os.environ.get(k)
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = str(v)
    try:
        yield
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def execute_api(protocol_name, method, path, data=None, params=None):
    """Execute an API request against a protocol using its api.yaml mapping."""
    protocol_path = Path("protocols") / protocol_name
    
    # Check if protocol exists
    if not protocol_path.exists():
        return {
            "status": 404,
            "body": {"error": f"Protocol '{protocol_name}' not found"}
        }
    
    # Check if api.yaml exists
    api_yaml_path = protocol_path / "api.yaml"
    if not api_yaml_path.exists():
        return {
            "status": 404,
            "body": {"error": f"No API defined for protocol '{protocol_name}'"}
        }
    
    # Load API specification
    try:
        api_spec = load_yaml(api_yaml_path)
    except Exception as e:
        return {"status": 500, "body": {"error": f"Failed to parse api.yaml: {e}"}}
    
    # Match path to operation
    spec_path, operation, path_params = match_path_to_operation(api_spec, method, path)
    
    if not operation:
        return {
            "status": 404,
            "body": {"error": f"No operation found for {method} {path}"}
        }
    
    # Get operationId
    operation_id = operation.get("operationId")
    if not operation_id:
        return {
            "status": 500,
            "body": {"error": f"No operationId defined for {method} {spec_path}"}
        }
    
    # Special handling for tick endpoint
    if operation_id == "tick.run":
        try:
            from core.db import create_db
            from core.tick import tick as run_tick

            db_path = os.environ.get('API_DB_PATH', 'api.db')
            # Use protocol handlers and honor existing CRYPTO_MODE; default to real
            crypto_mode = os.environ.get("CRYPTO_MODE") or "real"
            with _temp_env(HANDLER_PATH=str(protocol_path / "handlers"), CRYPTO_MODE=crypto_mode):
                db = create_db(db_path=db_path, protocol_name=protocol_name)

                # SQL-only: do not accept dict-db injection via API

                time_now_ms = (data or {}).get("time_now_ms")
                run_tick(db, time_now_ms=time_now_ms)

            return {"status": 200, "headers": {"Content-Type": "application/json"}, "body": {"jobsRun": 5, "eventsProcessed": 0}}
        except Exception as e:
            return {"status": 500, "body": {"error": f"Tick execution failed: {e}"}}
    
    # Extract handler and command for regular operations
    try:
        handler_name, command_name = extract_handler_command(operation_id)
    except ValueError as e:
        return {
            "status": 500,
            "body": {"error": str(e)}
        }
    
    # Prepare command input
    input_data = prepare_command_input(operation, path_params, params, data)
    try:
        from core.db import create_db
        db_path = os.environ.get('API_DB_PATH', 'api.db')
        # Use protocol handlers and honor existing CRYPTO_MODE; default to real
        crypto_mode = os.environ.get("CRYPTO_MODE") or "real"
        with _temp_env(HANDLER_PATH=str(protocol_path / "handlers"), CRYPTO_MODE=crypto_mode):
            db = create_db(db_path=db_path, protocol_name=protocol_name)

            # SQL-only: do not accept dict-db injection via API

            db, result = run_command(handler_name, command_name, input_data, db)

        status_code = 201 if method.upper() == "POST" else 200
        return format_response(result, method, status_code)
    except Exception as e:
        # Check if the underlying exception is a ValueError (validation error)
        # These should return 400 (Bad Request) instead of 500
        error_msg = str(e)
        if "ValueError" in error_msg or (hasattr(e, '__cause__') and isinstance(e.__cause__, ValueError)):
            return {"status": 400, "body": {"error": f"Bad request: {error_msg}"}}
        return {"status": 500, "body": {"error": f"Command execution failed: {e}"}}

def main():
    parser = argparse.ArgumentParser(description="Execute API requests against a protocol")
    parser.add_argument("protocol", help="Protocol name")
    parser.add_argument("method", help="HTTP method", 
                       choices=["GET", "POST", "PUT", "DELETE", "PATCH"])
    parser.add_argument("path", help="Request path (e.g., /messages)")
    parser.add_argument("--data", help="Request body as JSON string")
    parser.add_argument("--params", help="Query parameters as JSON string")
    
    args = parser.parse_args()
    
    # Parse JSON data
    data = None
    if args.data:
        try:
            data = json.loads(args.data)
        except json.JSONDecodeError as e:
            print(f"Error: Invalid JSON in --data: {e}")
            sys.exit(1)
    
    # Parse JSON params
    params = None
    if args.params:
        try:
            params = json.loads(args.params)
        except json.JSONDecodeError as e:
            print(f"Error: Invalid JSON in --params: {e}")
            sys.exit(1)
    
    # Execute API request
    response = execute_api(
        args.protocol,
        args.method,
        args.path,
        data=data,
        params=params
    )
    
    # Print response
    print(f"HTTP {response['status']}")
    if 'headers' in response:
        for key, value in response['headers'].items():
            print(f"{key}: {value}")
    print()
    
    if 'body' in response:
        print(json.dumps(response['body'], indent=2))
    
    # Exit with error code if not successful
    if response['status'] >= 400:
        sys.exit(1)

if __name__ == "__main__":
    main()
