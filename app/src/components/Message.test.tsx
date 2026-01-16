import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import { Message } from './Message'

const mockMessage = {
  message_id: 'msg1',
  channel_id: 'ch1',
  author_id: 'u1',
  author_name: 'Alice',
  content: 'Hello everyone!',
  created_at: Math.floor(Date.now() / 1000) - 3600,
  edited_at: null,
  attachments: [],
  reactions: [],
}

describe('Message', () => {
  it('renders message with author name and content', () => {
    render(<Message data={[mockMessage]} />)
    expect(screen.getByText('Alice')).toBeInTheDocument()
    expect(screen.getByText('Hello everyone!')).toBeInTheDocument()
  })

  it('renders avatar placeholder', () => {
    render(<Message data={[mockMessage]} />)
    // Avatar is a View with backgroundColor - just verify render doesn't crash
    expect(screen.getByText('Alice')).toBeInTheDocument()
  })

  it('shows edited indicator when message was edited', () => {
    const editedMessage = { ...mockMessage, edited_at: Date.now() / 1000 }
    render(<Message data={[editedMessage]} />)
    expect(screen.getByText('(edited)')).toBeInTheDocument()
  })

  it('does not show edited indicator when not edited', () => {
    render(<Message data={[mockMessage]} />)
    expect(screen.queryByText('(edited)')).not.toBeInTheDocument()
  })

  it('renders file attachment', () => {
    const messageWithAttachment = {
      ...mockMessage,
      attachments: [
        {
          file_id: 'f1',
          filename: 'report.pdf',
          mime_type: 'application/pdf',
          size: 250000,
          status: 'complete',
        },
      ],
    }
    render(<Message data={[messageWithAttachment]} />)
    expect(screen.getByText('report.pdf')).toBeInTheDocument()
  })

  it('renders reactions', () => {
    const messageWithReactions = {
      ...mockMessage,
      reactions: [{ emoji: '👍', count: 2, users: ['u2', 'u3'] }],
    }
    render(<Message data={[messageWithReactions]} />)
    expect(screen.getByText('👍')).toBeInTheDocument()
    expect(screen.getByText('2')).toBeInTheDocument()
  })

  it('renders multiple messages from same sender as group', () => {
    const messages = [
      mockMessage,
      { ...mockMessage, message_id: 'msg2', content: 'Second message' },
    ]
    render(<Message data={messages} />)
    expect(screen.getByText('Hello everyone!')).toBeInTheDocument()
    expect(screen.getByText('Second message')).toBeInTheDocument()
    // Only one author name should show
    expect(screen.getAllByText('Alice')).toHaveLength(1)
  })

  it('shows Anonymous for messages without author name', () => {
    const anonMessage = { ...mockMessage, author_name: '' }
    render(<Message data={[anonMessage]} />)
    expect(screen.getByText('Anonymous')).toBeInTheDocument()
  })
})
