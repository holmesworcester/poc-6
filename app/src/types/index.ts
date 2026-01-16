// API response types matching our Python API

export interface NetworkResponse {
  network_id: string
  name: string
  created_at?: number
}

export interface ChannelResponse {
  channel_id: string
  name: string
  group_id: string
  disappearing_time_ms?: number
  is_main?: boolean
  created_at?: number
}

export interface UserResponse {
  user_id: string
  name: string | null
  created_at?: number
}

export interface AttachmentResponse {
  file_id: string
  filename: string
  mime_type: string | null
  size: number | null
  status: string
}

export interface ReactionResponse {
  emoji: string
  count: number
  users: string[]
}

export interface MessageResponse {
  message_id: string
  channel_id: string
  author_id: string
  author_name: string | null
  content: string
  created_at: number
  edited_at: number | null
  attachments: AttachmentResponse[]
  reactions: ReactionResponse[]
}

export interface PaginatedResponse<T> {
  items: T[]
  cursors?: {
    older: string | null
    newer: string | null
  }
}

// App state types
export type Screen = 'sidebar' | 'chat'

export interface AppState {
  screen: Screen
  currentNetworkId: string | null
  currentChannelId: string | null
  sidebarOpen: boolean
}
