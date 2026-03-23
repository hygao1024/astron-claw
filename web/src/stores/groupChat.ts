import { defineStore } from 'pinia'
import { ref, computed, triggerRef } from 'vue'
import type {
  GroupChatMessage, GroupChatAgent, ToolCall, MediaItem,
  AgentPanel, AgentStatus, ContentBlock,
} from '@/types'

const AGENT_COLORS = [
  '#4f8ff7', '#22c55e', '#f59e0b', '#ef4444', '#a855f7',
  '#06b6d4', '#ec4899', '#84cc16', '#f97316', '#6366f1',
]

function getAgentColor(token: string): string {
  let hash = 0
  for (let i = 0; i < token.length; i++) {
    hash = token.charCodeAt(i) + ((hash << 5) - hash)
  }
  return AGENT_COLORS[Math.abs(hash) % AGENT_COLORS.length]
}

export const useGroupChatStore = defineStore('groupChat', () => {
  // Timeline messages: user messages + delegation markers
  const messages = ref<GroupChatMessage[]>([])
  const groupId = ref<string | null>(null)
  const agents = ref<GroupChatAgent[]>([])
  const sending = ref(false)

  // Agent panels: per-agent collapsible content with ordered blocks
  const agentPanels = ref<Record<string, AgentPanel>>({})

  // Delegation tracking
  const delegatedAgents = ref(new Set<string>())
  const completedAgents = ref(new Set<string>())
  const activeAgents = ref(new Set<string>())
  const allTasksDone = ref(false)

  // Computed
  const sortedPanels = computed(() => {
    const panels = Object.values(agentPanels.value).filter(p => p.blocks.length > 0)
    return panels.sort((a, b) => {
      if (a.agent.role === 'leader' && b.agent.role !== 'leader') return -1
      if (a.agent.role !== 'leader' && b.agent.role === 'leader') return 1
      const aTime = a.messages[0]?.timestamp ?? Infinity
      const bTime = b.messages[0]?.timestamp ?? Infinity
      return aTime - bTime
    })
  })

  const completionSummary = computed(() => ({
    total: delegatedAgents.value.size,
    done: completedAgents.value.size,
    allDone: allTasksDone.value,
  }))

  function reset() {
    messages.value = []
    groupId.value = null
    agents.value = []
    sending.value = false
    agentPanels.value = {}
    delegatedAgents.value = new Set()
    completedAgents.value = new Set()
    activeAgents.value = new Set()
    allTasksDone.value = false
  }

  function addUserMessage(content: string, media?: MediaItem[]) {
    delegatedAgents.value = new Set()
    completedAgents.value = new Set()
    activeAgents.value = new Set()
    allTasksDone.value = false

    for (const panel of Object.values(agentPanels.value)) {
      panel.status = 'idle'
      panel.blocks = []
      panel.messages = []
      panel.collapsed = false
    }

    messages.value.push({
      role: 'user',
      content,
      timestamp: Date.now(),
      media,
    })
    triggerRef(messages)
  }

  function ensurePanel(token: string, agent?: GroupChatAgent): AgentPanel {
    if (!agentPanels.value[token]) {
      agentPanels.value[token] = {
        agent: agent || { token, name: token },
        status: 'idle',
        collapsed: false,
        blocks: [],
        messages: [],
        color: getAgentColor(token),
      }
      triggerRef(agentPanels)
    }
    return agentPanels.value[token]
  }

  // Get the last text block or create a new one (for appending chunks)
  function getOrAppendTextBlock(panel: AgentPanel): ContentBlock & { type: 'text' } {
    const last = panel.blocks[panel.blocks.length - 1]
    if (last && last.type === 'text') {
      return last as ContentBlock & { type: 'text' }
    }
    const block: ContentBlock = { type: 'text', content: '' }
    panel.blocks.push(block)
    return block as ContentBlock & { type: 'text' }
  }

  // Get the last thinking block or create one (for appending thinking chunks)
  function getOrAppendThinkingBlock(panel: AgentPanel): ContentBlock & { type: 'thinking' } {
    const last = panel.blocks[panel.blocks.length - 1]
    if (last && last.type === 'thinking') {
      return last as ContentBlock & { type: 'thinking' }
    }
    const block: ContentBlock = { type: 'thinking', content: '' }
    panel.blocks.push(block)
    return block as ContentBlock & { type: 'thinking' }
  }

  function handleWSEvent(event: Record<string, unknown>) {
    const type = event.type as string

    if (type === 'session') {
      groupId.value = event.groupId as string
      if (event.agents) {
        agents.value = event.agents as GroupChatAgent[]
        for (const a of agents.value) {
          ensurePanel(a.token, a)
        }
      }
      return
    }

    if (type === 'all_done') {
      allTasksDone.value = true
      return
    }

    if (type === 'delegation') {
      const from = event.from as GroupChatAgent | undefined
      const to = event.to as GroupChatAgent | undefined
      const content = (event.content as string) || ''

      if (to?.token) {
        delegatedAgents.value.add(to.token)
        ensurePanel(to.token, to)
      }

      messages.value.push({
        role: 'system',
        content: `**${from?.name || 'Leader'}** delegated to **${to?.name || 'Agent'}**: ${content}`,
        timestamp: Date.now(),
        isDelegation: true,
        agent: from,
      })
      triggerRef(messages)
      triggerRef(agentPanels)
      return
    }

    // All other events carry agent info
    const agent = event.agent as GroupChatAgent | undefined
    const agentToken = agent?.token || 'unknown'

    switch (type) {
      case 'chunk': {
        const panel = ensurePanel(agentToken, agent)
        panel.status = 'streaming'
        activeAgents.value.add(agentToken)
        // Ensure first message timestamp for sorting
        if (panel.messages.length === 0) {
          panel.messages.push({ role: 'assistant', content: '', timestamp: Date.now(), agent: agent || panel.agent })
        }
        const textBlock = getOrAppendTextBlock(panel)
        textBlock.content += (event.content as string) || ''
        triggerRef(agentPanels)
        break
      }

      case 'thinking': {
        const panel = ensurePanel(agentToken, agent)
        panel.status = 'streaming'
        activeAgents.value.add(agentToken)
        if (panel.messages.length === 0) {
          panel.messages.push({ role: 'assistant', content: '', timestamp: Date.now(), agent: agent || panel.agent })
        }
        const thinkBlock = getOrAppendThinkingBlock(panel)
        thinkBlock.content += (event.content as string) || ''
        triggerRef(agentPanels)
        break
      }

      case 'tool_call': {
        const panel = ensurePanel(agentToken, agent)
        panel.status = 'streaming'
        activeAgents.value.add(agentToken)
        if (panel.messages.length === 0) {
          panel.messages.push({ role: 'assistant', content: '', timestamp: Date.now(), agent: agent || panel.agent })
        }
        const name = (event.name as string) || ''
        const tc: ToolCall = {
          id: (event.id as string) || `${name}-${Date.now()}`,
          name,
          arguments: (event.input as string) || (event.arguments as string) || '',
          status: 'running',
        }
        // Push a tool block in chronological order
        panel.blocks.push({ type: 'tool', tool: tc })
        triggerRef(agentPanels)
        break
      }

      case 'tool_result': {
        const panel = agentPanels.value[agentToken]
        if (panel) {
          const name = (event.name as string) || ''
          const id = event.id as string | undefined
          // Find the matching tool block (search from end for efficiency)
          for (let i = panel.blocks.length - 1; i >= 0; i--) {
            const block = panel.blocks[i]
            if (block.type !== 'tool') continue
            const tc = block.tool
            const match = id ? tc.id === id : (tc.name === name && tc.status === 'running')
            if (match) {
              tc.result = (event.content as string) || ''
              tc.status =
                (event.status as string) === 'error' || (event.is_error as boolean)
                  ? 'error'
                  : 'completed'
              break
            }
          }
        }
        triggerRef(agentPanels)
        break
      }

      case 'media': {
        const panel = ensurePanel(agentToken, agent)
        if (panel.messages.length === 0) {
          panel.messages.push({ role: 'assistant', content: '', timestamp: Date.now(), agent: agent || panel.agent })
        }
        const mediaContent = (event.content as string) || (event.url as string) || ''
        if (mediaContent) {
          panel.blocks.push({ type: 'media', url: mediaContent })
        }
        triggerRef(agentPanels)
        break
      }

      case 'error': {
        const panel = ensurePanel(agentToken, agent)
        if (panel.messages.length === 0) {
          panel.messages.push({ role: 'assistant', content: '', timestamp: Date.now(), agent: agent || panel.agent })
        }
        panel.blocks.push({ type: 'error', content: (event.content as string) || 'Unknown error' })
        triggerRef(agentPanels)
        break
      }

      case 'done': {
        const panel = agentPanels.value[agentToken]
        if (panel) {
          panel.status = 'completed'
          // If done event carries content and we have no text yet, add it
          if (event.content) {
            const hasText = panel.blocks.some(b => b.type === 'text' && b.content)
            if (!hasText) {
              panel.blocks.push({ type: 'text', content: event.content as string })
            }
          }
          // Mark any running tools as completed
          for (const block of panel.blocks) {
            if (block.type === 'tool' && block.tool.status === 'running') {
              block.tool.status = 'completed'
            }
          }
        }
        activeAgents.value.delete(agentToken)
        completedAgents.value.add(agentToken)
        checkAllDone()
        triggerRef(agentPanels)
        break
      }
    }
  }

  function checkAllDone() {
    if (delegatedAgents.value.size === 0) return
    for (const token of delegatedAgents.value) {
      if (!completedAgents.value.has(token)) return
    }
    allTasksDone.value = true
  }

  function togglePanel(token: string) {
    const panel = agentPanels.value[token]
    if (panel) {
      panel.collapsed = !panel.collapsed
      triggerRef(agentPanels)
    }
  }

  return {
    messages,
    groupId,
    agents,
    sending,
    agentPanels,
    activeAgents,
    completedAgents,
    delegatedAgents,
    allTasksDone,
    sortedPanels,
    completionSummary,
    reset,
    addUserMessage,
    handleWSEvent,
    togglePanel,
    getAgentColor,
  }
})
