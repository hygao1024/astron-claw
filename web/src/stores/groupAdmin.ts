import { defineStore } from 'pinia'
import { ref } from 'vue'
import * as groupApi from '@/api/group'
import type { GroupInfo, GroupAgent } from '@/types'

export const useGroupAdminStore = defineStore('groupAdmin', () => {
  const groups = ref<GroupInfo[]>([])
  const loading = ref(false)
  const page = ref(1)
  const pageSize = ref(20)
  const total = ref(0)

  async function fetchGroups() {
    loading.value = true
    try {
      const data = await groupApi.listGroups({
        page: page.value,
        page_size: pageSize.value,
      })
      groups.value = data.groups || []
      total.value = data.total || 0
    } finally {
      loading.value = false
    }
  }

  async function createGroup(name: string, description: string) {
    const data = await groupApi.createGroup(name, description)
    await fetchGroups()
    return data.group
  }

  async function deleteGroup(groupId: string) {
    await groupApi.deleteGroup(groupId)
    await fetchGroups()
  }

  async function updateGroup(groupId: string, updates: { name?: string; description?: string }) {
    await groupApi.updateGroup(groupId, updates)
    await fetchGroups()
  }

  async function addAgent(groupId: string, token: string) {
    await groupApi.addGroupAgent(groupId, token)
  }

  async function removeAgent(groupId: string, token: string) {
    await groupApi.removeGroupAgent(groupId, token)
  }

  async function updateAgentRole(groupId: string, token: string, role: 'leader' | 'member') {
    await groupApi.updateGroupAgentRole(groupId, token, role)
  }

  async function getGroup(groupId: string): Promise<{ group: GroupInfo; agents: GroupAgent[] }> {
    const data = await groupApi.getGroup(groupId)
    return { group: data.group, agents: data.agents || [] }
  }

  return {
    groups,
    loading,
    page,
    pageSize,
    total,
    fetchGroups,
    createGroup,
    deleteGroup,
    updateGroup,
    addAgent,
    removeAgent,
    updateAgentRole,
    getGroup,
  }
})
