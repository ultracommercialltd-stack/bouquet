# Hermes Puppet Master integration

This directory adds a guarded, API-only NationStates operator to the Bouquet fork.

## Architecture

- **Bouquet userscript:** browser-side, human-initiated executor for NationStates HTML actions that must remain manual under the Script Rules.
- **Hermes native plugin:** headless autonomous boundary for official API reads and `ping` keepalive.
- **Policy gate:** private API commands that mutate nation state are disabled by default. HTML restricted actions are classified as manual-only; prohibited HTML actions are denied.

The integration is based on Bouquet's action taxonomy, request identification, one-request-at-a-time discipline, and rate-header handling. It does not run Bouquet's DOM/cookie transport on the server.

## Secret handling

`credentials.json` is mode `0600`. The model never receives passwords, X-Autologin, or X-Pin values. The Hermes instance has terminal, file, browser, web, and code-execution toolsets disabled.

Add the main nation in manual mode:

```bash
python3 -m hermes.ns_puppetmaster.onboard "United Paradisia"
```

Add an autonomous puppet with an explicit profile:

```bash
python3 -m hermes.ns_puppetmaster.onboard "Puppet Name" \
  --autonomous-issues \
  --decision-profile preserve
```

Profiles are `preserve`, `mirror_united_paradisia`, and `census_target`. The main nation is always forced to manual issue approval even if its roster entry is misconfigured.

For a census target, add `--census-target "Civil Rights"` (or another explicit World Census statistic). Census-target execution fails closed when the statistic is missing.

The command prompts through `getpass`, exchanges the password for `X-Autologin` using the official `q=ping` private shard, and never stores the password or X-Pin.

## Tests

```bash
python3 -m unittest discover -s hermes/tests -v
```

## Compliance sources

- NationStates API documentation: https://www.nationstates.net/pages/api.html
- NationStates HTML Script Rules: https://forum.nationstates.net/viewtopic.php?p=16394966#p16394966
- Bouquet upstream: https://github.com/audreyreal/bouquet

Bouquet is GPLv3. This fork preserves the upstream license and attribution.
