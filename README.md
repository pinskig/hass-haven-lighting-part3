# Haven Lighting Part 3

A revived and modernized Home Assistant integration for **Haven Lighting** smart landscape systems.

> ℹ️ **Note:** This project is a fork and "Redux" of the original [haven-hass](https://github.com/mickeyschwab/haven-hass) integration by **@mickeyschwab**. Huge thanks for the original work. This version was rewritten for the new **Haven Lighting API (2025)** after the platform cloud migration.

## 🌟 Features
- **Zone support:** Control individual zones (for example, "Left House" and "Path Lights").
- **Group support:** Import Haven app groups (for example, "Evens" and "Front Yard") as entities.
- **Full light controls:** On/off, brightness (0–100%), RGB color, and white temperature (2700K–5000K).
- **Fast state sync:** Group changes fan out quickly to related entities.
- **Safe discovery:** New zones/groups appear automatically. Missing targets become unavailable and retain their registry entries and customization.
- **Shared polling:** One refresh per location every 60 seconds updates all zone/group entities. Successful commands request a shared refresh after a five-second debounce window.
- **Account request protection:** Polling, commands, discovery, and authentication share a cooldown and a one-second minimum gap between outbound requests. Provider cooldowns survive setup flows, reloads, and Home Assistant restarts.
- **Accurate state:** Report RGB, white temperature, and active effects with brightness from validated, immutable snapshots; unchanged polls do not cause redundant state writes.
- **Updated auth/API handling:** Modern endpoints and session device-ID behavior for improved login reliability.

## 🧱 Compatibility
- **Home Assistant:** 2024.1.0 or later. Automated integration tests cover 2024.1.0 and 2026.10.4 with the actual Home Assistant framework.
- **Install method:** HACS custom repository.
- **Account:** Valid Haven Lighting cloud account credentials.

## 🚀 Installation (HACS)
1. Open **HACS** in Home Assistant.
2. Click the **three dots** (top-right) → **Custom repositories**.
3. Add: `https://github.com/pinskig/hass-haven-lighting-part3`
4. Category: **Integration**
5. Click **Download** and then restart Home Assistant.

### Updating an existing installation

Download or redownload the latest repository version in HACS, then restart Home Assistant. The integration manifest for this update is **1.0.8**.

On first setup, existing registry identities are migrated to account/location/type-specific identifiers. When the old device's kind and parent are known, the migration preserves entity IDs, device IDs, custom names, and area assignments. It also handles a legacy target that first reappears after setup. Ambiguous legacy records are retained and logged for review instead of being assigned to a potentially different target. Existing automations keep their references for targets that migrate successfully.

Version 1.0.7 begins persisting provider cooldowns when it receives them. Earlier versions did not write those deadlines to disk, so the first upgrade cannot reconstruct a wait that existed only in an older process's memory.

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

Color-, temperature-, and effect-only `light.turn_on` actions explicitly request power on, even if the last poll reported the light on. This handles lights switched off through the Haven app or a schedule between polls. It adds one paced request when the light was already on; a brightness command already requests power on.

## 🛠️ Troubleshooting
- **Login fails:** Check the credentials. A temporary connection error can also mean a provider cooldown is still active; repeated setup attempts and restarts respect that same deadline.
- **Entities missing:** Allow the next normal discovery cycle. New targets are added automatically, including after an initially empty inventory. Check the logs if a legacy identity could not be migrated safely.
- **Delayed updates:** Normal background refreshes run once per minute. A 429 response pauses every request for that account for at least the server's requested wait. When no longer wait is supplied, repeated rate limits use 60-, 300-, then 900-second cooldowns. A 503 response with Retry-After also pauses requests without increasing the 429 escalation counter. Commands during cooldown report an error instead of sending another request.
- **Expired credentials:** Use the integration's reauthentication prompt to enter the password again. Temporary API failures preserve cached light data and mark entities unavailable until a successful refresh.
- **Removed targets:** They remain in the registries and become unavailable. Remove permanently retired devices through Home Assistant when appropriate; discovery does not delete them automatically.
- **Duplicate account entries:** Setup prevents a second active entry for the same normalized email. Registry conflicts with another entry are reported rather than silently changing its devices.
- **Unexpected redirect:** Fixed API endpoints reject redirects so login bodies are not forwarded and extra requests cannot bypass pacing. Check the configured API environment if the service has moved.
- **Need logs:** Enable debug logging for `custom_components.haven` and inspect Home Assistant logs. Each `Haven HTTP` debug record describes an actual request attempt with its method, endpoint family, response status (or transport failure), and elapsed time. These records omit credentials, tokens, request/response bodies, and target IDs. Calls rejected locally during cooldown produce no HTTP record. A normal location refresh makes two GET requests: one for zones and one for groups.

The API endpoints are defined once in `havenlighting/config.py`. Both defaults remain on the staging host used by this integration's existing account. The persistent Requests session uses separate five-second connection and thirty-second read-inactivity timeouts; these are not a total wall-clock deadline. Session closure does not clear the account budget. Persisted cooldown metadata contains a hashed account key, retry deadline/status, and backoff count; it contains no password or access token.


## 📝 Changes & Updates (Maintainer + ChatGPT)
To make recent updates easier to track, this section can be updated whenever you ship a change with ChatGPT support.

### Latest updates
- **1.0.8:** Make color-, temperature-, and effect-only turn-on actions request power on despite stale cached state. Preserve reported brightness during Fire, Sunset, and Ocean effects. Add debug records for actual HTTP attempts and regression tests for stale state, effect brightness, request accounting, and sensitive-data exclusion.
- **2026-11-01 — 1.0.7:** Persist shared account cooldowns across flows, reloads, and restarts; honor 503 retry deadlines; pool HTTP connections and reject redirects. Validate API values, separate zone/group identities, migrate known legacy registry entries without changing their IDs, retain unavailable targets, and discover additions automatically. Publish immutable snapshots, correct color/effect state, propagate refresh cancellation, and serialize command batches with polling. Add real Home Assistant tests and a CI matrix for the minimum and current supported versions.
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

The unit tests mock all Haven HTTP responses and do not need account credentials. Client tests include adapter-level transport checks, request concurrency, malformed responses, immutable state, and command batching. The adapter and registry policy tests use explicitly labeled contract doubles.

Run the real Home Assistant integration suite in a separate environment. For the current supported release, use Python 3.14.2 or later:

```sh
python -m pip install -r tests/requirements-ha-current.txt
python -m pytest tests/ha -v
```

For the minimum Home Assistant release, use Python 3.11:

```sh
python -m pip install uv==0.13.5
uv pip install --python python --override tests/requirements-ha-min-overrides.txt -r tests/requirements-ha-min.txt
python -m pytest tests/ha -v
```

The minimum-version fixture package was generated for Home Assistant 2024.1.2. The documented override installs **2024.1.0 itself** so the suite tests the declared compatibility floor. The framework suite uses real registries, setup/unload and reauthentication flows, coordinator scheduling, and debouncing. Network sockets and live Requests calls are blocked; only explicit fake Haven responses are allowed. GitHub Actions runs the unit suite and both framework environments.

## 🙌 Credits
- **Original creator:** [Mickey Schwab (@mickeyschwab)](https://github.com/mickeyschwab)
- **2025 API rewrite:** [Stephen Crescenti (@screscenti)](https://github.com/screscenti)
