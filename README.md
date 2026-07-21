# PentagonHub HA

Home Assistant custom integration for PentagonHub deploy, capture, and release
workflows.

HACS repository layout:

```text
custom_components/pentagonhub_ha/
```

## Role

- pair with the PentagonHub Core;
- report connection state through sensors and diagnostics;
- expose local diagnostics HTTP API;
- manage release storage under `/config/.pentagonhub_ha`;
- apply and export managed files;
- detect drift and create backups before apply.

The integration should not require git or GitHub credentials on Live HA. It
must not create containers or manage Cloudflare/Caddy routes.

## Installation

Add this repository to HACS as a custom integration repository:

```text
https://github.com/simprl/pentagonhub-ha
```

Then install `PentagonHub HA` from HACS and restart Home Assistant.

After restart, add the integration from:

```text
Settings -> Devices & services -> Add integration -> PentagonHub HA
```

By default the integration connects to:

```text
https://center.pentagonhub.com/api
```

For development or managed installs, provide the Core API URL assigned by
PentagonHub Center or Worker.

## Current MVP Surface

Implemented now:

- config flow for HA-first device-code pairing;
- storage of the scoped PentagonHub installation token in the Home Assistant
  config entry;
- immediate and periodic heartbeat to PentagonHub Core;
- connection-state sensor;
- redacted diagnostics;
- local `/api/pentagonhub_ha/status` endpoint for authenticated HA users.

Deferred until the matching PentagonHub Core endpoint exists:

- outbound command WebSocket;
- release apply/export/rollback services and buttons.
