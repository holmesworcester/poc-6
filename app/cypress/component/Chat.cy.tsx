import React from 'react'
import { Chat } from '../../src/components/Chat'

describe('Chat Component', () => {
  const mockNetwork = { network_id: 'net1', name: 'Work' }
  const mockChannel = { channel_id: 'ch1', name: 'general', group_id: 'g1', is_main: true }

  beforeEach(() => {
    // Mock messages API
    cy.intercept('GET', '/api/networks/net1/channels/ch1/messages*', {
      statusCode: 200,
      body: {
        items: [
          {
            message_id: 'msg1',
            channel_id: 'ch1',
            author_id: 'u1',
            author_name: 'Alice',
            content: 'Hello everyone!',
            created_at: Math.floor(Date.now() / 1000) - 3600, // 1 hour ago
            edited_at: null,
            attachments: [],
            reactions: [{ emoji: '👍', count: 2, users: ['u2', 'u3'] }],
          },
          {
            message_id: 'msg2',
            channel_id: 'ch1',
            author_id: 'u2',
            author_name: 'Bob',
            content: 'Hey Alice! Check out this file:',
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
          {
            message_id: 'msg3',
            channel_id: 'ch1',
            author_id: 'u1',
            author_name: 'Alice',
            content: 'Thanks! Here is a screenshot:',
            created_at: Math.floor(Date.now() / 1000) - 3400,
            edited_at: Math.floor(Date.now() / 1000) - 3300,
            attachments: [
              {
                file_id: 'f2',
                filename: 'screenshot.png',
                mime_type: 'image/png',
                size: 128000,
                status: 'complete',
              },
            ],
            reactions: [{ emoji: '🎉', count: 1, users: ['u2'] }],
          },
        ],
        cursors: { older: 'cursor_abc', newer: null },
      },
    }).as('getMessages')

    // Mock send message
    cy.intercept('POST', '/api/networks/net1/channels/ch1/messages', {
      statusCode: 201,
      body: { message_id: 'msg_new' },
    }).as('sendMessage')
  })

  it('renders chat header with channel name', () => {
    cy.mount(<Chat network={mockNetwork} channel={mockChannel} onBack={cy.stub()} />)
    cy.contains('#general').should('be.visible')
    cy.get('[data-testid="back-button"]').should('exist')
  })

  it('displays messages after loading', () => {
    cy.mount(<Chat network={mockNetwork} channel={mockChannel} onBack={cy.stub()} />)
    // Just assert on what we expect - Cypress will wait for it
    cy.contains('Hello everyone!').should('be.visible')
    cy.contains('Alice').should('be.visible')
    cy.contains('Bob').should('be.visible')
  })

  it('displays attachments in messages', () => {
    cy.mount(<Chat network={mockNetwork} channel={mockChannel} onBack={cy.stub()} />)
    // PDF attachment
    cy.contains('report.pdf').should('be.visible')
    cy.contains('244.1 KB').should('be.visible')
    // Image attachment
    cy.contains('screenshot.png').should('be.visible')
  })

  it('displays reactions on messages', () => {
    cy.mount(<Chat network={mockNetwork} channel={mockChannel} onBack={cy.stub()} />)
    cy.contains('👍').should('be.visible')
    cy.contains('2').should('be.visible')
    cy.contains('🎉').should('be.visible')
  })

  it('shows edited indicator', () => {
    cy.mount(<Chat network={mockNetwork} channel={mockChannel} onBack={cy.stub()} />)
    cy.contains('(edited)').should('be.visible')
  })

  it('can type in message input', () => {
    cy.mount(<Chat network={mockNetwork} channel={mockChannel} onBack={cy.stub()} />)
    // Wait for messages to load by asserting on message list
    cy.get('[data-testid="message-list"]').should('be.visible')
    cy.get('[data-testid="message-input"]').type('Hello from Cypress!', { force: true })
    cy.get('[data-testid="message-input"]').should('have.value', 'Hello from Cypress!')
  })

  it('send button is visible after typing', () => {
    cy.mount(<Chat network={mockNetwork} channel={mockChannel} onBack={cy.stub()} />)
    cy.get('[data-testid="message-list"]').should('be.visible')
    cy.get('[data-testid="message-input"]').type('test', { force: true })
    cy.get('[data-testid="send-button"]').should('be.visible')
  })

  it('can send a message', () => {
    cy.mount(<Chat network={mockNetwork} channel={mockChannel} onBack={cy.stub()} />)
    cy.get('[data-testid="message-list"]').should('be.visible')
    cy.get('[data-testid="message-input"]').type('New message from test', { force: true })
    cy.get('[data-testid="send-button"]').click()
    // Assert on the request that was made
    cy.get('@sendMessage.all').should('have.length', 1)
    // Input should be cleared after send
    cy.get('[data-testid="message-input"]').should('have.value', '')
  })

  it('calls onBack when back button is clicked', () => {
    const onBack = cy.stub().as('onBack')
    cy.mount(<Chat network={mockNetwork} channel={mockChannel} onBack={onBack} />)
    cy.get('[data-testid="message-list"]').should('be.visible')
    cy.get('[data-testid="back-button"]').click()
    cy.get('@onBack').should('have.been.calledOnce')
  })

  it('shows loading state initially', () => {
    // Delay the response to see loading state
    cy.intercept('GET', '/api/networks/net1/channels/ch1/messages*', {
      delay: 500,
      statusCode: 200,
      body: { items: [], cursors: {} },
    })
    cy.mount(<Chat network={mockNetwork} channel={mockChannel} onBack={cy.stub()} />)
    cy.contains('Loading messages...').should('be.visible')
  })

  it('shows empty state when no messages', () => {
    // Override the beforeEach intercept for this test
    cy.intercept('GET', '/api/networks/net1/channels/ch1/messages*', {
      statusCode: 200,
      body: { items: [], cursors: {} },
    }).as('getEmptyMessages')
    cy.mount(<Chat network={mockNetwork} channel={mockChannel} onBack={cy.stub()} />)
    // Empty state should appear after loading completes
    cy.contains('No messages yet').should('be.visible')
    cy.contains('Be the first to say something!').should('be.visible')
  })

  it('displays date dividers between messages', () => {
    cy.mount(<Chat network={mockNetwork} channel={mockChannel} onBack={cy.stub()} />)
    // Should see "Today" divider since messages are from today
    cy.contains('Today').should('be.visible')
  })

  it('loads older messages on scroll to end', () => {
    // First page of messages
    const firstPage = Array.from({ length: 10 }, (_, i) => ({
      message_id: `msg${i}`,
      channel_id: 'ch1',
      author_id: 'u1',
      author_name: 'Alice',
      content: `Message ${i}`,
      created_at: Math.floor(Date.now() / 1000) - i * 60,
      edited_at: null,
      attachments: [],
      reactions: [],
    }))

    // Second page (older messages)
    const secondPage = Array.from({ length: 10 }, (_, i) => ({
      message_id: `msg${i + 10}`,
      channel_id: 'ch1',
      author_id: 'u2',
      author_name: 'Bob',
      content: `Older message ${i + 10}`,
      created_at: Math.floor(Date.now() / 1000) - (i + 10) * 60,
      edited_at: null,
      attachments: [],
      reactions: [],
    }))

    cy.intercept('GET', '/api/networks/net1/channels/ch1/messages*', (req) => {
      if (req.query.cursor) {
        req.reply({
          statusCode: 200,
          body: {
            items: secondPage,
            cursors: { older: null, newer: 'cursor_first' },
          },
        })
      } else {
        req.reply({
          statusCode: 200,
          body: {
            items: firstPage,
            cursors: { older: 'cursor_older', newer: null },
          },
        })
      }
    }).as('getMessagesLazy')

    cy.mount(<Chat network={mockNetwork} channel={mockChannel} onBack={cy.stub()} />)

    // Wait for first page to load
    cy.contains('Message 0').should('be.visible')

    // Scroll to trigger lazy loading (RN Web FlatList needs ensureScrollable: false)
    cy.get('[data-testid="message-list"]').scrollTo('bottom', { ensureScrollable: false })

    // Should load older messages
    cy.contains('Older message 10').should('exist')
  })

  it('handles scroll gracefully when no more messages', () => {
    cy.mount(<Chat network={mockNetwork} channel={mockChannel} onBack={cy.stub()} />)

    // Wait for messages to load
    cy.contains('Hello everyone!').should('be.visible')

    // Scroll - should not crash even if no more messages
    cy.get('[data-testid="message-list"]').scrollTo('bottom', { ensureScrollable: false })

    // Messages should still be visible
    cy.contains('Hello everyone!').should('be.visible')
  })

  it('handles new messages arriving during interaction', () => {
    // Start with initial messages
    const initialMessages = [
      {
        message_id: 'msg1',
        channel_id: 'ch1',
        author_id: 'u1',
        author_name: 'Alice',
        content: 'Original message',
        created_at: Math.floor(Date.now() / 1000) - 100,
        edited_at: null,
        attachments: [],
        reactions: [],
      },
    ]

    cy.intercept('GET', '/api/networks/net1/channels/ch1/messages*', {
      statusCode: 200,
      body: {
        items: initialMessages,
        cursors: { older: null, newer: null },
      },
    }).as('getInitialMessages')

    cy.mount(<Chat network={mockNetwork} channel={mockChannel} onBack={cy.stub()} />)

    // Wait for initial load
    cy.contains('Original message').should('be.visible')

    // Now simulate new messages arriving (as if from a poll)
    const updatedMessages = [
      {
        message_id: 'msg_new',
        channel_id: 'ch1',
        author_id: 'u2',
        author_name: 'Bob',
        content: 'New message just arrived',
        created_at: Math.floor(Date.now() / 1000),
        edited_at: null,
        attachments: [],
        reactions: [],
      },
      ...initialMessages,
    ]

    cy.intercept('GET', '/api/networks/net1/channels/ch1/messages*', {
      statusCode: 200,
      body: {
        items: updatedMessages,
        cursors: { older: null, newer: null },
      },
    }).as('getUpdatedMessages')

    // Scroll to trigger any re-renders
    cy.get('[data-testid="message-list"]').scrollTo('bottom', { ensureScrollable: false })

    // Original message should still be visible
    cy.contains('Original message').should('be.visible')
  })
})
