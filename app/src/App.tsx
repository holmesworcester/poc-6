import React, { useState, useCallback } from 'react'
import { View, StyleSheet } from 'react-native'
import { Sidebar } from './components/Sidebar'
import { Chat } from './components/Chat'
import { palette } from './styles/theme'
import type { Screen, NetworkResponse, ChannelResponse } from './types'

export const App: React.FC = () => {
  // Navigation state
  const [screen, setScreen] = useState<Screen>('sidebar')

  // Data state
  const [currentNetwork, setCurrentNetwork] = useState<NetworkResponse | null>(null)
  const [currentChannel, setCurrentChannel] = useState<ChannelResponse | null>(null)

  // Navigation handlers
  const handleOpenSidebar = useCallback(() => {
    setScreen('sidebar')
  }, [])

  const handleSelectChannel = useCallback((network: NetworkResponse, channel: ChannelResponse) => {
    setCurrentNetwork(network)
    setCurrentChannel(channel)
    setScreen('chat')
  }, [])

  const handleSelectNetwork = useCallback((network: NetworkResponse) => {
    setCurrentNetwork(network)
    // Stay on sidebar to show channels
  }, [])

  return (
    <View style={styles.container} testID="app-root">
      {screen === 'sidebar' ? (
        <Sidebar
          currentNetwork={currentNetwork}
          onSelectNetwork={handleSelectNetwork}
          onSelectChannel={handleSelectChannel}
        />
      ) : (
        <Chat
          network={currentNetwork!}
          channel={currentChannel!}
          onBack={handleOpenSidebar}
        />
      )}
    </View>
  )
}

const styles = StyleSheet.create({
  container: {
    flex: 1,
    backgroundColor: palette.background.white,
  },
})
