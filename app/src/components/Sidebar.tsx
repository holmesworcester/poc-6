import React, { useState, useEffect, useCallback } from 'react'
import { View, Text, FlatList, TouchableOpacity, StyleSheet } from 'react-native'
import { palette } from '../styles/theme'
import { getNetworks, getChannels, getUsers } from '../api/client'
import { usePolling } from '../hooks/usePolling'
import type { NetworkResponse, ChannelResponse, UserResponse } from '../types'

interface SidebarProps {
  currentNetwork: NetworkResponse | null
  onSelectNetwork: (network: NetworkResponse) => void
  onSelectChannel: (network: NetworkResponse, channel: ChannelResponse) => void
}

export const Sidebar: React.FC<SidebarProps> = ({
  currentNetwork,
  onSelectNetwork,
  onSelectChannel,
}) => {
  const [networks, setNetworks] = useState<NetworkResponse[]>([])
  const [channels, setChannels] = useState<ChannelResponse[]>([])
  const [users, setUsers] = useState<UserResponse[]>([])
  const [networkDropdownOpen, setNetworkDropdownOpen] = useState(false)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  // Fetch networks on mount
  useEffect(() => {
    const fetchNetworks = async () => {
      try {
        const response = await getNetworks()
        setNetworks(response.items)
        // Auto-select first network if none selected
        if (!currentNetwork && response.items.length > 0) {
          onSelectNetwork(response.items[0])
        }
        setLoading(false)
      } catch (err) {
        setError(err instanceof Error ? err.message : 'Failed to load networks')
        setLoading(false)
      }
    }
    fetchNetworks()
  }, [currentNetwork, onSelectNetwork])

  // Fetch channels and users
  const fetchChannelsAndUsers = useCallback(async () => {
    if (!currentNetwork) return

    try {
      const [channelsRes, usersRes] = await Promise.all([
        getChannels(currentNetwork.network_id),
        getUsers(currentNetwork.network_id),
      ])
      setChannels(channelsRes.items)
      setUsers(usersRes.items)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load data')
    }
  }, [currentNetwork])

  // Poll channels and users every 100ms when visible
  usePolling({
    enabled: !loading && currentNetwork !== null,
    onPoll: fetchChannelsAndUsers,
  })

  const handleNetworkSelect = useCallback((network: NetworkResponse) => {
    onSelectNetwork(network)
    setNetworkDropdownOpen(false)
  }, [onSelectNetwork])

  const handleChannelSelect = useCallback((channel: ChannelResponse) => {
    if (currentNetwork) {
      onSelectChannel(currentNetwork, channel)
    }
  }, [currentNetwork, onSelectChannel])

  const renderChannelItem = ({ item }: { item: ChannelResponse }): React.ReactElement => (
    <TouchableOpacity
      style={styles.channelItem}
      onPress={() => handleChannelSelect(item)}
      testID={`channel-${item.name}`}
    >
      <Text style={styles.channelHash}>#</Text>
      <Text style={styles.channelName}>{item.name}</Text>
      {item.is_main && <Text style={styles.mainBadge}>main</Text>}
    </TouchableOpacity>
  )

  const renderUserItem = ({ item }: { item: UserResponse }): React.ReactElement => (
    <View style={styles.userItem} testID={`user-${item.user_id}`}>
      <View style={styles.avatar} />
      <Text style={styles.userName}>{item.name || 'Anonymous'}</Text>
    </View>
  )

  if (loading) {
    return (
      <View style={styles.container} testID="sidebar">
        <View style={styles.loading}>
          <Text style={styles.loadingText}>Loading...</Text>
        </View>
      </View>
    )
  }

  if (error) {
    return (
      <View style={styles.container} testID="sidebar">
        <View style={styles.error}>
          <Text style={styles.errorText}>{error}</Text>
        </View>
      </View>
    )
  }

  return (
    <View style={styles.container} testID="sidebar">
      {/* Header with network dropdown */}
      <View style={styles.header}>
        <TouchableOpacity
          style={styles.networkDropdown}
          onPress={() => setNetworkDropdownOpen(!networkDropdownOpen)}
          testID="network-dropdown"
        >
          <Text style={styles.networkName}>
            {currentNetwork?.name || 'Select Network'}
          </Text>
          <Text style={styles.dropdownArrow}>{networkDropdownOpen ? '▲' : '▼'}</Text>
        </TouchableOpacity>
      </View>

      {/* Network dropdown list */}
      {networkDropdownOpen && (
        <View style={styles.dropdownList} testID="network-list">
          {networks.map((network) => (
            <TouchableOpacity
              key={network.network_id}
              style={[
                styles.dropdownItem,
                currentNetwork?.network_id === network.network_id && styles.dropdownItemSelected,
              ]}
              onPress={() => handleNetworkSelect(network)}
              testID={`network-${network.network_id}`}
            >
              <Text style={styles.dropdownItemText}>{network.name}</Text>
              {currentNetwork?.network_id === network.network_id && (
                <Text style={styles.checkmark}>✓</Text>
              )}
            </TouchableOpacity>
          ))}
        </View>
      )}

      {/* Channels section */}
      <View style={styles.section}>
        <Text style={styles.sectionTitle}>CHANNELS</Text>
        <FlatList
          data={channels}
          keyExtractor={(item) => item.channel_id}
          renderItem={renderChannelItem}
          testID="channel-list"
        />
      </View>

      {/* Members section */}
      <View style={styles.section}>
        <Text style={styles.sectionTitle}>MEMBERS ({users.length})</Text>
        <FlatList
          data={users}
          keyExtractor={(item) => item.user_id}
          renderItem={renderUserItem}
          testID="user-list"
        />
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
    paddingHorizontal: 16,
    paddingVertical: 12,
    borderBottomWidth: 1,
    borderBottomColor: palette.background.gray06,
  },
  networkDropdown: {
    flexDirection: 'row',
    alignItems: 'center',
    justifyContent: 'space-between',
    paddingVertical: 8,
    paddingHorizontal: 12,
    backgroundColor: palette.background.gray06,
    borderRadius: 8,
  },
  networkName: {
    fontSize: 16,
    fontWeight: '600',
    color: palette.typography.main,
  },
  dropdownArrow: {
    fontSize: 12,
    color: palette.typography.gray50,
  },
  dropdownList: {
    backgroundColor: palette.background.white,
    borderBottomWidth: 1,
    borderBottomColor: palette.background.gray06,
  },
  dropdownItem: {
    flexDirection: 'row',
    alignItems: 'center',
    justifyContent: 'space-between',
    paddingVertical: 12,
    paddingHorizontal: 16,
  },
  dropdownItemSelected: {
    backgroundColor: palette.background.gray06,
  },
  dropdownItemText: {
    fontSize: 15,
    color: palette.typography.main,
  },
  checkmark: {
    fontSize: 14,
    color: palette.background.lushSky,
  },
  section: {
    flex: 1,
    paddingTop: 16,
  },
  sectionTitle: {
    fontSize: 12,
    fontWeight: '600',
    color: palette.typography.gray50,
    paddingHorizontal: 16,
    paddingBottom: 8,
    letterSpacing: 0.5,
  },
  channelItem: {
    flexDirection: 'row',
    alignItems: 'center',
    paddingVertical: 10,
    paddingHorizontal: 16,
  },
  channelHash: {
    fontSize: 16,
    color: palette.typography.gray50,
    marginRight: 4,
  },
  channelName: {
    fontSize: 15,
    color: palette.typography.main,
  },
  mainBadge: {
    fontSize: 10,
    color: palette.background.lushSky,
    marginLeft: 8,
    fontWeight: '500',
  },
  userItem: {
    flexDirection: 'row',
    alignItems: 'center',
    paddingVertical: 8,
    paddingHorizontal: 16,
  },
  avatar: {
    width: 32,
    height: 32,
    borderRadius: 16,
    backgroundColor: palette.typography.grayLight,
    marginRight: 12,
  },
  userName: {
    fontSize: 14,
    color: palette.typography.main,
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
  error: {
    flex: 1,
    justifyContent: 'center',
    alignItems: 'center',
    padding: 20,
  },
  errorText: {
    fontSize: 14,
    color: palette.typography.error,
    textAlign: 'center',
  },
})
