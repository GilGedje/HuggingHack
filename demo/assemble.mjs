// Turn the timestamped screencast frames from record.mjs into a constant 30 fps sequence
// for ffmpeg: for every 1/30 s tick, the latest frame at or before it (Chrome only sends a
// frame when something repaints, so still moments repeat the last one).
//   node demo/assemble.mjs <frames dir>   → writes <frames dir>/list.ffconcat
const fs = await import('node:fs')
const path = await import('node:path')
const dir = process.argv[2]
const frames = JSON.parse(fs.readFileSync(path.join(dir, 'frames.json'), 'utf8')).sort((a, b) => a.ts - b.ts)
const fps = 30, tick = 1 / fps
const first = frames[0].ts, last = frames.at(-1).ts
const lines = ['ffconcat version 1.0']
let i = 0
for (let k = 0; k <= Math.ceil((last - first) * fps); k++) {
  const at = first + k * tick
  while (i + 1 < frames.length && frames[i + 1].ts <= at) i++
  lines.push(`file '${frames[i].file}'`, `duration ${tick.toFixed(6)}`)
}
// The concat demuxer ignores the last entry's duration unless the file is repeated.
lines.push(`file '${frames.at(-1).file}'`)
fs.writeFileSync(path.join(dir, 'list.ffconcat'), lines.join('\n') + '\n')
console.log(JSON.stringify({ frames: frames.length, seconds: +(last - first).toFixed(1), ticks: (lines.length - 2) / 2 }))
