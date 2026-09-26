// Record the HuggingHack demo video against the seeded instance (demo/setup.sh).
//   PLAYWRIGHT_CORE=<path to playwright-core/index.mjs> node demo/record.mjs <work dir> <out.webm>
// A real Chrome drives the site as a person would, with a visible cursor and a caption bar
// injected into the page; Playwright records the session. demo/render.sh turns it into MP4.
const { chromium } = await import(process.env.PLAYWRIGHT_CORE)
const [work, output] = process.argv.slice(2)
const base = process.env.DEMO_URL || 'http://127.0.0.1:7870'
const password = process.env.DEMO_PASSWORD || 'demo-password-2026'
const fs = await import('node:fs')
const path = await import('node:path')

const W = 1440, H = 900
const browser = await chromium.launch({ channel: 'chrome', headless: process.env.DEMO_HEADED !== '1', args: process.env.DEMO_HEADED === '1' ? ['--window-position=0,0', '--window-size=1440,980'] : [] })
const context = await browser.newContext({
  viewport: { width: W, height: H },
  colorScheme: 'light',
  recordVideo: { dir: path.dirname(output), size: { width: W, height: H } },
})
// A cursor people can see, and a caption bar for the narration.
await context.addInitScript(() => {
  const ready = () => {
    if (document.getElementById('demo-cursor')) return
    const style = document.createElement('style')
    style.textContent = `
      #demo-cursor{position:fixed;left:0;top:0;width:26px;height:26px;z-index:2147483647;pointer-events:none;
        transform:translate(-3px,-2px);transition:transform .08s;filter:drop-shadow(0 1px 2px rgba(0,0,0,.45))}
      #demo-cursor.down{transform:translate(-3px,-2px) scale(.82)}
      #demo-caption{position:fixed;left:50%;bottom:34px;transform:translateX(-50%);max-width:1120px;padding:14px 24px;
        border-radius:14px;background:rgba(17,24,39,.92);color:#fff;font:600 23px/1.35 "IBM Plex Sans",system-ui,sans-serif;
        z-index:2147483646;pointer-events:none;opacity:0;transition:opacity .35s;letter-spacing:.1px;text-align:center}
      #demo-caption.on{opacity:1}
      #demo-caption em{color:#ffd21e;font-style:normal}`
    document.head.appendChild(style)
    const cursor = document.createElement('div')
    cursor.id = 'demo-cursor'
    cursor.innerHTML = '<svg viewBox="0 0 24 24" width="26" height="26"><path d="M5 3l14 9-6 1.5L16.5 21l-2.6 1.2L10.6 15 6 19z" fill="#fff" stroke="#111827" stroke-width="1.6" stroke-linejoin="round"/></svg>'
    const caption = document.createElement('div')
    caption.id = 'demo-caption'
    document.body.append(cursor, caption)
    document.addEventListener('mousemove', (e) => { cursor.style.left = e.clientX + 'px'; cursor.style.top = e.clientY + 'px' }, true)
    document.addEventListener('mousedown', () => cursor.classList.add('down'), true)
    document.addEventListener('mouseup', () => cursor.classList.remove('down'), true)
  }
  if (document.body) ready(); else document.addEventListener('DOMContentLoaded', ready)
})
const page = await context.newPage()
const marks = []
const t0 = Date.now()
const mark = (name) => marks.push({ name, at: (Date.now() - t0) / 1000 })
const wait = (ms) => page.waitForTimeout(ms)
const say = async (html, ms = 0) => {
  await page.evaluate((html) => { const c = document.getElementById('demo-caption'); if (c) { c.innerHTML = html; c.classList.add('on') } }, html)
  if (ms) await wait(ms)
}
const hush = () => page.evaluate(() => document.getElementById('demo-caption')?.classList.remove('on'))
let mouse = { x: W / 2, y: H / 2 }
const moveTo = async (locator, { dx = 0, dy = 0 } = {}) => {
  await locator.scrollIntoViewIfNeeded()
  const box = await locator.boundingBox()
  if (!box) return
  const x = box.x + Math.min(box.width / 2 + dx, box.width - 6), y = box.y + box.height / 2 + dy
  const steps = Math.max(12, Math.min(40, Math.round(Math.hypot(x - mouse.x, y - mouse.y) / 25)))
  await page.mouse.move(x, y, { steps })
  mouse = { x, y }
}
const click = async (locator, pause = 700) => { await moveTo(locator); await wait(180); await page.mouse.down(); await wait(70); await page.mouse.up(); await wait(pause) }
const type = async (locator, text) => { await click(locator, 200); await page.keyboard.type(text, { delay: 55 }) }
const go = async (hash, ms = 1200) => { await page.evaluate((h) => { location.hash = h }, hash); await wait(ms) }
const scroll = async (dy, ms = 900) => { await page.mouse.wheel(0, dy); await wait(ms) }

const card = async (title, subtitle, lines, ms) => {
  const logo = await (await fetch(base + '/hugginghack-mark-dark.svg')).text()
  await page.setContent(`<!doctype html><html><body style="margin:0;background:#0b0f19;color:#f9fafb;font-family:'IBM Plex Sans',system-ui,sans-serif;height:100vh;display:flex;align-items:center;justify-content:center">
    <div style="text-align:center;max-width:980px;padding:0 40px">
      <div style="width:120px;height:120px;margin:0 auto 28px">${logo}</div>
      <div style="font-size:64px;font-weight:700;letter-spacing:-1px">${title}</div>
      <div style="font-size:30px;color:#ffd21e;margin-top:14px;font-weight:600">${subtitle}</div>
      <div style="font-size:22px;color:#9ca3af;margin-top:34px;line-height:1.6">${lines.join('<br>')}</div>
    </div></body></html>`)
  await wait(ms)
}
const terminal = async (text, ms) => {
  await page.setContent(`<!doctype html><html><body style="margin:0;background:#0b0f19;height:100vh;display:flex;align-items:center;justify-content:center">
    <div style="width:1180px;background:#111827;border:1px solid #1f2937;border-radius:16px;box-shadow:0 30px 80px rgba(0,0,0,.6);overflow:hidden">
      <div style="height:38px;background:#1f2937;display:flex;align-items:center;gap:8px;padding:0 16px"><span style="width:12px;height:12px;border-radius:6px;background:#ff5f57"></span><span style="width:12px;height:12px;border-radius:6px;background:#febc2e"></span><span style="width:12px;height:12px;border-radius:6px;background:#28c840"></span><span style="margin-left:12px;color:#9ca3af;font:14px system-ui">gpu-node-07 — any machine on the network</span></div>
      <pre id="t" style="margin:0;padding:26px 30px;color:#e5e7eb;font:19px/1.55 'IBM Plex Mono',ui-monospace,Menlo,monospace;min-height:420px;white-space:pre-wrap"></pre>
    </div>
    <div id="demo-caption" class="on" style="position:fixed;left:50%;bottom:34px;transform:translateX(-50%);padding:14px 24px;border-radius:14px;background:rgba(17,24,39,.92);color:#fff;font:600 23px/1.35 'IBM Plex Sans',system-ui,sans-serif;border:1px solid #374151"><em style="color:#ffd21e;font-style:normal">HF_ENDPOINT</em> is all a client needs: hf, vLLM, Transformers and git pull from the hub, never from the internet.</div>
  </body></html>`)
  const lines = text.split('\n')
  for (const line of lines) {
    if (line.startsWith('$ ')) {
      await page.evaluate((s) => { document.getElementById('t').textContent += s }, '$ ')
      for (const ch of line.slice(2)) { await page.evaluate((s) => { document.getElementById('t').textContent += s }, ch); await wait(28) }
      await page.evaluate(() => { document.getElementById('t').textContent += '\n' }); await wait(350)
    } else {
      await page.evaluate((s) => { document.getElementById('t').textContent += s + '\n' }, line); await wait(160)
    }
  }
  await wait(ms)
}

// ---- 1. title
mark('title')
await card('HuggingHack', 'Your model hub. Air-gapped.', ['Bring a model across once. Every machine on the network pulls it with', '<span style="color:#e5e7eb">HF_ENDPOINT</span>, vLLM, Transformers, the hf CLI, or git clone.'], 4200)

// ---- 2. sign in
mark('signin')
await page.goto(base + '/'); await wait(900)
await say('Accounts, roles and single sign-on. <em>Sign in with Authentik</em>, or a local account.', 1600)
await type(page.getByRole('textbox', { name: /Username/ }), 'gil')
await type(page.getByRole('textbox', { name: /^Password/ }), password)
await click(page.getByRole('button', { name: /^Sign in$/ }), 1600)

// A previous take's upload would make the wizard refuse the name: remove it first.
await page.evaluate(async () => {
  const csrf = (await (await fetch('/api/auth/status')).json()).csrf_token
  await fetch('/api/repos?repo_id=acme-ai/Qwen3-0.6B-sql', { method: 'DELETE', headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrf }, body: JSON.stringify({ confirmation: 'acme-ai/Qwen3-0.6B-sql' }) })
  await fetch('/api/repos?repo_id=gil/Qwen3-0.6B-sql', { method: 'DELETE', headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrf }, body: JSON.stringify({ confirmation: 'gil/Qwen3-0.6B-sql' }) })
})

// ---- 3. explore
mark('explore')
await say('<em>Explore</em>: your offline library, with search, filters and sorting. No internet needed.', 1000)
const search = page.getByRole('textbox', { name: 'Search models' })
await type(search, 'qwen'); await wait(1400)
await page.keyboard.press('Meta+A'); await page.keyboard.press('Backspace'); await wait(700)
await click(page.getByRole('button', { name: /^Text Generation/ }), 1100)
await click(page.getByRole('button', { name: /^Text Generation/ }), 600)
await moveTo(page.getByRole('combobox', { name: 'Sort models' })); await wait(300)
await page.getByRole('combobox', { name: 'Sort models' }).selectOption('parameters'); await wait(1200)
await page.getByRole('combobox', { name: 'Sort models' }).selectOption('updated'); await wait(500)

// ---- 4. model page + use this model
mark('model')
await click(page.getByRole('button', { name: 'Open Qwen/Qwen3-0.6B model details' }), 1400)
await say('A model page: the card, files, commits, and everything the library read from the weights.', 1800)
await scroll(500, 1200); await scroll(-500, 700)
await click(page.getByRole('button', { name: 'Use this model' }), 1300)
await say('<em>Use this model</em>: copy-paste commands for vLLM, Transformers, hf and git, pointing at this hub.', 2600)
await page.keyboard.press('Escape'); await wait(700)

// ---- 5. model tree: quantizations and fine-tunes
mark('tree')
const tree = page.getByRole('heading', { name: 'Model tree' })
await moveTo(tree); await wait(400)
await say('The <em>model tree</em>: quantizations and fine-tunes link back to the base model, from their model cards.', 2200)
await click(page.getByRole('link', { name: 'RedHat/Qwen3-0.6B-quantized.w4a16' }), 1600)
await say('The W4A16 quantization: same base, a quarter of the memory. Precision and hardware tags come from the files.', 2200)
await moveTo(page.getByRole('heading', { name: 'Model tree' })); await wait(800)

// ---- 6. commits and diff
mark('commits')
await go('#/models/acme-ai/Qwen3-0.6B-support/commits', 1400)
await say('Every upload and change is a <em>commit</em>: who, when, and what changed.', 1600)
await click(page.getByRole('link', { name: 'Add evaluation results' }), 1500)
await say('The diff of the second commit: the evaluation table added to the model card.', 1600)
await scroll(400, 1400)

// ---- 7. deployment configs with results
mark('config')
await go('#/models/Qwen/Qwen3-0.6B/config', 1400)
await say('<em>Deployment configs</em>: the vLLM setup you actually ran, versioned, with the benchmark results.', 2000)
await moveTo(page.getByRole('heading', { name: 'Compare results' })); await wait(300)
await say('<em>Compare results</em>: the best value in each row is highlighted. #2 wins on TTFT; #3 wins on throughput and KV cache.', 3800)
await scroll(300, 900)
await click(page.getByRole('link', { name: /^#3 FP8 KV cache/ }).first(), 1500)
await say('Revision #3: the serve script, and its numbers on an A100.', 1400)
await scroll(400, 1600)

// ---- 8. upload
mark('upload')
await go('#/uploads', 1300)
await say('<em>Upload</em> a model from any browser: name it, choose who sees it and where it lives, drop the folder.', 1600)
await type(page.getByLabel('Model name'), 'Qwen3-0.6B-sql')
const owner = page.getByRole('button', { name: /^Owner/ })
if (await owner.isEnabled()) { await click(owner, 500); const option = page.getByRole('option', { name: /acme-ai/ }); if (await option.count()) await click(option, 500); else await page.keyboard.press('Escape') }
await click(page.getByRole('button', { name: 'Continue' }), 900)
await click(page.locator('label.choice-card', { hasText: 'Public' }), 700)
await moveTo(page.locator('label.choice-card', { hasText: 'NetApp primary' })); await wait(600)
await click(page.getByRole('button', { name: 'Continue' }), 900)
await moveTo(page.getByRole('button', { name: /Drop a model folder/ })); await wait(400)
await page.locator('input[type=file]').setInputFiles(path.join(work, 'upload', 'Qwen3-0.6B-sql')); await wait(1300)
await say('The listing is read from the files before anything is sent: task, precision, parameter count, license.', 1200)
await click(page.getByRole('button', { name: 'Continue' }), 1400)
await scroll(300, 1200)
await click(page.getByRole('button', { name: 'Create and upload' }), 800)
await say('Parts go <em>straight to the bucket</em>, several at a time. The server only signs the links.', 600)
for (let i = 0; i < 60; i++) { await wait(500); if (/committed/i.test(await page.evaluate(() => [...document.querySelectorAll('.toast')].map((e) => e.textContent).join(' ')))) break }
await wait(1600)

// ---- 9. admin
mark('admin')
await go('#/admin/users', 1300)
await say('<em>Administration</em>: accounts and roles. Admin, Member, Viewer; every risky action asks first.', 2200)
await go('#/admin/roles', 1300)
await say('What each role may do, straight from the server.', 1800)
await go('#/admin/organizations', 1200)
await say('<em>Organizations</em> own repositories together, with their own admins, writers and readers.', 1800)
await go('#/admin/storage', 1500)
await say('<em>Storage</em>: local disk and any number of S3 buckets, side by side, with the models in each.', 2200)
await scroll(300, 800)
const row = page.locator('[role=row]', { hasText: 'bartowski/SmolLM2-135M-Instruct-GGUF' }).first()
await click(row.getByRole('button', { name: 'Move' }), 900)
// Whichever bucket it is not in now (a previous take may have moved it already).
await click(page.getByRole('dialog').locator('label.choice-card:not(:has(input:disabled))').first(), 500)
await type(page.getByRole('dialog').getByRole('textbox'), 'bartowski/SmolLM2-135M-Instruct-GGUF')
await click(page.getByRole('dialog').getByRole('button', { name: 'Move model' }), 800)
await say('<em>Moving</em> a model between locations: copy, verify every hash, switch, then remove the old copy. Pulls keep working throughout.', 1000)
for (let i = 0; i < 90; i++) { await wait(500); if (!(await page.locator('[role=status][aria-label^="Moving"]').count())) break }
await wait(1500)

// ---- 10. pull from a client
mark('terminal')
const cleaned = fs.readFileSync(path.join(work, 'terminal.txt'), 'utf8').replace(/\x1b\[[0-9;]*m/g, '').split('\n')
  .filter((l) => l && !/^Hint:/.test(l) && !/^Fetching/.test(l)).map((l) => l.replace(/\S*hh-demo\/dl\S*/, './support')).join('\n')
await terminal(cleaned, 2600)

// ---- 11. dark mode and outro
mark('outro')
// The terminal card kept the app's URL, so a hash-only change would not reload the app.
await page.goto('about:blank'); await page.goto(base + '/#/models')
const toggle = page.getByRole('button', { name: 'Switch to dark theme' })
await toggle.waitFor({ timeout: 20000 }).catch(() => null)
await page.getByRole('button', { name: 'Open Qwen/Qwen3-0.6B model details' }).waitFor({ timeout: 20000 }).catch(() => null)
await wait(800)
if (await toggle.count()) await click(toggle, 1700)
await say('Light and dark, phone and desktop. Ready for one server, or many behind a route.', 2400)
await card('HuggingHack', 'Self-hosted. Air-gapped. Yours.', ['Hub protocol · git + LFS · S3 buckets · PostgreSQL · SSO · Helm', 'github.com/GilGedje/HuggingHack'], 4500)
mark('end')

await context.close()
const recorded = await page.video()?.path().catch(() => null)
await browser.close()
const produced = recorded || fs.readdirSync(path.dirname(output)).filter((f) => f.endsWith('.webm')).map((f) => path.join(path.dirname(output), f)).sort((a, b) => fs.statSync(b).mtimeMs - fs.statSync(a).mtimeMs)[0]
fs.renameSync(produced, output)
fs.writeFileSync(output.replace(/\.webm$/, '.marks.json'), JSON.stringify(marks, null, 1))
console.log(JSON.stringify({ output, seconds: marks.at(-1).at, marks }))
