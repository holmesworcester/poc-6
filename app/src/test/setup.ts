import '@testing-library/jest-dom'
import { vi } from 'vitest'

// Mock fetch for API calls
global.fetch = vi.fn()

// Mock document.hidden for polling
Object.defineProperty(document, 'hidden', {
  configurable: true,
  get: () => false,
})

// Reset mocks between tests
beforeEach(() => {
  vi.clearAllMocks()
})
