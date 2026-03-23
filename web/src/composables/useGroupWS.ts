import { ref, onUnmounted } from 'vue'

export interface GroupWSOptions {
  url: string
  onEvent: (event: Record<string, unknown>) => void
  onClose?: () => void
  onError?: (error: Event) => void
}

export function useGroupWS() {
  const connected = ref(false)
  let ws: WebSocket | null = null
  let pingTimer: ReturnType<typeof setInterval> | undefined
  let reconnectTimer: ReturnType<typeof setTimeout> | undefined
  let opts: GroupWSOptions | null = null
  let manualClose = false

  function connect(options: GroupWSOptions) {
    opts = options
    manualClose = false
    doConnect()
  }

  function doConnect() {
    if (!opts) return
    if (ws) {
      ws.onclose = null
      ws.onerror = null
      ws.onmessage = null
      ws.close()
    }

    ws = new WebSocket(opts.url)

    ws.onopen = () => {
      connected.value = true
      // Start heartbeat
      pingTimer = setInterval(() => {
        if (ws?.readyState === WebSocket.OPEN) {
          ws.send(JSON.stringify({ type: 'pong' }))
        }
      }, 25000)
    }

    ws.onmessage = (event) => {
      try {
        const data = JSON.parse(event.data)
        if (data.type === 'ping') {
          // Reply with pong
          ws?.send(JSON.stringify({ type: 'pong' }))
          return
        }
        opts?.onEvent(data)
      } catch {
        // Skip malformed messages
      }
    }

    ws.onclose = () => {
      connected.value = false
      clearInterval(pingTimer)
      opts?.onClose?.()
      if (!manualClose) {
        reconnectTimer = setTimeout(() => doConnect(), 3000)
      }
    }

    ws.onerror = (err) => {
      opts?.onError?.(err)
    }
  }

  function send(data: Record<string, unknown>) {
    if (ws?.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify(data))
    }
  }

  function close() {
    manualClose = true
    clearInterval(pingTimer)
    clearTimeout(reconnectTimer)
    ws?.close()
    ws = null
    connected.value = false
  }

  onUnmounted(() => {
    close()
  })

  return {
    connected,
    connect,
    send,
    close,
  }
}
