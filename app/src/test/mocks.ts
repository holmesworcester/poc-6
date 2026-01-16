import { vi } from 'vitest'

// Mock API responses
export const mockNetwork = { network_id: 'net1', name: 'Work' }
export const mockChannel = { channel_id: 'ch1', name: 'general', group_id: 'g1', is_main: true }

export const mockMessages = [
  {
    message_id: 'msg1',
    channel_id: 'ch1',
    author_id: 'u1',
    author_name: 'Alice',
    content: 'Hello everyone!',
    created_at: Math.floor(Date.now() / 1000) - 3600,
    edited_at: null,
    attachments: [],
    reactions: [{ emoji: '👍', count: 2, users: ['u2', 'u3'] }],
  },
  {
    message_id: 'msg2',
    channel_id: 'ch1',
    author_id: 'u2',
    author_name: 'Bob',
    content: 'Hey Alice!',
    created_at: Math.floor(Date.now() / 1000) - 3500,
    edited_at: null,
    attachments: [
      {
        file_id: 'f1',
        filename: 'report.pdf',
        mime_type: 'application/pdf',
        size: 250000,
        status: 'complete',
      },
    ],
    reactions: [],
  },
]

export function mockFetch(responses: Record<string, any>) {
  return vi.fn((url: string) => {
    for (const [pattern, response] of Object.entries(responses)) {
      if (url.includes(pattern)) {
        return Promise.resolve({
          ok: true,
          json: () => Promise.resolve(response),
        })
      }
    }
    return Promise.resolve({
      ok: false,
      status: 404,
      json: () => Promise.resolve({ detail: 'Not found' }),
    })
  })
}
