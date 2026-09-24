export interface Snippet {
  comment: string
  code: string
}

const EMBEDDING_TASKS = new Set(['feature-extraction', 'sentence-similarity'])

export function resolveServerUrl(publicUrl: string | null | undefined, origin: string): string {
  return (publicUrl || origin).replace(/\/+$/, '')
}

export function isLoopbackUrl(value: string): boolean {
  try {
    const { hostname } = new URL(value)
    return (
      hostname === 'localhost'
      || hostname === '::1'
      || hostname === '[::1]'
      || hostname.startsWith('127.')
    )
  } catch {
    return false
  }
}

function requestSnippet(repoId: string, task?: string | null): Snippet {
  if (task && EMBEDDING_TASKS.has(task)) {
    return {
      comment: 'Call the server using curl (OpenAI-compatible API):',
      code: [
        'curl -X POST "http://localhost:8000/v1/embeddings" \\',
        '\t-H "Content-Type: application/json" \\',
        `\t--data '{"model": "${repoId}", "input": "What is the capital of France?"}'`,
      ].join('\n'),
    }
  }
  return {
    comment: 'Call the server using curl (OpenAI-compatible API):',
    code: [
      'curl -X POST "http://localhost:8000/v1/chat/completions" \\',
      '\t-H "Content-Type: application/json" \\',
      "\t--data '{",
      `\t\t"model": "${repoId}",`,
      '\t\t"messages": [',
      '\t\t\t{',
      '\t\t\t\t"role": "user",',
      '\t\t\t\t"content": "What is the capital of France?"',
      '\t\t\t}',
      '\t\t]',
      "\t}'",
    ].join('\n'),
  }
}

export function vllmPipSnippets(repoId: string, server: string, task?: string | null): Snippet[] {
  return [
    {
      comment: 'Install vLLM from pip (use your internal package mirror when offline):',
      code: 'pip install vllm',
    },
    {
      comment: 'Pull the weights from HuggingHack instead of huggingface.co:',
      code: `export HF_ENDPOINT="${server}"`,
    },
    { comment: 'Load and run the model:', code: `vllm serve "${repoId}"` },
    requestSnippet(repoId, task),
  ]
}

export function vllmDockerSnippets(repoId: string, server: string, task?: string | null): Snippet[] {
  return [
    {
      comment: 'Deploy with docker on Linux (pull the image from your internal registry when offline):',
      code: [
        'docker run --runtime nvidia --gpus all \\',
        '\t--name my_vllm_container \\',
        '\t-v ~/.cache/huggingface:/root/.cache/huggingface \\',
        `\t--env "HF_ENDPOINT=${server}" \\`,
        '\t-p 8000:8000 \\',
        '\t--ipc=host \\',
        '\tvllm/vllm-openai:latest \\',
        `\t--model ${repoId}`,
      ].join('\n'),
    },
    requestSnippet(repoId, task),
  ]
}

export function gitCloneSnippets(repoId: string, server: string): Snippet[] {
  const url = `${server}/${repoId}`
  return [
    { comment: 'Make sure git-lfs is installed (https://git-lfs.com)', code: 'git lfs install' },
    { comment: 'Clone the repository with its weights:', code: `git clone ${url}` },
    {
      comment: 'If you want to clone without large files - just their pointers',
      code: `GIT_LFS_SKIP_SMUDGE=1 git clone ${url}`,
    },
  ]
}

export function hfCliSnippets(repoId: string, server: string): Snippet[] {
  return [
    {
      comment: 'Make sure the hf CLI is installed',
      code: 'pip install -U "huggingface_hub[cli]"',
    },
    { comment: 'Point it at HuggingHack:', code: `export HF_ENDPOINT="${server}"` },
    { comment: 'Download the model', code: `hf download ${repoId}` },
  ]
}
