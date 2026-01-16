import React from 'react'
import { View, Text, StyleSheet } from 'react-native'
import { palette } from '../styles/theme'
import type { MessageResponse, AttachmentResponse } from '../types'
import { DateTime } from 'luxon'

interface MessageProps {
  data: MessageResponse[]
}

function formatTime(timestamp: number): string {
  const tzOffsetHours = -new Date().getTimezoneOffset() / 60
  const formattedOffset = `UTC${tzOffsetHours >= 0 ? '+' : ''}${tzOffsetHours}`

  const messageTime = DateTime.fromSeconds(timestamp).setZone(formattedOffset)
  return messageTime.toLocaleString(DateTime.TIME_SIMPLE)
}

function formatFileSize(bytes: number | null): string {
  if (bytes === null) return ''
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`
}

const Attachment: React.FC<{ attachment: AttachmentResponse }> = ({ attachment }) => {
  const isImage = attachment.mime_type?.startsWith('image/')

  return (
    <View style={styles.attachment} testID={`attachment-${attachment.file_id}`}>
      <Text style={styles.attachmentIcon}>{isImage ? '🖼️' : '📄'}</Text>
      <View style={styles.attachmentInfo}>
        <Text style={styles.attachmentName} numberOfLines={1}>
          {attachment.filename}
        </Text>
        <Text style={styles.attachmentMeta}>
          {formatFileSize(attachment.size)}
          {attachment.mime_type && ` · ${attachment.mime_type}`}
        </Text>
      </View>
    </View>
  )
}

const MessageInner: React.FC<MessageProps> = ({ data }) => {
  const firstMessage = data[0]
  const time = formatTime(firstMessage.created_at)

  return (
    <View style={styles.container} testID={`message-${firstMessage.message_id}`}>
      {/* Avatar */}
      <View style={styles.avatarContainer}>
        <View style={styles.avatar} />
      </View>

      {/* Content */}
      <View style={styles.content}>
        {/* Header: name + time */}
        <View style={styles.header}>
          <Text style={styles.authorName}>
            {firstMessage.author_name || 'Anonymous'}
          </Text>
          <Text style={styles.timestamp}>{time}</Text>
          {firstMessage.edited_at && <Text style={styles.edited}>(edited)</Text>}
        </View>

        {/* Message texts */}
        {data.map((message, index) => (
          <View key={message.message_id} style={index > 0 ? styles.subsequentMessage : undefined}>
            <Text style={styles.messageText}>{message.content}</Text>

            {/* Attachments */}
            {message.attachments.map((attachment) => (
              <Attachment key={attachment.file_id} attachment={attachment} />
            ))}

            {/* Reactions */}
            {message.reactions.length > 0 && (
              <View style={styles.reactions}>
                {message.reactions.map((reaction) => (
                  <View key={reaction.emoji} style={styles.reaction}>
                    <Text style={styles.reactionEmoji}>{reaction.emoji}</Text>
                    <Text style={styles.reactionCount}>{reaction.count}</Text>
                  </View>
                ))}
              </View>
            )}
          </View>
        ))}
      </View>
    </View>
  )
}

const styles = StyleSheet.create({
  container: {
    flexDirection: 'row',
    paddingBottom: 16,
  },
  avatarContainer: {
    width: 40,
    alignItems: 'center',
    paddingRight: 12,
  },
  avatar: {
    width: 36,
    height: 36,
    borderRadius: 4,
    backgroundColor: palette.typography.grayLight,
  },
  content: {
    flex: 1,
  },
  header: {
    flexDirection: 'row',
    alignItems: 'baseline',
    paddingBottom: 2,
  },
  authorName: {
    fontSize: 15,
    fontWeight: '600',
    color: palette.typography.main,
  },
  timestamp: {
    fontSize: 13,
    color: palette.typography.subtitle,
    marginLeft: 8,
  },
  edited: {
    fontSize: 12,
    color: palette.typography.subtitle,
    marginLeft: 4,
  },
  subsequentMessage: {
    paddingTop: 4,
  },
  messageText: {
    fontSize: 14,
    color: palette.typography.main,
    lineHeight: 20,
  },
  attachment: {
    flexDirection: 'row',
    alignItems: 'center',
    backgroundColor: palette.background.gray06,
    borderRadius: 8,
    padding: 10,
    marginTop: 6,
  },
  attachmentIcon: {
    fontSize: 20,
    marginRight: 10,
  },
  attachmentInfo: {
    flex: 1,
  },
  attachmentName: {
    fontSize: 14,
    fontWeight: '500',
    color: palette.typography.main,
  },
  attachmentMeta: {
    fontSize: 12,
    color: palette.typography.subtitle,
    marginTop: 2,
  },
  reactions: {
    flexDirection: 'row',
    flexWrap: 'wrap',
    marginTop: 6,
  },
  reaction: {
    flexDirection: 'row',
    alignItems: 'center',
    backgroundColor: palette.background.gray06,
    borderRadius: 12,
    paddingHorizontal: 8,
    paddingVertical: 4,
    marginRight: 6,
    marginBottom: 4,
  },
  reactionEmoji: {
    fontSize: 14,
  },
  reactionCount: {
    fontSize: 12,
    color: palette.typography.gray50,
    marginLeft: 4,
  },
})

export const Message = React.memo(MessageInner)
