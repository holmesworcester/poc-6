import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { Chat } from './Chat'

const mockNetwork = { network_id: 'net1', name: 'Work' }
const mockChannel = { channel_id: 'ch1', name: 'general', group_id: 'g1', is_main: true }

const mockMessages = [
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
    edited_at: Math.floor(Date.now() / 1000) - 3400,
    attachments: [
      { file_id: 'f1', filename: 'report.pdf', mime_type: 'application/pdf', size: 250000, status: 'complete' },
    ],
    reactions: [],
  },
]

beforeEach(() => {
  global.fetch = vi.fn((url: string) => {
    if (url.includes('/messages')) {
      return Promise.resolve({
        ok: true,
        json: () => Promise.resolve({ items: mockMessages, cursors: { older: null, newer: null } }),
      })
    }
    return Promise.resolve({
      ok: true,
      json: () => Promise.resolve({ message_id: 'msg_new' }),
    })
  }) as any
})

describe('Chat', () => {
  it('renders chat header with channel name', async () => {
    render(<Chat network={mockNetwork} channel={mockChannel} onBack={vi.fn()} />)
    expect(screen.getByText('#general')).toBeInTheDocument()
  })

  it('displays messages after loading', async () => {
    render(<Chat network={mockNetwork} channel={mockChannel} onBack={vi.fn()} />)
    await waitFor(() => {
      expect(screen.getByText('Hello everyone!')).toBeInTheDocument()
    })
    expect(screen.getByText('Alice')).toBeInTheDocument()
  })

  it('displays attachments in messages', async () => {
    render(<Chat network={mockNetwork} channel={mockChannel} onBack={vi.fn()} />)
    await waitFor(() => {
      expect(screen.getByText('report.pdf')).toBeInTheDocument()
    })
  })

  it('displays reactions on messages', async () => {
    render(<Chat network={mockNetwork} channel={mockChannel} onBack={vi.fn()} />)
    await waitFor(() => {
      expect(screen.getByText('👍')).toBeInTheDocument()
    })
  })

  it('shows edited indicator', async () => {
    render(<Chat network={mockNetwork} channel={mockChannel} onBack={vi.fn()} />)
    await waitFor(() => {
      expect(screen.getByText('(edited)')).toBeInTheDocument()
    })
  })

  it('can type in message input', async () => {
    render(<Chat network={mockNetwork} channel={mockChannel} onBack={vi.fn()} />)
    await waitFor(() => {
      expect(screen.getByText('Hello everyone!')).toBeInTheDocument()
    })

    const input = screen.getByTestId('message-input')
    fireEvent.change(input, { target: { value: 'Hello from Vitest!' } })
    expect(input).toHaveValue('Hello from Vitest!')
  })

  it('calls onBack when back button is clicked', async () => {
    const onBack = vi.fn()
    render(<Chat network={mockNetwork} channel={mockChannel} onBack={onBack} />)

    const backButton = screen.getByTestId('back-button')
    fireEvent.click(backButton)
    expect(onBack).toHaveBeenCalledOnce()
  })

  it('shows loading state initially', () => {
    // Delay the fetch response
    global.fetch = vi.fn(() => new Promise(() => {})) as any
    render(<Chat network={mockNetwork} channel={mockChannel} onBack={vi.fn()} />)
    expect(screen.getByText('Loading messages...')).toBeInTheDocument()
  })

  it('shows empty state when no messages', async () => {
    global.fetch = vi.fn(() => Promise.resolve({
      ok: true,
      json: () => Promise.resolve({ items: [], cursors: {} }),
    })) as any

    render(<Chat network={mockNetwork} channel={mockChannel} onBack={vi.fn()} />)
    await waitFor(() => {
      expect(screen.getByText('No messages yet')).toBeInTheDocument()
    })
    expect(screen.getByText('Be the first to say something!')).toBeInTheDocument()
  })

  it('displays date dividers between messages', async () => {
    render(<Chat network={mockNetwork} channel={mockChannel} onBack={vi.fn()} />)
    await waitFor(() => {
      expect(screen.getByText('Today')).toBeInTheDocument()
    })
  })
})
