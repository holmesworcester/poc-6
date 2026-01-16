import { defineConfig } from 'cypress'

export default defineConfig({
  // Fast timeouts - tests should pass quickly or fail fast
  defaultCommandTimeout: 3000,

  e2e: {
    baseUrl: 'http://localhost:5173',
    specPattern: 'cypress/e2e/**/*.cy.{js,jsx,ts,tsx}',
    supportFile: 'cypress/support/e2e.ts',
  },
  video: false,
  screenshotOnRunFailure: true,
  screenshotsFolder: 'cypress/screenshots',
})
