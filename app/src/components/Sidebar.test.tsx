import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { Sidebar } from './Sidebar'

const mockNetworks = [
  { network_id: 'net1', name: 'Work' },
  { network_id: 'net2', name: 'Friends' },
]

const mockChannels = [
  { channel_id: 'ch1', name: 'general', group_id: 'g1', is_main: true },
  { channel_id: 'ch2', name: 'random', group_id: 'g2', is_main: false },
]

const mockUsers = [
  { user_id: 'u1', name: 'Alice' },
  { user_id: 'u2', name: 'Bob' },
]

beforeEach(() => {
  global.fetch = vi.fn((url: string) => {
    if (url.includes('/networks') && !url.includes('/channels') && !url.includes('/users')) {
      return Promise.resolve({
        ok: true,
        json: () => Promise.resolve({ items: mockNetworks }),
      })
    }
    if (url.includes('/channels')) {
      return Promise.resolve({
        ok: true,
        json: () => Promise.resolve({ items: mockChannels }),
      })
    }
    if (url.includes('/users')) {
      return Promise.resolve({
        ok: true,
        json: () => Promise.resolve({ items: mockUsers }),
      })
    }
    return Promise.resolve({ ok: false, status: 404 })
  }) as any
})

describe('Sidebar', () => {
  it('renders sidebar with loading state', () => {
    global.fetch = vi.fn(() => new Promise(() => {})) as any
    render(
      <Sidebar
        currentNetwork={null}
        onSelectNetwork={vi.fn()}
        onSelectChannel={vi.fn()}
      />
    )
    expect(screen.getByText('Loading...')).toBeInTheDocument()
  })

  it('displays networks in dropdown', async () => {
    render(
      <Sidebar
        currentNetwork={mockNetworks[0]}
        onSelectNetwork={vi.fn()}
        onSelectChannel={vi.fn()}
      />
    )

    await waitFor(() => {
      expect(screen.getByText('Work')).toBeInTheDocument()
    })

    // Open dropdown
    fireEvent.click(screen.getByTestId('network-dropdown'))
    await waitFor(() => {
      expect(screen.getByTestId('network-list')).toBeInTheDocument()
    })
  })

  it('displays channels list', async () => {
    render(
      <Sidebar
        currentNetwork={mockNetworks[0]}
        onSelectNetwork={vi.fn()}
        onSelectChannel={vi.fn()}
      />
    )

    await waitFor(() => {
      expect(screen.getByTestId('channel-general')).toBeInTheDocument()
      expect(screen.getByTestId('channel-random')).toBeInTheDocument()
    })
  })

  it('displays users list', async () => {
    render(
      <Sidebar
        currentNetwork={mockNetworks[0]}
        onSelectNetwork={vi.fn()}
        onSelectChannel={vi.fn()}
      />
    )

    await waitFor(() => {
      expect(screen.getByText('Alice')).toBeInTheDocument()
      expect(screen.getByText('Bob')).toBeInTheDocument()
    })
  })

  it('calls onSelectChannel when channel is clicked', async () => {
    const onSelectChannel = vi.fn()
    render(
      <Sidebar
        currentNetwork={mockNetworks[0]}
        onSelectNetwork={vi.fn()}
        onSelectChannel={onSelectChannel}
      />
    )

    await waitFor(() => {
      expect(screen.getByTestId('channel-general')).toBeInTheDocument()
    })

    fireEvent.click(screen.getByTestId('channel-general'))
    expect(onSelectChannel).toHaveBeenCalledOnce()
  })

  it('shows main channel badge', async () => {
    render(
      <Sidebar
        currentNetwork={mockNetworks[0]}
        onSelectNetwork={vi.fn()}
        onSelectChannel={vi.fn()}
      />
    )

    await waitFor(() => {
      expect(screen.getByTestId('channel-general')).toBeInTheDocument()
    })

    // General channel should have "main" badge
    const generalChannel = screen.getByTestId('channel-general')
    expect(generalChannel).toHaveTextContent('main')
  })
})
