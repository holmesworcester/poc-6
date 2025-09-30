"""
API Client wrapper for protocols.

This provides a clean REST-like interface to protocol APIs without HTTP networking.
It uses execute_api internally for local inter-process communication.
"""

from .api import execute_api


class APIClient:
    """Simple API client for protocols using direct function calls."""
    
    def __init__(self, protocol):
        """
        Initialize API client for a specific protocol.
        
        Args:
            protocol: Protocol name (e.g., "signed_groups", "message_via_tor")
        """
        self.protocol = protocol
    
    def get(self, endpoint, params=None):
        """
        Make a GET request.
        
        Args:
            endpoint: API endpoint path (e.g., "/identities")
            params: Optional query parameters dict
            
        Returns:
            Response dict with 'status' and 'body' keys
        """
        return execute_api(self.protocol, "GET", endpoint, params or {}, {})
    
    def post(self, endpoint, data=None):
        """
        Make a POST request.
        
        Args:
            endpoint: API endpoint path (e.g., "/identities")
            data: Request body data dict
            
        Returns:
            Response dict with 'status' and 'body' keys
        """
        return execute_api(self.protocol, "POST", endpoint, {}, data or {})
    
    def put(self, endpoint, data=None):
        """
        Make a PUT request.
        
        Args:
            endpoint: API endpoint path (e.g., "/identities/123")
            data: Request body data dict
            
        Returns:
            Response dict with 'status' and 'body' keys
        """
        return execute_api(self.protocol, "PUT", endpoint, {}, data or {})
    
    def delete(self, endpoint, params=None):
        """
        Make a DELETE request.
        
        Args:
            endpoint: API endpoint path (e.g., "/identities/123")
            params: Optional query parameters dict
            
        Returns:
            Response dict with 'status' and 'body' keys
        """
        return execute_api(self.protocol, "DELETE", endpoint, params or {}, {})
    
    def patch(self, endpoint, data=None):
        """
        Make a PATCH request.
        
        Args:
            endpoint: API endpoint path (e.g., "/identities/123")
            data: Request body data dict
            
        Returns:
            Response dict with 'status' and 'body' keys
        """
        return execute_api(self.protocol, "PATCH", endpoint, {}, data or {})