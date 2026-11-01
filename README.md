# Haven Lighting Part 3

A Home Assistant integration for Haven Lighting cloud-connected landscape lighting, maintained by **[Geoffrey Pinski (@pinskig)](https://github.com/pinskig)** with development and review assistance from **ChatGPT and Codex**.

This project was forked from [Stephen Crescenti's Haven Lighting Redux](https://github.com/screscenti/hass-haven-lighting-redux) on **April 19, 2026**. Redux supplied the 2025 Haven API rewrite and built on [Mickey Schwab's original integration](https://github.com/mickeyschwab/haven-hass). Part 3 builds on that foundation with shared polling, account-wide request protection, safer state and device handling, and automated regression coverage. The [development history](#ai-assisted-development-since-the-fork) records the changes made since the fork.

**Current integration version: 1.0.11** · **Home Assistant: 2024.1.0 or later** · **Installation: HACS custom repository**

[Features](#features-and-current-scope) · [Installation](#installation-and-configuration) · [Request handling](#polling-and-request-handling) · [Automations](#automations) · [Troubleshooting](#troubleshooting) · [Change history](#ai-assisted-development-since-the-fork) · [Development tests](#development-tests)

## Features and current scope

The integration discovers the Haven account's **default location** and exposes its zones and Haven app groups as Home Assistant light entities. It uses cloud polling; a working Haven account and internet connection are required.

| Capability | Behavior |
| --- | --- |
| Zones and groups | Control zones and groups separately, including targets whose numeric IDs overlap. Individual fixtures outside those zone/group records are not exposed separately. |
| Power | Turn zones and groups on or off. |
| Brightness | Home Assistant brightness is mapped to **10 nonzero Haven levels**; zero turns the target off. |
| RGB color | RGB input selects the nearest of **17 configured Haven color presets**. |
| White temperature | Selects the nearest supported preset: **2700, 3000, 3500, 3700, 4000, 4100, 4700, or 5000 K**. |
| Effects | **Fire, Sunset, and Ocean**, with brightness reported while an effect is active. |
| Inventory changes | New zones/groups appear on later refreshes, including after an initially empty inventory. Missing targets retain their registry entries and become unavailable. |
| State reporting | Validated snapshots report power, brightness, RGB/white mode, and effects. Unchanged polls avoid redundant Home Assistant state writes. |

Discovery currently uses the account's default location; it does not enumerate every location or provide a location selector. The brightness and color mappings above describe this integration's API controls, rather than continuous dimming or arbitrary RGB output.

## Installation and configuration

1. Open **HACS** in Home Assistant and open **Custom repositories**.
2. Add `https://github.com/pinskig/hass-haven-lighting-part3` with category **Integration**.
3. Download **Haven Lighting Part 3**, then restart Home Assistant.
4. Go to **Settings → Devices & Services → Add Integration**.
5. Search for **Haven Lighting Part 3** and enter your Haven account email and password.
6. After setup, zones and groups from the default location appear as light entities.

Setup prevents duplicate active entries for the same normalized email address. If credentials expire, use the integration's reauthentication prompt to update them.

### Updating an existing installation

Download or redownload the latest repository version in HACS, then restart Home Assistant. The current manifest version is **1.0.11**.

The registry migration introduced in 1.0.7 gives targets identifiers that distinguish their account, location, and zone/group type. When a legacy target's identity can be established, migration preserves its entity ID, device ID, custom names, and area assignments. Existing automations retain their references for successfully migrated targets. Migration also handles a legacy target that first reappears after setup.

Ambiguous legacy records are retained and logged for review. The integration does not guess which current target they represent; a newly discovered target may receive a separate entry. Check affected automation references when resolving an ambiguous record. Ownership conflicts with another integration entry are reported explicitly.

Version 1.0.7 began saving provider cooldowns to disk. The first upgrade from an earlier version cannot reconstruct a cooldown that existed only in the previous process's memory. Once saved, deadlines survive integration reloads and Home Assistant restarts.

## Polling and request handling

### Shared updates

One Home Assistant coordinator serves all entities for a discovered location. Background refreshes normally run about **once every 60 seconds**. A healthy refresh makes **two GET requests**, retrieving the zone list and group list. Overlapping refreshes share the result instead of independently polling for every entity.

Successful commands update the addressed target's cached state and request a shared refresh after a **five-second debounce window**. That refresh lets related zones and groups catch up with the server. Network delays and provider cooldowns can extend the wait. Polling continues while lights are off or the inventory is empty so changes can still be discovered. Authentication, discovery, commands, and post-command refreshes add traffic beyond the normal background polling.

Light commands accept an empty successful HTTP response as an acknowledgment, including HTTP 200 and 202. Acknowledgment updates the cached state; it does not prove the physical lights have finished changing. The scheduled inventory refresh reconciles the state reported by Haven. Nonempty malformed responses remain errors, and authentication and inventory requests still require valid response data.

Both inventory responses are validated before a new snapshot is published. A failed or malformed refresh preserves the previous snapshot and reports an update failure. Command batches cannot interleave with location polling. If a later command fails after an earlier command succeeded, the confirmed changes are retained and the failure is reported; a batch is not rolled back.

If a failed command leaves the cached state unchanged, the existing refresh schedule stays in place. Repeated failures therefore do not keep postponing that state check. These scheduled reads still respect the account's provider cooldown.

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

Use `light.turn_on` with fields such as `rgb_color`, `brightness`, or `color_temp_kelvin`. For example, this action selects the nearest supported blue preset and brightness level:

```yaml
service: light.turn_on
target:
  entity_id: light.front_yard
data:
  rgb_color: [0, 0, 255]
  brightness: 128
```

Replace `light.front_yard` with your entity ID. To select an effect, use `effect: Ocean`, `effect: Fire`, or `effect: Sunset`. To stop an active effect, use `effect: "off"`; without a replacement RGB color or white temperature, this selects **2700 K white**. It does not restore the previous color or turn the light off.

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
| Color or brightness differs from the requested value | Check the preset and brightness mappings in the feature table. |
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

Version 1.0.8 adds records such as:

```text
Haven HTTP attempt: method=GET endpoint=/LightAndZones/OrderedList status=200 elapsed=0.250s
```

Each record represents an actual HTTP transport attempt and includes its method, fixed endpoint-family label, status or transport-failure marker, and elapsed time. Timing excludes waiting for serialization or pacing. These records omit credentials, tokens, headers, bodies, target IDs, and query values. A locally rejected cooldown call produces no HTTP-attempt record. A transport failure does not establish that the request reached Haven.

The API defaults are centralized in [`havenlighting/config.py`](custom_components/haven/havenlighting/config.py). Both remain on the inherited `https://stg-api.havenlighting.com/api` host. The client reuses a Requests session with a **five-second connection timeout** and **thirty-second read-inactivity timeout**; these are not a total wall-clock deadline. The cooldown storage file contains a hashed account key and budget metadata, without passwords or access tokens. Account credentials are managed separately by the Home Assistant configuration entry.

For an unresolved problem, [open an issue](https://github.com/pinskig/hass-haven-lighting-part3/issues) with the integration and Home Assistant versions, the action that failed, and relevant sanitized logs.

## AI-assisted development since the fork

Geoffrey Pinski's work on this fork has used **ChatGPT and Codex** for code analysis, implementation, review, regression tests, and documentation. The merged early pull requests link to their Codex tasks. This section records the full merged post-fork history through 1.0.11, including earlier approaches that later releases replaced.

The upstream **1.0.5** baseline already supplied zone/group controls, brightness and color mappings, effect commands, token renewal, HACS installation, the 2025 API rewrite, staging endpoints, and session device-ID generation. Those are credited to the original and Redux projects. Part 3's changes are detailed below. Dates use **America/New_York** time; versions 1.0.6–1.0.11 all landed on October 31 locally, although GitHub's UTC dates cross into November 1.

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

- Updated device-registry iteration for newer Home Assistant versions, removing the mapping deprecation warning while retaining compatibility with the older registry API.
- Preserved the global check for devices owned by another configuration entry. Added real Home Assistant regressions for conflicting controller and zone identities, including preservation of the existing owner's custom name and area.
- Kept pending reconciliation and ordinary polling timers when a failed command leaves the cached state unchanged. Previously, republishing the unchanged snapshot canceled a pending command refresh and restarted the polling interval, which could leave the displayed state stale during repeated failures. Confirmed partial successes are still published.
- Added real Home Assistant regressions for a successful command followed by a malformed command reply, and for preserving provider cooldowns when that scheduled refresh runs. The fix does not replay commands or create an additional refresh timer.

## Development tests

Version **1.0.11** was verified locally in all three environments below. [GitHub Actions](https://github.com/pinskig/hass-haven-lighting-part3/actions/workflows/tests.yml) runs the same matrix on pushes and pull requests:

| Suite | Environment | Result |
| --- | --- | --- |
| Client and adapter regressions | Python 3.12 | 104 passed |
| Home Assistant integration | HA 2024.1.0 / Python 3.11 | 41 passed |
| Home Assistant integration | HA 2026.10.4 / Python 3.14 | 41 passed |

These are controlled offline checks. They exercise actual Home Assistant behavior against fake Haven responses; they do not certify live-provider quotas or physical-light behavior. The declared Home Assistant minimum is 2024.1.0; the framework matrix tests the two exact releases above.

### Unit and contract tests

Run from the repository root with Python 3.12 or later:

```sh
python -m pip install -r tests/requirements.txt
python -m unittest discover -s tests -v
```

No Haven credentials are needed. Tests cover transport behavior, request concurrency, malformed responses, immutable state, command batching, and registry policy. Adapter and registry contract tests use explicitly labeled doubles.

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
