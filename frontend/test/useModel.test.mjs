import assert from 'node:assert/strict'
import test from 'node:test'
import {
  gitCloneSnippets,
  hfCliSnippets,
  isLoopbackUrl,
  resolveServerUrl,
  vllmDockerSnippets,
  vllmPipSnippets,
} from '../src/useModel.ts'

const server = 'http://10.0.0.5:7860'

test('prefers the configured public URL over the browser origin', () => {
  assert.equal(resolveServerUrl('http://nas.lan:7860/', 'http://localhost:7860'), 'http://nas.lan:7860')
  assert.equal(resolveServerUrl(null, 'http://localhost:7860'), 'http://localhost:7860')
  assert.equal(isLoopbackUrl('http://localhost:7860'), true)
  assert.equal(isLoopbackUrl('http://127.0.0.1:7860'), true)
  assert.equal(isLoopbackUrl(server), false)
})

test('vLLM snippets pull through HF_ENDPOINT', () => {
  const pip = vllmPipSnippets('acme/chat', server, 'text-generation').map((item) => item.code)
  assert.deepEqual(pip.slice(0, 3), [
    'pip install vllm',
    `export HF_ENDPOINT="${server}"`,
    'vllm serve "acme/chat"',
  ])
  assert.match(pip[3], /\/v1\/chat\/completions/)
  const docker = vllmDockerSnippets('acme/embed', server, 'feature-extraction').map((item) => item.code)
  assert.match(docker[0], new RegExp(`--env "HF_ENDPOINT=${server}"`))
  assert.match(docker[0], /--model acme\/embed$/)
  assert.match(docker[1], /\/v1\/embeddings/)
})

test('clone snippets use the git URL and the hf CLI', () => {
  const git = gitCloneSnippets('acme/chat', server).map((item) => item.code)
  assert.deepEqual(git, [
    'git lfs install',
    `git clone ${server}/acme/chat`,
    `GIT_LFS_SKIP_SMUDGE=1 git clone ${server}/acme/chat`,
  ])
  const cli = hfCliSnippets('acme/chat', server).map((item) => item.code)
  assert.equal(cli.at(-1), 'hf download acme/chat')
  assert.ok(cli.includes(`export HF_ENDPOINT="${server}"`))
})
