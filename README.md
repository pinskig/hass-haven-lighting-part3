# Haven Lighting Part 3

A Home Assistant integration for Haven Lighting cloud-connected landscape lighting, maintained by **[Geoffrey Pinski (@pinskig)](https://github.com/pinskig)** with development and review assistance from **ChatGPT and Codex**.

This project was forked from [Stephen Crescenti's Haven Lighting Redux](https://github.com/screscenti/hass-haven-lighting-redux) on **April 19, 2026**. Redux supplied the 2025 Haven API rewrite and built on [Mickey Schwab's original integration](https://github.com/mickeyschwab/haven-hass). Part 3 builds on that foundation with shared polling, account-wide request protection, safer state and device handling, and automated regression coverage. The [development history](#ai-assisted-development-since-the-fork) records the changes made since the fork.

**Current integration version: 1.0.14** · **Home Assistant: 2024.1.0 or later** · **Installation: HACS custom repository**

[Features](#features-and-current-scope) · [Installation](#installation-and-configuration) · [Request handling](#polling-and-request-handling) · [Automations](#automations) · [Troubleshooting](#troubleshooting) · [Change history](#ai-assisted-development-since-the-fork) · [Development tests](#development-tests)

## Features and current scope

The integration discovers the Haven account's **default location** and exposes its zones and Haven app groups as Home Assistant light entities. It uses cloud polling; a working Haven account and internet connection are required.

| Capability | Behavior |
| --- | --- |
| Zones and groups | Control zones and groups separately, including targets whose numeric IDs overlap. Individual fixtures outside those zone/group records are not exposed separately. |
| Power | Turn zones and groups on or off. |
| Brightness | Home Assistant brightness is mapped to **10 nonzero Haven levels**; zero turns the target off. |
| RGB color | RGB input selects the nearest of **17 configured Haven color presets**. Exact RGB white `[255, 255, 255]` selects a white preset instead; see below. |
| White temperature | Selects the nearest configured preset: **2700, 3000, 3500, 3700, 4000, 4400, 4700, or 5000 K**. |
| Exact white presets | Each zone/group has a **White temperature** dropdown with those eight choices. Selecting one sends its exact Haven color ID and turns the target on, preserving brightness. |
| Effects | **Fire, Sunset, and Ocean**, with brightness reported while an effect is active. |
| Inventory changes | New zones/groups appear on later refreshes, including after an initially empty inventory. Missing targets retain their registry entries and become unavailable. |
| State reporting | Validated snapshots report power, brightness, RGB/white mode, and effects. Unchanged polls avoid redundant Home Assistant state writes. |

Discovery currently uses the account's default location; it does not enumerate every location or provide a location selector. The brightness and color mappings above describe this integration's API controls, rather than continuous dimming or arbitrary RGB output.

The palette is not discovered from each device's capabilities. White requests use Haven's existing white preset IDs through `SetColor`; the integration does not substitute RGB white or claim an arbitrary-Kelvin API. A request for **3200 K**, for example, maps to **3000 K**, which is the temperature subsequently reported to Home Assistant. Fixed-white and other products may have different capabilities; the current inventory responses do not establish a per-model capability contract.

The white IDs were checked against the Haven `colorDefinitions` response captured during the maintainer's Homey work on **May 26, 2025**. Version 1.0.14 corrects preset **6** from the inherited **4100 K** label to **4400 K**, without changing its numeric ID. This also corrects nearest-preset rounding: a 4100 K request now selects 4000 K. An exact midpoint selects the lower temperature.

## Installation and configuration

1. Open **HACS** in Home Assistant and open **Custom repositories**.
2. Add `https://github.com/pinskig/hass-haven-lighting-part3` with category **Integration**.
3. Download **Haven Lighting Part 3**, then restart Home Assistant.
4. Go to **Settings → Devices & Services → Add Integration**.
5. Search for **Haven Lighting Part 3** and enter your Haven account email and password.
6. After setup, zones and groups from the default location appear as light entities with a companion White temperature control on each device.

Setup prevents duplicate active entries for the same normalized email address. If credentials expire, use the integration's reauthentication prompt to update them.

### Updating an existing installation

Download or redownload the latest repository version in HACS, then restart Home Assistant. The current manifest version is **1.0.14**. A full Home Assistant restart loads the downloaded Python code; reloading the integration alone does not establish that the running code has changed.

The registry migration introduced in 1.0.7 gives targets identifiers that distinguish their account, location, and zone/group type. When a legacy target's identity can be established, migration preserves its entity ID, device ID, custom names, and area assignments. Existing automations retain their references for successfully migrated targets. Migration also handles a legacy target that first reappears after setup.

Ambiguous legacy records are retained and logged for review. The integration does not guess which current target they represent; a newly discovered target may receive a separate entry. Check affected automation references when resolving an ambiguous record. Ownership conflicts with another integration entry are reported explicitly.

Version 1.0.7 began saving provider cooldowns to disk. The first upgrade from an earlier version cannot reconstruct a cooldown that existed only in the previous process's memory. Once saved, deadlines survive integration reloads and Home Assistant restarts.

### Selecting white

Open **Settings → Devices & Services → Haven Lighting Part 3**, choose the zone/group device, and use **White temperature**. The dropdown selects an exact preset, avoiding intermediate slider values and white-looking selections on the RGB wheel:

| White temperature | Haven color ID |
| --- | --- |
| 2700 K | 1 |
| 3000 K | 2 |
| 3500 K | 3 |
| 3700 K | 4 |
| 4000 K | 5 |
| 4400 K | 6 |
| 4700 K | 7 |
| 5000 K | 8 |

Selecting white sends the existing temperature action through the linked light entity, which sends **SetColor → On**. It preserves brightness and uses the same account budget, action diagnostics, and polling as the light. The original light entity and automation references remain valid. Keep that light entity enabled; the dropdown reports an error if its linked light is disabled or missing.

The dropdown follows the shared reported preset, including changes made in the Haven app. It is **unknown** while a nonwhite color/effect or an unrecognized preset is selected, and **unavailable** when its target disappears or the coordinator is unavailable. It does not remember an old white choice as though it were still active.

The existing temperature slider remains supported and snaps to the nearest preset. Exact RGB white `[255, 255, 255]` now retains the white preset active when the command executes, or selects **2700 K** when coming from a color, effect, or unknown preset. Other RGB values still use the inherited color approximation: near-white RGB is not a reliable way to request a particular white temperature. Use the dropdown or an explicit `color_temp_kelvin` action for that purpose.

## Polling and request handling

### Shared updates

One Home Assistant coordinator serves all entities for a discovered location. Background refreshes normally run about **once every 60 seconds**. A healthy refresh makes **two GET requests**, retrieving the zone list and group list. Overlapping refreshes share the result instead of independently polling for every entity.

Successful commands update the addressed target's cached state and request a shared refresh after a **five-second debounce window**. That refresh lets related zones and groups catch up with the server. Network delays and provider cooldowns can extend the wait. Polling continues while lights are off or the inventory is empty so changes can still be discovered. Authentication, discovery, commands, and post-command refreshes add traffic beyond the normal background polling.

Light commands accept an empty successful HTTP response as an acknowledgment, including HTTP 200, 202, and 204. Acknowledgment updates the cached state; it does not prove the physical lights have finished changing. The scheduled inventory refresh reconciles the state reported by Haven. Version 1.0.13 also rejects an explicit JSON `success: false` in a command result object or top-level result list, even when HTTP itself succeeded. It does not infer a complete response schema from this field. Nonempty malformed responses remain errors, and authentication and inventory requests still require valid response data.

Both inventory responses are validated before a new snapshot is published. A failed or malformed refresh preserves the previous snapshot and reports an update failure. Command batches cannot interleave with location polling. If a later command fails after an earlier command was acknowledged, those accepted changes are retained optimistically and the failure is reported; a batch is not rolled back.

Failed command batches preserve the existing refresh schedule, including when brightness succeeds before color fails. Accepted changes are published without resetting the polling timer or canceling a pending refresh. Repeated failures therefore do not keep postponing that state check. These scheduled reads still respect the account's provider cooldown.

### Rapid adjustments and the color-temperature marker

Rapid color or temperature selections can queue faster than the account budget permits the corresponding `SetColor` and `On` requests. Deterministic tests reproduce older selections completing after newer selections have already been submitted. The observed 1.0.12 log contained successful HTTP responses and repeated color/On sequences, but did not contain the requested Kelvin values, service contexts, or reported states needed to prove a feedback loop or identify every source of the commands.

**Version 1.0.14 does not coalesce or drop queued service calls.** A prototype investigated for 1.0.13 admitted newer color intents early but changed how Home Assistant resolved a queued `light.toggle`: it could read old power state and issue On instead of Off. The release retains Home Assistant's existing service semaphore and the location/account locks. The white dropdown delegates to the same light service instead of independently changing power through a second platform. A safe future coalescer must preserve the original intent of relative actions, including toggle and brightness steps, before Home Assistant converts them to absolute commands. Private coroutine inspection and global service overrides are not used.

The diagnostics below distinguish service waiting, requested values, preset mapping, optimistic acknowledgments, and later inventory state. Snapping once to a configured preset is expected; repeated movement through older selections or movement after a later inventory read requires those records to identify the cause. Later valid inventory reports remain authoritative, so genuine changes from the Haven app are not permanently hidden.

### Shared account budget

Authentication, token renewal, discovery, polling, and commands use the same request gate for a normalized account email **within this Home Assistant instance**.

| Condition | Handling |
| --- | --- |
| Normal requests | Requests are serialized and start at least **one second apart**. |
| HTTP 429 | Successive rate limits impose minimum waits of **60, 300, then 900 seconds**. A longer valid `Retry-After` takes precedence. Both numeric seconds and HTTP dates are supported. |
| HTTP 503 with `Retry-After` | Honors the provider's wait without increasing the 429 escalation counter. |
| Request during cooldown | Rejects the call locally without sending HTTP, extending the deadline, or increasing the rate-limit counter. Commands report an error. |
| Recovery | Three consecutive successful HTTP responses reset the 429 escalation. |
| HTTP 401 | Allows one serialized token-renewal attempt and one retry; rejected credentials lead to reauthentication. |
| HTTP 400 from sign-in | Stops automatic setup retries for an existing entry and asks for reauthentication; new setup stays on the sign-in form. A rejected request alone does not identify whether the cause is account details, account status, or an API requirement. Other endpoints' HTTP 400 responses remain ordinary API errors. |
| Reload or restart | Preserves saved cooldown deadlines and escalation state. Closing a client does not clear the account budget. |

Explicit refreshes and repeated setup attempts respect the same gate. Ordinary transport failures and rate-limit responses do not trigger blind command retries. These safeguards reduce avoidable traffic; they do not establish Haven's service quotas or coordinate requests made by other Home Assistant instances or the Haven app.

## Automations

For an exact warm-white selection, use the existing light entity:

```yaml
action: light.turn_on
target:
  entity_id: light.front_yard
data:
  color_temp_kelvin: 2700
```

Or use the companion dropdown:

```yaml
action: select.select_option
target:
  entity_id: select.front_yard_white_temperature
data:
  option: "2700 K"
```

Replace both example entity IDs with the ones shown in your installation. The dropdown sends only its selected Kelvin value to the light service; its provider command uses the corresponding integer `colorId`.

Use `light.turn_on` with fields such as `rgb_color`, `brightness`, or `color_temp_kelvin`. For example, this action selects the nearest supported blue preset and brightness level:

```yaml
service: light.turn_on
target:
  entity_id: light.front_yard
data:
  rgb_color: [0, 0, 255]
  brightness: 128
```

Replace `light.front_yard` with your entity ID. To select an effect, use `effect: Ocean`, `effect: Fire`, or `effect: Sunset`. To stop an active effect, use `effect: "off"`; without a replacement RGB color or white temperature, this selects **2700 K white** if an effect is still active when the command executes. An existing static color is preserved. Version 1.0.13 makes this decision under the operation lock, after preceding commands, so a queued effect-off action neither misses a newly started effect nor overwrites a newer static color. It does not restore a previous color or turn the light off.

Color-, temperature-, and effect-only turn-on actions explicitly request power on, even if the last poll reported the target on. This handles a light switched off by the Haven app or a schedule between polls. It adds one paced request when the light was already on. A nonzero brightness command already requests power on; zero brightness turns the target off.

## Troubleshooting

| Symptom | What to check |
| --- | --- |
| Login or setup fails | Verify the account credentials. A temporary connection error can also mean a provider cooldown is active; repeated setup attempts and restarts respect the saved deadline. |
| Sign-in is rejected with HTTP 400 | For an existing entry, version 1.0.9 opens a reauthentication prompt instead of retrying automatically. Check that the same account can sign in to the Haven Lighting app, then reauthenticate in Home Assistant. This status alone does not prove the password is wrong. |
| Entities are missing | Confirm the targets are zones/groups in the account's default location. Allow the next successful refresh for new targets. Check logs for an ambiguous legacy identity or ownership conflict. |
| Updates are delayed | Normal polling is about once per minute. Commands schedule a debounced refresh, and rate limits can postpone all account traffic. |
| Entities become unavailable | Check the logs for authentication, connection, payload-validation, or cooldown errors. Failed refreshes retain cached data; targets absent from a successful inventory remain unavailable. |
| Credentials have expired | Complete the integration's reauthentication flow. |
| Retired targets remain listed | Registry entries are preserved to protect customization and automation references. Remove permanently retired devices through Home Assistant when appropriate. |
| Color or brightness differs from the requested value | Check the preset and brightness mappings in the feature table. Use White temperature for an exact white preset; near-white RGB selections still use color approximation. |
| White temperature is unknown | The reported preset is currently a color/effect or is not in the white map. Select one of the eight white options to change it. |
| The temperature control marker moves after release | One snap to the nearest configured preset is expected. Rapid adjustments can still create a command backlog in 1.0.14. The exact dropdown avoids continuous dragging. Capture action and poll diagnostics to distinguish queued selections from a later inventory report; physical output and the browser marker still need installation testing. |
| A light action fails with `Expecting value: line 1 column 1 (char 0)` | Version 1.0.10 fixes this error when a light command receives an empty successful response. Update and restart Home Assistant. A nonempty invalid response still fails, with an error identifying the endpoint family and HTTP status; include that message when reporting a remaining problem. |
| Logs warn that Haven uses `device_registry.devices` as a mapping | Version 1.0.11 uses the supported registry iteration on newer Home Assistant versions while retaining compatibility with older versions. This warning is separate from command-response parsing errors. |
| An API redirect is rejected | Check the configured API environment. Fixed endpoints reject redirects so login bodies are not forwarded and additional requests cannot bypass pacing. |

### Request diagnostics

Enable debug logging for `custom_components.haven` in Home Assistant. If configuring logging in YAML, merge this into the existing `logger` configuration:

```yaml
logger:
  logs:
    custom_components.haven: debug
```

Version 1.0.13 adds an action trace to the HTTP-attempt records introduced in 1.0.8 and extended in 1.0.12. Example format, with illustrative values:

```text
Haven HTTP attempt: method=POST endpoint=/Commands/SetColor status=200 elapsed=0.250s version=1.0.14 response_bytes=0 trace=012345abcdef
```

Each record represents an actual HTTP transport attempt and includes its method, fixed endpoint-family label, status or transport-failure marker, elapsed time, imported integration version, response byte count, and trace. Timing excludes waiting for serialization or pacing. The byte count describes the already-read body after any HTTP content decoding; `unavailable` means no response was returned. These records omit credentials, tokens, headers, body contents, target IDs, and query values. A locally rejected cooldown call produces no HTTP-attempt record. A transport failure does not establish that the request reached Haven. `trace=-` means the request occurred outside a traced entity action or poll, such as initial authentication.

The same opt-in DEBUG setting also enables these records:

| Record | Meaning |
| --- | --- |
| `Haven action: phase=service_received` / `service_finished` | Boundary of HA's service wrapper, including its semaphore wait. HA 2024.1 single-entity calls bypass this hook. Finished includes errors or cancellation; only `accepted` identifies an acknowledged batch. |
| `Haven action: phase=received` | Entity-level requested mode, Kelvin/RGB/brightness/effect, mapped command list, running HA and integration versions, and anonymous context/parent links. |
| `Haven action: phase=started` | Coordinator execution begins; `queue_wait` measures only the wait since entity-level receipt, not an earlier HA semaphore wait. |
| `Haven action: phase=accepted` / `error` | Before/after cached state, elapsed time, and `source=optimistic`; errors include HTTP status when available and whether cooldown blocked HTTP locally. |
| `Haven poll` / `Haven poll state` | A separate trace links the two inventory GETs to their reported state, including mode, preset ID and Kelvin, with `source=inventory`. |
| `Haven color configuration` | The entity's configured supported modes and white presets, explicitly labeled `capability_source=inherited_presets`. |

Action traces propagate into executor work, linking SetColor and On to the same action. Target, context, and parent references use a random process-local keyed hash; the new records contain no raw account, entity, device, context, or user IDs. A boolean records whether a user context exists. These links can help compare manual actions and automation chains, but do not by themselves identify a particular automation or prove a loop. They change after restart. Existing Home Assistant/framework messages and other integrations may contain identifying information; review the complete log before sharing it.

After updating and restarting Home Assistant, enable debug logging and look for `version=1.0.14` in a new HTTP-attempt record. This value comes from the imported client code, rather than rereading the downloaded manifest during each request. A successful command with `response_bytes=0` has an empty acknowledgment; a parsing error with a nonzero count needs further investigation without assuming that the body was empty.

### Manual temperature check

1. Redownload the update, fully restart Home Assistant, and verify a new request log says `version=1.0.14`. Record the actual Home Assistant version and Haven device/controller model.
2. Enable debug logging. Select red, then use the device's **White temperature** dropdown to select **2700 K**. Check the physical light and Home Assistant's active color mode and temperature. The action should map to `set_color(1)` followed by `turn_on`.
3. Select **4400 K** and **5000 K** separately and expect IDs 6 and 8. Change only brightness and verify temperature mode is preserved. With the existing temperature slider or a service action, try **3200 K** and expect **3000 K**.
4. Make a short series of rapid temperature changes, ending on **2700 K**. Stop interacting and let outstanding actions and a successful inventory refresh finish. Note whether the marker moves during command completion or only after the poll. This release still executes intermediate selections.
5. Try Fire followed by effect-off, and Fire followed by a static color and effect-off. Expect 2700 K in the first case and the selected static color in the second. Test an ordinary Off action separately.
6. Change the light in the Haven app, then allow a successful HA inventory refresh. Confirm that the external change appears without new command POSTs generated by the refresh.
7. Disable debug logging and save the relevant log excerpt, along with the exact action sequence, expected final temperature, observed marker behavior, and whether the physical light matched it. Include service/action/poll traces rather than only the WebSocket error wrapper.

The API defaults are centralized in [`havenlighting/config.py`](custom_components/haven/havenlighting/config.py). Both remain on the inherited `https://stg-api.havenlighting.com/api` host. The client reuses a Requests session with a **five-second connection timeout** and **thirty-second read-inactivity timeout**; these are not a total wall-clock deadline. The cooldown storage file contains a hashed account key and budget metadata, without passwords or access tokens. Account credentials are managed separately by the Home Assistant configuration entry.

For an unresolved problem, [open an issue](https://github.com/pinskig/hass-haven-lighting-part3/issues) with the integration and Home Assistant versions, the action that failed, and relevant sanitized logs.

## AI-assisted development since the fork

Geoffrey Pinski's work on this fork has used **ChatGPT and Codex** for code analysis, implementation, review, regression tests, and documentation. The merged early pull requests link to their Codex tasks. This section records the full merged post-fork history through 1.0.14, including earlier approaches that later releases replaced.

The upstream **1.0.5** baseline already supplied zone/group controls, brightness and color mappings, effect commands, token renewal, HACS installation, the 2025 API rewrite, staging endpoints, and session device-ID generation. Those are credited to the original and Redux projects. Part 3's changes are detailed below. Dates use **America/New_York** time; versions 1.0.6–1.0.13 all landed on October 31 locally, although GitHub's UTC dates cross into November 1.

### April 19, 2026 — Initial polling and rate-limit work

[PR #1](https://github.com/pinskig/hass-haven-lighting-part3/pull/1)

- Stopped entity updates from unconditionally forcing a location refresh.
- Added adaptive polling/error backoff, explicit HTTP 429 handling, rate-limit-header diagnostics, and a user-activity reset for faster follow-up after commands.
- Added state-change tracking and removal of absent targets from the in-memory inventory.

This early polling design was later replaced by the shared coordinator and account gate. Missing targets are now retained as unavailable rather than automatically removed from the registries.

### April 19, 2026 — Part 3 naming and repository metadata

[PR #2](https://github.com/pinskig/hass-haven-lighting-part3/pull/2)

Renamed the project and integration to **Haven Lighting Part 3**. Updated HACS naming, configuration wording, installation links, and manifest documentation/issue URLs to this repository. The API rewrite and device-ID behavior remained inherited from Redux.

### May 3, 2026 — Recovery and backoff refinement

[PR #3](https://github.com/pinskig/hass-haven-lighting-part3/pull/3)

Raised the early error-backoff ceiling from 80 to 300 seconds, classified rate-limit/timeout conditions for diagnostics, tracked consecutive successes, and reduced the interval gradually after recovery. Added logs for interval changes and user-activity resets. These adaptive intervals describe the historical implementation, not today's normal 60-second coordinator cadence.

### May 3, 2026 — First explicit cooldown gate

[PR #5](https://github.com/pinskig/hass-haven-lighting-part3/pull/5)

Added a location-refresh cooldown with 60/300/900-second escalation, numeric `Retry-After` propagation through API errors, success-based recovery, and suppression of repeated rate-limit error messages. This initial protection was limited to ordinary location refreshes and process memory. Full endpoint coverage and complete provider waits arrived in 1.0.6; account sharing and restart persistence arrived in 1.0.7.

### May 3, 2026 — Documentation consolidation

[PR #6](https://github.com/pinskig/hass-haven-lighting-part3/pull/6)

Reorganized the README around features, compatibility, HACS installation, configuration, automation examples, troubleshooting, and credits. Added the original **Maintainer + ChatGPT** update section, now replaced by this complete history.

### October 31, 2026 — Version 1.0.6

[Commit 6112f5c](https://github.com/pinskig/hass-haven-lighting-part3/commit/6112f5c54614f22f5d2b659295d85b05a64a141a)

- Introduced one coordinator per discovered location, 60-second shared polling, overlapping-refresh coalescing, and a five-second command-refresh debounce.
- Serialized HTTP requests and token renewal, added minimum request spacing, and enforced cooldowns across authentication, discovery, polling, and commands. Preserved rate-limit errors during renewal and honored full numeric or HTTP-date `Retry-After` waits.
- Retained clients and their budgets across automatic setup retries, kept cached inventory after failed refreshes, and surfaced rejected commands as errors.
- Added reauthentication, normalized-email duplicate prevention, and flow translations.
- Added **38 offline regression tests** for concurrency, cooldowns, and integration boundaries.

### October 31, 2026 — Version 1.0.7

[Commit 901a31e](https://github.com/pinskig/hass-haven-lighting-part3/commit/901a31eea56a897c788e640277b11e22a1b93e31)

- Shared account budgets across clients and setup/reauthentication flows; persisted cooldown deadlines and escalation across reloads and restarts. Added HTTP 503 `Retry-After` handling without increasing 429 escalation.
- Reused HTTP connections, rejected redirects, kept explicit authentication authoritative over ambient credential lookup, centralized API settings/timeouts, and closed sessions without clearing cooldowns.
- Validated API values before publishing immutable snapshots. Separated account/location/zone/group identities, protected registry ownership, and migrated proven legacy identities while preserving entity/device IDs and customization.
- Retained missing or ambiguous targets, discovered additions and reappearances, corrected RGB/white/effect state reporting, and added explicit effect-off behavior. Unchanged snapshots stopped generating redundant Home Assistant state writes.
- Serialized command batches with polling, preserved confirmed partial successes, and propagated refresh cancellation.
- Left logging configuration to Home Assistant while retaining explicit standalone setup. Declared the Requests dependency, removed generated bytecode/OS metadata, and cleaned up stale HACS ZIP metadata.
- Added a GitHub Actions matrix using actual Home Assistant **2024.1.0** and **2026.10.4** with blocked live network access. Coverage grew to **85 unit tests and 19 framework tests on each Home Assistant version**.

### October 31, 2026 — Version 1.0.8

[Commit 678728d](https://github.com/pinskig/hass-haven-lighting-part3/commit/678728d03194e4bda64752856cf324ba26b61743)

- Made color-, temperature-, and effect-only turn-on actions explicitly request power on despite stale cached state.
- Preserved reported brightness during Fire, Sunset, and Ocean effects.
- Added sanitized DEBUG records for actual HTTP attempts, including status and elapsed time, while distinguishing locally blocked calls.
- Added regressions for state divergence, effect brightness, request accounting, timing, and sensitive-data exclusion. Validation reached **91 unit tests and 29 framework tests on each Home Assistant version**.

### October 31, 2026 — Version 1.0.9

[Commit a326096](https://github.com/pinskig/hass-haven-lighting-part3/commit/a326096dc120fbf362862bef18e6238ea3dcb441)

- Stopped automatically retrying a sign-in request rejected with HTTP 400. Existing entries now request reauthentication; setup and reauthentication forms show a specific, neutral sign-in rejection message.
- Kept rate limits, service outages, and transport failures on their existing retry/cooldown paths. HTTP 400 from commands or discovery is not reclassified as a sign-in failure.
- Preserved the existing authentication URL, payload, and headers. This change repairs the retry behavior; it does not establish or resolve Haven's underlying reason for a particular rejected login.
- Added offline transport and real Home Assistant regressions for rejected sign-in, absence of automatic retries, successful recovery of the same entry, and preservation of transient-error handling.

### October 31, 2026 — Version 1.0.10

[Commit d658b94](https://github.com/pinskig/hass-haven-lighting-part3/commit/d658b94ae78c35d047c27ce155aeef885206674d)

- Accepted empty successful replies specifically for light commands. An empty HTTP 200 or 202 acknowledgment no longer causes a JSON-decoding error or prevents a color-only action from sending its subsequent power-on command.
- Kept authentication and inventory response validation, HTTP error handling, cooldowns, and the existing retry policy. Nonempty malformed command replies remain errors; commands are not replayed after a decoding failure.
- Added sanitized JSON-decoding errors that identify the endpoint family and HTTP status without including response bodies, credentials, or target IDs.
- Added transport and real Home Assistant regressions for empty command acknowledgments, color/power sequencing, debounced reconciliation, and malformed-response handling. The screenshot that prompted this fix did not include the live response body; offline reproduction establishes the client bug, not the provider's exact response in that incident.

### October 31, 2026 — Version 1.0.11

[Commit 76c68b7](https://github.com/pinskig/hass-haven-lighting-part3/commit/76c68b79562124b9f734df6da64bef693e2d3e48)

- Updated device-registry iteration for newer Home Assistant versions, removing the mapping deprecation warning while retaining compatibility with the older registry API.
- Preserved the global check for devices owned by another configuration entry. Added real Home Assistant regressions for conflicting controller and zone identities, including preservation of the existing owner's custom name and area.
- Kept pending reconciliation and ordinary polling timers when a failed command leaves the cached state unchanged. Previously, republishing the unchanged snapshot canceled a pending command refresh and restarted the polling interval, which could leave the displayed state stale during repeated failures. Confirmed partial successes are still published.
- Added real Home Assistant regressions for a successful command followed by a malformed command reply, and for preserving provider cooldowns when that scheduled refresh runs. The fix does not replay commands or create an additional refresh timer.

### October 31, 2026 — Version 1.0.12

- Extended refresh preservation to partially successful command batches. A confirmed brightness change followed by a failed color reply is published without canceling an already scheduled refresh or resetting the polling interval.
- Added the imported integration version and response byte count to each HTTP-attempt DEBUG record. Debugging enabled after startup can now distinguish running code from downloaded files and empty replies from nonempty replies, without recording response contents.
- Added a real Home Assistant regression for the partially successful brightness/color sequence, plus a consistency check between the imported version marker and the integration manifest.

### October 31, 2026 — Version 1.0.13

- Applied the useful command-result validation identified while reviewing the updated original projects: an explicit `success: false` now fails the command before publishing a false optimistic success. Empty successful replies remain valid acknowledgments, accepted partial changes remain visible, and failures do not replay commands or send a trailing On.
- Added opt-in, redacted action-to-HTTP traces, service/coordinator timing, requested and mapped values, configured color modes, and before/after state. Inventory reads have separate traces and explicitly identify reported state. Logs distinguish optimistic acknowledgment from physical-state confirmation.
- Fixed the effect-off decision to use execution-time state under the operation lock. Concurrent commands on older HA paths can no longer leave a newly started effect active or replace a newer static color with the white fallback.
- Rechecked RGB-to-white transitions, all eight configured white presets, boundary rounding, brightness-only mode preservation, empty responses, failures, rapid adjustments, and external reconciliation with actual Home Assistant. Added guards for current HA's serialized toggle and brightness-step behavior.
- Reproduced a rapid-adjustment backlog, but withheld a coalescing prototype after it introduced a relative-service ordering regression. The release preserves existing serialization and does not claim the marker problem is fully resolved. It adds the diagnostics needed to separate backlog, preset snapping, and subsequent provider state.
- Updated the README with the investigation limits, upstream decisions, debug fields, and a manual reproduction checklist.

#### Upstream comparison and integration decisions

The comparison examined [Mickey Schwab's Home Assistant integration at `8f5ac1c`](https://github.com/mickeyschwab/haven-hass/tree/8f5ac1c4775ff3f1895c8c7914b40503a9f83082) and [Python client at `39d8f5e`](https://github.com/mickeyschwab/haven-python/tree/39d8f5e8a27cf262a3a68a0f826844288cfc37af), alongside the Redux foundation. The explicit unsuccessful-result check is implemented locally without changing Part 3's public light controls, target identities, stored data, dependency requirements, or supported HA minimum.

The upstream `InformationSummaryWithAllZonesGroup` endpoint is a promising way to reduce inventory reads, but a representative zone/group payload and capability schema are still missing. Switching to it now could lose target type, color, or group membership semantics, so Part 3 retains its validated two-request inventory path. The upstream HTTP transport change is also deferred: this installation's observed requests succeeded with Requests, and there is no demonstrated need to add `curl_cffi`. No continuous-Kelvin endpoint or per-device capability mapping was established by that review. No changes have been submitted to the original repositories.

### November 1, 2026 — Version 1.0.14

- Added a **White temperature** dropdown to each zone/group device. Its eight exact options call the existing light temperature service, select Haven's numeric white preset, preserve brightness, and explicitly turn the target on. It shares the coordinator, polling, credentials, and request budget; existing light entity IDs and automations remain valid.
- Corrected preset ID 6 to **4400 K**, using the maintainer's recovered May 26, 2025 Haven `colorDefinitions` response from the Homey work. The recovered material also confirms Fire as ID 12; that ID remains unchanged. Corrected the affected nearest-preset rounding and reporting.
- Fixed exact RGB white `[255, 255, 255]` selecting Lavender. It now selects the current white preset at execution time, or 2700 K when coming from a nonwhite or unknown preset. Other RGB colors retain the existing approximation; no RGB-to-Kelvin conversion was invented for pale colors.
- Kept selector commands on the light service path to preserve current HA's handling of an in-flight white selection followed by toggle. Added regressions for exact payloads, brightness preservation, failure handling, empty acknowledgments, external changes, discovery, linked entity behavior, and white-selection ordering.
- The November 1 log demonstrates both a command backlog and near-white RGB approximation. It also shows cloud inventory acknowledging Fire/white IDs while the maintainer reported different physical output. This release fixes the confirmed selection issues and supplies an exact white control; physical device behavior remains part of the manual check.

## Development tests

Version **1.0.14** was verified locally in all three environments below. [GitHub Actions](https://github.com/pinskig/hass-haven-lighting-part3/actions/workflows/tests.yml) runs the same matrix on pushes and pull requests:

| Suite | Environment | Result |
| --- | --- | --- |
| Client and adapter regressions | Python 3.12 | 125 passed |
| Home Assistant integration | HA 2024.1.0 / Python 3.11 | 99 passed, 3 skipped |
| Home Assistant integration | HA 2026.10.4 / Python 3.14 | 102 passed |

These are controlled offline checks. They exercise actual Home Assistant behavior against fake Haven responses; they do not certify live-provider quotas or physical-light behavior. The declared Home Assistant minimum is 2024.1.0; the framework matrix tests the two exact releases above.

The three minimum-version skips cover concurrent single-entity toggle and relative-brightness ordering, including a white selection followed by toggle: HA 2024.1 bypasses its platform semaphore on that path, a pre-existing framework limitation this release does not fix. All three ordering tests run and pass on HA 2026.10.4. All preset, effect-off, empty-response, action-diagnostic, and external-reconciliation tests run on both versions. The tests verify final backend state and absence of poll-generated command loops; they do not observe the browser control marker or a physical Haven device.

### Unit and contract tests

Run from the repository root with Python 3.12 or later:

```sh
python -m pip install -r tests/requirements.txt
python -m unittest discover -s tests -v
```

No Haven credentials are needed. Tests cover transport behavior, request concurrency, malformed responses, immutable state, command batching, and registry policy. Adapter and registry contract tests use explicitly labeled doubles. Release changes must keep `havenlighting/version.py` and `manifest.json` in sync; the transport tests check this agreement.

### Real Home Assistant tests

Use separate environments for the two framework versions. For **Home Assistant 2026.10.4**, use Python 3.14.2 or later:

```sh
python -m pip install -r tests/requirements-ha-current.txt
python -m pytest tests/ha -v
```

For **Home Assistant 2024.1.0**, use Python 3.11:

```sh
python -m pip install uv==0.13.5
uv pip install --python python --override tests/requirements-ha-min-overrides.txt -r tests/requirements-ha-min.txt
python -m pytest tests/ha -v
```

The minimum-version fixture package was generated for Home Assistant 2024.1.2. The override deliberately installs **2024.1.0 itself** to test the declared compatibility floor. Framework tests use real registries, setup/unload and reauthentication flows, coordinator scheduling, debouncing, and light service handling. Network sockets and live Requests calls are blocked; only explicit fake Haven responses are allowed. [GitHub Actions](.github/workflows/tests.yml) runs the unit suite and both framework environments.

## Credits

- **Original integration:** [Mickey Schwab (@mickeyschwab)](https://github.com/mickeyschwab), [haven-hass](https://github.com/mickeyschwab/haven-hass).
- **Haven Lighting Redux and 2025 API rewrite:** [Stephen Crescenti (@screscenti)](https://github.com/screscenti), [hass-haven-lighting-redux](https://github.com/screscenti/hass-haven-lighting-redux).
- **Haven Lighting Part 3 maintenance and post-fork changes:** [Geoffrey Pinski (@pinskig)](https://github.com/pinskig), with **ChatGPT and Codex** assistance for analysis, implementation, review, testing, and documentation.
