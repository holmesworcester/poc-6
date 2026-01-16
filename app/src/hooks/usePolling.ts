import { useEffect, useRef, useCallback } from 'react'

const POLL_INTERVAL_MS = 100

interface UsePollingOptions {
  enabled?: boolean
  interval?: number
  onPoll: () => void | Promise<void>
}

/**
 * Hook for polling data at regular intervals.
 *
 * - Polls every 100ms by default (configurable)
 * - Automatically pauses when tab is hidden
 * - Resumes when tab becomes visible
 * - Call pollNow() to trigger immediate poll (e.g., after mutation)
 */
export function usePolling({ enabled = true, interval = POLL_INTERVAL_MS, onPoll }: UsePollingOptions) {
  const intervalRef = useRef<ReturnType<typeof setInterval> | null>(null)
  const isPollingRef = useRef(false)

  const poll = useCallback(async () => {
    if (isPollingRef.current) return // Skip if already polling
    isPollingRef.current = true
    try {
      await onPoll()
    } finally {
      isPollingRef.current = false
    }
  }, [onPoll])

  const startPolling = useCallback(() => {
    if (intervalRef.current) return // Already polling

    // Immediate first poll
    poll()

    // Then poll at interval
    intervalRef.current = setInterval(poll, interval)
  }, [poll, interval])

  const stopPolling = useCallback(() => {
    if (intervalRef.current) {
      clearInterval(intervalRef.current)
      intervalRef.current = null
    }
  }, [])

  // Poll immediately (e.g., after mutation)
  const pollNow = useCallback(() => {
    poll()
  }, [poll])

  // Handle visibility change
  useEffect(() => {
    if (!enabled) {
      stopPolling()
      return
    }

    const handleVisibilityChange = () => {
      if (document.hidden) {
        stopPolling()
      } else {
        startPolling()
      }
    }

    // Start polling if visible
    if (!document.hidden) {
      startPolling()
    }

    document.addEventListener('visibilitychange', handleVisibilityChange)

    return () => {
      stopPolling()
      document.removeEventListener('visibilitychange', handleVisibilityChange)
    }
  }, [enabled, startPolling, stopPolling])

  return { pollNow, isPolling: isPollingRef.current }
}
