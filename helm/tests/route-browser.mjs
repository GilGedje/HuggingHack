// Browser half of route-test.sh: a real Chrome through the route, as a user would.
//   node route-browser.mjs <phase> <base url> <spki pin> <state file> [upload folder]
// Needs playwright-core (PLAYWRIGHT_CORE: its index.mjs) and Google Chrome installed.
// phase "first": sign in, browse, create an organization, upload a model through the wizard.
// phase "after-restart": with the saved session, after every pod was replaced: still signed
// in, pages load, a write (saving a model) succeeds.
const { chromium } = await import(process.env.PLAYWRIGHT_CORE)
const [phase, base, spki, stateFile, folder] = process.argv.slice(2)
const hub = new URL(base).host
const browser = await chromium.launch({
  channel: 'chrome',
  headless: true,
  // Trust the test CA's server key, as a machine with the root CA installed would.
  args: [`--ignore-certificate-errors-spki-list=${spki}`],
})
const context = await browser.newContext(phase === 'first' ? {} : { storageState: stateFile })
const page = await context.newPage()
const out = { phase, errors: [], csp: [], hubPuts: 0, bucketPuts: 0, apiStatuses: {} }
page.on('pageerror', (e) => out.errors.push(String(e).slice(0, 160)))
page.on('console', (m) => {
  if (/Content Security Policy/.test(m.text())) out.csp.push(m.text().slice(0, 160))
  else if (m.type() === 'error' && !/favicon/.test(m.text())) out.errors.push(m.text().slice(0, 160))
})
page.on('request', (r) => {
  if (r.method() !== 'PUT') return
  if (new URL(r.url()).host === hub) out.hubPuts += 1
  else out.bucketPuts += 1
})
page.on('response', (r) => {
  if (new URL(r.url()).host === hub && r.url().includes('/api/')) {
    const key = String(r.status())
    out.apiStatuses[key] = (out.apiStatuses[key] || 0) + 1
  }
})
const toast = async (pattern, seconds = 120) => {
  for (let i = 0; i < seconds; i++) {
    const text = await page.evaluate(() => [...document.querySelectorAll('.toast')].map((e) => e.textContent.trim()).join(' | '))
    if (pattern.test(text)) return text
    await page.waitForTimeout(1000)
  }
  return 'no toast'
}

if (phase === 'first') {
  await page.goto(`${base}/`)
  await page.getByRole('textbox', { name: /Username/ }).fill('owner')
  await page.getByRole('textbox', { name: /^Password/ }).fill('route-owner-password')
  await page.getByRole('button', { name: /^Sign in$/ }).click()
  await page.waitForTimeout(1500)
  const cookies = await context.cookies()
  const session = cookies.find((c) => /session/i.test(c.name))
  out.sessionCookie = session ? { secure: session.secure, httpOnly: session.httpOnly, sameSite: session.sameSite } : null
  for (const route of ['#/models', '#/orgs', '#/admin/users', '#/admin/storage', '#/account']) {
    await page.goto(`${base}/${route}`)
    await page.waitForTimeout(700)
  }
  // A CSRF-protected write.
  await page.goto(`${base}/#/admin/organizations`)
  await page.waitForTimeout(800)
  await page.getByRole('button', { name: 'New organization' }).first().click()
  await page.getByRole('dialog').getByRole('textbox', { name: 'Name', exact: true }).fill('route-org')
  await page.getByRole('dialog').getByRole('button', { name: 'Create organization' }).click()
  out.createOrg = await toast(/created|could not/i, 15)
  // A direct upload: the parts go to the bucket's route, never through the hub.
  await page.goto(`${base}/#/uploads`)
  await page.waitForTimeout(1000)
  await page.getByLabel('Model name').fill('route-check')
  await page.getByRole('button', { name: 'Continue' }).click()
  await page.waitForTimeout(500)
  await page.locator('label.choice-card', { hasText: 'Public' }).click()
  await page.getByRole('button', { name: 'Continue' }).click()
  await page.waitForTimeout(500)
  await page.locator('input[type=file]').setInputFiles(folder)
  await page.waitForTimeout(1000)
  await page.getByRole('button', { name: 'Continue' }).click()
  await page.waitForTimeout(800)
  await page.getByRole('button', { name: 'Create and upload' }).click()
  out.upload = await toast(/committed|could not|failed/i, 300)
  await context.storageState({ path: stateFile })
} else {
  await page.goto(`${base}/#/models/owner/route-check`)
  await page.waitForTimeout(1500)
  out.stillSignedIn = !(await page.getByRole('button', { name: /^Sign in$/ }).count())
  out.modelHeading = (await page.locator('h1, h2').first().textContent().catch(() => '')).trim().slice(0, 60)
  await page.getByRole('button', { name: 'Save', exact: true }).first().click()
  out.saveModel = await toast(/saved|could not/i, 15)
}
console.log(JSON.stringify(out))
await browser.close()
