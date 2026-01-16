# Cypress Testing Rules

Best practices for writing Cypress tests in this project.

## Core Principles

### 1. No Arbitrary Waits

**Never use:**
- `cy.wait(1000)` or any wallclock timeout
- `cy.wait('@someAlias', { timeout: 10000 })` with long timeouts
- Manual delays to "let things load"

**Instead:**
- Use assertions that automatically retry until conditions are met
- Cypress automatically waits for elements to exist before interacting
- Use `cy.wait('@alias')` only for API interception (which is instantaneous)

```typescript
// BAD
cy.wait(2000)
cy.get('[data-testid="message"]').should('be.visible')

// GOOD
cy.get('[data-testid="message"]').should('be.visible')  // Retries automatically
```

### 2. Assert on UI State, Not Time

Let the UI tell you when it's ready:

```typescript
// BAD - hoping the data loaded
cy.wait(500)
cy.contains('Hello').click()

// GOOD - waiting for the data to appear
cy.contains('Hello').should('be.visible').click()
```

### 3. Use Intercepts for API Control

Mock API responses to make tests deterministic:

```typescript
cy.intercept('GET', '/api/messages', {
  statusCode: 200,
  body: { items: [{ id: '1', content: 'Hello' }] },
}).as('getMessages')

cy.visit('/')
cy.wait('@getMessages')  // Wait for API call (fast)
cy.contains('Hello').should('be.visible')
```

### 4. Prefer data-testid Over CSS/Text

Use stable selectors:

```typescript
// FRAGILE - breaks if text changes
cy.contains('Submit').click()

// FRAGILE - breaks if CSS changes
cy.get('.btn-primary').click()

// STABLE - survives refactoring
cy.get('[data-testid="submit-button"]').click()
```

### 5. One Assertion Per Behavior

Test one thing at a time. If a test fails, you should know exactly why:

```typescript
// BAD - multiple unrelated assertions
it('works', () => {
  cy.get('[data-testid="sidebar"]').should('exist')
  cy.get('[data-testid="message-list"]').should('exist')
  cy.contains('Alice').click()
  cy.get('[data-testid="chat"]').should('exist')
})

// GOOD - focused tests
it('shows sidebar on load', () => {
  cy.get('[data-testid="sidebar"]').should('exist')
})

it('navigates to chat when channel clicked', () => {
  cy.get('[data-testid="channel-general"]').click()
  cy.get('[data-testid="chat-general"]').should('exist')
})
```

### 6. Setup in beforeEach

Keep tests independent:

```typescript
beforeEach(() => {
  cy.intercept('GET', '/api/networks', { ... }).as('getNetworks')
  cy.intercept('GET', '/api/channels', { ... }).as('getChannels')
})

it('shows channels', () => {
  cy.mount(<Sidebar />)
  cy.wait(['@getNetworks', '@getChannels'])
  cy.get('[data-testid="channel-list"]').should('exist')
})
```

### 7. Use .should() for Retry-able Assertions

`.should()` automatically retries until timeout:

```typescript
// These retry automatically:
cy.get('[data-testid="count"]').should('have.text', '5')
cy.get('[data-testid="list"]').should('have.length', 10)
cy.get('[data-testid="button"]').should('not.be.disabled')

// .then() does NOT retry - avoid for assertions:
cy.get('[data-testid="count"]').then($el => {
  expect($el.text()).to.equal('5')  // BAD - no retry
})
```

### 8. Chain Commands Don't Need Waits

Cypress commands queue automatically:

```typescript
// This already waits properly:
cy.get('[data-testid="input"]').type('hello')
cy.get('[data-testid="submit"]').click()
cy.get('[data-testid="result"]').should('contain', 'hello')
```

## Common Patterns

### Testing Loading States

```typescript
it('shows loading then content', () => {
  cy.intercept('GET', '/api/data', {
    delay: 100,  // Small delay to see loading
    body: { items: [] }
  }).as('getData')

  cy.mount(<Component />)
  cy.contains('Loading').should('be.visible')
  cy.wait('@getData')
  cy.contains('Loading').should('not.exist')
})
```

### Testing Error States

```typescript
it('shows error on API failure', () => {
  cy.intercept('GET', '/api/data', {
    statusCode: 500,
    body: { error: 'Server error' }
  }).as('getData')

  cy.mount(<Component />)
  cy.wait('@getData')
  cy.contains('error').should('be.visible')
})
```

### Testing User Interactions

```typescript
it('sends message on submit', () => {
  cy.intercept('POST', '/api/messages', {
    statusCode: 201,
    body: { id: 'new' }
  }).as('sendMessage')

  cy.get('[data-testid="input"]').type('Hello')
  cy.get('[data-testid="send"]').click()

  cy.wait('@sendMessage')
    .its('request.body')
    .should('deep.equal', { text: 'Hello' })
})
```

## Forbidden Patterns

1. `cy.wait(ms)` - Never wait on wallclock time
2. `{ timeout: 30000 }` - Don't increase timeouts, fix the test
3. `cy.get().then()` for assertions - Use `.should()` instead
4. Sleeping between actions - Commands chain automatically
5. Flaky tests - If a test is flaky, it's wrong, not "sometimes failing"
