// API client for fetching data from Python backend

import type {
  NetworkResponse,
  ChannelResponse,
  UserResponse,
  MessageResponse,
  PaginatedResponse,
} from '../types'

// Base URL for API requests (proxied by Vite in dev)
const API_BASE = '/api'

// Convert base64 to URL-safe format
function toUrlSafe(id: string): string {
  return id.replace(/\+/g, '-').replace(/\//g, '_')
}

async function fetchJson<T>(url: string, options?: RequestInit): Promise<T> {
  const response = await fetch(url, {
    ...options,
    headers: {
      'Content-Type': 'application/json',
      ...options?.headers,
    },
  })
  if (!response.ok) {
    throw new Error(`API error: ${response.status} ${response.statusText}`)
  }
  return response.json()
}

// Network endpoints
export async function getNetworks(): Promise<PaginatedResponse<NetworkResponse>> {
  return fetchJson(`${API_BASE}/networks`)
}

// Channel endpoints
export async function getChannels(networkId: string): Promise<PaginatedResponse<ChannelResponse>> {
  return fetchJson(`${API_BASE}/networks/${toUrlSafe(networkId)}/channels`)
}

export async function getChannel(networkId: string, channelId: string): Promise<ChannelResponse> {
  return fetchJson(`${API_BASE}/networks/${toUrlSafe(networkId)}/channels/${toUrlSafe(channelId)}`)
}

// User endpoints
export async function getUsers(networkId: string): Promise<PaginatedResponse<UserResponse>> {
  return fetchJson(`${API_BASE}/networks/${toUrlSafe(networkId)}/users`)
}

// Message endpoints
export async function getMessages(
  networkId: string,
  channelId: string,
  options?: { cursor?: string; direction?: 'older' | 'newer'; limit?: number }
): Promise<PaginatedResponse<MessageResponse>> {
  const params = new URLSearchParams()
  if (options?.cursor) params.set('cursor', options.cursor)
  if (options?.direction) params.set('direction', options.direction)
  if (options?.limit) params.set('limit', options.limit.toString())

  const queryString = params.toString()
  const url = `${API_BASE}/networks/${toUrlSafe(networkId)}/channels/${toUrlSafe(channelId)}/messages${queryString ? `?${queryString}` : ''}`
  return fetchJson(url)
}

export async function sendMessage(
  networkId: string,
  channelId: string,
  text: string
): Promise<{ message_id: string }> {
  return fetchJson(
    `${API_BASE}/networks/${toUrlSafe(networkId)}/channels/${toUrlSafe(channelId)}/messages`,
    {
      method: 'POST',
      body: JSON.stringify({ text }),
    }
  )
}
