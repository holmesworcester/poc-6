describe('Chat App E2E', () => {
  beforeEach(() => {
    // Mock all API endpoints
    cy.intercept('GET', '/api/networks', {
      statusCode: 200,
      body: {
        items: [
          { network_id: 'net1', name: 'Work' },
          { network_id: 'net2', name: 'Friends' },
        ],
      },
    }).as('getNetworks')

    cy.intercept('GET', '/api/networks/net1/channels', {
      statusCode: 200,
      body: {
        items: [
          { channel_id: 'ch1', name: 'general', group_id: 'g1', is_main: true },
          { channel_id: 'ch2', name: 'random', group_id: 'g2', is_main: false },
        ],
      },
    }).as('getChannelsNet1')

    cy.intercept('GET', '/api/networks/net2/channels', {
      statusCode: 200,
      body: {
        items: [
          { channel_id: 'ch3', name: 'hangout', group_id: 'g3', is_main: true },
        ],
      },
    }).as('getChannelsNet2')

    cy.intercept('GET', '/api/networks/net1/users', {
      statusCode: 200,
      body: {
        items: [
          { user_id: 'u1', name: 'Alice' },
          { user_id: 'u2', name: 'Bob' },
        ],
      },
    }).as('getUsersNet1')

    cy.intercept('GET', '/api/networks/net2/users', {
      statusCode: 200,
      body: {
        items: [
          { user_id: 'u3', name: 'Charlie' },
        ],
      },
    }).as('getUsersNet2')

    cy.intercept('GET', '/api/networks/net1/channels/ch1/messages*', {
      statusCode: 200,
      body: {
        items: [
          {
            message_id: 'msg1',
            channel_id: 'ch1',
            author_id: 'u1',
            author_name: 'Alice',
            content: 'Hello from general!',
            created_at: Math.floor(Date.now() / 1000) - 3600,
            edited_at: null,
            attachments: [],
            reactions: [],
          },
        ],
        cursors: { older: null, newer: null },
      },
    }).as('getMessagesGeneral')

    cy.intercept('GET', '/api/networks/net1/channels/ch2/messages*', {
      statusCode: 200,
      body: {
        items: [
          {
            message_id: 'msg2',
            channel_id: 'ch2',
            author_id: 'u2',
            author_name: 'Bob',
            content: 'Random thoughts here',
            created_at: Math.floor(Date.now() / 1000) - 1800,
            edited_at: null,
            attachments: [],
            reactions: [],
          },
        ],
        cursors: { older: null, newer: null },
      },
    }).as('getMessagesRandom')

    cy.intercept('POST', '/api/networks/*/channels/*/messages', {
      statusCode: 201,
      body: { message_id: 'msg_new' },
    }).as('sendMessage')
  })

  it('loads app and shows sidebar', () => {
    cy.visit('/')

    cy.wait('@getNetworks')
    cy.get('[data-testid="sidebar"]').should('exist')
    cy.get('[data-testid="network-dropdown"]').should('contain', 'Work')
  })

  it('navigates from sidebar to chat and back', () => {
    cy.visit('/')

    cy.wait(['@getNetworks', '@getChannelsNet1', '@getUsersNet1'])

    // Click on general channel
    cy.get('[data-testid="channel-general"]').click()

    cy.wait('@getMessagesGeneral')

    // Should be in chat view
    cy.contains('#general').should('be.visible')
    cy.contains('Hello from general!').should('be.visible')

    // Go back to sidebar
    cy.get('[data-testid="back-button"]').click()

    // Should be back on sidebar
    cy.get('[data-testid="sidebar"]').should('exist')
    cy.get('[data-testid="channel-list"]').should('exist')
  })

  it('can switch between channels', () => {
    cy.visit('/')

    cy.wait(['@getNetworks', '@getChannelsNet1', '@getUsersNet1'])

    // Go to general channel
    cy.get('[data-testid="channel-general"]').click()
    cy.wait('@getMessagesGeneral')
    cy.contains('Hello from general!').should('be.visible')

    // Go back
    cy.get('[data-testid="back-button"]').click()

    // Go to random channel
    cy.get('[data-testid="channel-random"]').click()
    cy.wait('@getMessagesRandom')
    cy.contains('Random thoughts here').should('be.visible')
  })

  it('can switch networks', () => {
    cy.visit('/')

    cy.wait(['@getNetworks', '@getChannelsNet1', '@getUsersNet1'])

    // Should see Work network channels
    cy.get('[data-testid="channel-general"]').should('exist')
    cy.get('[data-testid="channel-random"]').should('exist')

    // Open network dropdown
    cy.get('[data-testid="network-dropdown"]').click()
    cy.get('[data-testid="network-list"]').should('be.visible')

    // Select Friends network
    cy.contains('Friends').click()

    cy.wait(['@getChannelsNet2', '@getUsersNet2'])

    // Should see Friends network channels
    cy.get('[data-testid="channel-hangout"]').should('exist')
    cy.get('[data-testid="channel-general"]').should('not.exist')
  })

  it('can send a message', () => {
    cy.visit('/')

    cy.wait(['@getNetworks', '@getChannelsNet1', '@getUsersNet1'])

    // Go to general channel
    cy.get('[data-testid="channel-general"]').click()
    cy.wait('@getMessagesGeneral')

    // Type and send message
    cy.get('[data-testid="message-input"]').type('Hello from Cypress E2E!')
    cy.get('[data-testid="send-button"]').click()

    cy.wait('@sendMessage').its('request.body').should('deep.equal', {
      text: 'Hello from Cypress E2E!',
    })

    // Input should be cleared
    cy.get('[data-testid="message-input"]').should('have.value', '')
  })

  it('displays users in sidebar', () => {
    cy.visit('/')

    cy.wait(['@getNetworks', '@getChannelsNet1', '@getUsersNet1'])

    cy.contains('MEMBERS (2)').should('be.visible')
    cy.contains('Alice').should('be.visible')
    cy.contains('Bob').should('be.visible')
  })

  it('shows main channel badge', () => {
    cy.visit('/')

    cy.wait(['@getNetworks', '@getChannelsNet1', '@getUsersNet1'])

    cy.get('[data-testid="channel-general"]').within(() => {
      cy.contains('main').should('be.visible')
    })
  })

  it('handles API errors gracefully', () => {
    cy.intercept('GET', '/api/networks', {
      statusCode: 500,
      body: { detail: 'Internal server error' },
    }).as('getNetworksError')

    cy.visit('/')

    cy.wait('@getNetworksError')
    cy.contains('API error').should('be.visible')
  })
})
