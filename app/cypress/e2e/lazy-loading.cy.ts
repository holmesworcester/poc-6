describe('Lazy Loading Messages', () => {
  const mockNetwork = { network_id: 'net1', name: 'Work' }
  const mockChannel = { channel_id: 'ch1', name: 'general', group_id: 'g1', is_main: true }

  beforeEach(() => {
    cy.intercept('GET', '/api/networks', {
      statusCode: 200,
      body: { items: [mockNetwork] },
    }).as('getNetworks')

    cy.intercept('GET', '/api/networks/net1/channels', {
      statusCode: 200,
      body: { items: [mockChannel] },
    }).as('getChannels')

    cy.intercept('GET', '/api/networks/net1/users', {
      statusCode: 200,
      body: { items: [] },
    }).as('getUsers')
  })

  it('loads more messages on scroll to top', () => {
    // First page of messages
    const firstPageMessages = Array.from({ length: 20 }, (_, i) => ({
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

    // Second page of messages (older)
    const secondPageMessages = Array.from({ length: 20 }, (_, i) => ({
      message_id: `msg${i + 20}`,
      channel_id: 'ch1',
      author_id: 'u2',
      author_name: 'Bob',
      content: `Older message ${i + 20}`,
      created_at: Math.floor(Date.now() / 1000) - (i + 20) * 60,
      edited_at: null,
      attachments: [],
      reactions: [],
    }))

    cy.intercept('GET', '/api/networks/net1/channels/ch1/messages', (req) => {
      if (req.query.cursor) {
        req.reply({
          statusCode: 200,
          body: {
            items: secondPageMessages,
            cursors: { older: null, newer: 'cursor_first' },
          },
        })
      } else {
        req.reply({
          statusCode: 200,
          body: {
            items: firstPageMessages,
            cursors: { older: 'cursor_older', newer: null },
          },
        })
      }
    }).as('getMessages')

    cy.visit('/')
    cy.wait(['@getNetworks', '@getChannels', '@getUsers'])

    // Navigate to channel
    cy.get('[data-testid="channel-general"]').click()
    cy.wait('@getMessages')

    // Should see first page messages
    cy.contains('Message 0').should('be.visible')
    cy.contains('Message 19').should('exist')

    // Scroll to trigger lazy loading (FlatList is inverted, so scroll "up" means toward older messages)
    cy.get('[data-testid="message-list"]').scrollTo('bottom')

    // Wait for second page to load
    cy.wait('@getMessages')

    // Should now see older messages too
    cy.contains('Older message 20').should('exist')
  })

  it('does not load more when no older cursor', () => {
    const messages = Array.from({ length: 5 }, (_, i) => ({
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

    cy.intercept('GET', '/api/networks/net1/channels/ch1/messages*', {
      statusCode: 200,
      body: {
        items: messages,
        cursors: { older: null, newer: null }, // No more messages
      },
    }).as('getMessages')

    cy.visit('/')
    cy.wait(['@getNetworks', '@getChannels', '@getUsers'])

    cy.get('[data-testid="channel-general"]').click()
    cy.wait('@getMessages')

    // Scroll should not trigger additional API calls
    cy.get('[data-testid="message-list"]').scrollTo('bottom')

    // Only one call should have been made
    cy.get('@getMessages.all').should('have.length', 1)
  })
})
