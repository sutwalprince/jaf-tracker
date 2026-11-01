# JAF Tracker: Chrome extension

Your placement dashboard in Chrome, and the bridge that hands your login to your JAF Tracker server.
The server does all the messaging (WhatsApp, Telegram, reminders); see the [main README](../README.md).

## What it does

- One page (`dashboard.html`, click the extension icon) with three tabs:
  - **My JAFs** (`#jafs`): the JAFs you've signed, with CTC for your program, location, CPI cutoff,
    deadlines, selection rounds with durations and the next test/PPT. Hide companies you're done with.
  - **Updates** (`#updates`, default): every placement-blog post about those companies, grouped by day
    and labelled by type; upcoming tests and PPTs; a filter for unread posts.
  - **Open JAFs** (`#open`): every JAF open right now, with the portal's own eligibility flags, bonus
    allowed or not, CTC, countdown, requirements and job description, and a link to the portal's Jobs page.
  `jafs.html` only redirects to `dashboard.html#open`; its code (`jafs.js`) is a module the dashboard imports.
- The logo's source is `icons/logo.svg`; the PNGs are rendered from it.
- **Send login to server**: whenever your login changes, sends your portal token and your cookies for
  `campus.placements.iitb.ac.in` and `sso.iitb.ac.in` (nothing else, never your password) to your
  server over Tailscale, protected by a secret key.
- Read/unread and hidden companies are shared with the website and your other devices.
- Syncs on its own every 15–30 minutes, shows the unread count on its icon, and tells you when you
  need to log in.

## Install

1. Open `chrome://extensions` and turn on **Developer mode** (top right).
2. Click **Load unpacked** and pick this `jaf-tracker` folder.
3. Log in to the placement portal as usual. The extension picks up your login by itself.

## Connect it to your server

Needs Tailscale on this laptop (same account as the server), so the server's private address opens.

1. On the dashboard, open **Send login to server**.
2. **Server address**: your site's Tailscale address, e.g. `https://jaf-tracker.<your-tailnet>.ts.net`.
3. **Server key**: the contents of `~/jaf-sync/key` on the server. Copy it to the laptop with
   `scp <server>:jaf-sync/key ~/.config/jaf-tracker/server-key`, then paste it from your own terminal.
4. Tick **Send my login to the server** and click **Send now**.

## Read-only

Every request to the portal is a GET, through one helper (`portalGet` in `background.js`).
`content.js` only reads your login token from the portal page. Nothing ever signs, unsigns, applies,
uploads or edits anything.

## Updating

After changing files here, click reload ↻ on the extension in `chrome://extensions`, and bump
`version` in `manifest.json` (Chrome caches the background code by version). Run `../server/deploy.sh`
to update the website too, since it reuses these pages.
