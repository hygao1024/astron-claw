<script setup lang="ts">
import { ref, watch, nextTick } from 'vue'
import type { AgentPanel } from '@/types'
import { renderMarkdown } from '@/utils/renderContent'
import ThinkingBlock from '@/components/chat/ThinkingBlock.vue'
import ToolCallCard from '@/components/chat/ToolCallCard.vue'

const props = defineProps<{ panel: AgentPanel }>()
const emit = defineEmits<{ toggle: [] }>()

// Independent scroll management
const cardBody = ref<HTMLElement | null>(null)
const isNearBottom = ref(true)

function onScroll() {
  if (!cardBody.value) return
  const el = cardBody.value
  isNearBottom.value = (el.scrollHeight - el.scrollTop - el.clientHeight) < 40
}

// Auto-scroll to bottom during streaming (watch blocks, not messages)
watch(
  () => props.panel.blocks,
  () => {
    if (isNearBottom.value && cardBody.value) {
      nextTick(() => {
        if (cardBody.value) {
          cardBody.value.scrollTop = cardBody.value.scrollHeight
        }
      })
    }
  },
  { deep: true },
)
</script>

<template>
  <div
    class="agent-card"
    :class="[panel.status, { collapsed: panel.collapsed }]"
    :style="{ '--agent-color': panel.color }"
  >
    <!-- Header -->
    <div class="card-header" @click="emit('toggle')">
      <span class="card-dot" :style="{ background: panel.color }"></span>
      <span v-if="panel.agent.role === 'leader'" class="leader-crown">&#9733;</span>
      <span class="card-agent-name" :style="{ color: panel.color }">
        {{ panel.agent.name || 'Agent' }}
      </span>
      <span class="card-status-tag" :class="panel.status">
        <span v-if="panel.status === 'streaming'" class="spinner-sm"></span>
        <span v-else-if="panel.status === 'completed'">&#10003;</span>
        {{ panel.status === 'streaming' ? 'Working...' : panel.status === 'completed' ? 'Done' : '' }}
      </span>
      <span class="card-chevron">&#9660;</span>
    </div>

    <!-- Body: render blocks in chronological order -->
    <div v-show="!panel.collapsed" ref="cardBody" class="card-body" @scroll="onScroll">
      <template v-for="(block, bi) in panel.blocks" :key="bi">
        <!-- Thinking block -->
        <ThinkingBlock v-if="block.type === 'thinking'" :content="block.content" />

        <!-- Tool call (rendered via ToolCallCard) -->
        <div v-else-if="block.type === 'tool'" class="card-tool-wrap">
          <ToolCallCard :tool="block.tool" />
        </div>

        <!-- Text content (markdown rendered) -->
        <div v-else-if="block.type === 'text' && block.content" class="msg-content markdown-body" v-html="renderMarkdown(block.content)"></div>

        <!-- Media -->
        <div v-else-if="block.type === 'media'" class="card-media">
          <img :src="block.url" alt="media" class="card-media-img" />
        </div>

        <!-- Error -->
        <div v-else-if="block.type === 'error'" class="card-error">
          <strong>Error:</strong> {{ block.content }}
        </div>
      </template>

      <div v-if="panel.blocks.length === 0" class="card-empty">
        No output yet
      </div>
    </div>
  </div>
</template>

<style scoped>
.agent-card {
  display: flex;
  flex-direction: column;
  border: 1px solid var(--border);
  border-left: 3px solid var(--agent-color, var(--border));
  border-radius: var(--radius-sm);
  background: var(--bg-secondary);
  overflow: hidden;
  min-height: 0;
  transition: border-color 0.2s ease, opacity 0.2s ease;
}

.agent-card.streaming {
  border-color: var(--agent-color, var(--border));
  animation: cardPulse 2s ease-in-out infinite;
}

.agent-card.completed {
  opacity: 0.88;
}

.agent-card.collapsed {
  flex: 0 0 auto;
}

/* Header */
.card-header {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 14px;
  cursor: pointer;
  user-select: none;
  flex-shrink: 0;
  border-bottom: 1px solid var(--border);
}
.card-header:hover {
  background: var(--bg-tertiary);
}

.agent-card.collapsed .card-header {
  border-bottom-color: transparent;
}

.card-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  flex-shrink: 0;
}

.leader-crown {
  font-size: 12px;
  color: #f59e0b;
}

.card-agent-name {
  font-weight: 700;
  font-size: 13px;
  text-transform: uppercase;
  letter-spacing: 0.5px;
}

.card-status-tag {
  margin-left: auto;
  font-size: 11px;
  display: flex;
  align-items: center;
  gap: 4px;
  color: var(--text-muted);
}
.card-status-tag.streaming {
  color: var(--warning);
}
.card-status-tag.completed {
  color: var(--success);
}

.card-chevron {
  font-size: 10px;
  color: var(--text-muted);
  transition: transform 0.2s ease;
}
.agent-card.collapsed .card-chevron {
  transform: rotate(-90deg);
}

/* Body - INDEPENDENT SCROLL */
.card-body {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  padding: 8px 14px 10px;
  display: flex;
  flex-direction: column;
  gap: 6px;
}

/* Tool card wrapper */
.card-tool-wrap :deep(.tool-card) {
  max-width: 100%;
}

/* Text content */
.msg-content {
  word-break: break-word;
  font-size: 14px;
  line-height: 1.6;
}

/* Markdown styles (same as MessageBubble) */
.markdown-body :deep(h1),
.markdown-body :deep(h2),
.markdown-body :deep(h3) {
  margin: 12px 0 6px;
  font-weight: 600;
}
.markdown-body :deep(h1) { font-size: 1.3em; }
.markdown-body :deep(h2) { font-size: 1.15em; }
.markdown-body :deep(h3) { font-size: 1.05em; }
.markdown-body :deep(p) { margin: 0.8em 0; }
.markdown-body :deep(p:first-child) { margin-top: 0; }
.markdown-body :deep(p:last-child) { margin-bottom: 0; }
.markdown-body :deep(ul),
.markdown-body :deep(ol) {
  margin: 6px 0;
  padding-left: 24px;
}
.markdown-body :deep(li) { margin: 2px 0; }
.markdown-body :deep(blockquote) {
  border-left: 3px solid var(--accent);
  padding: 6px 12px;
  margin: 8px 0;
  color: var(--text-secondary);
  background: var(--bg-tertiary);
  border-radius: 4px;
}
.markdown-body :deep(table) {
  border-collapse: collapse;
  margin: 8px 0;
  font-size: 13px;
  width: 100%;
}
.markdown-body :deep(th),
.markdown-body :deep(td) {
  border: 1px solid var(--border);
  padding: 6px 10px;
  text-align: left;
}
.markdown-body :deep(th) {
  background: var(--bg-tertiary);
  font-weight: 600;
}
.markdown-body :deep(code):not(.hljs) {
  background: var(--bg-tertiary);
  padding: 2px 6px;
  border-radius: 4px;
  font-family: var(--font-mono);
  font-size: 0.88em;
}
.markdown-body :deep(.code-wrapper) {
  margin: 10px 0;
  border-radius: var(--radius-sm);
  overflow: hidden;
  border: 1px solid var(--border);
}
.markdown-body :deep(.code-header) {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 6px 12px;
  background: var(--bg-tertiary);
  font-size: 12px;
  color: var(--text-muted);
}
.markdown-body :deep(.code-lang) {
  font-weight: 600;
  text-transform: uppercase;
}
.markdown-body :deep(.copy-btn) {
  background: none;
  border: 1px solid var(--border);
  color: var(--text-muted);
  padding: 2px 10px;
  border-radius: 4px;
  font-size: 11px;
  cursor: pointer;
  font-family: var(--font);
}
.markdown-body :deep(.copy-btn:hover) {
  color: var(--accent);
  border-color: var(--accent);
}
.markdown-body :deep(pre) {
  margin: 0;
  padding: 14px;
  overflow-x: auto;
  background: var(--bg-primary);
  font-size: 13px;
  line-height: 1.5;
}
.markdown-body :deep(a) {
  color: var(--accent);
}
.markdown-body :deep(img) {
  max-width: 100%;
  border-radius: var(--radius-sm);
}
.markdown-body :deep(hr) {
  border: none;
  border-top: 1px solid var(--border);
  margin: 12px 0;
}

/* Media */
.card-media-img {
  max-width: 100%;
  border-radius: 8px;
  margin: 4px 0;
}

/* Error */
.card-error {
  color: var(--error);
  font-size: 13px;
  padding: 6px 10px;
  background: rgba(239, 68, 68, 0.08);
  border: 1px solid rgba(239, 68, 68, 0.2);
  border-radius: 6px;
}

/* Empty */
.card-empty {
  color: var(--text-muted);
  font-size: 13px;
  text-align: center;
  padding: 16px 0;
}

/* Spinner */
.spinner-sm {
  width: 10px;
  height: 10px;
  border: 2px solid rgba(245, 158, 11, 0.3);
  border-top-color: var(--warning);
  border-radius: 50%;
  animation: toolSpin 0.8s linear infinite;
  display: inline-block;
}

@keyframes toolSpin {
  to {
    transform: rotate(360deg);
  }
}
@keyframes cardPulse {
  0%, 100% {
    opacity: 1;
  }
  50% {
    opacity: 0.85;
  }
}
</style>
