// Record the HuggingHack demo against the seeded instance (demo/setup.sh), as a story:
// an engineer finds a model, checks its quantization's history and benchmarks and takes the
// vLLM command; then the admin opens an organization, uploads a model into it, and moves it
// to another bucket.
//
//   PLAYWRIGHT_CORE=<playwright-core/index.mjs> node demo/record.mjs <work dir> <frames dir>
//
// Frames come from Chrome's own screencast (JPEG, with timestamps), not Playwright's 25 fps
// recorder; demo/assemble.mjs turns them into a constant-rate 30 fps video. The cursor glides
// and the page scrolls on requestAnimationFrame, so motion is smooth in the recording.
const { chromium } = await import(process.env.PLAYWRIGHT_CORE)
const fs = await import('node:fs')
const path = await import('node:path')
const [work, framesDir] = process.argv.slice(2)
const base = process.env.DEMO_URL || 'http://127.0.0.1:7870'
const password = process.env.DEMO_PASSWORD || 'demo-password-2026'
const headed = process.env.DEMO_HEADED === '1'
const W = 1440, H = 900
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
      caption(html, right) {
        if (html) { caption.innerHTML = html; caption.classList.toggle('right', !!right); caption.classList.add('on') }
        else caption.classList.remove('on')
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
const say = async (html, ms = 0, right = false) => { await page.evaluate(([h, r]) => window.__demo?.caption(h, r), [html, right]); if (ms) await wait(ms) }
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

// ================================== ACT 1: the engineer ==================================
await page.goto('about:blank'); await startCapture()
await card(titleCard('HuggingHack', 'Your model hub. Air-gapped.', [
  'Bring a model across once. Every machine on the network pulls it',
  'with <span style="color:#e5e7eb">HF_ENDPOINT</span>: vLLM, Transformers, the hf CLI, or git clone.',
]), 4200)

await goto(base + '/', page.getByRole('button', { name: /^Sign in$/ }))
await say('An engineer signs in: <em>company SSO</em> or a local account.')
await moveTo(page.getByRole('link', { name: 'Sign in with Authentik' }), { ms: 800 }); await wait(1200)
await type(page.getByRole('textbox', { name: /Username/ }), 'gil')
await type(page.getByRole('textbox', { name: /^Password/ }), password)
await hush(200)
await click(page.getByRole('button', { name: /^Sign in$/ }), 300)
await page.getByRole('button', { name: /model details$/ }).first().waitFor({ timeout: 20000 })
await wait(700)

await say('They need a small chat model for a support bot. <em>Search the library.</em>')
await type(page.getByRole('textbox', { name: 'Search models' }), 'qwen', 1600)
await hush()
await click(page.getByRole('button', { name: 'Open Qwen/Qwen3-0.6B model details' }), 200)
await page.getByRole('heading', { name: 'Model tree' }).waitFor({ timeout: 20000 }); await wait(500)
await say('Qwen3-0.6B. Its <em>model tree</em> shows every quantization and fine-tune in the library.')
await moveTo(page.getByRole('heading', { name: 'Model tree' }), { ms: 800 }); await wait(2600)
await say('A 4-bit quantization will fit the GPU better. <em>Open it.</em>')
await click(page.getByRole('link', { name: 'RedHat/Qwen3-0.6B-quantized.w4a16' }), 200, { ms: 700 })
await page.getByRole('heading', { name: 'Qwen3-0.6B-quantized.w4a16', level: 1 }).first().waitFor({ timeout: 20000 }); await wait(400)
await say('INT4 weights, a fraction of the memory, and <em>serving notes</em> the team left on the model card.')
await moveTo(page.locator('blockquote').first(), { ms: 800 }); await wait(3000)

await hush()
await click(page.getByRole('link', { name: /^Commits/ }), 200)
await page.getByRole('link', { name: 'Add serving notes for the L40' }).waitFor({ timeout: 20000 }); await wait(300)
await say('Every change is a <em>commit</em>: who changed what, and when.', 1600)
await click(page.getByRole('link', { name: 'Add serving notes for the L40' }), 300)
await page.getByText('README.md').first().waitFor({ timeout: 20000 }); await wait(300)
await say('The diff shows exactly what the notes added.')
await scrollBy(260, 1000); await wait(2200)

await hush()
await click(page.getByRole('link', { name: /^Config/ }), 200)
await page.getByRole('heading', { name: 'Compare results' }).waitFor({ timeout: 20000 }); await wait(400)
await say('<em>Deployment configs</em>: the vLLM flags the team ran, versioned, with measured results.', 2400)
await moveTo(page.getByRole('heading', { name: 'Compare results' }), { ms: 700 })
await scrollBy(320, 1000)
await say('The best value in each row is highlighted: <em>#2</em> answers first, <em>#3</em> has the most throughput and KV cache.', 4200)
await hush()
await click(page.getByRole('link', { name: /^#3 FP8 KV cache/ }).first(), 300)
await page.getByText('serve.sh').first().waitFor({ timeout: 20000 }); await wait(300)
await say('They take <em>#3</em>: FP8 KV cache on one L40. Here is the exact serve script.')
await scrollBy(300, 1000); await wait(2400)

await hush()
await page.evaluate(() => window.__demo.scroll(0, 700)); await wait(200)
await click(page.getByRole('button', { name: 'Use this model' }), 700)
await say('<em>Use this model</em>: the vLLM command, pointed at this hub instead of the internet.')
await moveTo(page.getByRole('dialog').locator('.snippet-code').first(), { ms: 800 }); await wait(3200)
await hush()
await page.keyboard.press('Escape'); await wait(500)

const transcript = fs.readFileSync(path.join(work, 'terminal.txt'), 'utf8').replace(/\x1b\[[0-9;]*m/g, '')
  .split('\n').filter((l) => l && !/^Hint:/.test(l) && !/^Fetching/.test(l))
await card(`<div style="width:1180px;background:#111827;border:1px solid #1f2937;border-radius:16px;box-shadow:0 30px 80px rgba(0,0,0,.6);overflow:hidden">
    <div style="height:38px;background:#1f2937;display:flex;align-items:center;gap:8px;padding:0 16px"><span style="width:12px;height:12px;border-radius:6px;background:#ff5f57"></span><span style="width:12px;height:12px;border-radius:6px;background:#febc2e"></span><span style="width:12px;height:12px;border-radius:6px;background:#28c840"></span><span style="margin-left:12px;color:#9ca3af;font:14px system-ui">gpu-node-07 — no internet access</span></div>
    <pre id="t" style="margin:0;padding:26px 30px;color:#e5e7eb;font:19px/1.6 'IBM Plex Mono',ui-monospace,Menlo,monospace;min-height:360px;white-space:pre-wrap"></pre></div>`, 50)
await page.evaluate(() => window.__demo.fade(false))
await say('On the GPU node, vLLM pulls through <em>HF_ENDPOINT</em>. This is the same pull with the hf CLI.')
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

// =================================== ACT 2: the admin ===================================
await goto(base + '/#/admin/organizations', page.getByRole('button', { name: 'New organization' }).first())
await say('Meanwhile, the <em>admin</em> sets up a new team: an organization of its own.', 1800)
await click(page.getByRole('button', { name: 'New organization' }).first(), 500)
const orgDialog = page.getByRole('dialog')
await type(orgDialog.getByLabel('Name', { exact: true }), 'data-lab')
await type(orgDialog.getByLabel(/Display name/), 'Data Lab')
await click(orgDialog.getByRole('button', { name: 'Create organization' }), 1600)
await say('<em>data-lab</em> exists; the admin is its first member. Now a model for it.', 1800)

await scene('#/uploads', page.getByLabel('Model name'))
await say('<em>Upload</em> from the browser: name it, choose the team, who sees it, and which bucket.')
await type(page.getByLabel('Model name'), 'Qwen3-0.6B-sql')
await click(page.getByRole('button', { name: /^Owner/ }), 400)
await click(page.getByRole('option', { name: /data-lab/ }), 500)
await click(page.getByRole('button', { name: 'Continue' }), 600)
await click(page.locator('label.choice-card', { hasText: 'Public' }), 500)
await click(page.locator('label.choice-card', { hasText: 'NetApp primary' }), 600)
await click(page.getByRole('button', { name: 'Continue' }), 600)
await moveTo(page.getByRole('button', { name: /Drop a model folder/ }), { ms: 700 }); await wait(300)
await page.locator('input[type=file]').setInputFiles(path.join(work, 'upload', 'Qwen3-0.6B-sql'))
for (let i = 0; i < 80 && !(await page.getByRole('button', { name: 'Continue' }).isEnabled()); i++) await wait(250)
await wait(900)
await click(page.getByRole('button', { name: 'Continue' }), 300)
await page.getByRole('button', { name: 'Create and upload' }).waitFor({ timeout: 20000 }); await wait(300)
await say('The listing is read from the files first: task, precision, parameters, license.')
await scrollBy(340, 1100); await wait(1800)
await click(page.getByRole('button', { name: 'Create and upload' }), 500)
// The upload panel in the corner follows every file live, and keeps going while you browse.
const dock = page.getByRole('region', { name: 'Uploads' })
const dockToggle = dock.locator('.upload-dock-toggle')
if ((await dockToggle.count()) && (await dockToggle.getAttribute('aria-expanded')) === 'false') await click(dockToggle, 400, { ms: 800 })
await say('The <em>upload panel</em> tracks it live: parts go straight to the bucket, several at a time.', 0, true)
const filesToggle = dock.locator('.upload-job-files-toggle').first()
if (await filesToggle.count()) await click(filesToggle, 300, { ms: 600 })
for (let i = 0; i < 240; i++) { await wait(250); if (/committed/i.test(await page.evaluate(() => [...document.querySelectorAll('.toast, [aria-label=Uploads]')].map((e) => e.textContent).join(' ')))) break }
await say('Done: committed as the first version of <em>data-lab/Qwen3-0.6B-sql</em>.', 2200, true)
const clear = page.getByRole('button', { name: 'Clear finished uploads' })
if (await clear.count()) await click(clear, 400)

await scene('#/admin/storage', page.getByPlaceholder('Filter models in every location'))
await say('<em>Storage</em>: the local disk and two S3 buckets, with the models in each.', 2000)
await type(page.getByPlaceholder('Filter models in every location'), 'sql', 900)
const row = page.locator('[role=row]', { hasText: 'data-lab/Qwen3-0.6B-sql' }).first()
await say('The new model sits in <em>NetApp primary</em>. The team wants it in the archive.')
await click(row.getByRole('button', { name: 'Move' }), 600)
const moveDialog = page.getByRole('dialog')
await click(moveDialog.locator('label.choice-card', { hasText: 'Archive bucket' }), 500)
await type(moveDialog.getByRole('textbox'), 'data-lab/Qwen3-0.6B-sql', 300)
await click(moveDialog.getByRole('button', { name: 'Move model' }), 600)
await say('Copy, <em>verify every hash</em>, switch. Pulls keep working the whole time.')
const moveStatus = () => page.evaluate(async () => {
  const moves = (await (await fetch('/api/storage/moves')).json()).items
  return moves.find((m) => m.repo_id === 'data-lab/Qwen3-0.6B-sql')?.status || 'none'
})
for (let i = 0; i < 120; i++) { await wait(250); if (['draining', 'done', 'failed'].includes(await moveStatus())) break }
await say('Switched. Download links handed out before the move stay valid until they expire.', 2600)
// The old copy waits out those links (a minute here): cut to the result.
await hush(200)
await page.evaluate(() => window.__demo.fade(true)); await wait(500)
await pause()
for (let i = 0; i < 180; i++) { if ((await moveStatus()) === 'done') break; await page.waitForTimeout(1000) }
await page.evaluate(() => { location.reload() })
await page.getByPlaceholder('Filter models in every location').waitFor({ timeout: 20000 })
await page.getByPlaceholder('Filter models in every location').fill('sql'); await wait(900)
await resume()
await startCapture()
await page.evaluate(() => window.__demo.fade(false)); await wait(600)
await say('Done: <em>data-lab/Qwen3-0.6B-sql</em> now lives in the Archive bucket, under the same name.')
await moveTo(page.locator('[role=row]', { hasText: 'data-lab/Qwen3-0.6B-sql' }).first(), { ms: 900 }); await wait(3200)
await hush(200)
await page.evaluate(() => window.__demo.fade(true)); await wait(500)

await card(titleCard('HuggingHack', 'Self-hosted. Air-gapped. Yours.', [
  'Hub protocol · git + LFS · S3 buckets · PostgreSQL · SSO · Helm for OpenShift',
  'github.com/GilGedje/HuggingHack',
]), 4500)

await stopCapture()
await new Promise((r) => setTimeout(r, 500))
await browser.close()
fs.writeFileSync(path.join(framesDir, 'frames.json'), JSON.stringify(frames))
console.log(JSON.stringify({ frames: frames.length, seconds: frames.length ? +(frames.at(-1).ts - frames[0].ts).toFixed(1) : 0 }))
