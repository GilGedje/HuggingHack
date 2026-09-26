// Record the upload video against the seeded instance (demo/render-upload.sh): every way a
// model comes in. A browser upload with its live progress, interrupted and resumed; a change
// committed to it; a folder copied into model storage and picked up by a scan; and the first
// pull from a GPU node.
//
//   PLAYWRIGHT_CORE=<playwright-core/index.mjs> node demo/record-upload.mjs <work dir> <frames dir>
//
// The capture, cursor, captions and fades are the same as demo/record.mjs. Close-ups use
// Chrome's pinch zoom, which magnifies the fixed upload panel like a camera would.
const { chromium } = await import(process.env.PLAYWRIGHT_CORE)
const fs = await import('node:fs')
const path = await import('node:path')
const { execFileSync } = await import('node:child_process')
const [work, framesDir] = process.argv.slice(2)
const base = process.env.DEMO_URL || 'http://127.0.0.1:7870'
const password = process.env.DEMO_PASSWORD || 'demo-password-2026'
const headed = process.env.DEMO_HEADED === '1'
const W = 1440, H = 900
const folders = path.join(work, 'upload-video')
fs.rmSync(framesDir, { recursive: true, force: true })
fs.mkdirSync(framesDir, { recursive: true })

const browser = await chromium.launch({
  channel: 'chrome',
  headless: !headed,
  args: headed ? ['--window-position=0,0', `--window-size=${W},${H + 80}`] : [],
})
const context = await browser.newContext({
  viewport: { width: W, height: H },
  deviceScaleFactor: 2,
  colorScheme: 'light',
  reducedMotion: 'no-preference',
})

// ---- overlay: cursor, caption, scene fades ----------------------------------------------
// Installed on every document; setContent pages (the cards) get it by hand, since
// init scripts do not run for them.
const overlay = () => {
  const ready = () => {
    if (document.getElementById('demo-cursor')) return
    const style = document.createElement('style')
    style.textContent = `
      #demo-cursor{position:fixed;left:720px;top:450px;width:26px;height:26px;z-index:2147483647;pointer-events:none;
        margin:-2px 0 0 -3px;filter:drop-shadow(0 1px 2px rgba(0,0,0,.45))}
      #demo-cursor svg{transition:transform .12s ease-out}
      #demo-cursor.down svg{transform:scale(.8)}
      #demo-caption{position:fixed;left:50%;bottom:34px;transform:translate(-50%,8px);max-width:1120px;padding:14px 26px;
        border-radius:14px;background:rgba(17,24,39,.93);color:#fff;font:600 23px/1.38 "IBM Plex Sans",system-ui,sans-serif;
        z-index:2147483646;pointer-events:none;opacity:0;transition:opacity .35s ease,transform .35s ease;text-align:center;
        box-shadow:0 12px 40px rgba(0,0,0,.25)}
      #demo-caption.on{opacity:1;transform:translate(-50%,0)}
      #demo-caption em{color:#ffd21e;font-style:normal}
      #demo-caption.right{left:auto;right:40px;transform:translate(0,8px);max-width:760px}
      #demo-caption.right.on{transform:translate(0,0)}
      #demo-fade{position:fixed;inset:0;background:#0b0f19;z-index:2147483645;pointer-events:none;opacity:1;transition:opacity .45s ease}
      #demo-fade.clear{opacity:0}`
    document.head.appendChild(style)
    const cursor = document.createElement('div')
    cursor.id = 'demo-cursor'
    cursor.innerHTML = '<svg viewBox="0 0 24 24" width="26" height="26"><path d="M5 3l14 9-6 1.5L16.5 21l-2.6 1.2L10.6 15 6 19z" fill="#fff" stroke="#111827" stroke-width="1.6" stroke-linejoin="round"/></svg>'
    const caption = document.createElement('div')
    caption.id = 'demo-caption'
    const fade = document.createElement('div')
    fade.id = 'demo-fade'
    document.body.append(fade, cursor, caption)
    // about:blank (the cards) has no storage; the cursor then starts centred.
    const store = { get: () => { try { return sessionStorage.getItem('demo-cursor') } catch { return null } },
                    set: (v) => { try { sessionStorage.setItem('demo-cursor', v) } catch {} } }
    const saved = JSON.parse(store.get() || 'null')
    if (saved) { cursor.style.left = saved.x + 'px'; cursor.style.top = saved.y + 'px' }
    requestAnimationFrame(() => requestAnimationFrame(() => fade.classList.add('clear')))
    const ease = (t) => (t < 0.5 ? 4 * t * t * t : 1 - Math.pow(-2 * t + 2, 3) / 2)
    window.__demo = {
      glide(x, y, ms) {
        return new Promise((done) => {
          const x0 = parseFloat(cursor.style.left), y0 = parseFloat(cursor.style.top), t0 = performance.now()
          const step = (now) => {
            const t = Math.min(1, (now - t0) / ms), e = ease(t)
            cursor.style.left = x0 + (x - x0) * e + 'px'; cursor.style.top = y0 + (y - y0) * e + 'px'
            if (t < 1) requestAnimationFrame(step)
            else { store.set(JSON.stringify({ x, y })); done() }
          }
          requestAnimationFrame(step)
        })
      },
      scroll(to, ms) {
        return new Promise((done) => {
          const y0 = window.scrollY, max = document.documentElement.scrollHeight - innerHeight
          const y1 = Math.max(0, Math.min(max, to)), t0 = performance.now()
          if (Math.abs(y1 - y0) < 2) return done()
          const step = (now) => {
            const t = Math.min(1, (now - t0) / ms)
            window.scrollTo(0, y0 + (y1 - y0) * ease(t))
            if (t < 1) requestAnimationFrame(step); else done()
          }
          requestAnimationFrame(step)
        })
      },
      press(down) { cursor.classList.toggle('down', down) },
      caption(html, place) {
        if (!html) return caption.classList.remove('on')
        caption.innerHTML = html
        caption.classList.toggle('right', place === 'right')
        const at = place && typeof place === 'object' ? place : null
        const k = at ? 1 / at.scale : 1
        Object.assign(caption.style, at
          ? { left: at.x + 'px', top: at.y + 'px', bottom: 'auto', maxWidth: at.width + 'px', transform: 'none',
              fontSize: 23 * k + 'px', padding: `${14 * k}px ${26 * k}px`, borderRadius: 14 * k + 'px', textAlign: 'left' }
          : { left: '', top: '', bottom: '', maxWidth: '', transform: '', fontSize: '', padding: '', borderRadius: '', textAlign: '' })
        caption.classList.add('on')
      },
      fade(out) { fade.classList.toggle('clear', !out) },
    }
  }
  if (document.body) ready(); else document.addEventListener('DOMContentLoaded', ready)
}
await context.addInitScript(overlay)

const page = await context.newPage()

// ---- capture: Chrome's screencast, restarted after every navigation ----------------------
const cdp = await context.newCDPSession(page)
const frames = []
let offset = 0, paused = false, pausedAt = 0, frameNo = 0
cdp.on('Page.screencastFrame', ({ data, metadata, sessionId }) => {
  cdp.send('Page.screencastFrameAck', { sessionId }).catch(() => {})
  if (paused) return
  const file = `f${String(frameNo++).padStart(6, '0')}.jpg`
  frames.push({ file, ts: metadata.timestamp - offset })
  fs.promises.writeFile(path.join(framesDir, file), Buffer.from(data, 'base64'))
})
// Chrome may or may not keep the screencast across a navigation: restart it either way.
const startCapture = async () => {
  await cdp.send('Page.stopScreencast').catch(() => {})
  await cdp.send('Page.startScreencast', { format: 'jpeg', quality: 90, maxWidth: 1920, maxHeight: 1200, everyNthFrame: 1 })
}
const stopCapture = () => cdp.send('Page.stopScreencast').catch(() => {})
// A jump cut: time spent paused is removed from the video.
const pause = async () => { paused = true; pausedAt = Date.now() / 1000 }
const resume = async () => { offset += Date.now() / 1000 - pausedAt; paused = false }

const wait = (ms) => page.waitForTimeout(ms)
const say = async (html, ms = 0, place = null) => { await page.evaluate(([h, p]) => window.__demo?.caption(h, p), [html, place]); if (ms) await wait(ms) }
const hush = async (ms = 350) => { await page.evaluate(() => window.__demo?.caption(null)); await wait(ms) }
const center = async (locator, dx = 0) => {
  const box = await locator.boundingBox()
  return box ? { x: box.x + Math.min(box.width / 2 + dx, box.width - 8), y: box.y + box.height / 2 } : null
}
const inView = async (locator) => {
  const box = await locator.boundingBox()
  if (!box) return
  if (box.y < 90 || box.y + box.height > H - 150) {
    const top = await page.evaluate(() => window.scrollY)
    await page.evaluate(([to]) => window.__demo.scroll(to, 750), [top + box.y - H / 3])
    await wait(120)
  }
}
const moveTo = async (locator, { ms = 650, dx = 0 } = {}) => {
  await locator.waitFor({ state: 'visible', timeout: 20000 })
  await inView(locator)
  const at = await center(locator, dx)
  if (!at) return
  await page.evaluate(([x, y, t]) => window.__demo.glide(x, y, t), [at.x, at.y, ms])
  await page.mouse.move(at.x, at.y)
}
const click = async (locator, after = 600, opts = {}) => {
  await moveTo(locator, opts)
  await wait(140)
  await page.evaluate(() => window.__demo.press(true)); await page.mouse.down(); await wait(80)
  await page.mouse.up(); await page.evaluate(() => window.__demo.press(false))
  await wait(after)
}
const type = async (locator, text, after = 250) => { await click(locator, 150); await page.keyboard.type(text, { delay: 60 }); await wait(after) }
const scrollBy = async (dy, ms = 900) => { await page.evaluate(([d, t]) => window.__demo.scroll(window.scrollY + d, t), [dy, ms]); await wait(150) }
const scene = async (hash, ready) => {
  await hush(250)
  await page.evaluate(() => window.__demo.fade(true)); await wait(450)
  await page.evaluate((h) => { window.scrollTo(0, 0); location.hash = h }, hash)
  await ready.waitFor({ state: 'visible', timeout: 20000 })
  await page.evaluate(() => window.scrollTo(0, 0)); await wait(250)
  await page.evaluate(() => window.__demo.fade(false)); await wait(500)
}
const goto = async (url, ready) => {
  // After a card (setContent keeps the address) a hash-only change would not load the app.
  await page.goto('about:blank')
  await page.goto(url)
  await startCapture()
  if (ready) await ready.waitFor({ state: 'visible', timeout: 20000 })
  await wait(600)
}
const card = async (html, ms) => {
  await page.setContent(`<!doctype html><html><body style="margin:0;background:#0b0f19;color:#f9fafb;font-family:'IBM Plex Sans',system-ui,sans-serif;height:100vh;display:flex;align-items:center;justify-content:center">${html}</body></html>`)
  await page.evaluate(overlay)
  await startCapture()
  await wait(ms)
  await page.evaluate(() => window.__demo.fade(true)); await wait(500)
}
const logo = await (await fetch(base + '/hugginghack-mark-dark.svg')).text()
const titleCard = (title, subtitle, lines) => `<div style="text-align:center;max-width:1000px;padding:0 40px">
  <div style="width:112px;height:112px;margin:0 auto 26px">${logo}</div>
  <div style="font-size:64px;font-weight:700;letter-spacing:-1px">${title}</div>
  <div style="font-size:30px;color:#ffd21e;margin-top:12px;font-weight:600">${subtitle}</div>
  <div style="font-size:22px;color:#9ca3af;margin-top:32px;line-height:1.6">${lines.join('<br>')}</div></div>`


// Pinch zoom around a point on screen. Zooming back out anchors in the zoomed view's own
// coordinates, so the same corner stays put. `region` is what the zoomed view shows, in page
// coordinates, for placing a caption inside it.
let zoomAt = null
const zoomIn = async (x, y, scale) => {
  await page.evaluate(() => window.__demo.glide(900, 300, 500)); await wait(520)
  await cdp.send('Input.synthesizePinchGesture', { x, y, scaleFactor: scale, relativeSpeed: 260 })
  zoomAt = { x, y, scale }
  await wait(200)
}
const zoomOut = async () => {
  const { x, y, scale } = zoomAt
  await cdp.send('Input.synthesizePinchGesture', { x: x / scale, y: y / scale, scaleFactor: 1 / scale, relativeSpeed: 260 })
  zoomAt = null
  await wait(250)
}
const region = () => {
  const { x, y, scale } = zoomAt
  const left = x - x / scale, top = y - y / scale
  return { left, top, right: left + W / scale, bottom: top + H / scale, scale }
}
// A caption beside the upload panel while zoomed in on it.
const sayBeside = async (html, ms = 0) => {
  const box = await dock.boundingBox()
  const r = region(), x = box.x + box.width + 14
  await say(html, ms, { x, y: r.top + 18, width: r.right - x - 14, scale: r.scale })
}
const dock = page.locator('aside[aria-label=Uploads]')
const dockText = () => page.evaluate(() => document.querySelector('[aria-label=Uploads]')?.textContent || '')
const percent = async () => +((await dockText()).match(/(\d+)%/)?.[1] || 0)
const openDock = async (showFiles = true) => {
  await dock.waitFor({ timeout: 20000 })
  const toggle = dock.locator('.upload-dock-toggle')
  if ((await toggle.count()) && (await toggle.getAttribute('aria-expanded')) === 'false') await click(toggle, 400, { ms: 700 })
  const files = dock.locator('.upload-job-files-toggle').first()
  if ((await files.count()) && (await files.getAttribute('aria-expanded')) === String(!showFiles)) await click(files, 300, { ms: 500 })
}
const committed = async (ms = 90000) => {
  for (let i = 0; i < ms / 250; i++) {
    await wait(250)
    const text = await page.evaluate(() => [...document.querySelectorAll('.toast, [aria-label=Uploads]')].map((e) => e.textContent).join(' '))
    if (/committed/i.test(text)) return true
  }
  return false
}
// A click drawn by the cursor on a file picker, whose files are then set directly: the native
// dialog cannot be recorded.
const pick = async (label, input, files) => {
  await moveTo(label, { ms: 700 })
  await page.evaluate(() => window.__demo.press(true)); await wait(90); await page.evaluate(() => window.__demo.press(false))
  await input.setInputFiles(files)
}

// ================================= a browser upload =====================================
await page.goto('about:blank'); await startCapture()
await card(titleCard('Bring a model in', 'Upload it, resume it, change it, or copy it in.', [
  'A model crosses the air gap once.',
  'After that, every machine on the network pulls it from HuggingHack.',
]), 4200)

await goto(base + '/', page.getByRole('button', { name: /^Sign in$/ }))
await say('An engineer has a fine-tune to share: <em>Qwen3-0.6B-sql</em>, about 4 GB.')
await type(page.getByRole('textbox', { name: /Username/ }), 'gil')
await type(page.getByRole('textbox', { name: /^Password/ }), password)
await hush(200)
await click(page.getByRole('button', { name: /^Sign in$/ }), 300)
await page.getByRole('button', { name: /model details$/ }).first().waitFor({ timeout: 20000 })
await wait(500)

await scene('#/uploads', page.getByLabel('Model name'))
await say('<em>Uploads</em> takes four steps. First the name, and who owns it: you or a team.')
await type(page.getByLabel('Model name'), 'Qwen3-0.6B-sql')
await click(page.getByRole('button', { name: /^Owner/ }), 400)
await click(page.getByRole('option', { name: /acme-ai/ }), 500)
await type(page.locator('textarea').first(), 'Text-to-SQL fine-tune for the analytics team.', 500)
await click(page.getByRole('button', { name: 'Continue' }), 600)
await say('Then who can see it, and where it is stored. <em>NetApp primary</em> takes uploads straight into the bucket.')
await click(page.locator('label.choice-card', { hasText: 'Public' }), 500)
await click(page.locator('label.choice-card', { hasText: 'NetApp primary' }), 1200)
await click(page.getByRole('button', { name: 'Continue' }), 600)
await say('Drop the folder. The check reads what is in it and flags what is missing: here, the tokenizer.')
await moveTo(page.getByRole('button', { name: /Drop a model folder/ }), { ms: 700 }); await wait(300)
await page.locator('input[type=file]').setInputFiles(path.join(folders, 'Qwen3-0.6B-sql'))
for (let i = 0; i < 80 && !(await page.getByRole('button', { name: 'Continue' }).isEnabled()); i++) await wait(250)
await wait(500)
await moveTo(page.locator('li.missing', { hasText: 'Tokenizer' }).first(), { ms: 700 }); await wait(2200)
await click(page.getByRole('button', { name: 'Continue' }), 300)
await page.getByRole('button', { name: 'Create and upload' }).waitFor({ timeout: 20000 }); await wait(300)
await say('The review shows how the library will list it, read from the files. <em>Correct</em> what does not fit.')
const library = page.locator('.listing-row').filter({ has: page.locator('dt', { hasText: /^Library/ }) })
await click(library.getByRole('button', { name: 'Edit' }), 300)
await type(library.getByRole('textbox'), 'transformers', 300)
await click(library.getByRole('button', { name: 'Done' }), 1400)
await hush()
await click(page.getByRole('button', { name: 'Create and upload' }), 500)

await openDock()
await say('The <em>upload panel</em> follows it live. A closer look:', 1400)
await hush(200)
await zoomIn(16, 884, 2)
await sayBeside('Each file goes up in parts, <em>several at a time</em>, straight to the bucket.', 3200)
await hush(200)
await zoomOut()

await say('Nobody has to wait on it: keep browsing, the upload carries on.')
await click(page.getByRole('link', { name: 'Models', exact: true }).first(), 400)
await page.getByRole('button', { name: /model details$/ }).first().waitFor({ timeout: 20000 })
await moveTo(page.getByRole('button', { name: /model details$/ }).nth(1), { ms: 800 })
for (let i = 0; i < 120 && (await percent()) < 45; i++) await wait(250)
await say('Partway through, the tab is closed.', 1500)
await hush(200)
await page.evaluate(() => window.__demo.fade(true)); await wait(500)
await page.reload()
await startCapture()
await dock.getByText(/closed or reloaded/).waitFor({ timeout: 20000 })
// Until the folder is chosen again the file list reads 0 B, though the bucket keeps the parts.
await openDock(false)
await wait(400)
await say('Back on the page, the panel still has it. The parts already in the bucket are kept.', 1600)
await hush(200)
await zoomIn(16, 884, 2)
await sayBeside('It asks for the same folder again, to <em>resume</em> where it stopped.', 3000)
await hush(200)
await zoomOut()
const choose = dock.locator('label', { hasText: 'Choose folder' }).first()
await pick(choose, choose.locator('input'), path.join(folders, 'Qwen3-0.6B-sql'))
await wait(600)
await openDock()
await zoomIn(16, 884, 2)
await sayBeside('Only the missing parts are sent.')
for (let i = 0; i < 48 && !/committed|ready/i.test(await dockText()); i++) await wait(250)
await hush(200)
await zoomOut()
await committed()
await say('Done: <em>acme-ai/Qwen3-0.6B-sql</em> is published, as one commit.', 2200)
const clear = page.getByRole('button', { name: 'Clear finished uploads' })
if (await clear.count()) await click(clear, 400)

// ================================= a change, as a commit =================================
await scene('#/models/acme-ai/Qwen3-0.6B-sql', page.getByRole('button', { name: 'Upload changes' }).first())
await say('The model page lists it as the review said, with the <em>transformers</em> correction.', 2600)
await say('A week later the team adds files. <em>Upload changes</em> commits them together.')
await click(page.getByRole('button', { name: 'Upload changes' }).first(), 600)
const dialog = page.getByRole('dialog')
const addFiles = dialog.locator('label', { hasText: 'Add files' })
await pick(addFiles, addFiles.locator('input'), ['generation_config.json', 'README.md'].map((f) => path.join(folders, 'changes', f)))
await wait(500)
await say('A new generation config, and a model card that <em>replaces</em> the old one.', 1800)
await type(dialog.getByLabel('Commit message'), 'Add sampling defaults and the prompt format', 400)
await say('It applies all at once when the commit finishes, so nobody pulls a half-finished change.')
await click(dialog.getByRole('button', { name: 'Commit changes' }), 600)
await committed(30000)
await wait(600)
if (await clear.count()) await click(clear, 300)
await hush()
await page.evaluate(() => window.__demo.scroll(0, 600)); await wait(200)
await click(page.getByRole('link', { name: /^Commits/ }), 200)
const commitLink = page.getByRole('link', { name: 'Add sampling defaults and the prompt format' })
await commitLink.waitFor({ timeout: 20000 }); await wait(300)
await say('It is a <em>commit</em> like any other: who, when, and what.', 1600)
await click(commitLink, 300)
await page.getByText('README.md').first().waitFor({ timeout: 20000 }); await wait(300)
await say('With a diff of the model card.')
await scrollBy(300, 1000); await wait(2400)

// ============================ a folder copied in, then a scan ============================
await scene('#/admin/storage', page.getByPlaceholder('Filter models in every location'))
await say('Some models come in on a disk. The admin copies the folder into <em>model storage</em>…', 2400)
// The copy itself, off screen: a move within the work folder, so the sparse weights stay sparse.
fs.mkdirSync(path.join(work, 'models', 'acme-ai'), { recursive: true })
fs.renameSync(path.join(folders, 'incoming', 'Qwen3-0.6B-sql-v2'), path.join(work, 'models', 'acme-ai', 'Qwen3-0.6B-sql-v2'))
const scan = page.getByRole('button', { name: /Scan storage|Scanning/ })
await say('…and presses <em>Scan storage</em>.')
await click(scan, 300)
for (let i = 0; i < 240 && /Scanning/.test(await scan.textContent()); i++) await wait(250)
await wait(600)
await type(page.getByPlaceholder('Filter models in every location'), 'sql-v2', 900)
const scanned = page.locator('[role=row]', { hasText: 'acme-ai/Qwen3-0.6B-sql-v2' }).first()
await say('Found on the local disk and listed like any other model. Nothing went through a browser.')
await moveTo(scanned, { ms: 900 }); await wait(3200)
await hush(200)
await page.evaluate(() => window.__demo.fade(true)); await wait(500)

// =============================== the first pull, for real ===============================
// A jump cut: the pull itself (4 GB) is left out; its real output is typed on the card.
await pause()
const hfHome = path.join(work, 'hf-upload')
fs.rmSync(hfHome, { recursive: true, force: true })
const host = new URL(base).host
const pulled = execFileSync(process.env.DEMO_HF || 'hf',
  ['download', 'acme-ai/Qwen3-0.6B-sql'],
  { env: { ...process.env, HF_ENDPOINT: base, HF_HOME: hfHome, HF_HUB_DISABLE_TELEMETRY: '1' }, encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe'] })
const snapshot = fs.readdirSync(path.join(hfHome, 'hub', 'models--acme-ai--Qwen3-0.6B-sql', 'snapshots'))[0]
const listed = fs.readdirSync(path.join(hfHome, 'hub', 'models--acme-ai--Qwen3-0.6B-sql', 'snapshots', snapshot)).join(' ')
const clean = (s) => s.replaceAll(hfHome, '~/.cache/huggingface').replaceAll(host, 'hub.acme.internal').replace(/\x1b\[[0-9;]*m/g, '')
const transcript = [
  '$ export HF_ENDPOINT=http://hub.acme.internal',
  '$ hf download acme-ai/Qwen3-0.6B-sql',
  ...clean(pulled).split('\n').filter((l) => l.trim() && !/^Fetching|^Hint:/.test(l)),
  '$ ls ~/.cache/huggingface/hub/models--acme-ai--Qwen3-0.6B-sql/snapshots/*/',
  listed,
]
await resume()
await card(`<div style="width:1180px;background:#111827;border:1px solid #1f2937;border-radius:16px;box-shadow:0 30px 80px rgba(0,0,0,.6);overflow:hidden">
    <div style="height:38px;background:#1f2937;display:flex;align-items:center;gap:8px;padding:0 16px"><span style="width:12px;height:12px;border-radius:6px;background:#ff5f57"></span><span style="width:12px;height:12px;border-radius:6px;background:#febc2e"></span><span style="width:12px;height:12px;border-radius:6px;background:#28c840"></span><span style="margin-left:12px;color:#9ca3af;font:14px system-ui">gpu-node-07 — no internet access</span></div>
    <pre id="t" style="margin:0;padding:26px 30px;color:#e5e7eb;font:19px/1.6 'IBM Plex Mono',ui-monospace,Menlo,monospace;min-height:300px;white-space:pre-wrap"></pre></div>`, 50)
await page.evaluate(() => window.__demo.fade(false))
await say('On a GPU node, the new model pulls like any other, through <em>HF_ENDPOINT</em>.')
for (const line of transcript) {
  if (line.startsWith('$ ')) {
    await page.evaluate(() => { document.getElementById('t').textContent += '$ ' })
    for (const ch of line.slice(2)) { await page.evaluate((c) => { document.getElementById('t').textContent += c }, ch); await wait(24) }
    await page.evaluate(() => { document.getElementById('t').textContent += '\n' }); await wait(420)
  } else {
    await page.evaluate((l) => { document.getElementById('t').textContent += l + '\n' }, line); await wait(200)
  }
}
await wait(2600)
await hush(200)
await page.evaluate(() => window.__demo.fade(true)); await wait(500)

await card(titleCard('HuggingHack', 'Bring it in once. Pull it everywhere.', [
  'Browser uploads straight to S3 · resume after a drop · changes as commits · scan a copied folder',
  'github.com/GilGedje/HuggingHack',
]), 4500)

await stopCapture()
await new Promise((r) => setTimeout(r, 500))
await browser.close()
fs.writeFileSync(path.join(framesDir, 'frames.json'), JSON.stringify(frames))
console.log(JSON.stringify({ frames: frames.length, seconds: frames.length ? +(frames.at(-1).ts - frames[0].ts).toFixed(1) : 0 }))
