import os
import json
from typing import List, Dict, Optional


def discover_handlers(base_path: str = "handlers") -> List[str]:
    """
    Discover all available handlers by looking for directories containing {folder}_handler.json
    
    Args:
        base_path: Base directory to search for handlers (default: "handlers")
        
    Returns:
        List of handler names
    """
    handlers = []
    
    if not os.path.exists(base_path):
        return handlers
        
    for item in os.listdir(base_path):
        handler_dir = os.path.join(base_path, item)
        if os.path.isdir(handler_dir):
            # Look for {folder}_handler.json pattern
            handler_json = os.path.join(handler_dir, f"{item}_handler.json")
            if os.path.exists(handler_json):
                handlers.append(item)
                
    return sorted(handlers)


def get_handler_commands(handler_name: str, base_path: str = "handlers") -> List[str]:
    """
    Get all available commands for a specific handler
    
    Args:
        handler_name: Name of the handler
        base_path: Base directory containing handlers
        
    Returns:
        List of command names (without .py extension)
    """
    commands = []
    handler_dir = os.path.join(base_path, handler_name)
    
    if not os.path.exists(handler_dir):
        return commands
        
    for file in os.listdir(handler_dir):
        if file.endswith(".py") and file not in ["__init__.py", "projector.py"]:
            commands.append(file[:-3])  # Remove .py extension
            
    return sorted(commands)


def load_handler_config(handler_name: str, base_path: str = "handlers") -> Optional[Dict]:
    """
    Load handler configuration from {handler_name}_handler.json
    
    Args:
        handler_name: Name of the handler
        base_path: Base directory containing handlers
        
    Returns:
        Handler configuration dict or None if not found
    """
    handler_json_path = os.path.join(base_path, handler_name, f"{handler_name}_handler.json")
    
    if not os.path.exists(handler_json_path):
        return None
        
    try:
        with open(handler_json_path, 'r') as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError):
        return None


def build_handler_map(base_path: str = "handlers") -> Dict[str, str]:
    """
    Build a mapping of event types to handler names based on directory names
    
    Args:
        base_path: Base directory containing handlers
        
    Returns:
        Dictionary mapping event types to handler names
    """
    handler_map = {}
    
    # Map directory names directly to event types
    for handler_name in discover_handlers(base_path):
        # The directory name IS the event type
        handler_map[handler_name] = handler_name
            
    return handler_map


def get_handler_path(handler_name: str, command: str, base_path: str = "handlers") -> Optional[str]:
    """
    Get the full path to a handler command module
    
    Args:
        handler_name: Name of the handler
        command: Command name
        base_path: Base directory containing handlers
        
    Returns:
        Full path to the command module or None if not found
    """
    module_path = os.path.join(base_path, handler_name, f"{command}.py")
    
    if os.path.exists(module_path):
        return module_path
        
    return None


def get_handler_schema(handler_name: str, base_path: str = "handlers") -> Optional[Dict]:
    """
    Get the event schema for a handler from its {handler_name}_handler.json
    
    Args:
        handler_name: Name of the handler
        base_path: Base directory containing handlers
        
    Returns:
        Event schema dict or None if not found
    """
    config = load_handler_config(handler_name, base_path)
    if config and "schema" in config:
        return config["schema"]
    return None