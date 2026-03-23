<script setup lang="ts">
import { ref, computed, onMounted, onUnmounted, nextTick, watch } from 'vue'
import { useRoute } from 'vue-router'
import { useGroupChatStore } from '@/stores/groupChat'
import { useGroupWS } from '@/composables/useGroupWS'
import { renderContent } from '@/utils/renderContent'
import AppHeader from '@/components/common/AppHeader.vue'
import GroupAgentCard from '@/components/group/GroupAgentCard.vue'

const route = useRoute()
const groupId = computed(() => route.params.groupId as string)
const store = useGroupChatStore()
const { connected, connect, send, close } = useGroupWS()

// Auth
const token = ref(localStorage.getItem('astron-token') || '')
const authError = ref('')
const isLoggedIn = ref(false)

// Chat
const inputText = ref('')
const timelineRef = ref<HTMLElement | null>(null)

// User messages + delegation markers for the compact timeline
const timelineMessages = computed(() =>
  store.messages.filter(
    (m) => m.role === 'user' || (m.role === 'system' && m.isDelegation),
  ),
)

// Active panels: only show agents that have output or are streaming
const activePanels = computed(() =>
  store.sortedPanels.filter(
    (p) => p.blocks.length > 0 || p.status === 'streaming',
  ),
)

function doLogin() {
  authError.value = ''
  if (!token.value.trim()) {
    authError.value = 'Please enter a token'
    return
  }
  localStorage.setItem('astron-token', token.value)
  connectWS()
}

function connectWS() {
  const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
  const host = window.location.host
  const url = `${protocol}//${host}/bridge/group/${groupId.value}?token=${encodeURIComponent(token.value)}`

  store.reset()
  connect({
    url,
    onEvent: (event) => {
      store.handleWSEvent(event)
    },
    onClose: () => {},
    onError: () => {
      authError.value = 'Connection failed. Check your token and try again.'
    },
  })
  isLoggedIn.value = true
}

function handleSend() {
  const content = inputText.value.trim()
  if (!content || !connected.value) return
  store.addUserMessage(content)
  send({ type: 'message', content })
  inputText.value = ''
  // Scroll timeline to bottom for the new user message
  nextTick(() => {
    if (timelineRef.value) {
      timelineRef.value.scrollTop = timelineRef.value.scrollHeight
    }
  })
}

function handleLogout() {
  close()
  isLoggedIn.value = false
  store.reset()
}

function onKeyDown(e: KeyboardEvent) {
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault()
    handleSend()
  }
}

// Auto-expand streaming panels
watch(
  () => store.agentPanels,
  (panels) => {
    for (const panel of Object.values(panels)) {
      if (panel.status === 'streaming' && panel.collapsed) {
        panel.collapsed = false
      }
    }
  },
  { deep: true },
)

onMounted(() => {
  if (token.value && groupId.value) {
    connectWS()
  }
})

onUnmounted(() => {
  close()
})
</script>

<template>
  <!-- Login screen -->
  <div v-if="!isLoggedIn" class="auth-screen">
    <div class="auth-card">
      <div class="auth-brand">
        <img src="/astron_logo.png" class="auth-logo" alt="Logo" />
        <h1>Group Chat</h1>
        <p>Enter your token to join</p>
      </div>
      <div v-if="authError" class="auth-error">{{ authError }}</div>
      <div class="form-group">
        <label>Token</label>
        <input v-model="token" type="password" placeholder="sk-..." @keydown.enter="doLogin" />
      </div>
      <button class="auth-btn" @click="doLogin">Join Group</button>
    </div>
  </div>

  <!-- Chat screen -->
  <div v-else class="page">
    <AppHeader title="Group Chat" :subtitle="groupId">
      <div class="conn-status" :class="{ online: connected }">
        <span class="conn-dot"></span>
        {{ connected ? 'Connected' : 'Reconnecting...' }}
      </div>
      <router-link to="/admin" class="icon-btn" title="Admin">&#9881;</router-link>
      <button class="icon-btn logout-btn" @click="handleLogout">Leave</button>
    </AppHeader>

    <!-- Status bar -->
    <div class="status-bar">
      <div
        v-for="panel in store.sortedPanels"
        :key="panel.agent.token"
        class="status-chip"
        :class="panel.status"
        :style="{ borderColor: panel.color }"
      >
        <span class="status-dot" :style="{ background: panel.color }"></span>
        <span v-if="panel.agent.role === 'leader'" class="leader-crown">&#9733;</span>
        {{ panel.agent.name || 'Agent' }}
        <span class="status-label">
          <template v-if="panel.status === 'streaming'">
            <span class="spinner-sm"></span>
          </template>
          <template v-else-if="panel.status === 'completed'">&#10003;</template>
        </span>
      </div>
      <div
        v-if="store.completionSummary.total > 0"
        class="completion-badge"
        :class="{ done: store.allTasksDone }"
      >
        <template v-if="store.allTasksDone">&#10003; All done</template>
        <template v-else>{{ store.completionSummary.done }}/{{ store.completionSummary.total }}</template>
      </div>
    </div>

    <!-- User Timeline (compact, independently scrollable) -->
    <div v-if="timelineMessages.length > 0" ref="timelineRef" class="user-timeline">
      <div v-for="(msg, i) in timelineMessages" :key="i" class="timeline-item">
        <!-- User message -->
        <div v-if="msg.role === 'user'" class="tl-user-msg">
          <span class="tl-user-label">You</span>
          <span class="tl-user-content" v-html="renderContent(msg.content)"></span>
        </div>
        <!-- Delegation marker -->
        <div v-else class="tl-delegation">
          <span class="delegation-arrow">&#10132;</span>
          <span v-html="renderContent(msg.content)"></span>
        </div>
      </div>
    </div>

    <!-- Agent Grid (fills remaining height, each card scrolls independently) -->
    <div
      v-if="activePanels.length > 0"
      class="agent-grid"
      :class="{ 'single-col': activePanels.length === 1 }"
    >
      <GroupAgentCard
        v-for="panel in activePanels"
        :key="panel.agent.token"
        :panel="panel"
        @toggle="store.togglePanel(panel.agent.token)"
      />
    </div>

    <!-- Empty state -->
    <div v-else class="empty-chat">
      Send a message to start the conversation with the group agents.
    </div>

    <!-- All done banner -->
    <div v-if="store.allTasksDone" class="all-done-banner">
      &#10003; All tasks completed
    </div>

    <!-- Input -->
    <div class="input-bar">
      <textarea
        v-model="inputText"
        placeholder="Type a message..."
        rows="1"
        @keydown="onKeyDown"
      ></textarea>
      <button class="send-btn" :disabled="!connected || !inputText.trim()" @click="handleSend">Send</button>
    </div>
  </div>
</template>

<style scoped>
/* ── Auth ────────────────────────────────────── */
.auth-screen { display: flex; align-items: center; justify-content: center; min-height: 100vh; padding: 20px; }
.auth-card { background: var(--bg-secondary); border: 1px solid var(--border); border-radius: 16px; padding: 40px; width: 100%; max-width: 420px; box-shadow: var(--shadow); }
.auth-brand { text-align: center; margin-bottom: 32px; }
.auth-logo { width: 64px; height: 64px; border-radius: 16px; margin-bottom: 16px; }
.auth-brand h1 { font-size: 24px; font-weight: 700; }
.auth-brand p { color: var(--text-secondary); font-size: 14px; margin-top: 4px; }
.auth-error { color: var(--error); font-size: 13px; margin-bottom: 12px; padding: 8px 12px; background: rgba(239,68,68,.1); border-radius: var(--radius-sm); }
.form-group { margin-bottom: 16px; }
.form-group label { display: block; font-size: 13px; font-weight: 600; color: var(--text-secondary); margin-bottom: 6px; }
.form-group input { width: 100%; padding: 12px 14px; background: var(--bg-input); border: 1px solid var(--border); border-radius: var(--radius-sm); color: var(--text-primary); font-size: 14px; font-family: var(--font); outline: none; }
.form-group input:focus { border-color: var(--accent); box-shadow: 0 0 0 3px var(--accent-dim); }
.auth-btn { width: 100%; padding: 12px 24px; border: none; border-radius: var(--radius-sm); font-size: 14px; font-weight: 600; font-family: var(--font); cursor: pointer; background: var(--accent); color: #fff; }
.auth-btn:hover { background: var(--accent-hover); }

/* ── Page Layout ─────────────────────────────── */
.page {
  width: 100%;
  max-width: 1400px;
  margin: 0 auto;
  padding: 0 20px;
  display: flex;
  flex-direction: column;
  height: 100vh;
}

.conn-status { display: flex; align-items: center; gap: 6px; font-size: 12px; color: var(--text-muted); padding: 4px 10px; border: 1px solid var(--border); border-radius: 12px; }
.conn-status.online { color: var(--success); border-color: rgba(34,197,94,.3); }
.conn-dot { width: 6px; height: 6px; border-radius: 50%; background: var(--text-muted); }
.conn-status.online .conn-dot { background: var(--success); animation: dotPulse 2s ease-in-out infinite; }

.icon-btn { display: inline-flex; align-items: center; justify-content: center; width: 36px; height: 36px; border-radius: var(--radius-sm); background: transparent; border: 1px solid var(--border); color: var(--text-secondary); cursor: pointer; font-size: 16px; text-decoration: none; }
.icon-btn:hover { background: var(--bg-tertiary); color: var(--text-primary); }
.logout-btn { width: auto; padding: 0 12px; font-size: 12px; font-family: var(--font); font-weight: 600; color: var(--text-muted); }
.logout-btn:hover { color: var(--error); border-color: var(--error); }

/* ── Status Bar ──────────────────────────────── */
.status-bar { display: flex; gap: 8px; padding: 8px 0; flex-wrap: wrap; align-items: center; flex-shrink: 0; }
.status-chip { display: inline-flex; align-items: center; gap: 4px; padding: 3px 10px; border-radius: 12px; font-size: 12px; font-weight: 600; border: 1px solid; background: var(--bg-secondary); }
.status-chip.streaming { animation: statusPulse 2s ease-in-out infinite; }
.status-chip.completed { opacity: 0.7; }
.status-dot { width: 6px; height: 6px; border-radius: 50%; }
.leader-crown { font-size: 12px; color: #f59e0b; }
.status-label { font-size: 10px; margin-left: 2px; display: inline-flex; align-items: center; gap: 3px; }
.completion-badge { margin-left: auto; font-size: 12px; padding: 4px 12px; border-radius: 12px; background: var(--bg-tertiary); border: 1px solid var(--border); color: var(--text-secondary); font-weight: 600; }
.completion-badge.done { background: rgba(34,197,94,.1); border-color: rgba(34,197,94,.3); color: var(--success); }

/* ── User Timeline (compact, independent scroll) ─ */
.user-timeline {
  max-height: 120px;
  overflow-y: auto;
  padding: 8px 0;
  border-bottom: 1px solid var(--border);
  flex-shrink: 0;
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.timeline-item {
  font-size: 13px;
  line-height: 1.4;
}

.tl-user-msg {
  display: flex;
  align-items: flex-start;
  gap: 8px;
  padding: 4px 8px;
  border-radius: 6px;
  background: var(--bg-tertiary);
}

.tl-user-label {
  flex-shrink: 0;
  font-weight: 700;
  font-size: 11px;
  color: var(--accent);
  text-transform: uppercase;
  padding-top: 1px;
}

.tl-user-content {
  color: var(--text-primary);
  word-break: break-word;
}

.tl-delegation {
  display: flex;
  align-items: flex-start;
  gap: 6px;
  padding: 3px 8px;
  font-size: 12px;
  color: var(--text-secondary);
  border-left: 2px solid #f59e0b;
  margin-left: 8px;
}

.delegation-arrow {
  color: #f59e0b;
  font-size: 13px;
  flex-shrink: 0;
}

/* ── Agent Grid ──────────────────────────────── */
.agent-grid {
  flex: 1;
  min-height: 0;
  display: grid;
  grid-template-columns: repeat(2, 1fr);
  grid-auto-rows: 1fr;
  gap: 12px;
  padding: 12px 0;
}

.agent-grid.single-col {
  grid-template-columns: 1fr;
}

/* ── Empty State ─────────────────────────────── */
.empty-chat {
  flex: 1;
  display: flex;
  align-items: center;
  justify-content: center;
  text-align: center;
  color: var(--text-muted);
  font-size: 14px;
}

/* ── All Done Banner ─────────────────────────── */
.all-done-banner {
  text-align: center;
  padding: 10px;
  background: rgba(34,197,94,.08);
  border: 1px solid rgba(34,197,94,.2);
  border-radius: 8px;
  color: var(--success);
  font-weight: 600;
  font-size: 14px;
  flex-shrink: 0;
}

/* ── Input Bar ───────────────────────────────── */
.input-bar { display: flex; gap: 8px; padding: 12px 0; border-top: 1px solid var(--border); flex-shrink: 0; }
.input-bar textarea { flex: 1; padding: 10px 14px; background: var(--bg-input); border: 1px solid var(--border); border-radius: var(--radius-sm); color: var(--text-primary); font-size: 14px; font-family: var(--font); outline: none; resize: none; min-height: 40px; }
.input-bar textarea:focus { border-color: var(--accent); box-shadow: 0 0 0 3px var(--accent-dim); }
.send-btn { padding: 10px 20px; background: var(--accent); color: #fff; border: none; border-radius: var(--radius-sm); font-size: 14px; font-weight: 600; font-family: var(--font); cursor: pointer; }
.send-btn:hover:not(:disabled) { background: var(--accent-hover); }
.send-btn:disabled { opacity: .5; cursor: not-allowed; }

/* ── Spinner ─────────────────────────────────── */
.spinner-sm { width: 10px; height: 10px; border: 2px solid rgba(245,158,11,.3); border-top-color: var(--warning); border-radius: 50%; animation: toolSpin 0.8s linear infinite; display: inline-block; }

/* ── Animations ──────────────────────────────── */
@keyframes dotPulse { 0%, 100% { opacity: 1; transform: scale(1); } 50% { opacity: 0.4; transform: scale(1.4); } }
@keyframes statusPulse { 0%, 100% { opacity: 1; } 50% { opacity: 0.6; } }
@keyframes toolSpin { to { transform: rotate(360deg); } }

/* ── Responsive ──────────────────────────────── */
@media (max-width: 900px) {
  .agent-grid {
    grid-template-columns: 1fr;
    overflow-y: auto;
  }
  .agent-grid :deep(.agent-card) {
    max-height: 350px;
  }
}

@media (max-width: 640px) {
  .page { padding: 0 12px; }
  .user-timeline { max-height: 80px; }
  .agent-grid :deep(.agent-card) {
    max-height: 280px;
  }
}
</style>
