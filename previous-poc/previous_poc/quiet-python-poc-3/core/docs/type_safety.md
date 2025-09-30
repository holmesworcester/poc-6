# Type Safety in Event-Driven Systems

Our event-driven system uses JSON schemas to define the structure of events and commands. By extracting type information from these schemas, we can generate type definitions in multiple languages and reap the benefits. This document describes how to extract type information from handler schemas and use it to create type-safe interfaces in Python, Rust, and TypeScript.

## Schema Structure

Handler schemas are defined in `handler.json` files.

## Checking the API

An LLM can generate an API for us, but it's helpful to clamp it to the types in the actual commands, and have this be enforced by the IDE or our test_runner. Spefically, we should ensure that the types in the api spec match the types of params and api_response in the commands that correspond to the operation id in the API.

Possible flow:

- Use Pydantic models to validate API responses against OpenAPI YAML spec
- Create type-safe wrapper for execute_api that validates responses at runtime
- Add mypy static type checking to catch type errors at development time
- Generate API spec from handler command schemas that include api_response field
- Ensure test_runner validates that actual API responses match expected schemas
- Support compile-time type safety for better IDE support and early error detection 

## Python Implementation

### 1. Generate TypedDict from Schema

```python
from typing import TypedDict, Optional, Literal, List, Union
from typing_extensions import NotRequired  # Python 3.11+

# Generated from message handler schema
class MessageEvent(TypedDict):
    type: Literal["message"]
    sender: str
    text: str
    timestamp: str
    replyTo: NotRequired[str]  # Optional field

class MessageCreateInput(TypedDict):
    text: str
    replyTo: NotRequired[str]

class MessageCreateOutput(TypedDict):
    newlyCreatedEvents: List[MessageEvent]

# Envelope types
class VerifiedEnvelope(TypedDict):
    envelope: Literal["verified"]
    payload: MessageEvent
    metadata: dict
```

### 2. Type-Safe Handler Implementation

```python
# handlers/message/projector.py
from typing import Dict, Any, Optional
from .types import MessageEvent, VerifiedEnvelope

def project(envelope: VerifiedEnvelope, state: Dict[str, Any], current_identity: str) -> Optional[bool]:
    """Type-safe projector with IDE support."""
    payload: MessageEvent = envelope["payload"]
    
    # IDE knows payload has 'sender', 'text', etc.
    sender = payload["sender"]
    text = payload["text"]
    
    # Type checker ensures required fields are accessed
    message = {
        "text": text,
        "sender": sender,
        "timestamp": payload["timestamp"],
        "replyTo": payload.get("replyTo")  # Optional field
    }
    
    state.setdefault("messages", []).append(message)
    return True
```

### 3. Schema to Type Generator

```python
# generate_types.py
import json
from pathlib import Path
from typing import Dict, Any

def json_schema_to_typed_dict(schema: Dict[str, Any], name: str) -> str:
    """Convert JSON schema to TypedDict definition."""
    imports = ["from typing import TypedDict, Optional, Literal, List, Union\n"]
    
    # Build property types
    properties = []
    required = set(schema.get("required", []))
    
    for prop, prop_schema in schema.get("properties", {}).items():
        prop_type = get_python_type(prop_schema)
        if prop not in required:
            prop_type = f"NotRequired[{prop_type}]"
        properties.append(f"    {prop}: {prop_type}")
    
    return f"class {name}(TypedDict):\n" + "\n".join(properties)

def get_python_type(schema: Dict[str, Any]) -> str:
    """Map JSON schema type to Python type."""
    if "const" in schema:
        return f'Literal["{schema["const"]}"]'
    
    type_map = {
        "string": "str",
        "number": "float",
        "integer": "int",
        "boolean": "bool",
        "array": "List[Any]",  # Would need item type
        "object": "Dict[str, Any]"
    }
    
    return type_map.get(schema.get("type", "Any"), "Any")

# Generate types for all handlers
def generate_all_types():
    for handler_path in Path("handlers").glob("*/handler.json"):
        with open(handler_path) as f:
            config = json.load(f)
        
        handler_name = handler_path.parent.name
        output = generate_handler_types(config, handler_name)
        
        types_file = handler_path.parent / "types.py"
        types_file.write_text(output)
```

### 4. Runtime Validation with Types

```python
from typing import Type, TypeVar, cast
from .schema_validator import validate_event

T = TypeVar('T', bound=TypedDict)

def validate_and_cast(data: dict, event_type: Type[T]) -> T:
    """Validate data against schema and return typed result."""
    # Extract event type name from class
    type_name = event_type.__annotations__.get("type", {}).get("__args__", [None])[0]
    
    is_valid, error = validate_event(type_name, data)
    if not is_valid:
        raise ValueError(f"Validation failed: {error}")
    
    return cast(T, data)

# Usage
try:
    typed_event = validate_and_cast(raw_data, MessageEvent)
    # Now typed_event is fully typed with IDE support
    print(typed_event["sender"])  # IDE knows this field exists
except ValueError as e:
    print(f"Invalid event: {e}")
```

## Rust Implementation

### 1. Generate Rust Types from Schema

```rust
// Generated in src/handlers/message/types.rs
use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(tag = "type")]
pub struct MessageEvent {
    #[serde(rename = "type")]
    pub event_type: String,  // Always "message"
    pub sender: String,
    pub text: String,
    pub timestamp: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub reply_to: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct MessageCreateInput {
    pub text: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub reply_to: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct MessageCreateOutput {
    pub newly_created_events: Vec<MessageEvent>,
}

// Envelope types
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct VerifiedEnvelope<T> {
    pub envelope: String,  // "verified"
    pub payload: T,
    pub metadata: serde_json::Value,
}
```

### 2. Type-Safe Handler Implementation

```rust
// src/handlers/message/projector.rs
use crate::handlers::message::types::{MessageEvent, VerifiedEnvelope};
use std::collections::HashMap;
use serde_json::Value;

pub fn project(
    envelope: &VerifiedEnvelope<MessageEvent>,
    state: &mut HashMap<String, Value>,
    current_identity: &str,
) -> Result<(), Box<dyn std::error::Error>> {
    let payload = &envelope.payload;
    
    // Rust compiler ensures type safety
    let message = serde_json::json!({
        "text": payload.text,
        "sender": payload.sender,
        "timestamp": payload.timestamp,
        "replyTo": payload.reply_to,
    });
    
    // Update state
    let messages = state
        .entry("messages".to_string())
        .or_insert_with(|| Value::Array(vec![]));
    
    if let Value::Array(ref mut msgs) = messages {
        msgs.push(message);
    }
    
    Ok(())
}
```

### 3. Schema to Rust Type Generator

```rust
// build.rs or separate tool
use serde_json::Value;
use std::fs;
use std::path::Path;

fn json_schema_to_rust_struct(schema: &Value, name: &str) -> String {
    let mut fields = Vec::new();
    let properties = schema["properties"].as_object().unwrap();
    let required = schema["required"].as_array().unwrap_or(&vec![]);
    
    for (prop_name, prop_schema) in properties {
        let rust_name = to_snake_case(prop_name);
        let rust_type = get_rust_type(prop_schema);
        
        let is_required = required.iter()
            .any(|r| r.as_str() == Some(prop_name));
        
        let field_type = if is_required {
            rust_type
        } else {
            format!("Option<{}>", rust_type)
        };
        
        fields.push(format!(
            "    #[serde(rename = \"{}\")]\n    pub {}: {},",
            prop_name, rust_name, field_type
        ));
    }
    
    format!(
        "#[derive(Debug, Clone, Serialize, Deserialize)]\npub struct {} {{\n{}\n}}",
        name,
        fields.join("\n")
    )
}

fn get_rust_type(schema: &Value) -> String {
    match schema["type"].as_str() {
        Some("string") => "String".to_string(),
        Some("number") => "f64".to_string(),
        Some("integer") => "i64".to_string(),
        Some("boolean") => "bool".to_string(),
        Some("array") => "Vec<serde_json::Value>".to_string(),
        Some("object") => "serde_json::Value".to_string(),
        _ => "serde_json::Value".to_string(),
    }
}
```

### 4. Validation with Rust Types

```rust
use jsonschema::{Draft, JSONSchema};
use serde::de::DeserializeOwned;

pub fn validate_and_deserialize<T: DeserializeOwned>(
    data: &serde_json::Value,
    schema: &serde_json::Value,
) -> Result<T, Box<dyn std::error::Error>> {
    // Compile the schema
    let compiled = JSONSchema::options()
        .with_draft(Draft::Draft7)
        .compile(schema)?;
    
    // Validate
    if let Err(errors) = compiled.validate(data) {
        let error_messages: Vec<String> = errors
            .map(|e| e.to_string())
            .collect();
        return Err(format!("Validation failed: {}", error_messages.join(", ")).into());
    }
    
    // Deserialize to typed struct
    Ok(serde_json::from_value(data.clone())?)
}

// Usage
let typed_event: MessageEvent = validate_and_deserialize(&raw_data, &schema)?;
println!("Sender: {}", typed_event.sender);  // Compile-time type safety
```

## TypeScript Implementation

### 1. Generate TypeScript Types from Schema

```typescript
// Generated in handlers/message/types.ts
export interface MessageEvent {
  type: "message";
  sender: string;
  text: string;
  timestamp: string;
  replyTo?: string;
}

export interface MessageCreateInput {
  text: string;
  replyTo?: string;
}

export interface MessageCreateOutput {
  newlyCreatedEvents: MessageEvent[];
}

// Envelope types
export interface VerifiedEnvelope<T> {
  envelope: "verified";
  payload: T;
  metadata: Record<string, unknown>;
}

// Type guards
export function isMessageEvent(event: unknown): event is MessageEvent {
  return (
    typeof event === "object" &&
    event !== null &&
    "type" in event &&
    event.type === "message" &&
    "sender" in event &&
    typeof event.sender === "string" &&
    "text" in event &&
    typeof event.text === "string"
  );
}
```

### 2. Type-Safe Handler Implementation

```typescript
// handlers/message/projector.ts
import { MessageEvent, VerifiedEnvelope } from "./types";

interface State {
  messages: Array<{
    text: string;
    sender: string;
    timestamp: string;
    replyTo?: string;
  }>;
  [key: string]: unknown;
}

export function project(
  envelope: VerifiedEnvelope<MessageEvent>,
  state: State,
  currentIdentity: string
): boolean | void {
  const { payload } = envelope;
  
  // TypeScript ensures type safety
  const message = {
    text: payload.text,
    sender: payload.sender,
    timestamp: payload.timestamp,
    ...(payload.replyTo && { replyTo: payload.replyTo })
  };
  
  if (!state.messages) {
    state.messages = [];
  }
  
  state.messages.push(message);
  return true;
}
```

### 3. Schema to TypeScript Generator

```typescript
// generateTypes.ts
import { compile } from "json-schema-to-typescript";
import * as fs from "fs/promises";
import * as path from "path";
import { glob } from "glob";

async function generateHandlerTypes() {
  const handlerFiles = await glob("handlers/*/handler.json");
  
  for (const handlerFile of handlerFiles) {
    const config = JSON.parse(await fs.readFile(handlerFile, "utf-8"));
    const handlerName = path.basename(path.dirname(handlerFile));
    
    let output = "// Auto-generated types from schema\n\n";
    
    // Generate event type
    if (config.schema) {
      const eventType = await compile(config.schema, toPascalCase(handlerName) + "Event");
      output += eventType + "\n";
    }
    
    // Generate command types
    for (const [cmdName, cmdConfig] of Object.entries(config.commands || {})) {
      if (cmdConfig.input) {
        const inputType = await compile(
          cmdConfig.input,
          toPascalCase(handlerName) + toPascalCase(cmdName) + "Input"
        );
        output += inputType + "\n";
      }
      
      if (cmdConfig.output) {
        const outputType = await compile(
          cmdConfig.output,
          toPascalCase(handlerName) + toPascalCase(cmdName) + "Output"
        );
        output += outputType + "\n";
      }
    }
    
    // Add type guards
    output += generateTypeGuards(config, handlerName);
    
    await fs.writeFile(
      path.join(path.dirname(handlerFile), "types.ts"),
      output
    );
  }
}

function generateTypeGuards(config: any, handlerName: string): string {
  const eventName = toPascalCase(handlerName) + "Event";
  const requiredFields = config.schema?.required || [];
  
  const checks = requiredFields.map(field => {
    const fieldType = config.schema.properties[field]?.type;
    return `    "${field}" in event && typeof event.${field} === "${fieldType}"`;
  }).join(" &&\n");
  
  return `
export function is${eventName}(event: unknown): event is ${eventName} {
  return (
    typeof event === "object" &&
    event !== null &&
${checks}
  );
}
`;
}
```

### 4. Runtime Validation with Types

```typescript
// validation.ts
import Ajv from "ajv";
import type { MessageEvent } from "./handlers/message/types";

const ajv = new Ajv();

export function validateAndType<T>(
  data: unknown,
  schema: object
): T {
  const validate = ajv.compile(schema);
  
  if (!validate(data)) {
    throw new Error(
      `Validation failed: ${ajv.errorsText(validate.errors)}`
    );
  }
  
  return data as T;
}

// Usage with type inference
import messageSchema from "./handlers/message/schema.json";

try {
  const typedEvent = validateAndType<MessageEvent>(rawData, messageSchema);
  // TypeScript knows all the fields
  console.log(typedEvent.sender);
  
  if (typedEvent.replyTo) {
    // Optional field handling
    console.log(`Reply to: ${typedEvent.replyTo}`);
  }
} catch (error) {
  console.error("Invalid event:", error);
}
```

## Best Practices

1. **Generate Types in CI/CD**: Run type generation as part of your build process to ensure types stay in sync with schemas.

2. **Version Your Types**: When schemas change, version your types to maintain backward compatibility.

3. **Use Strict Mode**: Enable strict type checking in all languages:
   - Python: Use `mypy --strict`
   - Rust: Use `#![deny(warnings)]`
   - TypeScript: Use `"strict": true` in tsconfig.json

4. **Validate at Boundaries**: Always validate data when it enters your system, then work with typed data internally.

5. **Document Schema Changes**: Keep a changelog of schema modifications and their impact on generated types.

## Integration Example

Here's how the three languages can work together in a type-safe manner:

```python
# Python: Generate event
from handlers.message.types import MessageEvent

event: MessageEvent = {
    "type": "message",
    "sender": "user123",
    "text": "Hello, world!",
    "timestamp": "2024-01-20T10:00:00Z"
}

# Serialize and send
import json
json_data = json.dumps(event)
```

```rust
// Rust: Receive and process
use crate::handlers::message::types::MessageEvent;

let event: MessageEvent = serde_json::from_str(&json_data)?;
println!("Received message from {}", event.sender);
```

```typescript
// TypeScript: Handle in frontend
import { MessageEvent, isMessageEvent } from "./handlers/message/types";

const data = await response.json();

if (isMessageEvent(data)) {
  // TypeScript knows data is MessageEvent
  displayMessage(data.text, data.sender);
} else {
  console.error("Invalid message event");
}
```

This approach ensures type safety across your entire system while maintaining the flexibility of JSON schemas for validation and documentation.