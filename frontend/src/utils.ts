export function formatBytes(bytes = 0, precision = 1): string {
  if (!Number.isFinite(bytes) || bytes <= 0) return '0 B'
  const units = ['B', 'KB', 'MB', 'GB', 'TB', 'PB']
  const index = Math.min(Math.floor(Math.log(bytes) / Math.log(1024)), units.length - 1)
  return `${(bytes / 1024 ** index).toFixed(index === 0 ? 0 : precision)} ${units[index]}`
}

export function formatNumber(value = 0): string {
  return new Intl.NumberFormat('en', {
    notation: value >= 10_000 ? 'compact' : 'standard',
    maximumFractionDigits: 1,
  }).format(value)
}

export function relativeTime(value?: string | null): string {
  if (!value) return 'Unknown'
  const timestamp = new Date(value).getTime()
  if (Number.isNaN(timestamp)) return 'Unknown'
  const seconds = Math.round((timestamp - Date.now()) / 1000)
  const formatter = new Intl.RelativeTimeFormat('en', { numeric: 'auto' })
  const ranges: Array<[Intl.RelativeTimeFormatUnit, number]> = [
    ['year', 31_536_000],
    ['month', 2_592_000],
    ['week', 604_800],
    ['day', 86_400],
    ['hour', 3_600],
    ['minute', 60],
  ]
  for (const [unit, size] of ranges) {
    if (Math.abs(seconds) >= size) return formatter.format(Math.round(seconds / size), unit)
  }
  return formatter.format(seconds, 'second')
}

export function initials(repoId: string): string {
  const parts = repoId.split('/')
  return (parts[0]?.slice(0, 2) || 'HH').toUpperCase()
}

// Hugging Face task names that are not plain title case (huggingface.co/api/tasks).
const TASK_LABELS: Record<string, string> = {
  'any-to-any': 'Any-to-Any',
  'audio-text-to-text': 'Audio-Text-to-Text',
  'audio-to-audio': 'Audio-to-Audio',
  'fill-mask': 'Fill-Mask',
  'image-text-to-image': 'Image-Text-to-Image',
  'image-text-to-text': 'Image-Text-to-Text',
  'image-text-to-video': 'Image-Text-to-Video',
  'image-to-3d': 'Image-to-3D',
  'image-to-image': 'Image-to-Image',
  'image-to-text': 'Image-to-Text',
  'image-to-video': 'Image-to-Video',
  'text-to-3d': 'Text-to-3D',
  'text-to-image': 'Text-to-Image',
  'text-to-speech': 'Text-to-Speech',
  'text-to-video': 'Text-to-Video',
  'video-text-to-text': 'Video-Text-to-Text',
  'video-to-video': 'Video-to-Video',
  'zero-shot-classification': 'Zero-Shot Classification',
  'zero-shot-image-classification': 'Zero-Shot Image Classification',
  'zero-shot-object-detection': 'Zero-Shot Object Detection',
}

export function taskLabel(task?: string | null): string {
  if (!task) return 'Model'
  if (TASK_LABELS[task]) return TASK_LABELS[task]
  return task
    .split('-')
    .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
    .join(' ')
}

/** A short name for the browser or tool behind a session, from its user agent. */
export function describeDevice(agent?: string | null): string {
  if (!agent) return 'Unknown device'
  const browser =
    /Edg\//.test(agent) ? 'Edge'
      : /Chrome\//.test(agent) ? 'Chrome'
        : /Firefox\//.test(agent) ? 'Firefox'
          : /Safari\//.test(agent) ? 'Safari'
            : /curl|python|httpx|huggingface/i.test(agent) ? 'Command line'
              : 'Browser'
  const system =
    /Windows/.test(agent) ? 'Windows'
      : /Mac OS X|Macintosh/.test(agent) ? 'macOS'
        : /Android/.test(agent) ? 'Android'
          : /iPhone|iPad/.test(agent) ? 'iOS'
            : /Linux/.test(agent) ? 'Linux'
              : ''
  return system ? `${browser} on ${system}` : browser
}

/** Where a user's or organization's picture is served; none when they have not set one. */
export function avatarUrl(namespace: string, version?: string | null): string | null {
  return version ? `/api/avatars/${encodeURIComponent(namespace)}?v=${encodeURIComponent(version)}` : null
}
