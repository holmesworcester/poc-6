#!/usr/bin/env python3
"""
Schema validator that checks handler test data against SQL schema definitions.
This will eventually be integrated into the test runner.
"""

import json
import os
import re
import sys
from typing import Dict, List, Set, Tuple, Any
from collections import defaultdict
from pathlib import Path


class SQLSchemaParser:
    """Parse SQL CREATE TABLE statements to extract schema information."""
    
    def __init__(self, schema_file: str):
        self.schema_file = schema_file
        self.tables = {}
        self._parse_schema()
    
    def _parse_schema(self):
        """Parse SQL file to extract table and column information."""
        with open(self.schema_file, 'r') as f:
            content = f.read()
        
        # Remove comments
        content = re.sub(r'--.*$', '', content, flags=re.MULTILINE)
        
        # Find all CREATE TABLE statements
        table_pattern = r'CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?(\w+)\s*\((.*?)\);'
        
        for match in re.finditer(table_pattern, content, re.IGNORECASE | re.DOTALL):
            table_name = match.group(1).lower()
            table_body = match.group(2)
            
            columns = self._parse_columns(table_body)
            self.tables[table_name] = columns
    
    def _parse_columns(self, table_body: str) -> Dict[str, Dict]:
        """Parse column definitions from CREATE TABLE body."""
        columns = {}
        
        # Split by comma but not within parentheses
        lines = self._split_table_body(table_body)
        
        for line in lines:
            line = line.strip()
            if not line:
                continue
                
            # Skip INDEX, FOREIGN KEY, etc.
            if any(line.upper().startswith(keyword) for keyword in 
                   ['INDEX', 'FOREIGN KEY', 'PRIMARY KEY', 'UNIQUE']):
                continue
            
            # Parse column definition
            parts = line.split()
            if len(parts) >= 2:
                col_name = parts[0].strip('`"')
                col_type = parts[1].upper()
                
                # Check for NOT NULL
                not_null = 'NOT NULL' in line.upper()
                primary_key = 'PRIMARY KEY' in line.upper()
                unique = 'UNIQUE' in line.upper()
                
                columns[col_name] = {
                    'type': col_type,
                    'not_null': not_null or primary_key,
                    'primary_key': primary_key,
                    'unique': unique
                }
        
        return columns
    
    def _split_table_body(self, body: str) -> List[str]:
        """Split table body by commas, respecting parentheses."""
        lines = []
        current = []
        paren_depth = 0
        
        for char in body:
            if char == '(':
                paren_depth += 1
            elif char == ')':
                paren_depth -= 1
            elif char == ',' and paren_depth == 0:
                lines.append(''.join(current))
                current = []
                continue
            current.append(char)
        
        if current:
            lines.append(''.join(current))
        
        return lines


class HandlerSchemaValidator:
    """Validate handler test data against SQL schema."""
    
    def __init__(self, schema_parser: SQLSchemaParser):
        self.schema = schema_parser
        self.errors = []
        self.warnings = []
        
        # Map handler state paths to SQL tables
        self.state_to_table_map = {
            'messages': 'messages',
            'identities': 'identities',
            'peers': 'peers',
            'known_senders': 'known_senders',
            'key_map': 'key_map',
            'pending_missing_key': 'pending_missing_key',
            'unknown_events': 'unknown_events',
            'outgoing': 'outgoing'
        }
        
        # Special handling for certain fields
        self.special_cases = {
            'known_senders': 'array_of_strings',  # Handler uses array of strings
            'test_placeholders': ['*', '?', 'alice_pub', 'bob_pub']  # Test wildcards
        }
    
    def validate_handler(self, handler_path: str) -> Tuple[List[str], List[str]]:
        """Validate a handler's test data against the schema."""
        self.errors = []
        self.warnings = []
        
        try:
            with open(handler_path, 'r') as f:
                handler = json.load(f)
        except Exception as e:
            self.errors.append(f"Failed to load handler: {e}")
            return self.errors, self.warnings
        
        handler_type = handler.get('type', 'unknown')
        
        # Check projector tests
        if 'projector' in handler and 'tests' in handler['projector']:
            for i, test in enumerate(handler['projector']['tests']):
                self._validate_test(test, f"{handler_type}.projector.test[{i}]")
        
        # Check command tests
        for cmd_name, cmd_def in handler.get('commands', {}).items():
            for i, test in enumerate(cmd_def.get('tests', [])):
                self._validate_test(test, f"{handler_type}.{cmd_name}.test[{i}]")
        
        return self.errors, self.warnings
    
    def _validate_test(self, test: Dict, test_path: str):
        """Validate a single test's database operations."""
        # Check given state
        given_db = test.get('given', {}).get('db', {})
        if 'state' in given_db:
            self._validate_state(given_db['state'], f"{test_path}.given", read_only=True)
        
        # Check then state
        then = test.get('then', {})
        if 'db' in then:
            then_db = then['db']
            if 'state' in then_db:
                self._validate_state(then_db['state'], f"{test_path}.then", read_only=False)
            
            # Check outgoing queue
            if 'outgoing' in then_db:
                self._validate_outgoing(then_db['outgoing'], f"{test_path}.then")
        
        # Check return value for new events
        if 'return' in then:
            return_val = then['return']
            if isinstance(return_val, dict):
                self._check_new_events(return_val, f"{test_path}.then.return")
    
    def _validate_state(self, state: Dict, path: str, read_only: bool):
        """Validate state object against schema."""
        for key, value in state.items():
            if key in self.state_to_table_map:
                table_name = self.state_to_table_map[key]
                
                # Special case: known_senders is array of strings in handlers
                if key == 'known_senders' and isinstance(value, list):
                    for i, sender in enumerate(value):
                        if not isinstance(sender, str):
                            self.errors.append(f"{path}.{key}[{i}]: Expected string")
                    continue
                
                if table_name in self.schema.tables:
                    if isinstance(value, list):
                        for i, item in enumerate(value):
                            self._validate_record(item, table_name, f"{path}.{key}[{i}]")
                    elif isinstance(value, dict) and key != 'key_map':
                        # For nested identity structures
                        for sub_key, sub_value in value.items():
                            self._validate_record(sub_value, table_name, f"{path}.{key}.{sub_key}")
                    elif key == 'key_map':
                        # Special handling for key_map
                        self._validate_key_map(value, f"{path}.{key}")
                else:
                    self.warnings.append(f"{path}: Table '{table_name}' not found in schema")
            elif key == 'eventStore':
                # Event store is handled differently
                self._validate_event_store(value, f"{path}.{key}")
            else:
                self.warnings.append(f"{path}: Unknown state key '{key}'")
    
    def _validate_record(self, record: Dict, table_name: str, path: str):
        """Validate a single record against table schema."""
        if not isinstance(record, dict):
            self.errors.append(f"{path}: Expected dict, got {type(record).__name__}")
            return
        
        table_schema = self.schema.tables[table_name]
        
        # Check for test wildcards
        has_wildcards = any(v == '*' for v in record.values() if isinstance(v, str))
        if has_wildcards:
            # Skip detailed validation for test placeholders
            return
        
        # Check for required fields (NOT NULL columns)
        for col_name, col_def in table_schema.items():
            if col_def['not_null'] and col_name not in ['id', 'created_at', 'updated_at', 'event_id']:
                # Special handling for certain fields
                if table_name == 'messages' and col_name == 'event_id':
                    # event_id might be in metadata or generated
                    continue
                if table_name == 'identities' and col_name in ['created_at', 'updated_at']:
                    # Timestamps might be auto-generated
                    continue
                
                # Check field presence
                if col_name not in record:
                    # Check for alternative field names
                    if table_name == 'identities' and col_name == 'privkey':
                        if 'keypair' in record and 'private' in record['keypair']:
                            continue
                    elif table_name == 'identities' and col_name == 'pubkey':
                        if 'keypair' in record and 'public' in record['keypair']:
                            continue
                    
                    # Don't error on missing timestamp/sig in test data
                    if col_name in ['timestamp', 'sig', 'signature'] and path.endswith(']'):
                        continue
                    
                    # Don't error on metadata for unknown_events if data is present
                    if table_name == 'unknown_events' and col_name == 'metadata' and 'data' in record:
                        continue
                    
                    # Don't error on auto-generated fields in test data
                    if col_name in ['added_at', 'created_at', 'updated_at'] and 'test' in path:
                        continue
                        
                    self.errors.append(f"{path}: Missing required field '{col_name}'")
        
        # Check for unknown fields
        for field_name in record.keys():
            if field_name not in table_schema:
                # Special cases
                if table_name == 'identities' and field_name == 'keypair':
                    # Validate keypair structure
                    if isinstance(record['keypair'], dict):
                        if 'public' not in record['keypair'] or 'private' not in record['keypair']:
                            self.errors.append(f"{path}.keypair: Missing public or private key")
                    continue
                if field_name in ['id', 'event_id', 'created_at', 'updated_at']:
                    # These might be auto-generated
                    continue
                # Event type field is used for routing but not stored in messages table
                if table_name == 'messages' and field_name == 'type':
                    continue
                # Test-specific fields
                if field_name == '*':
                    continue
                
                self.warnings.append(f"{path}: Unknown field '{field_name}' for table '{table_name}'")
    
    def _validate_key_map(self, key_map: Dict, path: str):
        """Validate key_map structure."""
        for key_hash, key_value in key_map.items():
            # Skip validation for obvious test data
            if key_value in ['outerKey', 'innerKey', 'validKey', 'key12345']:
                continue
                
            # Check key hash format (64 char hex) - but be lenient for test data
            if len(key_hash) == 64 and not re.match(r'^[a-fA-F0-9]{64}$', key_hash):
                self.warnings.append(f"{path}.{key_hash}: Invalid key hash format")
            elif len(key_hash) > 64:
                self.warnings.append(f"{path}.{key_hash}: Key hash too long")
            
            # Check key value format
            if not isinstance(key_value, str):
                self.errors.append(f"{path}.{key_hash}: Key value must be string")
    
    def _validate_event_store(self, event_store: Dict, path: str):
        """Validate event store structure."""
        for pubkey, events in event_store.items():
            if not isinstance(events, list):
                self.errors.append(f"{path}.{pubkey}: Expected list of events")
                continue
            
            for i, event in enumerate(events):
                if not isinstance(event, dict):
                    self.errors.append(f"{path}.{pubkey}[{i}]: Expected dict")
    
    def _validate_outgoing(self, outgoing: List, path: str):
        """Validate outgoing queue entries."""
        if not isinstance(outgoing, list):
            self.errors.append(f"{path}.outgoing: Expected list")
            return
        
        for i, entry in enumerate(outgoing):
            if isinstance(entry, dict):
                # Validate against outgoing table schema
                if 'outgoing' in self.schema.tables:
                    self._validate_record(entry, 'outgoing', f"{path}.outgoing[{i}]")
            else:
                # Some handlers put raw data in outgoing
                self.warnings.append(f"{path}.outgoing[{i}]: Raw data in outgoing queue")
    
    def _check_new_events(self, return_val: Dict, path: str):
        """Check new events in command return values."""
        for key in ['newEvents', 'new_events', 'newlyCreatedEvents']:
            if key in return_val:
                events = return_val[key]
                if isinstance(events, list):
                    for i, event in enumerate(events):
                        if isinstance(event, dict) and 'type' in event:
                            # Validate event structure based on type
                            event_type = event['type']
                            if event_type == 'message':
                                self._validate_record(event, 'messages', f"{path}.{key}[{i}]")


def validate_protocol(protocol_dir: str):
    """Validate all handlers in a protocol against its schema."""
    schema_file = os.path.join(protocol_dir, 'schema.sql')
    
    if not os.path.exists(schema_file):
        print(f"ERROR: No schema.sql found in {protocol_dir}")
        return False
    
    print(f"\nValidating protocol: {protocol_dir}")
    print(f"Using schema: {schema_file}")
    
    # Parse schema
    try:
        schema_parser = SQLSchemaParser(schema_file)
        print(f"Found {len(schema_parser.tables)} tables in schema")
    except Exception as e:
        print(f"ERROR: Failed to parse schema: {e}")
        return False
    
    # Find all handlers
    handlers_dir = os.path.join(protocol_dir, 'handlers')
    if not os.path.exists(handlers_dir):
        print(f"ERROR: No handlers directory found")
        return False
    
    # Validate each handler
    validator = HandlerSchemaValidator(schema_parser)
    all_errors = []
    all_warnings = []
    
    for root, dirs, files in os.walk(handlers_dir):
        # Look for {folder}_handler.json pattern
        handler_name = os.path.basename(root)
        handler_json_name = f"{handler_name}_handler.json"
        if handler_json_name in files:
            handler_path = os.path.join(root, handler_json_name)
            
            print(f"\nChecking handler: {handler_name}")
            errors, warnings = validator.validate_handler(handler_path)
            
            if errors:
                print(f"  ERRORS: {len(errors)}")
                for error in errors:
                    print(f"    - {error}")
                all_errors.extend(errors)
            
            if warnings:
                print(f"  WARNINGS: {len(warnings)}")
                for warning in warnings:
                    print(f"    - {warning}")
                all_warnings.extend(warnings)
            
            if not errors and not warnings:
                print("  ✓ All checks passed")
    
    # Summary
    print(f"\n{'='*60}")
    print(f"SUMMARY for {protocol_dir}:")
    print(f"  Total errors: {len(all_errors)}")
    print(f"  Total warnings: {len(all_warnings)}")
    
    return len(all_errors) == 0


def main():
    """Main entry point."""
    # Dynamically discover all protocols
    protocols = []
    protocols_dir = 'protocols'
    
    if os.path.exists(protocols_dir):
        for item in sorted(os.listdir(protocols_dir)):
            protocol_path = os.path.join(protocols_dir, item)
            if os.path.isdir(protocol_path):
                # Check if it has a handlers directory to confirm it's a protocol
                handlers_path = os.path.join(protocol_path, 'handlers')
                if os.path.exists(handlers_path) and os.path.isdir(handlers_path):
                    protocols.append(protocol_path)
    
    if not protocols:
        print("WARNING: No protocols found in protocols/ directory")
        return
    
    print(f"Discovered protocols: {protocols}")
    
    all_passed = True
    for protocol in protocols:
        passed = validate_protocol(protocol)
        all_passed = all_passed and passed
    
    if not all_passed:
        sys.exit(1)
    else:
        print("\nAll schema validations passed!")


if __name__ == '__main__':
    main()