import json
import os
from typing import Any, Dict, Optional
from core.handler_discovery import load_handler_config, get_handler_schema

def validate_against_schema(data: Any, schema: Dict[str, Any]) -> tuple[bool, Optional[str]]:
    """
    Simple JSON schema validator for the framework.
    Returns (is_valid, error_message)
    """
    try:
        # Basic type validation
        if "type" in schema:
            expected_type = schema["type"]
            if expected_type == "object" and not isinstance(data, dict):
                return False, f"Expected object, got {type(data).__name__}"
            elif expected_type == "string" and not isinstance(data, str):
                return False, f"Expected string, got {type(data).__name__}"
            elif expected_type == "array" and not isinstance(data, list):
                return False, f"Expected array, got {type(data).__name__}"
            elif expected_type == "boolean" and not isinstance(data, bool):
                return False, f"Expected boolean, got {type(data).__name__}"
            elif expected_type == "number" and not isinstance(data, (int, float)):
                return False, f"Expected number, got {type(data).__name__}"
        
        # Object property validation
        if schema.get("type") == "object" and isinstance(data, dict):
            # Check required properties
            required = schema.get("required", [])
            for prop in required:
                if prop not in data:
                    return False, f"Missing required property: {prop}"
            
            # Validate properties
            properties = schema.get("properties", {})
            for key, value in data.items():
                if key in properties:
                    is_valid, error = validate_against_schema(value, properties[key])
                    if not is_valid:
                        return False, f"Property '{key}': {error}"
                elif schema.get("additionalProperties") is False:
                    return False, f"Additional property not allowed: {key}"
            
            # Validate property schemas
            for prop, prop_schema in properties.items():
                if prop in data:
                    # Check minLength for strings
                    if prop_schema.get("type") == "string" and "minLength" in prop_schema:
                        if len(data[prop]) < prop_schema["minLength"]:
                            return False, f"Property '{prop}' must have at least {prop_schema['minLength']} characters"
                    
                    # Check pattern for strings
                    if prop_schema.get("type") == "string" and "pattern" in prop_schema:
                        import re
                        if not re.match(prop_schema["pattern"], data[prop]):
                            return False, f"Property '{prop}' does not match pattern: {prop_schema['pattern']}"
                    
                    # Check const values
                    if "const" in prop_schema and data[prop] != prop_schema["const"]:
                        return False, f"Property '{prop}' must be '{prop_schema['const']}'"
        
        # Array validation
        if schema.get("type") == "array" and isinstance(data, list):
            if "minItems" in schema and len(data) < schema["minItems"]:
                return False, f"Array must have at least {schema['minItems']} items"
            if "maxItems" in schema and len(data) > schema["maxItems"]:
                return False, f"Array must have at most {schema['maxItems']} items"
            
            # Validate items
            if "items" in schema:
                for i, item in enumerate(data):
                    is_valid, error = validate_against_schema(item, schema["items"])
                    if not is_valid:
                        return False, f"Item {i}: {error}"
        
        return True, None
        
    except Exception as e:
        return False, f"Validation error: {str(e)}"


def load_schema(schema_ref: Any, base_path: str) -> Optional[Dict[str, Any]]:
    """
    Load a schema, resolving $ref if needed.
    """
    if isinstance(schema_ref, dict):
        if "$ref" in schema_ref:
            # Resolve the reference
            ref_path = os.path.join(base_path, schema_ref["$ref"])
            if os.path.exists(ref_path):
                with open(ref_path, 'r') as f:
                    return json.load(f)
        else:
            # It's an inline schema
            return schema_ref
    return None


def validate_command_input(handler_name: str, command_name: str, params: Dict[str, Any]) -> tuple[bool, Optional[str]]:
    """
    Validate command input against its schema if defined.
    """
    # Get handler base path (for tests vs production)
    handler_base = os.environ.get("HANDLER_PATH", "handlers")
    handler_dir = f"{handler_base}/{handler_name}"
    
    # Load handler config
    config = load_handler_config(handler_name, handler_base)
    if not config:
        return True, None  # No config, skip validation
    
    # Check if command has input schema
    command_config = config.get("commands", {}).get(command_name, {})
    if "input" in command_config:
        schema = load_schema(command_config["input"], handler_dir)
        if schema:
            return validate_against_schema(params, schema)
    
    return True, None  # No schema defined, skip validation


def validate_command_output(handler_name: str, command_name: str, output: Any) -> tuple[bool, Optional[str]]:
    """
    Validate command output against its schema if defined.
    """
    # Get handler base path (for tests vs production)
    handler_base = os.environ.get("HANDLER_PATH", "handlers")
    handler_dir = f"{handler_base}/{handler_name}"
    
    # Load handler config
    config = load_handler_config(handler_name, handler_base)
    if not config:
        return True, None  # No config, skip validation
    
    # Check if command has output schema
    command_config = config.get("commands", {}).get(command_name, {})
    if "output" in command_config:
        schema = load_schema(command_config["output"], handler_dir)
        if schema:
            return validate_against_schema(output, schema)
    
    return True, None  # No schema defined, skip validation


def validate_event(event_type: str, event_data: Dict[str, Any]) -> tuple[bool, Optional[str]]:
    """
    Validate an event against its handler's schema if defined.
    
    Args:
        event_type: Type of the event (matches handler directory name)
        event_data: Event data to validate
        
    Returns:
        Tuple of (is_valid, error_message)
    """
    # Get handler base path (for tests vs production)
    handler_base = os.environ.get("HANDLER_PATH", "handlers")
    
    # Get the schema for this event type
    schema = get_handler_schema(event_type, handler_base)
    if not schema:
        return True, None  # No schema defined, skip validation
        
    return validate_against_schema(event_data, schema)