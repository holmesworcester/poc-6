import React, { useState, useEffect, useCallback, useRef, useMemo } from 'react'
import {
  View,
  Text,
  FlatList,
  TextInput,
  TouchableOpacity,
  StyleSheet,
  Animated,
  NativeSyntheticEvent,
  NativeScrollEvent,
  ViewToken,
} from 'react-native'
import { palette } from '../styles/theme'
import { getMessages, sendMessage } from '../api/client'
import { Message } from './Message'
import { MessagesDivider } from './MessagesDivider'
import { usePolling } from '../hooks/usePolling'
import type { NetworkResponse, ChannelResponse, MessageResponse } from '../types'
import { DateTime } from 'luxon'

// UI constants from Quiet mobile
const DEFAULT_PADDING = 20
const DATE_FADE_IN_DURATION = 100
const DATE_FADE_OUT_DURATION = 200
const DATE_VISIBILITY_TIMEOUT = 2000

interface ChatProps {
  network: NetworkResponse
  channel: ChannelResponse
  onBack: () => void
}

interface ListItem {
  type: 'divider' | 'message'
  id: string
  displayDate: string
  messageGroup?: MessageResponse[]
}

// Group messages by date and sender (like Quiet mobile)
function groupMessages(messages: MessageResponse[]): ListItem[] {
  if (messages.length === 0) return []

  const out: ListItem[] = []
  const byDate: Record<string, MessageResponse[][]> = {}

  // Sort by created_at ascending
  const sorted = [...messages].sort((a, b) => a.created_at - b.created_at)

  // Group by date
  sorted.forEach((msg) => {
    const date = formatDisplayDate(msg.created_at)
    if (!byDate[date]) byDate[date] = []

    // Group consecutive messages from same author
    const lastGroup = byDate[date][byDate[date].length - 1]
    if (lastGroup && lastGroup[0].author_id === msg.author_id) {
      lastGroup.push(msg)
    } else {
      byDate[date].push([msg])
    }
  })

  // Build list items
  Object.keys(byDate).forEach((dateKey) => {
    out.push({ type: 'divider', id: `divider-${dateKey}`, displayDate: dateKey })

    byDate[dateKey].forEach((group) => {
      if (group.length === 0) return
      out.push({
        type: 'message',
        id: group[0].message_id,
        displayDate: dateKey,
        messageGroup: group,
      })
    })
  })

  // Reverse for inverted FlatList (newest at bottom)
  return out.reverse()
}

function formatDisplayDate(timestamp: number): string {
  const tzOffsetHours = -new Date().getTimezoneOffset() / 60
  const formattedOffset = `UTC${tzOffsetHours >= 0 ? '+' : ''}${tzOffsetHours}`

  const messageDate = DateTime.fromSeconds(timestamp).setZone(formattedOffset)
  const now = DateTime.now().setZone(formattedOffset)
  const diffInDays = now.startOf('day').diff(messageDate.startOf('day'), 'days').days

  if (diffInDays === 0) return 'Today'
  if (diffInDays === 1) return 'Yesterday'
  if (diffInDays >= 2 && diffInDays <= 4) return messageDate.toFormat('cccc')
  return messageDate.toLocaleString(DateTime.DATE_MED)
}

export const Chat: React.FC<ChatProps> = ({ network, channel, onBack }) => {
  const [messages, setMessages] = useState<MessageResponse[]>([])
  const [inputText, setInputText] = useState('')
  const [loading, setLoading] = useState(true)
  const [currentVisibleTimestamp, setCurrentVisibleTimestamp] = useState<number | null>(null)
  const [olderCursor, setOlderCursor] = useState<string | null>(null)

  const flatListRef = useRef<FlatList<ListItem>>(null)
  const fadeAnim = useRef(new Animated.Value(0)).current
  const isScrolling = useRef(false)
  const scrollTimer = useRef<ReturnType<typeof setTimeout> | null>(null)

  // Fetch messages (used by polling)
  const fetchMessages = useCallback(async () => {
    try {
      const response = await getMessages(network.network_id, channel.channel_id, { limit: 50 })
      setMessages(response.items)
      setOlderCursor(response.cursors?.older || null)
      if (loading) setLoading(false)
    } catch (err) {
      console.error('Failed to load messages:', err)
      if (loading) setLoading(false)
    }
  }, [network.network_id, channel.channel_id, loading])

  // Initial fetch
  useEffect(() => {
    setLoading(true)
    fetchMessages()
  }, [network.network_id, channel.channel_id]) // eslint-disable-line react-hooks/exhaustive-deps

  // Poll for new messages every 100ms when visible
  const { pollNow } = usePolling({
    enabled: !loading,
    onPoll: fetchMessages,
  })

  // Group messages for display
  const listData = useMemo(() => groupMessages(messages), [messages])

  // Load older messages
  const handleEndReached = useCallback(async () => {
    if (!olderCursor) return

    try {
      const response = await getMessages(network.network_id, channel.channel_id, {
        cursor: olderCursor,
        direction: 'older',
        limit: 50,
      })
      setMessages((prev) => [...prev, ...response.items])
      setOlderCursor(response.cursors?.older || null)
    } catch (err) {
      console.error('Failed to load older messages:', err)
    }
  }, [network.network_id, channel.channel_id, olderCursor])

  // Send message
  const handleSend = useCallback(async () => {
    const text = inputText.trim()
    if (!text) return

    setInputText('')
    try {
      const result = await sendMessage(network.network_id, channel.channel_id, text)
      // Optimistically add the message
      const newMessage: MessageResponse = {
        message_id: result.message_id,
        channel_id: channel.channel_id,
        author_id: 'self', // Will be replaced on next fetch
        author_name: 'You',
        content: text,
        created_at: Math.floor(Date.now() / 1000),
        edited_at: null,
        attachments: [],
        reactions: [],
      }
      setMessages((prev) => [newMessage, ...prev])
      // Trigger immediate poll to sync with server
      pollNow()
    } catch (err) {
      console.error('Failed to send message:', err)
      setInputText(text) // Restore input on error
    }
  }, [inputText, network.network_id, channel.channel_id])

  // Scroll handling for floating date
  const handleScroll = useCallback(
    (_event: NativeSyntheticEvent<NativeScrollEvent>) => {
      if (!isScrolling.current) {
        isScrolling.current = true
        Animated.timing(fadeAnim, {
          toValue: 1,
          duration: DATE_FADE_IN_DURATION,
          useNativeDriver: true,
        }).start()
      }

      if (scrollTimer.current) {
        clearTimeout(scrollTimer.current)
      }

      scrollTimer.current = setTimeout(() => {
        isScrolling.current = false
        Animated.timing(fadeAnim, {
          toValue: 0,
          duration: DATE_FADE_OUT_DURATION,
          useNativeDriver: true,
        }).start()
      }, DATE_VISIBILITY_TIMEOUT)
    },
    [fadeAnim]
  )

  // Track visible items for date marker
  const viewabilityConfig = useRef({
    itemVisiblePercentThreshold: 0,
  }).current

  // Use ref for onViewableItemsChanged to avoid FlatList invariant violation
  // "Changing onViewableItemsChanged on the fly is not supported"
  const onViewableItemsChangedRef = useRef(
    ({ viewableItems }: { viewableItems: ViewToken[] }) => {
      if (!viewableItems || viewableItems.length === 0) return

      const visibleMessages = viewableItems.filter((v) => {
        const item = v.item as ListItem
        return item?.type === 'message'
      })

      if (visibleMessages.length === 0) return

      let oldestTimestamp = Number.MAX_SAFE_INTEGER
      visibleMessages.forEach((token) => {
        const messageItem = token.item as ListItem
        if (messageItem.type === 'message' && messageItem.messageGroup && messageItem.messageGroup.length > 0) {
          const messageTimestamp = messageItem.messageGroup[0].created_at
          if (messageTimestamp && messageTimestamp < oldestTimestamp) {
            oldestTimestamp = messageTimestamp
          }
        }
      })

      if (oldestTimestamp !== Number.MAX_SAFE_INTEGER) {
        setCurrentVisibleTimestamp(oldestTimestamp)
      }
    }
  ).current

  // Cleanup timer on unmount
  useEffect(() => {
    return () => {
      if (scrollTimer.current) {
        clearTimeout(scrollTimer.current)
      }
    }
  }, [])

  const renderItem = useCallback(({ item }: { item: ListItem }): React.ReactElement => {
    if (item.type === 'divider') {
      return <MessagesDivider title={item.displayDate} />
    }

    return <Message data={item.messageGroup!} />
  }, [])

  return (
    <View style={styles.container} testID={`chat-${channel.name}`}>
      {/* Header */}
      <View style={styles.header}>
        <TouchableOpacity style={styles.backButton} onPress={onBack} testID="back-button">
          <Text style={styles.backArrow}>←</Text>
        </TouchableOpacity>
        <Text style={styles.channelTitle}>#{channel.name}</Text>
        <View style={styles.headerSpacer} />
      </View>

      {/* Messages area */}
      <View style={styles.messagesContainer}>
        {/* Floating date marker */}
        {currentVisibleTimestamp && (
          <Animated.View style={[styles.dateMarker, { opacity: fadeAnim }]}>
            <MessagesDivider timestamp={currentVisibleTimestamp} isSticky />
          </Animated.View>
        )}

        {loading ? (
          <View style={styles.loading}>
            <Text style={styles.loadingText}>Loading messages...</Text>
          </View>
        ) : messages.length === 0 ? (
          <View style={styles.empty}>
            <Text style={styles.emptyText}>No messages yet</Text>
            <Text style={styles.emptyHint}>Be the first to say something!</Text>
          </View>
        ) : (
          <FlatList
            ref={flatListRef}
            style={styles.list}
            inverted
            data={listData}
            keyExtractor={(item) => item.id}
            renderItem={renderItem}
            onEndReached={handleEndReached}
            onEndReachedThreshold={0.7}
            viewabilityConfig={viewabilityConfig}
            onViewableItemsChanged={onViewableItemsChangedRef}
            showsVerticalScrollIndicator={false}
            onScroll={handleScroll}
            scrollEventThrottle={16}
            testID="message-list"
          />
        )}
      </View>

      {/* Input area */}
      <View style={styles.inputContainer}>
        <View style={styles.inputRow}>
          <View style={styles.inputWrapper}>
            <TextInput
              style={styles.input}
              value={inputText}
              onChangeText={setInputText}
              placeholder={`Message #${channel.name}`}
              placeholderTextColor={palette.typography.subtitle}
              multiline
              testID="message-input"
            />
            <TouchableOpacity style={styles.attachButton} testID="attach-button">
              <Text style={styles.attachIcon}>📎</Text>
            </TouchableOpacity>
          </View>
          <TouchableOpacity
            style={[styles.sendButton, !inputText.trim() && styles.sendButtonDisabled]}
            onPress={handleSend}
            disabled={!inputText.trim()}
            testID="send-button"
          >
            <Text style={styles.sendIcon}>➤</Text>
          </TouchableOpacity>
        </View>
      </View>
    </View>
  )
}

const styles = StyleSheet.create({
  container: {
    flex: 1,
    backgroundColor: palette.background.white,
  },
  header: {
    flexDirection: 'row',
    alignItems: 'center',
    paddingHorizontal: 8,
    paddingVertical: 12,
    borderBottomWidth: 1,
    borderBottomColor: palette.background.gray06,
  },
  backButton: {
    width: 44,
    height: 44,
    justifyContent: 'center',
    alignItems: 'center',
  },
  backArrow: {
    fontSize: 20,
    color: palette.typography.main,
  },
  channelTitle: {
    flex: 1,
    fontSize: 16,
    fontWeight: '600',
    textAlign: 'center',
    color: palette.typography.main,
  },
  headerSpacer: {
    width: 44,
  },
  messagesContainer: {
    flex: 1,
  },
  dateMarker: {
    position: 'absolute',
    zIndex: 20,
    width: '100%',
    backgroundColor: palette.background.white,
  },
  list: {
    paddingHorizontal: DEFAULT_PADDING,
  },
  loading: {
    flex: 1,
    justifyContent: 'center',
    alignItems: 'center',
  },
  loadingText: {
    fontSize: 16,
    color: palette.typography.subtitle,
  },
  empty: {
    flex: 1,
    justifyContent: 'center',
    alignItems: 'center',
  },
  emptyText: {
    fontSize: 16,
    color: palette.typography.main,
    marginBottom: 4,
  },
  emptyHint: {
    fontSize: 14,
    color: palette.typography.subtitle,
  },
  inputContainer: {
    paddingHorizontal: DEFAULT_PADDING,
    paddingVertical: 12,
    borderTopWidth: 1,
    borderTopColor: palette.background.gray06,
  },
  inputRow: {
    flexDirection: 'row',
    alignItems: 'flex-end',
  },
  inputWrapper: {
    flex: 1,
    flexDirection: 'row',
    alignItems: 'center',
    backgroundColor: palette.background.gray06,
    borderRadius: 20,
    paddingHorizontal: 16,
    paddingVertical: 8,
    marginRight: 8,
  },
  input: {
    flex: 1,
    fontSize: 15,
    color: palette.typography.main,
    maxHeight: 100,
    paddingTop: 0,
    paddingBottom: 0,
  },
  attachButton: {
    padding: 4,
  },
  attachIcon: {
    fontSize: 18,
  },
  sendButton: {
    width: 40,
    height: 40,
    borderRadius: 20,
    backgroundColor: palette.background.lushSky,
    justifyContent: 'center',
    alignItems: 'center',
  },
  sendButtonDisabled: {
    backgroundColor: palette.typography.grayLight,
  },
  sendIcon: {
    fontSize: 18,
    color: palette.background.white,
  },
})
