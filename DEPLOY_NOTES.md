# Deploy notes — ServiceUptimeAttestation

## Contract

- File: `contracts/ServiceUptimeAttestation.py`
- Class: `ServiceUptimeAttestation`
- Runner (line 1, pinned): `py-genlayer:1jb45aa8ynh2a9c9xn3b7qqh8sm5q93hwfp7jqmwsfhh8jpz09h6`
  — the hash the official project boilerplate pins
  (`genlayer-project-boilerplate`, `contracts/football_bets.py`), verified
  against the boilerplate on 2026-11-13. Keep the blank line after it.
- `genvm-lint check` passes (3 checks) and validation passes: 10 methods
  (8 view, 2 write). Note: the linter reports a newer runner is available
  (`1zr6nqk...`); the contract targets the boilerplate's runner, and
  moving to a newer runner requires the renamed APIs, not just the hash —
  do not change the hash alone.
- Direct tests: 23 passed
  (`./.venv/bin/python -m pytest tests/direct -q`).

## Constructor

**No constructor arguments.** `__init__` takes none and only zeroes the
two counters. Deploy from Studio (https://studio.genlayer.com/contracts,
StudioNet network) or the CLI:

```bash
genlayer deploy --contract contracts/ServiceUptimeAttestation.py
```

No value needs to be sent with any call. Consensus rounds (fetching
the page + model calls) run on the leader and validators; Studio shows
the fee estimate before you confirm — if an `attest_uptime` fee looks
wrong, do not confirm; re-check the service URL first.

## Post-deploy checklist

Run these on Studio against the deployed address, in order.

### 1. `request_check` — happy path

Args:

- `service_name`: `Example`
- `url`: `https://example.com/`
- `expected_marker`: `Example Domain`

Expected: returns `{request_id: 1, status: "REQUESTED"}`. Then call
`get_request(1)` and confirm `service_name: "Example"`, `service_key:
"EXAMPLE"`, `url: "https://example.com/"`, `expected_marker:
"Example Domain"`, and `requester` is your address.

### 2. `request_check` — service name normalization

Args: `service_name`: `  example  `, `url`:
`https://example.com/`, `expected_marker`: `Example Domain`.

Expected: returns a new request id (2) with `service_name:
"example"` (whitespace collapsed) and the same `service_key`
(`EXAMPLE`) as request 1. A second request for the same service is
allowed; each request is attested independently.

### 3. `request_check` — bad input rejected

- `url`: `http://example.com/` (not HTTPS). Expected: reverts with
  `[EXPECTED]`.
- `url`: `https://example.com:8080/` (explicit port). Expected:
  reverts with `[EXPECTED]`.
- `expected_marker`: a marker with a newline in it. Expected: reverts
  with `[EXPECTED]`.

### 4. `attest_uptime` — happy path (UP)

Args: `request_id: 1`.

Expected: returns `{attestation_id: 1, request_id: 1, service_name:
"Example", verdict: "UP", http_status: 200, status: "ATTESTED"}`.
Then call `get_attestation(1)` and confirm: `verdict: "UP"`,
`http_status: 200`, `marker_found: true`, `valid_until` is exactly
one hour after `verified_at`. Call `get_request(1)` and confirm
`status: "ATTESTED"`. Call `service_status("Example")` and confirm
`verdict: "UP"`, `fresh: true`.

If it reverts with `[TRANSIENT]`, the fetch failed — wait and retry
the same call. If it reverts with `[LLM_ERROR]`, the model returned a
malformed classification — retry once.

### 5. `attest_uptime` — DOWN from a missing marker

1. `request_check("Example", "https://example.com/", "This Text Is Not On The Page")`
   → `{request_id: 3, ...}`.
2. `attest_uptime(3)`.

Expected: returns `{attestation_id: 2, verdict: "DOWN", http_status:
200, status: "ATTESTED"}`. The page loads but no longer serves the
expected content, so the verdict is `DOWN` — decided without the
model. `get_attestation(2)` confirms `marker_found: false`.

### 6. `attest_uptime` — already attested

Args: `request_id: 1`.

Expected: reverts with `[EXPECTED]` (`request 1 is already ATTESTED`).

### 7. Second service — a status page

1. Open `https://www.githubstatus.com/` in a browser first and check
   what it currently reports. If it shows all systems operational,
   expect `UP` below; if it reports an incident, expect `DEGRADED`
   (or `DOWN` for a major outage) — use what the page shows.
2. `request_check("GitHub Status", "https://www.githubstatus.com/", "")`
   → `{request_id: 4, ...}`.
3. `attest_uptime(4)` → expect `{attestation_id: 3, service_name:
   "GitHub Status", verdict: <as the page shows>, http_status: 200}`.
4. `get_latest_attestation("GitHub Status")` → attestation 3.
5. `get_latest_attestation("Example")` → still attestation 2 — the
   two services are independent (the Example service now has two
   probes: attestation 1 `UP` and attestation 2 `DOWN`).

### 8. `uptime_summary` and `service_status` — happy path

- `uptime_summary("Example")` → `{service_name: "Example", checks: 2,
  up: 1, degraded: 0, down: 1, uptime_pct: 50}` — only the `UP` probe
  counts toward uptime.
- `uptime_summary("GitHub Status")` → `{checks: 1, up: 1 (or
  degraded/down 1, matching step 7), uptime_pct: 100 (or 0)}`.
- `uptime_summary("Nobody")` → `{checks: 0, up: 0, degraded: 0, down:
  0, uptime_pct: 0}`.
- `service_status("Nobody")` → `{verdict: "", fresh: false,
  attestation_id: 0}` — never reverts.
- `get_latest_attestation("Nobody")` → reverts with `[EXPECTED]` (no
  attestation for that service).
- `get_request(999)` / `get_attestation(999)` → revert with
  `[EXPECTED]` (unknown id).

### 9. Views (read-only, no fee)

- `list_requests()` → all requests (4 after step 7).
- `list_attestations()` → all attestations (3 after step 7).
- `stats()` → `{requests: 4, attestations: 3, fresh_attestations: 3}`
  (numbers grow if you ran extra requests; `fresh_attestations` drops
  to 0 one hour after the last attestation).

## If something looks wrong

- Deploy rejected with `invalid_contract`: the Depends line is the
  usual suspect — confirm line 1 is exactly the pinned hash and line 2
  is blank.
- `attest_uptime` reverts `[LLM_ERROR]`: the model returned something
  outside the expected shape. Retry once; if it persists, the page may
  be too long or too ambiguous — the model only reads the first 6000
  characters, so a status page that buries its status deep in the
  markup may need a shorter URL form.
- `attest_uptime` reverts `[EXTERNAL]` with an HTTP 4xx: the probe URL
  is wrong (page gone, moved, or access denied). Fix the URL and
  request again — a 4xx is a bad probe, not a down service.
- `attest_uptime` reverts `[TRANSIENT]`: the fetch itself failed.
  Wait and retry the same call.
- `attest_uptime` hangs or times out on Studio: the consensus round is
  waiting on validators. Wait for the Studio timeout message; nothing
  was written. Retry.
- The verdict is `DOWN` when the service looks up: check
  `get_attestation` — if `http_status` is 200 and `marker_found` is
  false, the marker is the cause (the page changed); if the page
  itself reports an incident, the model classification is the cause
  and the verdict is correct. Pick a marker that only the working
  service serves.
- The verdict flips between probes: the service is flapping, or a
  load balancer answers inconsistently. A round where leader and
  validator disagree stores nothing — that is the contract working
  as designed, not a bug. Wait for the service to settle and probe
  again.
