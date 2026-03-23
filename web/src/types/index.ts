// ── Token ──────────────────────────────────────
export interface Token {
  token: string
  name: string
  created_at: string
  expires_at: string
  bot_online: boolean
  chat_count?: number
}

// ── Chat ───────────────────────────────────────
export interface ChatSession {
  id: string
  number: number
}

export interface ToolCall {
  id: string
  name: string
  arguments: string
  result?: string
  status: 'running' | 'completed' | 'error'
}

export interface ChatMessage {
  role: 'user' | 'assistant'
  content: string
  timestamp: number
  toolCalls?: ToolCall[]
  thinking?: string
  media?: MediaItem[]
}

export interface MediaItem {
  type: 'url' | 'base64'
  content: string
  mimeType?: string
}

// ── SSE Events ─────────────────────────────────
export type SSEEventType =
  | 'session'
  | 'chunk'
  | 'tool_call'
  | 'tool_result'
  | 'thinking'
  | 'media'
  | 'error'
  | 'done'

export interface SSEEvent {
  event: SSEEventType
  data: Record<string, unknown>
}

// ── API Response ───────────────────────────────
export interface ApiResponse {
  code: number
  error?: string
  [key: string]: unknown
}

export interface TokenListResponse {
  code: number
  tokens: Token[]
  total: number
  page: number
  page_size: number
  online_bots: number
  total_tokens: number
  active_chats?: number
}

export interface SessionListResponse {
  code: number
  sessions: ChatSession[]
}

export interface CreateSessionResponse {
  code: number
  sessionId: string
  sessionNumber: number
  sessions: ChatSession[]
}

// ── Admin Auth ─────────────────────────────────
export interface AuthStatusResponse {
  code: number
  need_setup: boolean
  authenticated: boolean
}

// ── Metrics ────────────────────────────────────
export interface MetricSample {
  labels: Record<string, string>
  value: number
}

export interface MetricFamily {
  name: string
  type: 'counter' | 'gauge' | 'histogram' | 'summary' | 'untyped'
  help: string
  samples: MetricSample[]
}

// ── Group ─────────────────────────────────────
export interface GroupInfo {
  group_id: string
  name: string
  description: string
  agent_count?: number
  created_at: number
  updated_at: number
}

export interface GroupAgent {
  token: string
  name: string
  role?: 'leader' | 'member'
  bot_online: boolean
  added_at: number
}

export interface GroupDetail {
  group: GroupInfo
  agents: GroupAgent[]
}

export interface GroupListResponse {
  code: number
  groups: GroupInfo[]
  total: number
  page: number
  page_size: number
}

// ── Group Chat ────────────────────────────────
export interface GroupChatAgent {
  token: string
  name: string
  role?: 'leader' | 'member'
}

export interface GroupChatMessage {
  role: 'user' | 'assistant' | 'system'
  content: string
  timestamp: number
  agent?: GroupChatAgent
  toolCalls?: ToolCall[]
  thinking?: string
  media?: MediaItem[]
  isDelegation?: boolean
}

export type AgentStatus = 'idle' | 'streaming' | 'completed'

// Ordered content blocks for chronological rendering
export type ContentBlock =
  | { type: 'thinking'; content: string }
  | { type: 'tool'; tool: ToolCall }
  | { type: 'text'; content: string }
  | { type: 'media'; url: string }
  | { type: 'error'; content: string }

export interface AgentPanel {
  agent: GroupChatAgent
  status: AgentStatus
  collapsed: boolean
  blocks: ContentBlock[]
  color: string
  // Legacy — kept for sortedPanels filter compatibility
  messages: GroupChatMessage[]
}
