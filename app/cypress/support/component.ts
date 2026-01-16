import { mount } from 'cypress/react18'

// Import styles if needed
// import '../../src/index.css'

declare global {
  namespace Cypress {
    interface Chainable {
      mount: typeof mount
    }
  }
}

Cypress.Commands.add('mount', mount)

// Prevent uncaught exceptions from failing tests
Cypress.on('uncaught:exception', () => {
  return false
})
