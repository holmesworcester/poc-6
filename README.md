# POC-6 CLI Prototype

This worktree contains the design prototype for a Python CLI that provides an interactive and non-interactive interface to the POC-6 event-sourced messaging system.

## Purpose

Create a command-line interface that:
1. Uses only the event functions (like the API will) - no direct database access
2. Supports both interactive and non-interactive modes
3. Shows system state between commands for visibility
4. Enables the same operations as our scenario tests (send messages, invite users, link devices, etc.)
5. Provides isomorphic commands between interactive and non-interactive modes

## Documentation Files

### [CLI_PROTOTYPE.md](./CLI_PROTOTYPE.md)
**Start here!** This is the complete design prototype including:
- Design principles and rationale
- Interactive mode examples with full input/output
- Non-interactive mode examples
- Complete command reference
- State display format specification
- Example flows from all major scenario tests
- Implementation architecture outline
- Testing strategy

### [ARCHITECTURE_RULES.md](./ARCHITECTURE_RULES.md)
**Critical rules that must be followed during implementation:**
- The function-only API rule (no direct DB access)
- Why this rule exists and its benefits
- What functions to use for state display
- How to handle missing functions
- Code review checklist
- Function discovery guide

## How to Use These Documents

### 1. Review Phase (Current)
Read through the prototype documents and provide feedback:
- Is the design complete?
- Are the examples clear?
- Are there missing commands or features?
- Is the state display format readable?
- Are the interactive/non-interactive modes truly isomorphic?

### 2. Planning Phase (Next)
After review, create an implementation plan:
- Break down into implementation phases
- Prioritize commands (core commands first)
- Identify missing event functions that need to be added
- Design the testing framework
- Create development milestones

### 3. Implementation Phase (Future)
Build the CLI following the prototype:
- Start with session management and command parser
- Implement core commands (new-network, send, sync, show-all)
- Add remaining commands iteratively
- Create CLI test scripts for all scenario tests
- Add advanced features (file attachments, etc.)

## Key Design Decisions

### Slack-Like Interface
The UI mimics Slack's three-section layout:
1. **Accounts** - List of all accounts (peers) with selection indicator
2. **Sidebar** - List of channels in selected account
3. **Main** - List of messages in selected channel

Simple bulleted lists, no fancy formatting. Groups, invites, and admin status are hidden (under the hood).

### Function-Only API Access
The CLI will act as an API client, using ONLY the event functions from `events/` modules. This means:
- No raw SQL queries in CLI code
- No direct table access
- Same constraints as scenario tests
- Ready for transition to real networked API

See [ARCHITECTURE_RULES.md](./ARCHITECTURE_RULES.md) for complete details.

### Isomorphic Commands
Commands work identically in both modes:
```bash
# Interactive mode
> new-network --name Alice
> send "Hello"

# Non-interactive mode (same commands)
./cli.py --new-network Alice --send "Hello"
```

### State Visibility
After each command, show the three-section state display:
```
ACCOUNTS:
  * Alice - alice@network_abc123

SIDEBAR (Alice):
  * #general

MAIN (#general):
  [2000ms] Alice: Hello
```

This helps users understand:
- What changed as a result of the command
- What each account currently sees
- Whether sync is needed for convergence
- The current state of the distributed system

### Human and LLM Readability
Output format optimized for:
- Quick scanning by humans (visual hierarchy, colors)
- Easy parsing by LLMs (structured, consistent format)
- Debugging (shows IDs, timestamps, relationships)
- Testing (can be compared programmatically)

## Example Workflows

The prototype includes complete example workflows for:
1. **One Player Messaging** - Single user sending messages to themselves
2. **Three Player Messaging** - Multi-user network with message exchange
3. **Device Linking** - Same user across multiple devices
4. **Admin Groups** - Admin permissions and invite security

Each workflow shows both interactive and non-interactive usage.

## Next Steps

1. **Review the prototype** - Read through both documents and provide feedback
2. **Discuss any questions** - Clarify design decisions, missing features, etc.
3. **Create implementation plan** - Break down into concrete tasks with priorities
4. **Start implementation** - Begin with core commands and session management

## TODOs and Prerequisites

### Backend Changes Needed

See [TODO_DEVICE_NAME.md](./TODO_DEVICE_NAME.md) for details on adding device_name to link events.

**Summary**: The CLI design requires device names (e.g., "Desktop", "Phone") to distinguish between multiple devices for the same user. This needs to be added to:
- Link events (`link.join()` function)
- Network creation (`user.new_network()` function)
- Query functions to retrieve device names

**Estimated effort**: ~3 hours

This should be implemented before or during CLI development.

## Questions for Discussion

Before implementation, we should discuss:

1. **Auto-tick configuration** - Is default 10 rounds good? Should it be 20?
2. **Command syntax preferences** - Are the current command names intuitive?
3. **Priority order** - Which commands should we implement first?
4. **Testing approach** - Should CLI tests replace/augment scenario tests, or coexist?
5. **Missing functions** - What query functions do we need to add to event modules?
6. **Output formats** - Do we need JSON output mode? CSV? Other formats?
7. **Error handling** - How should errors be displayed in interactive vs non-interactive modes?

## Implementation Estimate

Rough effort estimate:
- **Core CLI framework**: 2-3 days
  - Session management, command parser, state display engine
- **Basic commands**: 3-4 days
  - new-network, new-peer, switch, send, sync, show, show-all
- **Group/admin commands**: 2-3 days
  - create-group, add-member, create-invite
- **Multi-device commands**: 2-3 days
  - create-link-invite, link-device
- **Testing framework**: 2-3 days
  - CLI script runner, assertion system, comparison to scenario tests
- **Polish and documentation**: 1-2 days
  - Error handling, help system, examples

**Total: ~12-18 days of focused work**

This could be done iteratively:
- Week 1: Core framework + basic commands (usable for simple workflows)
- Week 2: All commands implemented (feature complete)
- Week 3: Testing, polish, documentation (production ready)

---

**Status**: Design prototype complete, awaiting review and planning phase.
