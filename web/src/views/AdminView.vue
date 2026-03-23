<script setup lang="ts">
import { ref, computed, onMounted, onUnmounted } from 'vue'
import { NModal, NButton, NInput, NSelect, NSpace, useMessage } from 'naive-ui'
import { useAdminStore } from '@/stores/admin'
import { useGroupAdminStore } from '@/stores/groupAdmin'
import AppHeader from '@/components/common/AppHeader.vue'
import type { Token, GroupInfo, GroupAgent } from '@/types'

const admin = useAdminStore()
const groupAdmin = useGroupAdminStore()
const message = useMessage()

// Tab state
const activeTab = ref<'tokens' | 'groups'>('tokens')

// Auth form state
const password = ref('')
const password2 = ref('')
const authError = ref('')
const authLoading = ref(false)

// Create modal
const showCreateModal = ref(false)
const createName = ref('')
const createExpiry = ref(86400)
const createLoading = ref(false)

// Token result modal
const showResultModal = ref(false)
const resultToken = ref('')

// Edit modal
const showEditModal = ref(false)
const editTarget = ref('')
const editName = ref('')
const editExpiry = ref(-1)
const editLoading = ref(false)

// Delete confirm modal
const showDeleteModal = ref(false)
const deleteTarget = ref('')

// Refresh timer
let refreshTimer: ReturnType<typeof setInterval> | undefined
const REFRESH_INTERVAL = 5000

// Search
const searchInput = ref('')

const expiryOptions = [
  { label: '1 Hour', value: 3600 },
  { label: '6 Hours', value: 21600 },
  { label: '1 Day', value: 86400 },
  { label: '7 Days', value: 604800 },
  { label: '30 Days', value: 2592000 },
  { label: 'Never', value: 0 },
]

const editExpiryOptions = [
  { label: 'Keep Current', value: -1 },
  ...expiryOptions,
]

const PAGE_SIZE_OPTIONS = [5, 10, 20]

onMounted(async () => {
  admin.pageSize = parseInt(localStorage.getItem('astron-page-size') || '10')
  await admin.checkAuth()
  if (admin.authenticated) startRefresh()
  document.addEventListener('keydown', onEscKey)
})

onUnmounted(() => {
  stopRefresh()
  document.removeEventListener('keydown', onEscKey)
})

function onEscKey(e: KeyboardEvent) {
  if (e.key === 'Escape') {
    if (showDeleteModal.value) showDeleteModal.value = false
    else if (showEditModal.value) showEditModal.value = false
    else if (showCreateModal.value) showCreateModal.value = false
    else if (showResultModal.value) showResultModal.value = false
  }
}

function startRefresh() {
  admin.fetchTokens()
  refreshTimer = setInterval(() => admin.fetchTokens(), REFRESH_INTERVAL)
}
function stopRefresh() {
  if (refreshTimer) { clearInterval(refreshTimer); refreshTimer = undefined }
}

// Auth handlers
async function handleSetup() {
  authError.value = ''
  if (password.value.length < 4) { authError.value = 'Password must be at least 4 characters'; return }
  if (password.value !== password2.value) { authError.value = 'Passwords do not match'; return }
  authLoading.value = true
  try {
    await admin.setup(password.value)
    startRefresh()
  } catch (e) { authError.value = (e as Error).message }
  finally { authLoading.value = false; password.value = ''; password2.value = '' }
}

async function handleLogin() {
  authError.value = ''
  authLoading.value = true
  try {
    await admin.login(password.value)
    startRefresh()
  } catch (e) { authError.value = (e as Error).message }
  finally { authLoading.value = false; password.value = '' }
}

async function handleLogout() {
  stopRefresh()
  await admin.logout()
}

// Token CRUD
async function handleCreate() {
  createLoading.value = true
  try {
    const token = await admin.createToken(createName.value, createExpiry.value)
    showCreateModal.value = false
    resultToken.value = token
    showResultModal.value = true
    createName.value = ''
    createExpiry.value = 86400
  } catch (e) { message.error((e as Error).message) }
  finally { createLoading.value = false }
}

function openEdit(t: Token) {
  editTarget.value = t.token
  editName.value = t.name
  editExpiry.value = -1
  showEditModal.value = true
}

async function handleEdit() {
  editLoading.value = true
  try {
    const updates: { name?: string; expires_in?: number } = { name: editName.value }
    if (editExpiry.value !== -1) updates.expires_in = editExpiry.value
    await admin.updateToken(editTarget.value, updates)
    showEditModal.value = false
    message.success('Token updated')
  } catch (e) { message.error((e as Error).message) }
  finally { editLoading.value = false }
}

function openDelete(token: string) {
  deleteTarget.value = token
  showDeleteModal.value = true
}

async function confirmDelete() {
  try {
    await admin.deleteToken(deleteTarget.value)
    showDeleteModal.value = false
    message.success('Token deleted')
  } catch (e) { message.error((e as Error).message) }
}

async function handleCleanup() {
  try {
    const r = await admin.cleanup()
    message.success(`Cleaned ${r.removed_tokens} tokens, ${r.removed_sessions} sessions`)
  } catch (e) { message.error((e as Error).message) }
}

function copyToken(token: string, btn?: HTMLElement) {
  navigator.clipboard.writeText(token)
  if (btn) {
    const orig = btn.textContent
    btn.textContent = '\u2713'
    btn.classList.add('copied')
    setTimeout(() => {
      btn.textContent = orig
      btn.classList.remove('copied')
    }, 1200)
  }
  message.success('Copied to clipboard')
}

function maskToken(t: string): string {
  if (t.length <= 12) return t
  return t.slice(0, 7) + '...' + t.slice(-4)
}

function formatTime(ts: string): string {
  const d = new Date(ts)
  const pad = (n: number) => String(n).padStart(2, '0')
  return d.getFullYear() + '-' + pad(d.getMonth() + 1) + '-' + pad(d.getDate()) + ' ' + pad(d.getHours()) + ':' + pad(d.getMinutes())
}

function formatRelativeExpiry(ts: string): string {
  const diff = new Date(ts).getTime() - Date.now()
  if (diff < 0) return 'Expired'
  if (diff > 365 * 86400000) return 'Never'
  if (diff < 60000) return `in ${Math.floor(diff / 1000)}s`
  if (diff < 3600000) return `in ${Math.floor(diff / 60000)}m`
  if (diff < 86400000) {
    const h = Math.floor(diff / 3600000)
    const m = Math.floor((diff % 3600000) / 60000)
    return `in ${h}h ${m}m`
  }
  return `in ${Math.floor(diff / 86400000)}d`
}

// Search debounce
let searchTimer: ReturnType<typeof setTimeout> | undefined
function handleSearch(val: string) {
  searchInput.value = val
  admin.search = val
  if (searchTimer) clearTimeout(searchTimer)
  searchTimer = setTimeout(() => { admin.page = 1; admin.fetchTokens() }, 300)
}

function clearSearch() {
  searchInput.value = ''
  admin.search = ''
  admin.page = 1
  admin.fetchTokens()
}

// Sorting
function toggleSort(field: string) {
  if (admin.sortBy === field) {
    admin.sortOrder = admin.sortOrder === 'desc' ? 'asc' : 'desc'
  } else {
    admin.sortBy = field
    admin.sortOrder = 'desc'
  }
  admin.page = 1
  admin.fetchTokens()
}

function toggleOnlineFilter() {
  admin.botStatus = admin.botStatus === 'online' ? '' : 'online'
  admin.page = 1
  admin.fetchTokens()
}

// Pagination
const totalPages = computed(() => Math.ceil(admin.totalCount / admin.pageSize))
const offset = computed(() => (admin.page - 1) * admin.pageSize)
const rangeStart = computed(() => offset.value + 1)
const rangeEnd = computed(() => Math.min(offset.value + admin.pageSize, admin.totalCount))

function goToPage(p: number) {
  admin.page = p
  admin.fetchTokens()
}

function changePageSize(size: number) {
  admin.pageSize = size
  localStorage.setItem('astron-page-size', String(size))
  admin.page = 1
  admin.fetchTokens()
}

function buildPageRange(current: number, total: number): (number | '...')[] {
  if (total <= 7) return Array.from({ length: total }, (_, i) => i + 1)
  const pages: (number | '...')[] = [1]
  if (current > 3) pages.push('...')
  for (let i = Math.max(2, current - 1); i <= Math.min(total - 1, current + 1); i++) {
    pages.push(i)
  }
  if (current < total - 2) pages.push('...')
  pages.push(total)
  return pages
}

function onCopyBtnClick(e: MouseEvent, token: string) {
  copyToken(token, e.target as HTMLElement)
}

// ── Groups ──────────────────────────────────────
const showCreateGroupModal = ref(false)
const createGroupName = ref('')
const createGroupDesc = ref('')
const createGroupLoading = ref(false)

const showGroupDetailModal = ref(false)
const detailGroup = ref<GroupInfo | null>(null)
const detailAgents = ref<GroupAgent[]>([])
const detailLoading = ref(false)
const addAgentToken = ref('')

const showEditGroupModal = ref(false)
const editGroupId = ref('')
const editGroupName = ref('')
const editGroupDesc = ref('')
const editGroupLoading = ref(false)

const showDeleteGroupModal = ref(false)
const deleteGroupId = ref('')

function switchTab(tab: 'tokens' | 'groups') {
  activeTab.value = tab
  if (tab === 'groups') {
    groupAdmin.fetchGroups()
  }
}

async function handleCreateGroup() {
  createGroupLoading.value = true
  try {
    await groupAdmin.createGroup(createGroupName.value, createGroupDesc.value)
    showCreateGroupModal.value = false
    createGroupName.value = ''
    createGroupDesc.value = ''
    message.success('Group created')
  } catch (e) { message.error((e as Error).message) }
  finally { createGroupLoading.value = false }
}

async function openGroupDetail(group: GroupInfo) {
  detailLoading.value = true
  showGroupDetailModal.value = true
  try {
    const data = await groupAdmin.getGroup(group.group_id)
    detailGroup.value = data.group
    detailAgents.value = data.agents
  } catch (e) { message.error((e as Error).message) }
  finally { detailLoading.value = false }
}

async function handleAddAgent() {
  if (!detailGroup.value || !addAgentToken.value) return
  try {
    await groupAdmin.addAgent(detailGroup.value.group_id, addAgentToken.value)
    addAgentToken.value = ''
    // Refresh detail
    const data = await groupAdmin.getGroup(detailGroup.value.group_id)
    detailAgents.value = data.agents
    groupAdmin.fetchGroups()
    message.success('Agent added')
  } catch (e) { message.error((e as Error).message) }
}

async function handleRemoveAgent(token: string) {
  if (!detailGroup.value) return
  try {
    await groupAdmin.removeAgent(detailGroup.value.group_id, token)
    const data = await groupAdmin.getGroup(detailGroup.value.group_id)
    detailAgents.value = data.agents
    groupAdmin.fetchGroups()
    message.success('Agent removed')
  } catch (e) { message.error((e as Error).message) }
}

async function handleToggleRole(agent: GroupAgent) {
  if (!detailGroup.value) return
  const newRole = agent.role === 'leader' ? 'member' : 'leader'
  try {
    await groupAdmin.updateAgentRole(detailGroup.value.group_id, agent.token, newRole)
    const data = await groupAdmin.getGroup(detailGroup.value.group_id)
    detailAgents.value = data.agents
    message.success(`${agent.name || 'Agent'} is now ${newRole}`)
  } catch (e) { message.error((e as Error).message) }
}

function openEditGroup(group: GroupInfo) {
  editGroupId.value = group.group_id
  editGroupName.value = group.name
  editGroupDesc.value = group.description
  showEditGroupModal.value = true
}

async function handleEditGroup() {
  editGroupLoading.value = true
  try {
    await groupAdmin.updateGroup(editGroupId.value, {
      name: editGroupName.value,
      description: editGroupDesc.value,
    })
    showEditGroupModal.value = false
    message.success('Group updated')
  } catch (e) { message.error((e as Error).message) }
  finally { editGroupLoading.value = false }
}

function openDeleteGroup(groupId: string) {
  deleteGroupId.value = groupId
  showDeleteGroupModal.value = true
}

async function confirmDeleteGroup() {
  try {
    await groupAdmin.deleteGroup(deleteGroupId.value)
    showDeleteGroupModal.value = false
    message.success('Group deleted')
  } catch (e) { message.error((e as Error).message) }
}

function copyGroupId(groupId: string) {
  navigator.clipboard.writeText(groupId)
  message.success('Group ID copied')
}
</script>

<template>
  <!-- Setup screen -->
  <div v-if="admin.needSetup" class="auth-screen">
    <div class="auth-card">
      <div class="auth-brand">
        <img src="/astron_logo.png" class="auth-logo" alt="Logo" />
        <h1>Astron Claw</h1>
        <p>Set up admin password</p>
      </div>
      <div v-if="authError" class="auth-error">{{ authError }}</div>
      <div class="form-group">
        <label>Password</label>
        <input v-model="password" type="password" placeholder="At least 4 characters" @keydown.enter="handleSetup" />
      </div>
      <div class="form-group">
        <label>Confirm Password</label>
        <input v-model="password2" type="password" placeholder="Confirm password" @keydown.enter="handleSetup" />
      </div>
      <button class="auth-btn" :disabled="authLoading" @click="handleSetup">
        {{ authLoading ? 'Setting up...' : 'Set Password' }}
      </button>
    </div>
  </div>

  <!-- Login screen -->
  <div v-else-if="!admin.authenticated" class="auth-screen">
    <div class="auth-card">
      <div class="auth-brand">
        <img src="/astron_logo.png" class="auth-logo" alt="Logo" />
        <h1>Astron Claw</h1>
        <p>Admin Login</p>
      </div>
      <div v-if="authError" class="auth-error">{{ authError }}</div>
      <div class="form-group">
        <label>Password</label>
        <input v-model="password" type="password" placeholder="Enter admin password" @keydown.enter="handleLogin" />
      </div>
      <button class="auth-btn" :disabled="authLoading" @click="handleLogin">
        {{ authLoading ? 'Logging in...' : 'Login' }}
      </button>
    </div>
  </div>

  <!-- Main admin panel -->
  <div v-else class="page">
    <AppHeader title="Astron Claw" subtitle="Admin">
      <router-link to="/" class="icon-btn" title="Chat">&#8962;</router-link>
      <router-link to="/metrics" class="icon-btn" title="Metrics">&#128202;</router-link>
      <button class="icon-btn logout-btn" @click="handleLogout">Logout</button>
    </AppHeader>

    <!-- Stats -->
    <div class="stats">
      <div class="stat-card">
        <div class="label">Total Tokens</div>
        <div class="value accent">{{ admin.totalTokens }}</div>
      </div>
      <div class="stat-card">
        <div class="label">Online Bots</div>
        <div class="value success">{{ admin.onlineBots }}</div>
      </div>
      <div class="stat-card">
        <div class="label">Active Chats</div>
        <div class="value warning">{{ admin.activeChats }}</div>
      </div>
    </div>

    <!-- Tab Switcher -->
    <div class="tab-bar">
      <button class="tab-btn" :class="{ active: activeTab === 'tokens' }" @click="switchTab('tokens')">Tokens</button>
      <button class="tab-btn" :class="{ active: activeTab === 'groups' }" @click="switchTab('groups')">Groups</button>
    </div>

    <!-- ============ Tokens Tab ============ -->
    <div v-if="activeTab === 'tokens'">
    <!-- Toolbar -->
    <div class="toolbar">
      <button class="btn btn-primary" @click="showCreateModal = true">+ New Token</button>
      <button class="btn btn-secondary" @click="handleCleanup">Cleanup Expired</button>
      <button
        class="btn btn-secondary"
        :class="{ active: admin.botStatus === 'online' }"
        @click="toggleOnlineFilter"
      >
        &#9679; Online
      </button>
      <div class="search-bar">
        <input
          :value="searchInput"
          type="text"
          placeholder="Search tokens..."
          @input="handleSearch(($event.target as HTMLInputElement).value)"
        />
        <button
          class="search-clear"
          :class="{ visible: searchInput.length > 0 }"
          @click="clearSearch"
        >&times;</button>
      </div>
      <div class="spacer"></div>
      <span class="auto-refresh">Auto-refresh: 5s</span>
    </div>

    <!-- Table -->
    <div class="table-card">
      <table>
        <thead>
          <tr>
            <th>#</th>
            <th>Name</th>
            <th>Token</th>
            <th>Created</th>
            <th>Expires</th>
            <th
              class="sortable"
              :class="{ sorted: admin.sortBy === 'bot_online' }"
              @click="toggleSort('bot_online')"
            >
              Bot
              <span class="sort-arrow">{{ admin.sortBy === 'bot_online' && admin.sortOrder === 'asc' ? '\u25B2' : '\u25BC' }}</span>
            </th>
            <th
              class="sortable"
              :class="{ sorted: admin.sortBy === 'chat_count' }"
              @click="toggleSort('chat_count')"
            >
              Chats
              <span class="sort-arrow">{{ admin.sortBy === 'chat_count' && admin.sortOrder === 'asc' ? '\u25B2' : '\u25BC' }}</span>
            </th>
            <th>Actions</th>
          </tr>
        </thead>
        <tbody>
          <!-- Skeleton loading -->
          <template v-if="admin.loading && admin.tokens.length === 0">
            <tr v-for="n in 4" :key="'skel-' + n" class="skeleton-row">
              <td><span class="skeleton skel-sm"></span></td>
              <td><span class="skeleton skel-md"></span></td>
              <td><span class="skeleton skel-lg"></span></td>
              <td><span class="skeleton skel-md"></span></td>
              <td><span class="skeleton skel-md"></span></td>
              <td><span class="skeleton skel-badge"></span></td>
              <td><span class="skeleton skel-sm"></span></td>
              <td><span class="skeleton skel-lg"></span></td>
            </tr>
          </template>
          <!-- Empty state -->
          <tr v-else-if="admin.tokens.length === 0">
            <td colspan="8" class="empty-state">
              {{ admin.search ? `No tokens matching "${admin.search}"` : 'No tokens yet. Click "+ New Token" to create one.' }}
            </td>
          </tr>
          <!-- Data rows -->
          <tr v-else v-for="(t, i) in admin.tokens" :key="t.token">
            <td class="time-cell">{{ offset + i + 1 }}</td>
            <td class="time-cell">
              <template v-if="t.name">{{ t.name }}</template>
              <span v-else style="color:var(--text-muted)">&mdash;</span>
            </td>
            <td>
              <div class="token-cell">
                <span class="masked">{{ maskToken(t.token) }}</span>
                <button class="copy-btn" @click="onCopyBtnClick($event, t.token)">Copy</button>
              </div>
            </td>
            <td class="time-cell">{{ formatTime(t.created_at) }}</td>
            <td class="time-cell">{{ formatRelativeExpiry(t.expires_at) }}</td>
            <td>
              <span class="badge" :class="t.bot_online ? 'badge-online' : 'badge-offline'">
                <span class="badge-dot"></span>
                {{ t.bot_online ? 'Online' : 'Offline' }}
              </span>
            </td>
            <td>{{ t.chat_count ?? 0 }}</td>
            <td style="white-space:nowrap">
              <a
                class="chat-link"
                :href="'/?token=' + encodeURIComponent(t.token)"
                target="_blank"
                title="Open chat"
              >Chat &#8599;</a>
              <button class="btn btn-secondary btn-sm" @click="openEdit(t)">Edit</button>
              <button class="btn btn-danger btn-sm" @click="openDelete(t.token)">Delete</button>
            </td>
          </tr>
        </tbody>
      </table>
      <!-- Pagination inside table card -->
      <div v-if="admin.tokens.length > 0" class="pagination">
        <span class="page-info">Per page</span>
        <select
          class="page-size-select"
          :value="admin.pageSize"
          @change="changePageSize(+($event.target as HTMLSelectElement).value)"
        >
          <option v-for="opt in PAGE_SIZE_OPTIONS" :key="opt" :value="opt">{{ opt }}</option>
        </select>
        <span class="page-info" style="margin: 0 4px">|</span>
        <span class="page-info">{{ rangeStart }}&ndash;{{ rangeEnd }} of {{ admin.totalCount }}</span>
        <template v-if="totalPages > 1">
          <span class="page-info" style="margin: 0 4px">|</span>
          <button :disabled="admin.page <= 1" @click="goToPage(admin.page - 1)">&laquo;</button>
          <template v-for="p in buildPageRange(admin.page, totalPages)" :key="p">
            <span v-if="p === '...'" class="page-info">...</span>
            <button
              v-else
              :class="{ active: p === admin.page }"
              @click="goToPage(p as number)"
            >{{ p }}</button>
          </template>
          <button :disabled="admin.page >= totalPages" @click="goToPage(admin.page + 1)">&raquo;</button>
        </template>
      </div>
    </div>

    <!-- Create Token Modal -->
    <NModal v-model:show="showCreateModal" preset="card" title="Create Token" style="max-width: 440px">
      <div class="form-group">
        <label>Name (optional)</label>
        <NInput v-model:value="createName" placeholder="e.g. My Bot" />
      </div>
      <div class="form-group">
        <label>Expiry</label>
        <NSelect v-model:value="createExpiry" :options="expiryOptions" />
      </div>
      <template #footer>
        <NSpace justify="end">
          <NButton @click="showCreateModal = false">Cancel</NButton>
          <NButton type="primary" :loading="createLoading" @click="handleCreate">Create</NButton>
        </NSpace>
      </template>
    </NModal>

    <!-- Token Result Modal -->
    <NModal v-model:show="showResultModal" preset="card" title="Token Created" style="max-width: 440px">
      <p style="margin-bottom:12px;color:var(--text-secondary);font-size:13px">Copy this token — it won't be shown again in full.</p>
      <div class="token-result" @click="copyToken(resultToken)">
        <code>{{ resultToken }}</code>
      </div>
      <template #footer>
        <NSpace justify="end">
          <NButton @click="copyToken(resultToken)">Copy</NButton>
          <NButton type="primary" @click="showResultModal = false">Done</NButton>
        </NSpace>
      </template>
    </NModal>

    <!-- Edit Token Modal -->
    <NModal v-model:show="showEditModal" preset="card" title="Edit Token" style="max-width: 440px">
      <div class="form-group">
        <label>Name</label>
        <NInput v-model:value="editName" placeholder="Token name" />
      </div>
      <div class="form-group">
        <label>Expiry</label>
        <NSelect v-model:value="editExpiry" :options="editExpiryOptions" />
      </div>
      <template #footer>
        <NSpace justify="end">
          <NButton @click="showEditModal = false">Cancel</NButton>
          <NButton type="primary" :loading="editLoading" @click="handleEdit">Save</NButton>
        </NSpace>
      </template>
    </NModal>

    <!-- Delete Confirm Modal -->
    <NModal v-model:show="showDeleteModal" preset="card" title="Delete Token" style="max-width: 440px">
      <p style="color:var(--text-secondary);font-size:14px;line-height:1.5">Are you sure? Connected clients will be disconnected.</p>
      <template #footer>
        <NSpace justify="end">
          <NButton @click="showDeleteModal = false">Cancel</NButton>
          <NButton type="error" @click="confirmDelete">Delete</NButton>
        </NSpace>
      </template>
    </NModal>
    </div><!-- end tokens tab -->

    <!-- ============ Groups Tab ============ -->
    <div v-if="activeTab === 'groups'">
      <div class="toolbar">
        <button class="btn btn-primary" @click="showCreateGroupModal = true">+ New Group</button>
      </div>

      <div class="table-card">
        <table>
          <thead>
            <tr>
              <th>#</th>
              <th>Name</th>
              <th>Group ID</th>
              <th>Description</th>
              <th>Agents</th>
              <th>Actions</th>
            </tr>
          </thead>
          <tbody>
            <tr v-if="groupAdmin.loading && groupAdmin.groups.length === 0" v-for="n in 3" :key="'gskel-' + n" class="skeleton-row">
              <td><span class="skeleton skel-sm"></span></td>
              <td><span class="skeleton skel-md"></span></td>
              <td><span class="skeleton skel-lg"></span></td>
              <td><span class="skeleton skel-md"></span></td>
              <td><span class="skeleton skel-sm"></span></td>
              <td><span class="skeleton skel-lg"></span></td>
            </tr>
            <tr v-else-if="groupAdmin.groups.length === 0">
              <td colspan="6" class="empty-state">No groups yet. Click "+ New Group" to create one.</td>
            </tr>
            <tr v-else v-for="(g, i) in groupAdmin.groups" :key="g.group_id">
              <td class="time-cell">{{ i + 1 }}</td>
              <td>{{ g.name || '—' }}</td>
              <td>
                <div class="token-cell">
                  <span class="masked">{{ g.group_id.slice(0, 8) }}...</span>
                  <button class="copy-btn" @click="copyGroupId(g.group_id)">Copy</button>
                </div>
              </td>
              <td class="time-cell" style="max-width:200px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">{{ g.description || '—' }}</td>
              <td>
                <span class="badge badge-online" style="background:rgba(79,143,247,.12);color:var(--accent)">{{ g.agent_count ?? 0 }}</span>
              </td>
              <td style="white-space:nowrap">
                <button class="btn btn-secondary btn-sm" @click="openGroupDetail(g)">Detail</button>
                <button class="btn btn-secondary btn-sm" @click="openEditGroup(g)">Edit</button>
                <button class="btn btn-danger btn-sm" @click="openDeleteGroup(g.group_id)">Delete</button>
              </td>
            </tr>
          </tbody>
        </table>
      </div>

      <!-- Create Group Modal -->
      <NModal v-model:show="showCreateGroupModal" preset="card" title="Create Group" style="max-width: 480px">
        <div class="form-group">
          <label>Name</label>
          <NInput v-model:value="createGroupName" placeholder="e.g. AI Team" />
        </div>
        <div class="form-group">
          <label>Description</label>
          <NInput v-model:value="createGroupDesc" type="textarea" placeholder="Group description" :rows="3" />
        </div>
        <template #footer>
          <NSpace justify="end">
            <NButton @click="showCreateGroupModal = false">Cancel</NButton>
            <NButton type="primary" :loading="createGroupLoading" @click="handleCreateGroup">Create</NButton>
          </NSpace>
        </template>
      </NModal>

      <!-- Group Detail Modal -->
      <NModal v-model:show="showGroupDetailModal" preset="card" :title="detailGroup?.name || 'Group Detail'" style="max-width: 600px">
        <div v-if="detailLoading" style="text-align:center;padding:20px;color:var(--text-muted)">Loading...</div>
        <template v-else-if="detailGroup">
          <div style="margin-bottom:16px">
            <div style="font-size:12px;color:var(--text-muted);margin-bottom:4px">Group ID</div>
            <div class="token-cell">
              <code style="font-size:13px">{{ detailGroup.group_id }}</code>
              <button class="copy-btn" @click="copyGroupId(detailGroup.group_id)">Copy</button>
            </div>
          </div>
          <div v-if="detailGroup.description" style="margin-bottom:16px;font-size:13px;color:var(--text-secondary)">{{ detailGroup.description }}</div>

          <div style="font-size:13px;font-weight:600;margin-bottom:8px">Agents ({{ detailAgents.length }})</div>

          <!-- Add agent -->
          <div style="display:flex;gap:8px;margin-bottom:12px">
            <NInput v-model:value="addAgentToken" placeholder="Paste token to add" size="small" style="flex:1" />
            <NButton size="small" type="primary" @click="handleAddAgent" :disabled="!addAgentToken">Add</NButton>
          </div>

          <div v-if="detailAgents.length === 0" style="text-align:center;padding:20px;color:var(--text-muted);font-size:13px">No agents in this group.</div>
          <table v-else style="font-size:13px;width:100%">
            <thead>
              <tr>
                <th>Name</th>
                <th>Token</th>
                <th>Role</th>
                <th>Status</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="a in detailAgents" :key="a.token">
                <td>{{ a.name || '—' }}</td>
                <td><code style="font-size:12px">{{ a.token.slice(0, 10) }}...</code></td>
                <td>
                  <button
                    class="btn btn-sm"
                    :class="a.role === 'leader' ? 'btn-primary' : 'btn-secondary'"
                    style="min-width:70px"
                    @click="handleToggleRole(a)"
                  >
                    {{ a.role === 'leader' ? 'Leader' : 'Member' }}
                  </button>
                </td>
                <td>
                  <span class="badge" :class="a.bot_online ? 'badge-online' : 'badge-offline'" style="font-size:11px">
                    <span class="badge-dot"></span>
                    {{ a.bot_online ? 'Online' : 'Offline' }}
                  </span>
                </td>
                <td><button class="btn btn-danger btn-sm" @click="handleRemoveAgent(a.token)">Remove</button></td>
              </tr>
            </tbody>
          </table>
        </template>
      </NModal>

      <!-- Edit Group Modal -->
      <NModal v-model:show="showEditGroupModal" preset="card" title="Edit Group" style="max-width: 480px">
        <div class="form-group">
          <label>Name</label>
          <NInput v-model:value="editGroupName" placeholder="Group name" />
        </div>
        <div class="form-group">
          <label>Description</label>
          <NInput v-model:value="editGroupDesc" type="textarea" placeholder="Group description" :rows="3" />
        </div>
        <template #footer>
          <NSpace justify="end">
            <NButton @click="showEditGroupModal = false">Cancel</NButton>
            <NButton type="primary" :loading="editGroupLoading" @click="handleEditGroup">Save</NButton>
          </NSpace>
        </template>
      </NModal>

      <!-- Delete Group Confirm Modal -->
      <NModal v-model:show="showDeleteGroupModal" preset="card" title="Delete Group" style="max-width: 440px">
        <p style="color:var(--text-secondary);font-size:14px;line-height:1.5">Are you sure? All agents will be removed from this group.</p>
        <template #footer>
          <NSpace justify="end">
            <NButton @click="showDeleteGroupModal = false">Cancel</NButton>
            <NButton type="error" @click="confirmDeleteGroup">Delete</NButton>
          </NSpace>
        </template>
      </NModal>
    </div><!-- end groups tab -->
  </div>
</template>

<style scoped>
/* ===== Auth Screen ===== */
.auth-screen {
  display: flex; align-items: center; justify-content: center;
  min-height: 100vh; padding: 20px;
}
.auth-card {
  background: var(--bg-secondary); border: 1px solid var(--border); border-radius: 16px;
  padding: 40px; width: 100%; max-width: 420px; box-shadow: var(--shadow); animation: fadeUp .5s ease;
}
.auth-brand { text-align: center; margin-bottom: 32px; }
.auth-logo { width: 64px; height: 64px; border-radius: 16px; margin-bottom: 16px; }
.auth-brand h1 { font-size: 24px; font-weight: 700; letter-spacing: -0.5px; }
.auth-brand p { color: var(--text-secondary); font-size: 14px; margin-top: 4px; }
.auth-error {
  color: var(--error); font-size: 13px; margin-bottom: 12px;
  padding: 8px 12px; background: rgba(239,68,68,.1); border-radius: var(--radius-sm);
}
.form-group { margin-bottom: 16px; }
.form-group label { display: block; font-size: 13px; font-weight: 600; color: var(--text-secondary); margin-bottom: 6px; }
.form-group input {
  width: 100%; padding: 12px 14px; background: var(--bg-input); border: 1px solid var(--border);
  border-radius: var(--radius-sm); color: var(--text-primary); font-size: 14px;
  font-family: var(--font); outline: none; transition: border-color var(--transition);
}
.form-group input:focus { border-color: var(--accent); box-shadow: 0 0 0 3px var(--accent-dim); }
.form-group input::placeholder { color: var(--text-muted); }
.auth-btn {
  width: 100%; display: inline-flex; align-items: center; justify-content: center; gap: 8px;
  padding: 12px 24px; border: none; border-radius: var(--radius-sm);
  font-size: 14px; font-weight: 600; font-family: var(--font); cursor: pointer;
  background: var(--accent); color: #fff; transition: all var(--transition);
}
.auth-btn:hover:not(:disabled) { background: var(--accent-hover); transform: translateY(-1px); }
.auth-btn:disabled { opacity: .5; cursor: not-allowed; transform: none; }

/* ===== Page Layout ===== */
.page { max-width: 1000px; margin: 0 auto; padding: 24px 20px; }

/* ===== Tab Bar ===== */
.tab-bar { display: flex; gap: 4px; margin-bottom: 16px; }
.tab-btn {
  padding: 8px 20px; border: 1px solid var(--border); border-radius: var(--radius-sm);
  background: transparent; color: var(--text-secondary); font-size: 13px; font-weight: 600;
  font-family: var(--font); cursor: pointer; transition: all var(--transition);
}
.tab-btn:hover { background: var(--bg-tertiary); color: var(--text-primary); }
.tab-btn.active { background: var(--accent); color: #fff; border-color: var(--accent); }

/* ===== Stats Cards ===== */
.stats { display: grid; grid-template-columns: repeat(3, 1fr); gap: 16px; margin-bottom: 28px; }
.stat-card {
  background: var(--bg-secondary); border: 1px solid var(--border); border-radius: var(--radius);
  padding: 20px; box-shadow: var(--shadow);
  transition: transform var(--transition), box-shadow var(--transition);
}
.stat-card:hover {
  transform: translateY(-2px);
  box-shadow: 0 8px 32px rgba(0, 0, 0, 0.35);
}
.stat-card .label { font-size: 13px; color: var(--text-muted); margin-bottom: 6px; }
.stat-card .value { font-size: 28px; font-weight: 700; }
.stat-card .value.accent { color: var(--accent); }
.stat-card .value.success { color: var(--success); }
.stat-card .value.warning { color: var(--warning); }

/* ===== Icon Buttons ===== */
.icon-btn {
  display: inline-flex; align-items: center; justify-content: center;
  width: 36px; height: 36px; border-radius: var(--radius-sm); background: transparent;
  border: 1px solid var(--border); color: var(--text-secondary); cursor: pointer;
  font-size: 16px; transition: all var(--transition); text-decoration: none;
}
.icon-btn:hover { background: var(--bg-tertiary); color: var(--text-primary); border-color: var(--text-muted); }
.logout-btn { width: auto; padding: 0 12px; font-size: 12px; font-family: var(--font); font-weight: 600; color: var(--text-muted); }
.logout-btn:hover { color: var(--error); border-color: var(--error); }

/* ===== Toolbar ===== */
.toolbar { display: flex; align-items: center; gap: 10px; margin-bottom: 16px; flex-wrap: wrap; }
.spacer { flex: 1; }
.auto-refresh { font-size: 12px; color: var(--text-muted); }

/* ===== Buttons ===== */
.btn {
  display: inline-flex; align-items: center; justify-content: center; gap: 6px;
  padding: 10px 18px; border: none; border-radius: var(--radius-sm);
  font-size: 13px; font-weight: 600; font-family: var(--font); cursor: pointer;
  transition: all var(--transition);
}
.btn-primary { background: var(--accent); color: #fff; }
.btn-primary:hover { background: var(--accent-hover); }
.btn-secondary { background: var(--bg-tertiary); color: var(--text-secondary); border: 1px solid var(--border); }
.btn-secondary:hover { color: var(--text-primary); border-color: var(--text-muted); }
.btn-secondary.active { color: var(--success); border-color: var(--success); background: rgba(34, 197, 94, 0.08); }
.btn-secondary.active:hover { color: var(--success); background: rgba(34, 197, 94, 0.15); }
.btn-danger { background: transparent; color: var(--error); border: 1px solid rgba(239, 68, 68, 0.3); }
.btn-danger:hover { background: rgba(239, 68, 68, 0.1); }
.btn-sm { padding: 6px 12px; font-size: 12px; margin: 0 4px; }

/* ===== Search Bar ===== */
.search-bar { position: relative; }
.search-bar input {
  width: 180px; padding: 10px 30px 10px 14px;
  background: var(--bg-input); border: 1px solid var(--border); border-radius: var(--radius-sm);
  color: var(--text-primary); font-size: 13px; font-family: var(--font); outline: none;
  transition: width 0.25s ease, border-color var(--transition), box-shadow var(--transition);
}
.search-bar input:focus { width: 260px; border-color: var(--accent); box-shadow: 0 0 0 3px var(--accent-dim); }
.search-bar input::placeholder { color: var(--text-muted); }
.search-clear {
  position: absolute; right: 6px; top: 50%; transform: translateY(-50%);
  width: 20px; height: 20px; border: none; border-radius: 50%;
  background: var(--bg-tertiary); color: var(--text-muted); cursor: pointer;
  display: none; align-items: center; justify-content: center;
  font-size: 12px; line-height: 1; padding: 0;
  transition: background var(--transition), color var(--transition);
}
.search-clear.visible { display: flex; }
.search-clear:hover { background: var(--border); color: var(--text-primary); }

/* ===== Table ===== */
.table-card {
  background: var(--bg-secondary); border: 1px solid var(--border);
  border-radius: var(--radius); box-shadow: var(--shadow); overflow: hidden;
}
table { width: 100%; border-collapse: collapse; font-size: 13px; }
thead th {
  text-align: left; padding: 12px 16px; font-weight: 600;
  color: var(--text-muted); font-size: 12px; text-transform: uppercase;
  letter-spacing: 0.5px; background: var(--bg-tertiary);
  border-bottom: 1px solid var(--border);
}
thead th.sortable { cursor: pointer; user-select: none; transition: color var(--transition); }
thead th.sortable:hover { color: var(--text-secondary); }
thead th .sort-arrow {
  display: inline-block; margin-left: 3px; font-size: 10px;
  opacity: 0.3; transition: opacity var(--transition);
}
thead th.sorted .sort-arrow { opacity: 1; color: var(--accent); }
tbody td { padding: 12px 16px; border-bottom: 1px solid var(--border); vertical-align: middle; }
tbody tr:last-child td { border-bottom: none; }
tbody tr { transition: background var(--transition); }
tbody tr:hover { background: var(--accent-dim); }

.time-cell { color: var(--text-secondary); font-size: 12px; white-space: nowrap; }

.token-cell {
  font-family: var(--font-mono); font-size: 13px;
  display: flex; align-items: center; gap: 6px;
}
.token-cell .masked { color: var(--text-secondary); }

.copy-btn {
  padding: 3px 8px; font-size: 11px; background: transparent;
  border: 1px solid var(--border); border-radius: 4px;
  color: var(--text-muted); cursor: pointer; font-family: var(--font);
  transition: all var(--transition);
}
.copy-btn:hover { background: var(--bg-tertiary); color: var(--text-primary); }
.copy-btn.copied { color: var(--success); border-color: var(--success); }

/* ===== Badges ===== */
.badge {
  display: inline-flex; align-items: center; gap: 4px;
  padding: 3px 10px; border-radius: 12px; font-size: 12px; font-weight: 600;
}
.badge-online { background: rgba(34, 197, 94, 0.12); color: var(--success); }
.badge-offline { background: rgba(107, 113, 148, 0.12); color: var(--text-muted); }
.badge-dot { width: 6px; height: 6px; border-radius: 50%; background: currentColor; }
.badge-online .badge-dot { animation: dotPulse 2s ease-in-out infinite; }

/* ===== Chat Link ===== */
.chat-link {
  display: inline-flex; align-items: center; gap: 4px;
  padding: 4px 10px; border: 1px solid rgba(79, 143, 247, 0.3); border-radius: 4px;
  background: transparent; color: var(--accent); font-size: 12px;
  font-family: var(--font); font-weight: 600; cursor: pointer;
  text-decoration: none; transition: all var(--transition); white-space: nowrap;
}
.chat-link:hover { background: var(--accent-dim); border-color: var(--accent); }

/* ===== Skeleton Loading ===== */
.skeleton { background: var(--bg-tertiary); border-radius: 4px; animation: skeleton 1.2s ease-in-out infinite; }
.skeleton-row td { padding: 14px 16px; border-bottom: 1px solid var(--border); }
.skel-sm { display: inline-block; width: 24px; height: 14px; }
.skel-md { display: inline-block; width: 70px; height: 14px; }
.skel-lg { display: inline-block; width: 120px; height: 14px; }
.skel-badge { display: inline-block; width: 60px; height: 22px; border-radius: 12px; }

.empty-state { text-align: center; padding: 48px 20px; color: var(--text-muted); font-size: 14px; }

/* ===== Pagination ===== */
.pagination {
  display: flex; align-items: center; justify-content: center; gap: 6px;
  padding: 14px 16px; border-top: 1px solid var(--border);
  background: var(--bg-tertiary); flex-wrap: wrap;
}
.pagination button {
  min-width: 36px; height: 36px;
  display: inline-flex; align-items: center; justify-content: center;
  padding: 0 10px; border: 1px solid var(--border); border-radius: var(--radius-sm);
  background: transparent; color: var(--text-secondary); font-size: 13px;
  font-family: var(--font); cursor: pointer; transition: all var(--transition);
}
.pagination button:hover:not(:disabled):not(.active) {
  background: var(--bg-secondary); color: var(--text-primary); border-color: var(--text-muted);
}
.pagination button.active { background: var(--accent); color: #fff; border-color: var(--accent); }
.pagination button:disabled { opacity: 0.35; cursor: not-allowed; }
.pagination .page-info { font-size: 12px; color: var(--text-muted); padding: 0 8px; }
.page-size-select {
  height: 36px; padding: 0 8px; border: 1px solid var(--border); border-radius: var(--radius-sm);
  background: var(--bg-input); color: var(--text-primary); font-size: 13px;
  font-family: var(--font); cursor: pointer; outline: none; transition: border-color var(--transition);
}
.page-size-select:hover { border-color: var(--text-muted); }
.page-size-select:focus { border-color: var(--accent); }

/* ===== Token Result ===== */
.token-result {
  padding: 14px; background: var(--bg-tertiary); border: 1px solid var(--border);
  border-radius: var(--radius-sm); cursor: pointer; word-break: break-all;
  font-family: var(--font-mono); font-size: 13px; transition: background var(--transition);
}
.token-result:hover { background: var(--accent-dim); }

/* ===== Animations ===== */
@keyframes dotPulse {
  0%, 100% { opacity: 1; transform: scale(1); }
  50% { opacity: 0.4; transform: scale(1.4); }
}
@keyframes skeleton {
  0%, 100% { opacity: 0.4; }
  50% { opacity: 0.7; }
}

/* ===== Mobile ===== */
@media (max-width: 640px) {
  .stats { grid-template-columns: 1fr; }
  .toolbar { flex-wrap: wrap; }
  .search-bar { order: 10; width: 100%; }
  .search-bar input { width: 100%; }
  .search-bar input:focus { width: 100%; }
  table { font-size: 12px; }
  thead th, tbody td { padding: 10px 12px; }
  .auth-card { padding: 28px 20px; }
}
</style>
