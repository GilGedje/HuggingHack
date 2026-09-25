# HuggingHack frontend

HuggingHack is a self-hosted, Hugging Face–style model hub for **air-gapped networks**. This folder is its web UI: a React 18 + Vite + TypeScript single-page app using `HashRouter`. The FastAPI backend serves the built `dist/` (see the root CLAUDE.md for the whole project and `backend/CLAUDE.md` for the API). The UI must work with **no internet at runtime**, and whoever maintains it may be offline too. Everything it needs ships in the build.

Paths below are relative to `frontend/` unless marked *(repo root)*.

## Commands

| Command | What it does |
|---|---|
| `npm ci` | Installs exactly what `package-lock.json` pins. Needs Node ≥ 22 (`engines`); the lockfile is kept with npm 10.9.8. Run it on a machine that can reach a registry or mirror. |
| `npm run dev` | Vite dev server. `vite.config.ts` proxies `/api` to `http://127.0.0.1:7860` with `changeOrigin` and `xfwd: true`. `xfwd` sends `X-Forwarded-Host`, which the backend's `same_site_origin` (`backend/app/main.py`) accepts, so writes from the dev origin pass the cross-site write check. |
| `npm run build` | Runs `tsc -b && vite build`. This is the real type check, because `tsc -b` follows the references in `tsconfig.json`. The output goes to `dist/`, which is gitignored. Expect a ">500 kB chunk" warning; it is normal. |
| `npx tsc -p tsconfig.app.json` | Type-checks `src/` without building (`noEmit` is set in that config). |
| `npm test` | Runs `node --experimental-strip-types --test`, which picks up `test/*.test.mjs`. |

- **`npx tsc --noEmit -p .` checks nothing.** `tsconfig.json` has `"files": []` and only `references`. Without `-b` there are zero input files (`--listFilesOnly` prints nothing) and it exits 0. Use `-b` or `-p tsconfig.app.json` instead.
- **How tests load TypeScript.** Tests import sources directly, e.g. `import { errorDetail } from '../src/api.ts'`, and Node strips the types. There is no bundler and no DOM, so a module under test must be:
  - a `.ts` file (not `.tsx`)
  - written in erasable syntax only: no `enum`, `namespace`, or parameter properties
  - free of DOM access at import time
  - importing other `src` files as `import type` only, because extensionless runtime imports like `'./motion'` fail in Node. `appTheme.ts` is untestable for this reason.

  Put logic in such plain modules (`roles.ts`, `visibility.ts`, `catalog.ts`, `uploadPlan.ts`…) and keep components thin. The flag needs Node 22.6 or newer.
- **How the backend serves the build.** `backend/app/main.py` mounts `StaticFiles(html=True)` at `/`. It uses the first of `backend/static/` (the Docker image copies `dist` there) or `frontend/dist/` that contains `index.html`. It picks the folder once, at import. If `dist/` did not exist when uvicorn started, restart the backend after building. HTML is sent with `Cache-Control: no-cache`, and asset names are content-hashed.
- There is no ESLint or Prettier installed. The `eslint-disable` comments in the code do nothing. Tailwind is present only for its preflight reset (`@tailwind base`); no utility classes are used, so don't start using them.

## Structure

`main.tsx` imports the bundled fonts and `styles.css`, applies the stored theme before first paint, and renders `App`.

**Top-level `src/`**
- `App.tsx`: auth gate (setup, sign-in, error, loading); `Application` (providers, toast, routes); `ModelsPage` (Explore catalog).
- `api.ts`: the `request<T>()` helper and the `api` object, one method per endpoint. Details:
  - `request()` sets JSON `Content-Type` for string bodies and adds `X-CSRF-Token` on non-GET/HEAD requests. The token comes from `authStatus`, `login` and `setup` via `applyAuth`.
  - On 401 it dispatches `hugginghack:unauthorized`.
  - It throws `Error(errorDetail(...))`. `errorDetail` turns a 422 `[{loc,msg}]` list into one sentence. A response with no `detail` (a proxy's HTML page, a crash) gets `statusMessage(status)`, and a network failure becomes `UNREACHABLE_MESSAGE`, never the browser's "Failed to fetch".
  - `uploadResumable` does chunked PUTs with `Upload-Offset`/`Upload-Length`.
- `types.ts`: every API payload type. Keep it in step with the backend's responses.
- `access.tsx`: `AccessProvider` and `useAccess()`, which give `user` and `can(capability)`. Capabilities come from the server. `ADMIN_CAPABILITIES` is also here.
- `theme.ts`: pure theme helpers: `resolveTheme`, `applyTheme` (sets `<html data-theme>` and `meta[name=theme-color]`), `THEME_STORAGE_KEY`, `THEME_EVENT`, `announceThemePreference`.
- `appTheme.ts`: the `useAppTheme` hook. The account preference wins, otherwise the stored choice; "system" follows the device live, and changes cross-fade.
- `motion.ts`: the motion system. Contents:
  - exit timings: `TOAST_EXIT_MS`, `DOCK_EXIT_MS`, and the internal `DIALOG_EXIT_MS` and `HIGHLIGHT_GLIDE_MS`
  - `prefersReducedMotion`
  - hooks: `useFadeOnChange`, `useStepDirection`, `useClosingTransition`, `useTabIndicator`, `useSlidingHighlight`, `useStickySidebar`
  - screen and theme cross-fades: `crossfade`, `crossfadeTheme`
- `focus.ts`: `focusAfterRemoval`, for keyboard focus when a list row disappears.
- `catalog.ts`: task and precision lists, parameter-size stops, and the Explore filters in the URL (`CATALOG_FILTER_KEYS`, `readCatalogFilters`, `writeCatalogFilters`).
- `uploads.tsx`: `UploadProvider` and `useUploads()`. It holds a global queue that runs one job at a time on any page, plus the bottom upload dock. The dock sets `--upload-dock-space`.
- `uploadPlan.ts`: pre-upload checks: `isSkipped`, `isRecorded`, `uploadCommitMessage`, `planUpload`, `detectPrecision`.
- `uploadStore.ts`: cross-tab leases for unfinished uploads in `localStorage`: `LEASE_HEARTBEAT_MS`, `LEASE_TTL_MS`, `claimOrphans`, `parseTabRecord`.
- `modelCard.ts`: model-card markdown prep (front matter, math), URL resolvers, `modelCardSanitizeSchema`.
- `markdownAlerts.ts`: rehype plugin for `> [!NOTE]` callouts. `markdownText.ts`: `markdownSummary`, and `applyFormat` for the editor toolbar.
- `roles.ts`: `ROLE_LABELS`, `roleConfirmation`, `disableConfirmation`, and for organizations `ORG_ROLE_LABELS`, `orgRoleConfirmation`, `effectiveOrgRole` (Viewers only read) and `actingOrgAdmins` (the server's last-admin count). `visibility.ts`: `VISIBILITIES`, `visibilityLabel`, `visibilityAudience`, `visibilityAllowed`, `visibilityConfirmation`.
- `ssoError.ts`: `ssoErrorMessage` maps `sso_error` codes to fixed sentences.
- `useModel.ts`: "Use this model" snippets (vLLM pip/docker, git clone, hf CLI), `resolveServerUrl`.
- `gguf.ts`: GGUF header inspection. It lazy-imports `@huggingface/gguf` and fetches byte ranges only through `/api/library/gguf-range`.
- `listingFields.ts`, `listingPreview.ts`: listing overrides, and what an upload sends so the server can preview its listing. The ceilings mirror `backend/app/listing.py`.
- `configCompare.ts`: compares config-revision results. `storageMoves.ts`: storage-move progress. `modelTree.ts`: lineage wording. `pagination.ts`: `pageList`. `dropFiles.ts`: folder drag-and-drop. `avatarImage.ts`: square 256 px WebP avatars. `utils.ts`: `formatBytes`, `relativeTime`, `describeDevice`, `avatarUrl`…

**`src/components/`**
- `Shell.tsx`: top bar, primary nav (sliding underline), phone menu, theme toggle, account chip.
- `Dialog.tsx`: `DialogFrame`, the backdrop and panel for every dialog. `ConfirmDialog.tsx`: `ConfirmProvider` and `useConfirm()`.
- `Skeletons.tsx`: `ModelCardSkeletons`, `RowSkeletons`, `ModelPageSkeleton`, `StorageSkeleton`. `LoadError.tsx`: the `.page-error` with Retry for a failed load.
- `Markdown.tsx`: `MarkdownText` (user-written markdown) and `MarkdownEditor` (write/preview tabs with a toolbar). `MarkdownParts.tsx`: `MarkdownImage` and `DropWhenImagesFail` (broken images vanish), `MarkdownParagraph` (alert icons).
- `ModelDetails.tsx`: `ModelCardDocument` (README renderer) and `ModelActions` (cache and runtime dispatch).
- `AccountPages.tsx`: `AuthScreen` (setup, sign-in, SSO) and `SavedPage` (saved models and collections).
- `AdminUserDetail.tsx`: one account in admin. `RepositorySettings.tsx`: rename, transfer, visibility, description, delete. `MoveModelDialog.tsx`: move to another storage location.
- `UploadWizard.tsx`: new-repository upload steps. `UploadChangeDialog.tsx`: add, replace or delete files in an existing repo. `NamespacePicker.tsx`: choose the owner.
- `ConfigSection.tsx`: the Config tab. `GgufInspector.tsx`: the GGUF tab. `ModelTree.tsx`: lineage card. `FileDiff.tsx`: unified diff. `ListingEditor.tsx`: listing overrides.
- `ModelFilters.tsx`: Explore filter UI (`EMPTY_FILTERS`, `applyFilters`). `RepositoryRows.tsx`: `LibraryModelRow` cards. `ListPager.tsx`: `useListQuery` (list state in the URL) and `ListPager`.
- `UseModel.tsx`: `UseModelDialog` and `CopyButton`. `DownloadLink.tsx`: a link that acknowledges the click. `Avatar.tsx`: `Avatar`, `AvatarEditor`. `ChoiceCard.tsx`: a radio drawn as a card. `PasswordForm.tsx`.

**`src/pages/`**
- `ModelPage.tsx`: `/models/:owner/:name/*`, with sections for the card, `tree`, `commits`, `commit/:id`, `config`, `gguf` and `settings`.
- `AdminPage.tsx`: `/admin/:tab` for users, organizations, roles, storage, runtimes and server, plus `/admin/users/:userId`. It embeds `StoragePage.tsx` and `RuntimesPage.tsx`.
- `AccountPage.tsx`: `/account/:tab` for profile, security, tokens and preferences. `OrganizationPage.tsx`: `/orgs` (`OrganizationsIndex`) and `/orgs/:name/:tab`. `UploadsPage.tsx`: `/uploads`, only when `can('repos.create')`.

**Routes** live in `Application` in `App.tsx`. Legacy paths (`/local`, `/storage`, `/runtimes`, `/settings`, `/downloads`) redirect, and `*` goes to `/models`. Keep the **HashRouter**: the backend has no SPA fallback, and plain paths like `/owner/name` belong to git and `hf` clients.

## Design system (`src/styles.css`, one file)

- **Tokens.** `:root` defines colors (`--canvas`, `--canvas-subtle`, `--canvas-raised`, `--border`, `--border-strong`, `--text`, `--text-secondary`, `--text-muted`, `--brand-primary`, `--brand-warm`, `--success`/`-bg`, `--danger`/`-bg`, `--warning`/`-bg`), `--shadow`, and easing (`--ease-out`, `--ease-exit`). Use tokens, never hex. Mixes use `color-mix(in oklch, …)`.
- **Dark mode** is a single selector, `[data-theme='dark']`, which redefines the same tokens and sets `color-scheme: dark`. There is **no `prefers-color-scheme` in CSS**. The "system" preference is resolved in JS (`darkQuery`/`useAppTheme`), and `applyTheme` sets the attribute on `<html>`. Any new color needs a dark value, via tokens or a `[data-theme='dark'] .x` override.
- **Typography.** Fonts are bundled through `@fontsource`, and `main.tsx` imports them. Families:
  - IBM Plex Sans (400/500/600/700) for body, 15px
  - IBM Plex Mono (400/600) for code, `.eyebrow` and badges
  - Bricolage Grotesque Variable for headings and brand
  - KaTeX fonts come with `katex.min.css`

  **Never** use Google Fonts or a CDN. A new weight needs its `@fontsource/.../<weight>.css` import; otherwise the browser fakes it.
- **Spacing.** There is no spacing scale. Match the neighbouring rules (px values, `gap`-driven flex and grid). Add new CSS near related rules, or at the end in a commented block.
- **Buttons.**
  - `.download-button` is the **primary** (yellow) button.
  - `.secondary-button`, and `.danger-button` for destructive actions.
  - `.icon-button` needs an `aria-label`.
  - Text buttons: `.text-link`, `.quiet-link`, `.danger-text`.
  - Modifiers: `.compact`, `.wide`.
  - Pressed buttons scale to 0.97, and text buttons dim instead. Both only happen for classes listed in the `:active` selector lists near "transition-property", so add a new button class there.
- **Pills and tags.** `.status-pill` is green by default, with `.danger` and `.pending` variants. Also `.local-badge`, `.repo-tags span`, `.task-tag`.
- **Errors.** `.page-error` (icon, strong, p, and a Retry button) and `.inline-error`. `.empty-state` is for empty lists.
- **Tables.** Rows are CSS grids. At `max-width: 640px`, `.admin-user-row`, `.file-browser-row` and `.storage-model-row` stack: the header row hides, and each cell with a `data-label` names itself through `::before`. A new table needs `data-label` on its cells **and** a rule in that 640px block.
- **Tab strips.** Tabs are direct children of one element that has `ref={useTabIndicator(key)}`, with `.active` on the current tab. One underline glides between them (`data-indicator`). `key` must change whenever the active tab *or the set of tabs* changes. The CSS covers `.model-tabs > div:first-child`, `.drawer-tabs`, `.primary-nav` and `.markdown-editor-tabs`. The edge fade (`data-overflow` plus `mask-image`) exists only for the first two. Reuse `.model-tabs` rather than inventing a new strip.
- **`useSlidingHighlight`.** One highlight box follows `.active` in a list (the `data-highlight` attribute and `--highlight-*` variables). Only `.collection-sidebar` (Saved) is styled for it.
- **`useFadeOnChange(key)`** gives an opacity-only fade when `key` changes. Don't add a transform: it would become the containing block for fixed-position dialogs. `App` fades once per page; tabbed pages fade their own body.
- **`useClosingTransition(onClose)`** returns `{closing, close}`. `close()` plays the exit, then calls `onClose`.
- **`crossfade`** swaps whole screens with the View Transitions API (sign-out). `crossfadeTheme` switches light and dark.
- **Exit durations in `motion.ts` must match `styles.css`.** Dialog 160 ms (`dialog-out`), toast 180 (`toast-out`), dock 180 (`.upload-dock.leaving`), highlight glide 380.
- **Reduced motion means fades, not pops.** The global `prefers-reduced-motion` block cuts animations and transitions to 0.01 ms. Dialogs, `.namespace-menu`, `.toast` and `.upload-dock` then **cross-fade** (160 ms in, 140 ms out), so exits still wait for their timers. Spinners keep turning. The hooks check `prefersReducedMotion()` and jump instead of gliding. Anything new that slides or scales must get a fade alternative in that block.
- **Focus.**
  - `DialogFrame` traps Tab inside the panel and closes on Escape or a backdrop click. When a confirm is stacked on top, only the topmost dialog handles keys. On unmount it returns focus to the opener.
  - `DialogFrame` does **not** set initial focus, so each dialog focuses its first field (or Cancel) itself.
  - When a row is removed, call `const refocus = focusAfterRemoval(trigger)` *before* confirming or removing, then `refocus()` after success.
  - The focus ring is a 3px outline in `--brand-primary` on `:focus-visible`.
- **Touch.**
  - Under `@media (pointer: coarse)`, small text buttons get padding and a negative margin for a finger-sized target without shifting layout. Extend that rule for new small text buttons.
  - Controls revealed on hover must also be shown under `@media (hover: none)`, as `.collection-delete` is.
  - Below 520px, dialog footer buttons stack full width with `min-height: 44px`, the confirm button on top.
  - `main.tsx` adds a `touchstart` listener so iOS applies `:active`.
- **Phones.** Breakpoints are 1240, 1080, 900, 780 (phone layout and menu), 640, 520 and 480. `body` has `min-width: 320px`. Test at **390px**: no horizontal page scroll. Wide content wraps (`overflow-wrap: anywhere`) or scrolls inside its own box.

## Patterns to follow

- **Confirm anything risky** with `const confirm = useConfirm(); if (!(await confirm({...}))) return`. That covers visibility changes, role changes, disabling accounts, removing members, signing out sessions or tokens, and deletes.
  - Title: a question ("Delete X?"). `confirmLabel`: names the action. `message`: what happens and whether it can be undone. `eyebrow`: optional.
  - Pass `danger: true` for destructive actions or anything that grants or removes admin rights or makes a repo public.
  - Pass `requireText: <name>` for what cannot be undone (see `AdminPage` delete user/org and `UploadsPage` delete repo).
  - Reuse `roleConfirmation`, `disableConfirmation` and `visibilityConfirmation` instead of rewording.
  - Only ask for the risky direction; enabling an account doesn't ask.
- **Snap back on Cancel.** A controlled input shows the new value while the question is open and goes back to the old one if declined or failed. See `changeVisibility` in `RepositorySettings.tsx` (`previous`) and `pendingVisibility` in `UploadsPage.tsx`. A select bound to server data (`user.role`) snaps back simply by not updating it.
- **Stale-response guards** on every fetch whose inputs can change:
  - in effects, `let ignore = false … return () => { ignore = true }`
  - for re-callable loaders, a counter: `const request = ++latestRequest.current; … if (request !== latestRequest.current) return` (`ModelsPage`, `ModelPage`, `ConfigSection`)
  - or store the result with its key, as `ModelPage` does with `loaded.repoId === repoId`
- **Loading and errors.**
  - Show a skeleton from `Skeletons.tsx` that matches the layout. It fades in after 120 ms (`.skeleton-reveal`).
  - A failed load shows `.page-error` with the message and a **Retry** button (`LoadError` from `components/LoadError.tsx`). Never swallow the error or show an empty list in its place.
  - Form and dialog errors go in `.inline-error`, or `.form-message` in account forms, next to what failed.
- **Toasts.**
  - `onToast(message, 'success' | 'error')` is passed down as a **prop** from `Application`; there is no toast context.
  - Success toasts are full past-tense sentences that end with a period and name the thing: `` `${repoId} was restored to the local cache.` ``, `'Description saved.'`. Names in running text use curly quotes “ ”.
  - Error toasts use the server message, falling back to `'Could not <do thing>.'`. The older `'Unable to …'` form without a period is legacy.
  - The `ToastHandler` type and the `errorMessage`/`message(reason, fallback)` helpers are declared separately in each file; copy the local one.
- **Filters in the URL.**
  - Explore filters use `readCatalogFilters`/`writeCatalogFilters` with `setSearchParams(..., { replace: true })`, so dragging a slider doesn't add history entries.
  - Admin lists use `useListQuery` (`q`, `page`, `per_page`, plus filters; debounced search). The page size is remembered per browser.
  - The address is the source of truth.
- **Uploads.**
  - `UploadWizard` and `UploadChangeDialog` call `useUploads().enqueue(...)`, and `uploads.tsx` does the rest.
  - Each file goes through `uploadResumable`, then `finalizeUpload` (new repo) or `startChange` → `uploadChangeFile` → `commitChange` / `abortChange` (change sessions).
  - On success it dispatches `hugginghack:repository-changed`, which pages listen for to refresh.
  - Each tab saves unfinished jobs under its own `hugginghack-uploads:<tab id>` key and heartbeats every 20 s. Another tab adopts them only once the lease (120 s) has lapsed (`uploadStore.ts`). `File` objects can't be stored, so restored jobs come back without their files.
  - **Keep these mirrors of backend rules in sync.** `isSkipped` mirrors `RESERVED_PARTS`, `RESERVED_FILENAMES` and `PART_SUFFIX` in `backend/app/uploads.py`. `isRecorded` mirrors `indexer.hidden_path` and `PART_SUFFIXES` (`.hugginghack-part`, `.hugginghack-s3-part`). Tests: `test/uploadPlan.test.mjs`.
- **Markdown.** There are two pipelines, and neither may use `dangerouslySetInnerHTML`.
  - **User-written text** (organization About, via `MarkdownText`) runs `remark-gfm` → `rehype-sanitize` (default schema) → alerts. There is **no `rehype-raw`**, so raw HTML tags are stripped (`<b>x</b>` renders `x`) and never rendered. Keep it that way.
  - **Model cards** (`ModelCardDocument`) run `prepareModelCardMarkdown` → `remark-gfm` and `remark-math` (no single `$`) → `rehype-raw` → `rehype-sanitize` with `modelCardSanitizeSchema` → `rehypeGithubAlerts` → `rehype-katex`. Sanitize must come right after `rehype-raw`. The schema strips `srcSet` (it bypasses the URL check) and only adds `align`, `width`, `height` and `open`.
  - `urlTransform` uses `resolveLocalModelCardUrl`:
    - relative images load through `/api/library/asset`
    - **external images are dropped**
    - relative links are removed
    - only http(s) and mailto links stay
  - `MarkdownImage` removes images that fail to load, and `DropWhenImagesFail` removes links left empty as a result.
- **SSO errors.** The callback lands on `#/?sso_error=<code>`. `ssoErrorMessage` maps the code to a fixed sentence, falling back to a generic one, and the parameter is removed from the address. Never display the raw value: anyone can write it into a link.
- **Access.** Hide UI with `can('capability')` from `useAccess()`. The server enforces the same table, so hiding is only cosmetic.

## Security and CSP

The backend sends this CSP on every UI response (`CONTENT_SECURITY_POLICY` in `backend/app/main.py`):

```
default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:;
font-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'self'; form-action 'self'; frame-ancestors 'none'
```

Model-card assets (`/api/library/asset`) and avatars get the stricter `default-src 'none'; sandbox` instead.

- No inline `<script>`, no `eval` or `new Function`, no `javascript:` URLs, no workers or scripts from a CDN. The built `dist/index.html` must contain only the hashed `/assets/*.js` module.
- No external URLs of any kind: fonts, images, fetches, iframes. `connect-src 'self'` blocks them anyway. External *links* (`<a target="_blank" rel="noreferrer noopener">`) are fine.
- Inline `style` attributes and CSS custom properties are allowed (`'unsafe-inline'`); the motion hooks depend on this.
- Never use `dangerouslySetInnerHTML`. Render text through React, which escapes it. Don't turn free text from the address into a message; map it to fixed strings as `ssoError.ts` does.
- All API calls go through `request()` or an `api.*` method, so CSRF, `credentials: 'same-origin'` and error handling stay uniform. A raw `fetch` for a write must set `X-CSRF-Token` itself, as `uploadResumable` does.
- **Vite dev sends no CSP.** A CSP violation only shows on the build served by the backend on :7860.

## Air-gapped constraints

- The runtime makes no network calls beyond its own origin. `@huggingface/gguf` is lazy-loaded and fetched through the backend's range proxy. Snippet text may *mention* URLs, but the page never loads them.
- Dependencies are pinned exact in `package.json` and locked in `package-lock.json`. They are installed with `npm ci` on a connected build machine, or from an internal mirror (`docs/AIRGAPPED.md`, *repo root*), and then shipped as the Docker image or `dist/`.
- **Don't add dependencies casually.** Every one has to be carried across the air gap and audited. Prefer a small local helper. If you must add one:
  - pin the exact version
  - commit the lockfile
  - make sure it needs no network at runtime (no CDN assets, no telemetry)
  - make sure it works under the CSP (no `eval`)
- Plain-HTTP LAN servers are not a secure context, so `crypto.randomUUID` and `navigator.clipboard` may be missing. Follow the existing fallbacks: `TAB_ID` in `uploads.tsx`, and `CopyButton` selecting the text.
- `localStorage` can throw (private windows). Wrap every read and write in `try/catch`, and use it only for conveniences.

## How to add things

**A page**
1. Create `src/pages/FooPage.tsx` that exports `function FooPage({ onToast }: { onToast: ToastHandler })`. Declare `ToastHandler` locally like the other pages.
2. Add a `<Route>` in `Application` (`App.tsx`), gated with `can(...)` where needed, as `/uploads` is. For a nav entry, add it to `links` in `Shell.tsx` with its capability.
3. Layout: `section-page` › `section-hero` › `section-hero-inner` (`.eyebrow`, `h1`, and a `nav.model-tabs` strip for tabs) › `section-body`. For tabs, copy `AdminPage`/`AccountPage`: a `TABS` array, `useTabIndicator` on the strip's inner `div`, `useFadeOnChange(tab)` on the body, and `<Navigate replace>` for an unknown tab.
4. Load with a stale guard, and show skeletons, `.page-error` with Retry, and an empty state. Check 390px and dark mode.

**A dialog**
1. Copy `NewOrganizationDialog` in `AdminPage.tsx`: `const { closing, close } = useClosingTransition(onClose)`, then `<DialogFrame labelledBy="<id>" className="add-user-dialog" closing={closing} onDismiss={close} onSubmit={submit}>`.
2. Inside: `header.use-model-header` (with `.eyebrow`, `h2` whose `id` matches `labelledBy`, and a close `.icon-button`), `div.use-model-body`, `.inline-error`, and a `.add-user-footer` with a leading `<span>` hint, Cancel (`.secondary-button`), then the primary button last.
3. Focus the first field in an effect. On success, call `close()`, not `onClose()`, so the exit plays. Render it conditionally from the parent (`{open && <Dialog …/>}`).
4. For a plain yes/no, don't build a dialog: use `useConfirm()`.

**An API call**
1. Add the payload type to `types.ts`.
2. Add a method to `api` in `api.ts`:
   ```ts
   fooBar: (id: string, payload: {...}) =>
     request<Foo>(`/api/foo/${encodeURIComponent(id)}`, { method: 'PATCH', body: JSON.stringify(payload) })
   ```
   Encode every path segment. Use `repoPath` or `URLSearchParams` for `owner/name` IDs.
3. Call it with try/catch, and pass `reason instanceof Error ? reason.message : 'Could not …'` to `onToast(..., 'error')` or to inline error state.
4. If the call changes a repository, dispatch `hugginghack:repository-changed` so open pages refresh.

## Verifying UI changes offline

1. Run `npm test`, then `npm run build`. It must pass `tsc -b` and produce no new warnings besides the chunk size.
2. Run the backend on a **copy** of the data, never the live `data/` and `models/`. From the *repo root*, with the backend requirements installed (`pip install -r backend/requirements-dev.txt`):
   ```sh
   cp -R data data-test          # models-test/: copy only a few small models; both paths are gitignored
   MODEL_STORAGE=$PWD/models-test DATA_DIR=$PWD/data-test ACCOUNTS_ENABLED=false \
     python -m uvicorn app.main:app --app-dir backend --port 7860
   ```
   Make sure `DATABASE_URL`, the `S3_*` settings and runtime targets in your environment don't point at live services. `ACCOUNTS_ENABLED=false` skips sign-in, so to check sign-in, sessions, tokens or roles, leave accounts on and create a throwaway owner on the copy.
3. Open `http://127.0.0.1:7860`. This is the built app with the real CSP, not `npm run dev`. Check:
   - **1280px and 390px** wide, with no horizontal scroll at 390
   - **light and dark**
   - **reduced motion** (OS setting or DevTools emulation): things fade, nothing slides or pops
   - keyboard only: Tab stays in dialogs, and focus returns to the opener
   - the console shows no CSP errors
4. A headless-browser pass at both widths and themes is recommended for anything visual. Any browser automation must run locally, against 127.0.0.1.

## Gotchas

- The backend picks its static folder at startup. If `backend/static/` exists it wins over `frontend/dist/`, and a backend started before `dist/` existed serves no UI until you restart it.
- `useTabIndicator` and `useSlidingHighlight` only measure **direct children** with `.active` (`:scope > .active`). A wrapper element breaks them.
- Page containers must never get a lasting `transform`, `filter` or `will-change: transform`: any of them traps the fixed-position dialogs inside the container.
- `hugginghack:unauthorized` triggers a re-check of the session and the "Your session expired" notice. Don't dispatch it for anything but a 401. When a session expires mid-visit, `App` keeps the signed-in app mounted but hidden (`.app-frame`, keyed by user id) behind the sign-in form, so a running upload keeps its `File` objects; signing back in as the same account shows it again, anyone else gets a fresh app.
- Search inputs debounce by 250 ms (`ModelsPage`, `useListQuery`). Keep that when adding list searches.
- The theme is saved in three places: `localStorage` (`hugginghack-theme`), the account preference (`api.updatePreferences`), and the live `THEME_EVENT`. Use `announceThemePreference` and let `useAppTheme` apply it.
- `index.html` has `meta[name=theme-color]`, which `applyTheme` updates. `public/` holds the two logo SVGs, served from `/`.
