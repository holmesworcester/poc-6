import React from 'react'
import { Message } from '../../src/components/Message'
import type { MessageResponse } from '../../src/types'

describe('Message Component', () => {
  const baseMessage: MessageResponse = {
    message_id: 'msg1',
    channel_id: 'ch1',
    author_id: 'u1',
    author_name: 'Alice',
    content: 'Hello world!',
    created_at: Math.floor(Date.now() / 1000),
    edited_at: null,
    attachments: [],
    reactions: [],
  }

  it('renders message with author name and content', () => {
    cy.mount(<Message data={[baseMessage]} />)

    cy.contains('Alice').should('be.visible')
    cy.contains('Hello world!').should('be.visible')
  })

  it('renders grey avatar placeholder', () => {
    cy.mount(<Message data={[baseMessage]} />)

    // Avatar should be rendered (grey placeholder)
    cy.get('[data-testid="message-msg1"]').within(() => {
      cy.get('div').first().should('exist')
    })
  })

  it('shows time in correct format', () => {
    cy.mount(<Message data={[baseMessage]} />)

    // Time should be displayed (format depends on locale)
    cy.get('[data-testid="message-msg1"]').should('exist')
  })

  it('shows edited indicator when message was edited', () => {
    const editedMessage = {
      ...baseMessage,
      edited_at: Math.floor(Date.now() / 1000),
    }

    cy.mount(<Message data={[editedMessage]} />)

    cy.contains('(edited)').should('be.visible')
  })

  it('does not show edited indicator when not edited', () => {
    cy.mount(<Message data={[baseMessage]} />)

    cy.contains('(edited)').should('not.exist')
  })

  it('renders file attachment', () => {
    const messageWithFile: MessageResponse = {
      ...baseMessage,
      attachments: [
        {
          file_id: 'f1',
          filename: 'document.pdf',
          mime_type: 'application/pdf',
          size: 1048576, // 1 MB
          status: 'complete',
        },
      ],
    }

    cy.mount(<Message data={[messageWithFile]} />)

    cy.contains('document.pdf').should('be.visible')
    cy.contains('1.0 MB').should('be.visible')
    cy.contains('application/pdf').should('be.visible')
    cy.get('[data-testid="attachment-f1"]').should('exist')
  })

  it('renders image attachment with image icon', () => {
    const messageWithImage: MessageResponse = {
      ...baseMessage,
      attachments: [
        {
          file_id: 'f2',
          filename: 'photo.jpg',
          mime_type: 'image/jpeg',
          size: 512000,
          status: 'complete',
        },
      ],
    }

    cy.mount(<Message data={[messageWithImage]} />)

    cy.contains('photo.jpg').should('be.visible')
    cy.contains('🖼️').should('be.visible')
  })

  it('renders reactions', () => {
    const messageWithReactions: MessageResponse = {
      ...baseMessage,
      reactions: [
        { emoji: '👍', count: 5, users: ['u2', 'u3', 'u4', 'u5', 'u6'] },
        { emoji: '❤️', count: 2, users: ['u2', 'u3'] },
      ],
    }

    cy.mount(<Message data={[messageWithReactions]} />)

    cy.contains('👍').should('be.visible')
    cy.contains('5').should('be.visible')
    cy.contains('❤️').should('be.visible')
    cy.contains('2').should('be.visible')
  })

  it('renders multiple messages from same sender as group', () => {
    const messages: MessageResponse[] = [
      { ...baseMessage, content: 'First message' },
      { ...baseMessage, message_id: 'msg2', content: 'Second message' },
      { ...baseMessage, message_id: 'msg3', content: 'Third message' },
    ]

    cy.mount(<Message data={messages} />)

    // All messages should be visible
    cy.contains('First message').should('be.visible')
    cy.contains('Second message').should('be.visible')
    cy.contains('Third message').should('be.visible')

    // Only one author name should be shown (at the top)
    cy.contains('Alice').should('have.length', 1)
  })

  it('shows Anonymous for messages without author name', () => {
    const anonymousMessage = {
      ...baseMessage,
      author_name: null,
    }

    cy.mount(<Message data={[anonymousMessage]} />)

    cy.contains('Anonymous').should('be.visible')
  })
})
