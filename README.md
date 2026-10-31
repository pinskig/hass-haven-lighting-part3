# Haven Lighting Part 3

A revived and modernized Home Assistant integration for **Haven Lighting** smart landscape systems.

> ℹ️ **Note:** This project is a fork and "Redux" of the original [haven-hass](https://github.com/mickeyschwab/haven-hass) integration by **@mickeyschwab**. Huge thanks for the original work. This version was rewritten for the new **Haven Lighting API (2025)** after the platform cloud migration.

## 🌟 Features
- **Zone support:** Control individual zones (for example, "Left House" and "Path Lights").
- **Group support:** Import Haven app groups (for example, "Evens" and "Front Yard") as entities.
- **Full light controls:** On/off, brightness (0–100%), RGB color, and white temperature (2700K–5000K).
- **Fast state sync:** Group changes fan out quickly to related entities.
- **Entity cleanup:** Removed zones/groups are cleaned up on reload.
- **Shared polling:** One refresh per location every 60 seconds updates all zone/group entities. Successful commands request a shared refresh after a five-second debounce window.
- **Account request protection:** Polling, commands, discovery, and authentication share a cooldown and a one-second minimum gap between outbound requests.
- **Updated auth/API handling:** Modern endpoints and session device-ID behavior for improved login reliability.

## 🧱 Compatibility
- **Home Assistant:** Recent versions that support modern config entries/integration flows.
- **Install method:** HACS custom repository.
- **Account:** Valid Haven Lighting cloud account credentials.

## 🚀 Installation (HACS)
1. Open **HACS** in Home Assistant.
2. Click the **three dots** (top-right) → **Custom repositories**.
3. Add: `https://github.com/pinskig/hass-haven-lighting-part3`
4. Category: **Integration**
5. Click **Download** and then restart Home Assistant.

### Updating an existing installation

Download or redownload the latest repository version in HACS, then restart Home Assistant. The integration manifest for this fix is **1.0.6**. Earlier rate-limit patches still identified themselves as 1.0.5, so that older version number does not identify which patches are installed.

## ⚙️ Configuration
1. Go to **Settings → Devices & Services**.
2. Click **Add Integration** and search for **Haven Lighting Part 3**.
3. Enter your Haven Lighting **username (email)** and **password**.
4. After setup, your Haven zones/groups should appear as light entities.

## 💡 Automation Tip (RGB/Brightness)
In Home Assistant automations, advanced color controls are often easiest via a service call:

1. In your automation, add an **Action**.
2. Choose **Call service**.
3. Select **`light.turn_on`**.
4. Target your Haven entity (for example, `light.front_yard`).
5. Set fields such as **rgb_color**, **brightness**, or **color_temp_kelvin**.

## 🛠️ Troubleshooting
- **Login fails:** Re-check Haven credentials and retry after a short wait.
- **Entities missing:** Reload the integration or restart Home Assistant.
- **Delayed updates:** Normal background refreshes run once per minute. After a 429 response, every request from the client pauses for at least the server's requested wait. When no longer wait is supplied, repeated rate limits use 60-, 300-, then 900-second cooldowns. Commands during cooldown report an error instead of sending another request.
- **Expired credentials:** Use the integration's reauthentication prompt to enter the password again. Temporary API failures preserve cached light data and mark entities unavailable until a successful refresh.
- **Need logs:** Enable debug logging for `custom_components.haven` and inspect Home Assistant logs.


## 📝 Changes & Updates (Maintainer + ChatGPT)
To make recent updates easier to track, this section can be updated whenever you ship a change with ChatGPT support.

### Latest updates
- **2026-10-31 — 1.0.6:** Centralize location polling; collapse overlapping refreshes; enforce cooldowns across all API endpoints; preserve 429 responses during token renewal; honor full numeric or HTTP-date Retry-After waits; retain cached light identities after failed refreshes; add reauthentication and duplicate-account prevention. Add offline regression tests for concurrency, rate limits, and Home Assistant integration boundaries.
- Documentation refresh for setup, automation guidance, and troubleshooting.
- Clarified compatibility expectations and HACS install flow.

### Suggested format for future updates
- `YYYY-MM-DD` — Short summary of what changed.
- `YYYY-MM-DD` — Another concise update note.

## Development tests

Run from the repository root with Python 3.12 or later:

```sh
python -m pip install -r tests/requirements.txt
python -m unittest discover -s tests -v
```

The tests mock all Haven HTTP responses and do not need account credentials. Core tests exercise the real request/client code. Home Assistant adapter tests use small framework stubs to verify our integration boundary; they do not replace testing an installed integration in Home Assistant.

## 🙌 Credits
- **Original creator:** [Mickey Schwab (@mickeyschwab)](https://github.com/mickeyschwab)
- **2025 API rewrite:** [Stephen Crescenti (@screscenti)](https://github.com/screscenti)
