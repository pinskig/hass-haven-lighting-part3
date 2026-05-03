# Haven Lighting Part 3

A revived and modernized Home Assistant integration for **Haven Lighting** smart landscape systems.

> ℹ️ **Note:** This project is a fork and "Redux" of the original [haven-hass](https://github.com/mickeyschwab/haven-hass) integration by **@mickeyschwab**. Huge thanks for the original work. This version was rewritten for the new **Haven Lighting API (2025)** after the platform cloud migration.

## 🌟 Features
- **Zone support:** Control individual zones (for example, "Left House" and "Path Lights").
- **Group support:** Import Haven app groups (for example, "Evens" and "Front Yard") as entities.
- **Full light controls:** On/off, brightness (0–100%), RGB color, and white temperature (2700K–5000K).
- **Fast state sync:** Group changes fan out quickly to related entities.
- **Entity cleanup:** Removed zones/groups are cleaned up on reload.
- **Smarter polling:** Keeps a steady refresh cadence and backs off when the Haven API signals load/rate limits.
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
- **Delayed updates:** Short API backoff windows can happen during rate limiting; behavior should recover automatically.
- **Need logs:** Enable debug logging for `custom_components.haven` and inspect Home Assistant logs.


## 📝 Changes & Updates (Maintainer + ChatGPT)
To make recent updates easier to track, this section can be updated whenever you ship a change with ChatGPT support.

### Latest updates
- Documentation refresh for setup, automation guidance, and troubleshooting.
- Clarified compatibility expectations and HACS install flow.

### Suggested format for future updates
- `YYYY-MM-DD` — Short summary of what changed.
- `YYYY-MM-DD` — Another concise update note.

## 🙌 Credits
- **Original creator:** [Mickey Schwab (@mickeyschwab)](https://github.com/mickeyschwab)
- **2025 API rewrite:** [Stephen Crescenti (@screscenti)](https://github.com/screscenti)
