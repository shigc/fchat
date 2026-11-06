# Fchat — a WeChat-style Rocket.Chat desktop client

> 中文：[README.md](README.md) ｜ English

A desktop chat client built on **Python 3 + PyQt5 + the official Rocket.Chat
Python API**. The layout and interactions follow the conventions people know
from WeChat-style chat apps: three columns (navigation rail, conversation
list, chat pane), green/white bubbles, round avatars, inline time labels.

## Features

- **Three-pane WeChat layout**: navigation rail (chats / settings) | conversation list | chat pane
- **Login**: modal sign-in dialog with an optional "remember password" toggle
  (stored locally, encrypted — see the configuration section below). With
  "auto-login on start" enabled in Settings, the next launch signs in in the
  background using the stored credentials and falls back to a pre-filled login
  dialog on failure (changed password, no network).
- **End-to-end decryption**: supports Rocket.Chat `rc.v2.aes-sha2` E2E rooms for
  both messages and files (an E2E password can be supplied at login).
- **Conversation list**: channels `#`, private groups and DMs in one list with
  unread badges.
  - Sorting is fixed to "pinned first, then newest message first"; unpinning
    drops a conversation back to its time position.
  - Display names prefer the server-configured localised name (`fname`, then a
    short `topic`); DMs are resolved by `username`. Channels with no localised
    name still show the original slug.
  - The time column always prints Chinese weekday names and does not follow the
    system locale.
- **Chat pane**:
  - Text bubbles (own messages green on the right, others white on the left,
    sender nickname shown in groups).
  - Image messages fetch thumbnails asynchronously (with attachment auth
    headers) and distinguish "loading" from "failed"; clicking opens the full
    image in the system browser. File messages render as `[文件]`.
  - Opening a conversation lands on the newest message; scrolling to the top
    loads older history without jumping the reading position.
  - System messages and gaps over 5 minutes insert time labels automatically.
  - Infinite pagination: older history loads on scroll and is restored after
    switching conversations (no duplicate fetches).
  - Clicking a message avatar opens a profile card (avatar, name, username,
    status, roles, email); clicking anywhere outside closes it.
  - Both context menus use vector icons painted with QPainter that follow the
    light/dark theme.
  - Attachments: pick an image or file to send via `rooms_media` upload +
    `rooms.mediaConfirm` — the same two-step flow as the official client, with
    the message created server-side.
  - Offline OCR: right-click an image message (or an inline image in a mixed
    message) to "recognise text". Runs locally with RapidOCR / PP-OCRv4 — no
    network, no image bytes uploaded. The result window is editable before you
    insert it into the composer; the image viewer has an "extract text" button too.
  - AI assistant (**off by default**, you supply the endpoint): summarise a
    conversation, produce daily/weekly digests, or ask questions about chat
    content. Only chat **text** is sent to the OpenAI-compatible service you
    configure; images, files and attachments never leave the machine. First use
    requires consent, and E2E rooms get an extra warning.
  - Image-hosting upload (**off by default**): optionally rewrites image links
    to inline-ready URLs from your own host. See "Where your data goes".
- **Scrollbars** in the conversation list and message flow appear only when
  needed: thin at rest, thicker and darker on hover.
- **Realtime**: messages are delivered over **DDP** with automatic reconnection
  and exponential backoff. It prefers a direct WebSocket; if a reverse proxy
  blocks the handshake it silently falls back to SockJS long polling (the same
  fallback the web client uses) and remembers that choice.
- **System tray**: closing the window hides it to the tray (minimising still
  keeps it on the taskbar). New messages blink the tray icon when the window is
  not in front; hovering the icon shows a cross-conversation unread preview card
  that closes when you move away, and clicking an entry restores the window and
  highlights that message. The right-click menu opens the app or config, or quits.
- **Avatars** load asynchronously from `/avatar/<username>` and fall back to an
  initial-letter placeholder.

## Stack

| Layer | Choice |
| --- | --- |
| UI | PyQt5 (Qt 5.15) |
| Data / history | `rocketchat-API` (REST) |
| Realtime | small hand-written DDP client (`websocket-client` + SockJS long-polling fallback) |

## Installation

```bash
# Python 3.10 or newer (3.10–3.12 recommended, see note); use a virtualenv
pip install -r requirements.txt
```

> The lower bound comes from the pinned versions themselves: `requests==2.33.1`
> requires Python ≥ 3.10, `keyring`/`websocket-client` require ≥ 3.9. The main
> dependencies also install on 3.13/3.14 (PyQt5 5.15.11 ships a `cp38-abi3`
> wheel, `pyqt5-sip` has `cp313`/`cp314`), but the optional OCR package is
> officially capped at `<3.13`, so on newer Pythons text recognition is simply
> unavailable. CI runs 3.10 / 3.12 only; anything above that is "installs and
> works, but not covered by CI".

## Running

```bash
python main.py
```

## Optional dependency: offline OCR

Image "recognise text" needs `rapidocr-onnxruntime` (models ship inside the
wheel; inference is entirely local):

```bash
pip install -r requirements-ocr.txt
```

Without it, the menu item explains how to install it and everything else keeps
working. It is split into its own file precisely because the package only
supports up to Python 3.12 — putting it in the main requirements would break
installation for 3.13+ users.

## Where your data goes

Apart from the Rocket.Chat server you log into, the program can only contact the
targets below. Everything except the server is **off by default or only active
once you configure an address**, and there is no built-in telemetry or
analytics anywhere in the source.

| Destination | Default | What is sent |
| --- | --- | --- |
| Your Rocket.Chat server | required | credentials, messages, attachments, avatars |
| AI service (your own OpenAI-compatible URL) | off | chat **text** of the selected conversation (last 200 messages by default, capped at 1000 messages / ~30k characters) plus your question; requests carry `Authorization: Bearer <token>`. Images, files and attachments are **not** sent |
| Image host (your own URL) | off | the file bytes being uploaded + `Bearer <token>` |
| Update server (baked in at build time) | on, can be disabled | no credentials; GETs `manifest.json` and the installer, **https only** |
| OCR | — | nothing leaves the machine; image bytes are only decoded locally |

Boundaries worth knowing:

- You fill in the AI and image-host addresses yourself. **First use of the AI
  shows a consent dialog** (re-asked after you change the endpoint), and E2E
  rooms get a second warning that decrypted plaintext will leave the machine.
- Images/avatars pointing at third-party domains are loaded **without** your
  Rocket.Chat session credentials (same-origin rule: `core/url_guard.py`). The
  same applies to attachment downloads that redirect: credentials are dropped as
  soon as a hop leaves the server origin, and hops are capped.
- Conversation caches (message history, drafts, images) are stored **in
  plaintext** in the local cache directory to make offline start-up and
  troubleshooting possible. See [SECURITY.md](SECURITY.md).

## Packaging (optional)

`build_release.py` is a one-shot release script (Cython compile → PyInstaller →
Inno Setup installer). Build settings are read from a `.env` file in the project
root (template: [`.env.example`](.env.example)) and can be overridden by
environment variables of the same name:

| Key | Meaning |
| --- | --- |
| `FCHAT_UPDATE_BASE_URL` | auto-update base URL (empty disables updating) |
| `FCHAT_UPDATE_MANIFEST` | manifest file name (default `manifest.json`) |
| `FCHAT_GCC` | gcc path used by Cython |
| `FCHAT_UPLOAD_DIR` | where finished installers are copied |

```bash
# copy the template and fill in real values
cp .env.example .env
python build_release.py
```

> Requires gcc, Cython and Inno Setup; only needed to ship Windows installers.

## Configuration (config.yaml)

Stored in the platform standard location (created once you sign in from the
login dialog):

- Windows: `%APPDATA%/Fchat/config.yaml`
- Other platforms: `~/.config/fchat/config.yaml`

Passwords and the E2E password are written with **Fernet symmetric encryption**;
the key lives in the **OS credential store (keyring)** — Windows Credential
Manager (DPAPI) / macOS Keychain / Linux Secret Service — and no longer as a
`secret.key` file. The login password is only written when "remember password"
is ticked; `e2e_password` is stored encrypted whenever it has been supplied.
**If keyring is unavailable** (no desktop session, CI, broken environment) the
password is not persisted at all rather than degraded to plaintext, so you sign
in manually each time.

YAML structure (for reference; ciphertext fields are shown as `gAAAA...`).
`save_config` writes nine top-level sections — `server` is only one of them:

```yaml
server:
  server_url: https://chat.example.com
  username: myname
  password: gAAAA...        # empty unless "remember password" is ticked
  remember_password: true
  auto_login: false
  e2e_password: gAAAA...
  status: online
appearance:
  pinned_rooms: []
  shortcuts: {}
  theme: light
  accent_color: '#07C160'
  font_family: Microsoft YaHei
  font_size: 13
  chat_wallpaper: ''
  collapsed_favorites: false
storage:
  storage_dir: ''
upload_service:
  url: ''
  token: gAAAA...
  enabled: false
update:
  auto_check: true
llm:
  url: ''
  model: ''
  token: gAAAA...
  enabled: false
  consent: false
reminders: []
notify:
  enabled: true
  mentions_only: false
send:
  mode: enter
```

| Key | Meaning |
| --- | --- |
| `server.server_url` | Rocket.Chat server, e.g. `https://chat.example.com` or `http://ip:3000` |
| `server.username` | login username (not the e-mail address) |
| `server.password` | login password; Fernet ciphertext when remembered, empty otherwise |
| `server.remember_password` | `true`/`false`; prerequisite for auto-login |
| `server.auto_login` | `true`/`false`; sign in at start with stored credentials, fall back to the dialog on failure. Toggled in Settings → General |
| `server.e2e_password` | E2E decryption password; enables reading encrypted rooms |
| `server.status` | last selected presence |
| `appearance.*` | theme / accent / font / chat background / pinned list / shortcuts, maintained by Settings and context menus |
| `storage.storage_dir` | cache and log root; empty means the platform standard directory |
| `upload_service.*` | image host URL, Bearer token (ciphertext) and enable flag |
| `update.auto_check` | check for a newer version at start |
| `llm.*` | AI endpoint / model / token (ciphertext) / enable flag / consent flag |
| `reminders` | scheduled reminders |
| `notify.*` | tray notification switch and "mentions and DMs only" |
| `send.mode` | `enter` or `ctrlenter` |

> Migration: on first load, a legacy plaintext `config.ini` in the project root
> is imported into the encrypted YAML and never read again (the original file is
> not deleted). Configuration is written automatically after login; both http
> and https server addresses are accepted.

## Usage

1. Start the app; the login dialog asks for server URL, username and password.
   - Only with "remember password" ticked is the password persisted encrypted —
     which is also the prerequisite for auto-login.
   - After signing in, enable "auto-login on start" in Settings → General to go
     straight to the main window next time.
2. The main window shows the conversation list on the left; pick one to chat.
   - A conversation opens at the newest message; scrolling up loads older history.
   - Right-click a conversation to pin/unpin or leave; pinned ones always sort first.
   - Click a message avatar to see that user; click outside the card to dismiss.
   - Right-click a message to copy, quote, or recall it; attachments add a save entry.
3. Type in the composer and press Enter to send; the "+" button at the right
   attaches an image or file.
4. Realtime messages arrive automatically and the conversation list reorders as
   new messages land.

## Verification

Unit tests cover config encryption/migration and the partial savers not
overwriting each other, conversation sorting and pin fallback, message-flow
scroll anchoring, repaints when async images arrive, delegate geometry and
avatar hit-testing, vector-icon legibility and theme following, the E2E
decryption chain, and DDP transport (SockJS framing, handshake fallback, batched
outbound frames and reconnect-on-send-failure, subscription dialect switching,
duplicate-push deduplication). They run offline — no real server needed:

```bash
python -m unittest discover -s tests -v
```

## Layout

`core/` is the pure-logic layer (by convention it never imports Qt, so it is
directly testable); `ui/` is the PyQt5 layer.

```
fchat/
├── main.py                  # entry point
├── config.py                # config I/O (YAML + Fernet + keyring + ini migration)
├── core/
│   ├── models.py            # Conversation / Message / User
│   ├── api_client.py        # REST layer (rocketchat_API wrapper + E2E + image host)
│   ├── realtime.py          # DDP client (WebSocket and SockJS long-polling transports)
│   ├── e2e.py               # end-to-end decryption (rc.v2.aes-sha2)
│   ├── messages.py          # unified message parsing for REST and realtime
│   ├── markdown.py          # lightweight Markdown → Qt rich text
│   ├── url_guard.py         # URL guard for outbound credentials and OS-opened links
│   ├── ai_client.py         # OpenAI-compatible client (SSE streaming + cancellation)
│   ├── ai_context.py        # context collection and prompt building (count/window/char caps)
│   ├── ocr.py               # offline OCR (RapidOCR / PP-OCRv4, local only)
│   ├── updater.py           # auto-update (manifest / download / sha256; must not import Qt)
│   ├── version.py           # single source of the app version
│   ├── paths.py             # single source of data directories (cache / logs)
│   ├── log_setup.py         # logging setup (rotation + crash capture)
│   ├── ssl_prewarm.py       # SSL pre-warm and REST concurrency limit
│   ├── msg_cache.py         # message history snapshots
│   ├── conv_cache.py        # conversation list snapshots
│   ├── status_cache.py      # presence cache
│   ├── read_state.py        # per-conversation local read marker
│   ├── drafts.py            # unsent drafts
│   └── stickers.py          # custom sticker packs
├── ui/
│   ├── main_window.py       # main window (three panes + login/auto-login + realtime + pagination)
│   ├── sidebar.py           # leftmost icon rail
│   ├── conversation_list.py # conversation list (pins + newest-first)
│   ├── chat_view.py         # message flow (scroll anchoring / hit testing / context menu)
│   ├── message_model.py     # message model and delegate (virtualised drawing)
│   ├── chat_input.py        # composer / attach button / quote preview
│   ├── chat_background.py   # chat background presets (flat/gradient/texture, light+dark)
│   ├── window_chrome.py     # custom min/max/close buttons
│   ├── widgets.py           # shared widgets (avatars, bubbles)
│   ├── icons.py             # vector menu icons painted with QPainter
│   ├── theme.py             # light/dark theme + accent + font → QSS
│   ├── login_dialog.py      # login dialog + LoginRunner (reused by auto-login)
│   ├── dialog_base.py       # frameless rounded card dialog base
│   ├── confirm_dialog.py    # confirmation dialog
│   ├── settings_page.py     # settings (general/appearance/font/upload/AI/about)
│   ├── profile_popup.py     # profile card opened from an avatar
│   ├── room_panels.py       # members / files / pinned-messages overlays
│   ├── room_info_popup.py   # channel/team info card
│   ├── directory_panel.py   # directory (channels / users / teams)
│   ├── create_room_dialogs.py  # new DM / channel / team
│   ├── forward_dialog.py    # forward target picker
│   ├── quick_switcher.py    # Ctrl+K conversation switcher
│   ├── global_search_panel.py  # cross-room search
│   ├── message_list_dialog.py  # pinned / starred messages
│   ├── read_receipts_popup.py  # read/unread roster
│   ├── reminders_dialog.py  # reminder list
│   ├── emoji_picker.py      # emoji picker
│   ├── image_viewer.py      # full-size viewer (paging / zoom / rotate / save as)
│   ├── ocr_dialog.py        # editable OCR result window
│   ├── screenshot_capture.py   # region capture overlay
│   ├── upload_dialog.py     # upload progress dialog
│   ├── download_dialog.py   # download progress dialog
│   ├── ai_panel.py          # AI summary overlay (digest / daily / weekly)
│   ├── ai_dialog.py         # AI result dialog (streaming / stop / insert)
│   ├── ai_worker.py         # cancellable streaming AI worker
│   ├── api_workers.py       # async task threads (run_async)
│   ├── realtime_bridge.py   # DDP → Qt signal bridge (QThread)
│   ├── avatar_loader.py     # async avatar/image loading and caching
│   ├── user_status.py       # presence normalisation and on-demand fetch
│   ├── tray.py              # system tray (hide-to-tray / blink / hover preview)
│   ├── updater_dialog.py    # update download dialog
│   └── single_instance.py   # single-instance guard
├── tests/                   # unit tests (unittest, offline)
└── resources/style.qss      # WeChat palette stylesheet
```

## Notes

- Realtime no longer requires the server to expose WebSocket: if the reverse
  proxy drops the `Upgrade` header the handshake returns 400 and the client
  switches to SockJS long polling (exactly like the web client). To restore
  direct WebSocket, forward the `Connection`/`Upgrade` headers in the proxy.
- Subscribing to `stream-room-messages` has two server dialects (an `rid` array
  vs a single `rid`); the client tries the modern form first and switches
  wholesale if it is rejected. Some deployments deliver the same message twice,
  which is deduplicated on receipt by message fingerprint.
- Long-polling sessions on some deployments are destroyed roughly 60 seconds
  after the last `xhr` poll (`xhr_send` then always returns 404 while `xhr`
  still reads frames — a "receive-only" zombie connection). Outbound frames are
  therefore batched into few `xhr_send` calls, and any send failure tears the
  connection down and reconnects instead of pumping frames into a dead session.
- If the network is unreachable or the server is not ready on first start, the
  connection failure is reported without blocking the UI.
- The tray hover preview relies on the OS exposing icon coordinates; Windows 11
  hides icons in the taskbar overflow ("^") area, where the card is unavailable
  and tooltips plus click/right-click menus are used instead (dragging the icon
  back into the visible area restores it).
- This is a Chinese-localised desktop client, best suited to your own
  Rocket.Chat server (configure it in the login dialog, or see the config notes
  above).

## License

Copyright (C) 2026 shigc

This program is free software; you can redistribute it and/or modify it under
the terms of the **GNU General Public License as published by the Free Software
Foundation; either version 2 of the License, or (at your option) any later
version** — see [LICENSE](LICENSE).

Why GPL rather than MIT, specifically: the UI layer uses PyQt5, which Riverbank
Software dual-licenses under GPL-2.0-or-later **or** a commercial agreement. MIT
is only one-way compatible with GPL — MIT code can be folded into a GPL project,
but an MIT licence cannot relieve a GPL dependency of its obligations. Without a
commercial licence, the project as a whole must be distributed under GPL.

In practice:

- You may use, modify and redistribute it, commercially included; any
  redistribution must carry the GPL text and the corresponding source, and
  derivative works stay under GPL.
- Closed-source commercial use means buying a Riverbank PyQt commercial licence,
  or porting the UI layer to LGPL-licensed PySide6.
- Component-by-component licence inventory and distribution notes:
  [THIRD_PARTY_LICENSES.md](THIRD_PARTY_LICENSES.md).
- If you need an MIT or dual-licensing route, open an issue before sending a
  pull request: once published under GPL, taking that licence back is not
  feasible with respect to existing contributors.

This program is distributed in the hope that it will be useful, but WITHOUT ANY
WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS FOR A
PARTICULAR PURPOSE. See the license for details.

## Disclaimer

- Fchat is an independent third-party Rocket.Chat client and is **not affiliated
  with Tencent WeChat**; the "WeChat-style" wording describes the interaction
  style only and implies no endorsement or trademark licence.
- Rocket.Chat is a trademark of its respective owner; using it here does not
  indicate any relationship with the Rocket.Chat project.
