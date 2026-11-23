# ServiceUptimeAttestation

A reusable on-chain registry of service availability: anyone requests
a probe for a public service (a name, a URL, and an optional expected
content marker), validator consensus probes that URL independently,
once per validator, and agrees on a verdict — `UP`, `DEGRADED`, or
`DOWN`. Each agreed probe is stored with its verdict and HTTP status,
so other contracts and readers can check whether a service was
reachable at a given time, and how often it has been up, without
trusting a single prober or a single caller.

## Why consensus is needed

A single probe proves nothing about a service. The prober's own
network could be down, a load balancer could answer one request and
drop the next, or a page could serve a cached copy while the service
behind it is failing. Here the leader and each validator fetch the
same URL independently and the verdict is only written when they
agree. A service that flaps between probes produces no attestation
at all: disagreement is reported as a failed round, not papered over
with one side's answer.

The verdict is derived from facts every side can observe on its own
fetch — the HTTP status and whether the expected marker is present —
plus one judgement call, the model's reading of the page for incident
language. A non-200 status, or a missing marker, means `DOWN` without
asking the model anything: a page that answers 503, or that no longer
contains the text the service is supposed to serve, is not up. Only
when the page loads and the marker is there does the model classify
it as operational, degraded, or down, so a status page reporting an
incident is caught even though it answers 200.

## What it does

- `request_check` — request a probe for a service (e.g. `Example`
  at `https://example.com/` with marker `Example Domain`). The marker
  is optional: pass an empty string when any page that loads with
  HTTP 200 should be judged on its content alone. Returns the request
  id.
- `attest_uptime` — run validator consensus on a requested service
  and store the attestation: the verdict (`UP` / `DEGRADED` /
  `DOWN`), the HTTP status observed, whether the marker was found,
  and the verification time. An attestation is fresh for one hour.
- Views: `get_request`, `get_attestation`,
  `get_latest_attestation`, `service_status` (never reverts),
  `uptime_summary`, `list_requests`, `list_attestations`, `stats`.

Probes are point-in-time records: after one hour
(`VALIDITY_SECONDS = 3600`) an attestation is still readable as
history, but `service_status` reports `fresh: false` and a new probe
is needed for the current status. `uptime_summary` counts how many
probes for a service came back `UP`, `DEGRADED`, and `DOWN`, and the
uptime percentage counts only `UP` probes against the total — a
degraded probe never inflates the number.

## State design

| Field | Type | Purpose |
| --- | --- | --- |
| `requests` | `TreeMap[u256, UptimeRequest]` | Requests by id |
| `attestations` | `TreeMap[u256, UptimeAttestation]` | Attestations by id |
| `request_ids` / `attestation_ids` | `DynArray[u256]` | Insertion order for listing and for scanning back to the latest attestation of a service |
| `request_counter` / `attestation_counter` | `u256` | Monotonic ids |

`UptimeRequest` stores the service name and key (uppercased name),
the URL, the expected marker, the requester, the request time, and
the status (`REQUESTED` / `ATTESTED`). `UptimeAttestation` stores the
request id, the service name and key, the URL, the agreed verdict,
the HTTP status, whether the marker was found, the verification time,
and `valid_until`.

Two rules shape the design. First, a request is attested once: after
`attest_uptime` the request is `ATTESTED`, and a fresh probe needs a
new request — there is no path that overwrites an attestation, so
every stored probe stays auditable. Second, the service index is a
plain scan backwards over `attestation_ids` rather than a
`TreeMap[str, DynArray[u256]]` keyed by service: the
nested-collection form is an unproven construct in this runner, and
the scan is short in practice.

## How the equivalence check works

Attestation runs one consensus round over the same judge code
(`_judge`: fetch the URL with `gl.nondet.web.get`, decide the verdict
from the status and the marker in deterministic code, and only then
ask the model to classify the page with `gl.nondet.exec_prompt` and
`response_format="json"`). The round is
`gl.vm.run_nondet_unsafe(leader_fn, validator_fn)`.

**The leader and the validator each do the whole job.** Both sides
fetch the same URL, check the status and the marker, classify the
page, and derive the verdict. The stored verdict is the one both
sides agreed on.

**Agreement is the verdict, the status, and the marker.** The
validator requires the same verdict (`UP` / `DEGRADED` / `DOWN`), the
same HTTP status, and the same marker result on both sides, compared
with plain equality on the parsed values. There is no tolerance band:
a service either answered the same way for both sides or it did not,
and a service that flaps between probes must produce no attestation
rather than one side's answer.

**Some verdicts need no model at all.** A 5xx (or any non-200,
non-4xx) status is `DOWN` by itself, and a missing marker is `DOWN`
by itself — both decided in deterministic code before the model is
called. The model is only consulted for a page that loaded with the
marker present, to catch the case a status code cannot see: a status
page answering 200 while reporting an incident.

**A 4xx is a bad probe, not a down service.** When the URL answers
4xx the round fails with `[EXTERNAL]` and stores nothing: a missing
page, a bad path, or a rejected request says the probe URL is wrong,
and attesting `DOWN` for a typo would poison the service's record.
Fix the URL and request again.

**Leader failures.** If the leader fails, the validator re-runs the
judge and compares failure classes: `[EXPECTED]` and `[EXTERNAL]` must
match exactly, `[TRANSIENT]` only has to be transient on both sides,
and `[LLM_ERROR]` never agrees, so a bad model answer rotates the
leader instead of locking bad state in.

Storage is never read inside a nondet block: `attest_uptime` copies
the request to memory with `gl.storage.copy_to_memory` first and hands
the consensus round plain in-memory values. The leader and validator
functions never reference `self`.

No `strict_eq` is used on any LLM output anywhere in the contract:
the model classification is compared after validating it is one of
`operational` / `degraded` / `down`, and the verdict itself is
compared with plain equality on the parsed enum.

## Failure paths

| Prefix | Meaning | Examples | Caller should |
| --- | --- | --- | --- |
| `[EXPECTED]` | Bad input or invalid state | Empty or over-long service name, non-HTTPS URL, a URL with credentials or a port, an over-long marker or one with newlines, unknown request id, attesting an already-attested request | Fix the input; no retry |
| `[EXTERNAL]` | The outside world is at fault, deterministically | The service URL returned 4xx (page gone, moved, or access denied) | Fix the URL and request again; a dead URL cannot be probed |
| `[TRANSIENT]` | The outside world failed, possibly temporarily | The fetch itself failed (no answer from the URL) | Retry later; nothing was written |
| `[LLM_ERROR]` | The model answer was unusable | Non-JSON answer, a classification outside `operational` / `degraded` / `down`, a missing `status` key | Retry; the leader rotates, so a persistent failure means the page is the problem |

A failed `attest_uptime` writes nothing: the request stays
`REQUESTED` and the call can be retried.

## Methods

| Method | Access | Arguments | Returns |
| --- | --- | --- | --- |
| `request_check` | write | `service_name: str`, `url: str`, `expected_marker: str` (empty = no marker required) | `{request_id, status}` |
| `attest_uptime` | write | `request_id: u256` | `{attestation_id, request_id, service_name, verdict, http_status, valid_until, status}` |
| `get_request` | view | `request_id: u256` | Request dict |
| `get_attestation` | view | `attestation_id: u256` | Attestation dict |
| `get_latest_attestation` | view | `service_name: str` | Latest attestation for the service |
| `service_status` | view | `service_name: str` | `{service_name, verdict, fresh, attestation_id, checked_at, valid_until}` — never reverts |
| `uptime_summary` | view | `service_name: str` | `{service_name, checks, up, degraded, down, uptime_pct}` |
| `list_requests` | view | — | `{requests: [...]}` |
| `list_attestations` | view | — | `{attestations: [...]}` |
| `stats` | view | — | `{requests, attestations, fresh_attestations}` |

Notes on arguments: service names are stripped and internal
whitespace is collapsed (up to 80 characters); the lookup key is the
uppercased name, so the same service spelled two ways still finds
the same attestations. URLs must be HTTPS with a dotted hostname, no
credentials, no explicit port. The marker is plain text up to 200
characters with no newlines; it is checked against the full fetched
page, while the model only reads the first 6000 characters.

## Deploying on StudioNet

Prerequisites: a GenLayer Studio account and the Studio app at
https://studio.genlayer.com/contracts.

1. Open Studio, switch to the StudioNet network, and create a new
   contract from `contracts/ServiceUptimeAttestation.py` (paste the file
   or upload it).
2. Deploy with **no constructor arguments** — `__init__` takes none.
   The first line of the file pins the runner:
   `# { "Depends": "py-genlayer:1jb45aa8ynh2a9c9xn3b7qqh8sm5q93hwfp7jqmwsfhh8jpz09h6" }`.
   That is the hash the official project boilerplate pins
   (`genlayer-project-boilerplate`, `contracts/football_bets.py`), verified
   against the boilerplate on 2026-11-13. Keep the blank line after it:
   the runner parses the leading `#` lines as one JSON document, and a
   comment glued directly under the Depends line breaks deployment.
3. Note the deployed contract address. The full step-by-step checklist,
   including the exact calls to run after deploy and what each should
   return, is in `DEPLOY_NOTES.md`.

CLI alternative (same network, same file):

```bash
genlayer deploy --contract contracts/ServiceUptimeAttestation.py
```

## Reading attestations from another contract

Use a typed interface for the methods you need. The interface is only
for type safety; at runtime it is the same as `gl.get_contract_at`.

```python
from genlayer import *


@gl.contract_interface
class ServiceUptimeAttestationReader:
    @gl.public.view
    def service_status(self, service_name: str) -> dict: ...

    @gl.public.view
    def uptime_summary(self, service_name: str) -> dict: ...


# ... inside a consumer contract, with the registry address in storage:
registry = gl.get_contract_at(ServiceUptimeAttestationReader, self.registry)
status = registry.service_status("Example")
# status["verdict"] is "UP", "DEGRADED", "DOWN", or "" when the
# service has no attestation yet; status["fresh"] says whether the
# latest probe is within the one-hour window.
summary = registry.uptime_summary("Example")
# summary["uptime_pct"] counts only UP probes against the total.
```

Two things to keep in mind when composing. First, cross-contract calls
are asynchronous: the child call is created when the parent transaction
reaches `on='finalized'` by default, so do not write a consumer that
expects the verdict back in the same synchronous step — design around
the message flow (for example, read the status in a view first, then
act on it in a separate call). Second, `service_status` never reverts:
a service with no attestation returns an empty verdict and
`fresh: false`, which is a definitive answer that nothing is known
yet, not an error. A stale attestation (`fresh: false`) is a
definitive answer that the probe is old, not an error — only
`get_latest_attestation` raises, when the service has no attestation
at all. Consumers that need a current status should check `fresh`
and request a new probe when it is `false`.

## Verifying after deploy

Run each write method once on Studio and check the result against
`DEPLOY_NOTES.md`. In short: request a probe for a stable public
service, read it back with `get_request`; attest it and confirm
`get_attestation` shows verdict `UP`, HTTP status 200, the marker
found, and `valid_until` one hour after `verified_at`; confirm
`service_status` returns `fresh: true` for the service and `false`
for a service with no attestation; request a second service (a status
page) and a probe with a marker that does not match, and confirm the
second attests `UP` while the mismatched marker attests `DOWN`;
confirm `uptime_summary` counts the probes and the percentage only
counts `UP`.

## Tests

Direct tests (mocked web + LLM, no network) live in
`tests/direct/test_service_uptime_attestation.py` and follow the official
`genlayer-test` pattern (`direct_vm` / `direct_deploy` fixtures). They
cover request validation and service-name normalization, a successful
`UP` attestation with the one-hour validity window, a `DEGRADED`
attestation from a page reporting partial problems, a `DOWN`
attestation from a missing marker (decided without the model) and
from a 5xx status, a 4xx probe failing with `[EXTERNAL]`, a failed
fetch failing with `[TRANSIENT]`, malformed model classifications
(`[LLM_ERROR]`), validator agreement and disagreement (a different
verdict disagrees, a service flapping to 503 disagrees, via mock
swapping between leader and replay), double attestation and unknown
ids, `service_status` for an unknown service and going stale via
warp, `uptime_summary` counting verdicts (2 of 4 `UP` is 50%) and for
an empty service, services being independent of each other, and all
views.

```bash
./.venv/bin/python -m pytest tests/direct -q
./.venv/bin/genvm-lint check contracts/ServiceUptimeAttestation.py
```

## Limitations

- **Attestations are snapshots, not a live feed.** A probe is only as
  current as its `verified_at`. Consumers must check `fresh` and
  request a new probe when the window has passed; the contract never
  probes by itself.
- **One URL per probe, one verdict per round.** A service with several
  endpoints needs one request per endpoint; the contract has no notion
  of "the service is up if any endpoint answers". Probe the endpoint
  that matters and name the service accordingly.
- **The marker is a substring, not a parser.** A marker that appears
  anywhere in the page counts as found, including inside an error
  message or a comment. Pick a marker that only the working service
  serves — a distinctive heading or product name, not a generic word.
- **The model only reads the first 6000 characters.** An incident
  banner buried deep in a long status page may be missed; the verdict
  then rests on the HTTP status and the marker alone. Status pages
  that put the current status near the top work best.
- **Runner version.** The contract pins the boilerplate's runner hash
  (`1jb45aa8...`, rc7-era APIs such as `gl.vm.run_nondet_unsafe` and
  `gl.get_contract_at`). The SDK's current docs describe a newer runner
  with renamed APIs (`run_nondet`, `gl.contract.get_at`, and others);
  moving to it requires updating the consensus calls, not just the hash.
