import client from './client'

// ── Group CRUD ──────────────────────────────────
export async function listGroups(params: {
  page?: number
  page_size?: number
}) {
  const { data } = await client.get('/api/admin/groups', { params })
  return data
}

export async function createGroup(name: string, description: string) {
  const { data } = await client.post('/api/admin/groups', { name, description })
  return data
}

export async function getGroup(groupId: string) {
  const { data } = await client.get(`/api/admin/groups/${groupId}`)
  return data
}

export async function updateGroup(
  groupId: string,
  updates: { name?: string; description?: string },
) {
  const { data } = await client.patch(`/api/admin/groups/${groupId}`, updates)
  return data
}

export async function deleteGroup(groupId: string) {
  await client.delete(`/api/admin/groups/${groupId}`)
}

export async function addGroupAgent(groupId: string, token: string) {
  const { data } = await client.post(`/api/admin/groups/${groupId}/agents`, { token })
  return data
}

export async function removeGroupAgent(groupId: string, token: string) {
  await client.delete(`/api/admin/groups/${groupId}/agents/${token}`)
}

export async function updateGroupAgentRole(
  groupId: string,
  token: string,
  role: 'leader' | 'member',
) {
  const { data } = await client.patch(
    `/api/admin/groups/${groupId}/agents/${token}`,
    { role },
  )
  return data
}
