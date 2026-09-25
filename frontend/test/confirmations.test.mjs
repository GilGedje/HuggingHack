import assert from 'node:assert/strict'
import test from 'node:test'
import { actingOrgAdmins, effectiveOrgRole, orgRoleConfirmation, roleConfirmation } from '../src/roles.ts'
import { visibilityAudience, visibilityConfirmation } from '../src/visibility.ts'

test('going public names every account and tokenless pulls, as a danger', () => {
  const ask = visibilityConfirmation('acme/qwen', 'public', 'acme')
  assert.equal(ask.title, 'Make acme/qwen public?')
  assert.equal(ask.danger, true)
  assert.match(ask.message, /Every account/)
  assert.match(ask.message, /without a token/)
})

test('narrowing visibility says who keeps access', () => {
  const personal = visibilityConfirmation('jane/tiny', 'private', null)
  assert.equal(personal.danger, undefined)
  assert.match(personal.message, /only you and server admins/)
  const shared = visibilityConfirmation('acme/qwen', 'organization', 'acme')
  assert.equal(shared.title, 'Share acme/qwen with acme only?')
  assert.match(shared.message, /every member of acme/)
  assert.match(visibilityAudience('private', 'acme'), /server admins/)
})

test('role changes say what the new role can do', () => {
  const demote = roleConfirmation('jane', 'admin', 'viewer')
  assert.equal(demote.title, 'Make jane a viewer?')
  assert.equal(demote.confirmLabel, 'Make viewer')
  assert.equal(demote.danger, true)
  assert.match(demote.message, /cannot upload/)
  assert.match(demote.message, /lose the admin pages/)

  const promote = roleConfirmation('jane', 'member', 'admin')
  assert.equal(promote.title, 'Make jane an administrator?')
  assert.equal(promote.danger, true)
  assert.match(promote.message, /private ones included/)

  const member = roleConfirmation('jane', 'viewer', 'member')
  assert.equal(member.danger, false)
  assert.doesNotMatch(member.message, /lose/)
})

test('organization role changes ask first, as a danger when admin rights move', () => {
  const promote = orgRoleConfirmation('jane', 'Acme', 'write', 'admin')
  assert.equal(promote.title, 'Make jane an admin of Acme?')
  assert.equal(promote.confirmLabel, 'Make admin')
  assert.equal(promote.danger, true)
  assert.match(promote.message, /already a member, as Write/)

  const demote = orgRoleConfirmation('jane', 'Acme', 'admin', 'read')
  assert.equal(demote.danger, true)
  assert.match(demote.message, /stop managing/)

  const narrow = orgRoleConfirmation('jane', 'Acme', 'write', 'read')
  assert.equal(narrow.danger, false)
  assert.match(narrow.message, /no longer upload/)

  const added = orgRoleConfirmation('jane', 'Acme', null, 'admin')
  assert.equal(added.title, 'Add jane to Acme as an admin?')
  assert.equal(added.confirmLabel, 'Add as admin')
  assert.doesNotMatch(added.message, /already/)
})

test('only admins who can act count toward the last admin, as on the server', () => {
  const member = (role, extra = {}) => ({ id: role, username: 'x', display_name: 'x', role, joined_at: '', ...extra })
  assert.equal(actingOrgAdmins([member('admin'), member('write')]), 1)
  assert.equal(actingOrgAdmins([member('admin'), member('admin', { disabled: true }), member('admin', { server_role: 'viewer' })]), 1)
  assert.equal(actingOrgAdmins([member('admin', { server_role: 'member' }), member('admin')]), 2)
  assert.equal(effectiveOrgRole('admin', 'viewer'), 'read')
  assert.equal(effectiveOrgRole('write', 'member'), 'write')
  assert.equal(effectiveOrgRole('read', undefined), 'read')
})
