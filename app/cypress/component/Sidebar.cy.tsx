import React from 'react'
import { Sidebar } from '../../src/components/Sidebar'

describe('Sidebar Component', () => {
  beforeEach(() => {
    // Mock API responses
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
          { channel_id: 'ch3', name: 'announcements', group_id: 'g3', is_main: false },
        ],
      },
    }).as('getChannels')

    cy.intercept('GET', '/api/networks/net1/users', {
      statusCode: 200,
      body: {
        items: [
          { user_id: 'u1', name: 'Alice' },
          { user_id: 'u2', name: 'Bob' },
          { user_id: 'u3', name: 'Charlie' },
        ],
      },
    }).as('getUsers')
  })

  it('renders sidebar with loading state', () => {
    cy.mount(
      <Sidebar
        currentNetwork={null}
        onSelectNetwork={cy.stub()}
        onSelectChannel={cy.stub()}
      />
    )
    cy.get('[data-testid="sidebar"]').should('exist')
    cy.contains('Loading...').should('be.visible')
  })

  it('displays networks in dropdown', () => {
    cy.mount(
      <Sidebar
        currentNetwork={{ network_id: 'net1', name: 'Work' }}
        onSelectNetwork={cy.stub()}
        onSelectChannel={cy.stub()}
      />
    )
    // Network dropdown shows current network
    cy.get('[data-testid="network-dropdown"]').should('contain', 'Work')
    // Open dropdown
    cy.get('[data-testid="network-dropdown"]').click()
    cy.get('[data-testid="network-list"]').should('be.visible')
    cy.contains('Work').should('be.visible')
    cy.contains('Friends').should('be.visible')
  })

  it('displays channels list', () => {
    cy.mount(
      <Sidebar
        currentNetwork={{ network_id: 'net1', name: 'Work' }}
        onSelectNetwork={cy.stub()}
        onSelectChannel={cy.stub()}
      />
    )
    cy.get('[data-testid="channel-list"]').should('exist')
    cy.get('[data-testid="channel-general"]').should('exist')
    cy.get('[data-testid="channel-random"]').should('exist')
    cy.get('[data-testid="channel-announcements"]').should('exist')
  })

  it('displays users list', () => {
    cy.mount(
      <Sidebar
        currentNetwork={{ network_id: 'net1', name: 'Work' }}
        onSelectNetwork={cy.stub()}
        onSelectChannel={cy.stub()}
      />
    )
    cy.get('[data-testid="user-list"]').should('exist')
    cy.contains('Alice').should('be.visible')
    cy.contains('Bob').should('be.visible')
    cy.contains('Charlie').should('be.visible')
  })

  it('calls onSelectChannel when channel is clicked', () => {
    const onSelectChannel = cy.stub().as('onSelectChannel')
    cy.mount(
      <Sidebar
        currentNetwork={{ network_id: 'net1', name: 'Work' }}
        onSelectNetwork={cy.stub()}
        onSelectChannel={onSelectChannel}
      />
    )
    cy.get('[data-testid="channel-general"]').should('be.visible').click()
    cy.get('@onSelectChannel').should('have.been.calledOnce')
  })

  it('switches networks when selecting from dropdown', () => {
    const onSelectNetwork = cy.stub().as('onSelectNetwork')
    cy.mount(
      <Sidebar
        currentNetwork={{ network_id: 'net1', name: 'Work' }}
        onSelectNetwork={onSelectNetwork}
        onSelectChannel={cy.stub()}
      />
    )
    // Open dropdown and select different network
    cy.get('[data-testid="network-dropdown"]').click()
    cy.contains('Friends').click()
    cy.get('@onSelectNetwork').should('have.been.calledWith', {
      network_id: 'net2',
      name: 'Friends',
    })
  })

  it('shows main channel badge', () => {
    cy.mount(
      <Sidebar
        currentNetwork={{ network_id: 'net1', name: 'Work' }}
        onSelectNetwork={cy.stub()}
        onSelectChannel={cy.stub()}
      />
    )
    // General channel should have "main" badge
    cy.get('[data-testid="channel-general"]').within(() => {
      cy.contains('main').should('be.visible')
    })
    // Random channel should not have badge
    cy.get('[data-testid="channel-random"]').within(() => {
      cy.contains('main').should('not.exist')
    })
  })
})
