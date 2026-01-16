import React, { useMemo } from 'react'
import { View, Text, StyleSheet } from 'react-native'
import { palette } from '../styles/theme'
import { DateTime } from 'luxon'

interface MessagesDividerProps {
  title?: string
  timestamp?: number
  isSticky?: boolean
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

export const MessagesDivider: React.FC<MessagesDividerProps> = ({ title, timestamp, isSticky }) => {
  const displayText = useMemo(() => {
    if (timestamp !== undefined) {
      return formatDisplayDate(timestamp)
    }
    if (title) {
      return title
    }
    return 'Unknown Date'
  }, [title, timestamp])

  return (
    <View
      style={[styles.container, isSticky && styles.stickyContainer]}
      testID={isSticky ? `sticky-date-${displayText}` : `date-divider-${displayText}`}
    >
      <View style={[styles.pillContainer, isSticky && styles.stickyPillContainer]}>
        <Text style={styles.title}>{displayText}</Text>
      </View>
    </View>
  )
}

const styles = StyleSheet.create({
  container: {
    flexDirection: 'row',
    justifyContent: 'center',
    alignItems: 'center',
    paddingVertical: 10,
  },
  stickyContainer: {
    position: 'absolute',
    top: 0,
    left: 0,
    right: 0,
    zIndex: 10,
    paddingVertical: 8,
  },
  pillContainer: {
    paddingHorizontal: 18,
    paddingVertical: 5,
    backgroundColor: palette.background.white,
    borderWidth: 1,
    borderColor: palette.background.gray06,
    borderRadius: 72,
  },
  stickyPillContainer: {
    backgroundColor: palette.background.white,
    borderColor: '#EEEEEE',
  },
  title: {
    color: palette.typography.main,
    fontSize: 13,
    fontWeight: '400',
    lineHeight: 15,
  },
})
