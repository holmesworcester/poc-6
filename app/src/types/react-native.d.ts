// Type declarations for react-native when using react-native-web
// This provides proper types for the RN primitives we use

declare module 'react-native' {
  import * as React from 'react'

  // Core types
  export type ColorValue = string
  export type DimensionValue = number | string | 'auto' | undefined

  // Style types
  export interface ViewStyle {
    flex?: number
    flexDirection?: 'row' | 'column' | 'row-reverse' | 'column-reverse'
    justifyContent?: 'flex-start' | 'flex-end' | 'center' | 'space-between' | 'space-around' | 'space-evenly'
    alignItems?: 'flex-start' | 'flex-end' | 'center' | 'stretch' | 'baseline'
    alignSelf?: 'auto' | 'flex-start' | 'flex-end' | 'center' | 'stretch' | 'baseline'
    position?: 'absolute' | 'relative'
    top?: DimensionValue
    right?: DimensionValue
    bottom?: DimensionValue
    left?: DimensionValue
    width?: DimensionValue
    height?: DimensionValue
    minWidth?: DimensionValue
    minHeight?: DimensionValue
    maxWidth?: DimensionValue
    maxHeight?: DimensionValue
    margin?: DimensionValue
    marginTop?: DimensionValue
    marginRight?: DimensionValue
    marginBottom?: DimensionValue
    marginLeft?: DimensionValue
    marginHorizontal?: DimensionValue
    marginVertical?: DimensionValue
    padding?: DimensionValue
    paddingTop?: DimensionValue
    paddingRight?: DimensionValue
    paddingBottom?: DimensionValue
    paddingLeft?: DimensionValue
    paddingHorizontal?: DimensionValue
    paddingVertical?: DimensionValue
    borderWidth?: number
    borderTopWidth?: number
    borderRightWidth?: number
    borderBottomWidth?: number
    borderLeftWidth?: number
    borderRadius?: number
    borderTopLeftRadius?: number
    borderTopRightRadius?: number
    borderBottomLeftRadius?: number
    borderBottomRightRadius?: number
    borderColor?: ColorValue
    borderTopColor?: ColorValue
    borderRightColor?: ColorValue
    borderBottomColor?: ColorValue
    borderLeftColor?: ColorValue
    backgroundColor?: ColorValue
    opacity?: number
    overflow?: 'visible' | 'hidden' | 'scroll'
    zIndex?: number
    [key: string]: any
  }

  export interface TextStyle extends ViewStyle {
    color?: ColorValue
    fontSize?: number
    fontWeight?: 'normal' | 'bold' | '100' | '200' | '300' | '400' | '500' | '600' | '700' | '800' | '900'
    fontFamily?: string
    fontStyle?: 'normal' | 'italic'
    textAlign?: 'auto' | 'left' | 'right' | 'center' | 'justify'
    textDecorationLine?: 'none' | 'underline' | 'line-through' | 'underline line-through'
    textTransform?: 'none' | 'capitalize' | 'uppercase' | 'lowercase'
    lineHeight?: number
    letterSpacing?: number
  }

  export interface ImageStyle extends ViewStyle {
    resizeMode?: 'cover' | 'contain' | 'stretch' | 'repeat' | 'center'
    tintColor?: ColorValue
  }

  export type StyleProp<T> = T | T[] | undefined | null | false

  // StyleSheet
  export namespace StyleSheet {
    export function create<T extends Record<string, ViewStyle | TextStyle | ImageStyle>>(styles: T): T
    export function flatten<T>(style: StyleProp<T>): T
    export const absoluteFillObject: ViewStyle
    export const absoluteFill: ViewStyle
  }

  // Components
  export interface ViewProps {
    style?: StyleProp<ViewStyle>
    testID?: string
    children?: React.ReactNode
    [key: string]: any
  }

  export class View extends React.Component<ViewProps> {}

  export interface TextProps {
    style?: StyleProp<TextStyle>
    testID?: string
    numberOfLines?: number
    children?: React.ReactNode
    [key: string]: any
  }

  export class Text extends React.Component<TextProps> {}

  export interface TextInputProps {
    style?: StyleProp<TextStyle>
    testID?: string
    value?: string
    defaultValue?: string
    placeholder?: string
    placeholderTextColor?: ColorValue
    onChangeText?: (text: string) => void
    onChange?: (e: any) => void
    onEndEditing?: (e: any) => void
    multiline?: boolean
    numberOfLines?: number
    maxLength?: number
    editable?: boolean
    autoCapitalize?: 'none' | 'sentences' | 'words' | 'characters'
    autoCorrect?: boolean
    keyboardType?: string
    secureTextEntry?: boolean
    [key: string]: any
  }

  export class TextInput extends React.Component<TextInputProps> {
    clear(): void
    focus(): void
    blur(): void
    setNativeProps(props: object): void
  }

  export interface TouchableOpacityProps extends ViewProps {
    onPress?: () => void
    onLongPress?: () => void
    disabled?: boolean
    activeOpacity?: number
  }

  export class TouchableOpacity extends React.Component<TouchableOpacityProps> {}

  export interface FlatListProps<T> {
    data: T[] | null | undefined
    renderItem: (info: { item: T; index: number }) => React.ReactElement | null
    keyExtractor?: (item: T, index: number) => string
    style?: StyleProp<ViewStyle>
    contentContainerStyle?: StyleProp<ViewStyle>
    inverted?: boolean
    horizontal?: boolean
    numColumns?: number
    onEndReached?: () => void
    onEndReachedThreshold?: number
    onScroll?: (event: any) => void
    onMomentumScrollEnd?: (event: any) => void
    scrollEventThrottle?: number
    showsVerticalScrollIndicator?: boolean
    showsHorizontalScrollIndicator?: boolean
    viewabilityConfig?: object
    onViewableItemsChanged?: (info: { viewableItems: ViewToken[]; changed: ViewToken[] }) => void
    testID?: string
    ListHeaderComponent?: React.ComponentType<any> | React.ReactElement | null
    ListFooterComponent?: React.ComponentType<any> | React.ReactElement | null
    ListEmptyComponent?: React.ComponentType<any> | React.ReactElement | null
    ItemSeparatorComponent?: React.ComponentType<any> | null
    [key: string]: any
  }

  export interface ViewToken {
    item: any
    key: string
    index: number | null
    isViewable: boolean
  }

  export class FlatList<T = any> extends React.Component<FlatListProps<T>> {
    scrollToEnd(params?: { animated?: boolean }): void
    scrollToIndex(params: { index: number; animated?: boolean; viewOffset?: number; viewPosition?: number }): void
    scrollToItem(params: { item: T; animated?: boolean; viewPosition?: number }): void
    scrollToOffset(params: { offset: number; animated?: boolean }): void
  }

  // Animated
  export namespace Animated {
    export class Value {
      constructor(value: number)
      setValue(value: number): void
      setOffset(offset: number): void
      flattenOffset(): void
      extractOffset(): void
      addListener(callback: (state: { value: number }) => void): string
      removeListener(id: string): void
      removeAllListeners(): void
      stopAnimation(callback?: (value: number) => void): void
      interpolate(config: InterpolationConfigType): AnimatedInterpolation
    }

    export interface InterpolationConfigType {
      inputRange: number[]
      outputRange: number[] | string[]
      easing?: (input: number) => number
      extrapolate?: 'extend' | 'identity' | 'clamp'
      extrapolateLeft?: 'extend' | 'identity' | 'clamp'
      extrapolateRight?: 'extend' | 'identity' | 'clamp'
    }

    export type AnimatedInterpolation = Value

    export interface AnimationConfig {
      toValue: number | Value
      duration?: number
      delay?: number
      easing?: (value: number) => number
      useNativeDriver: boolean
    }

    export interface CompositeAnimation {
      start(callback?: (result: { finished: boolean }) => void): void
      stop(): void
      reset(): void
    }

    export function timing(value: Value, config: AnimationConfig): CompositeAnimation

    export class View extends React.Component<ViewProps & { style?: any }> {}
    export class Text extends React.Component<TextProps & { style?: any }> {}
  }

  // Events
  export interface NativeSyntheticEvent<T> {
    nativeEvent: T
    currentTarget: any
    target: any
    bubbles: boolean
    cancelable: boolean
    defaultPrevented: boolean
    eventPhase: number
    isTrusted: boolean
    preventDefault(): void
    stopPropagation(): void
    persist(): void
    timeStamp: number
    type: string
  }

  export interface NativeScrollEvent {
    contentInset: { bottom: number; left: number; right: number; top: number }
    contentOffset: { x: number; y: number }
    contentSize: { height: number; width: number }
    layoutMeasurement: { height: number; width: number }
    zoomScale: number
  }
}
