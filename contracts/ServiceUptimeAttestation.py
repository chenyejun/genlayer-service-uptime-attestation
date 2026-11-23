# { "Depends": "py-genlayer:1jb45aa8ynh2a9c9xn3b7qqh8sm5q93hwfp7jqmwsfhh8jpz09h6" }

"""ServiceUptimeAttestation — a service availability attestation registry.

Anyone requests an availability check for a public service (a name, a
URL, and an optional expected content marker) and validator consensus
probes that URL independently, once per validator, and agrees on a
verdict: UP, DEGRADED, or DOWN. Each agreed probe is stored with its
verdict and HTTP status, so other contracts and readers can check
whether a service was reachable at a given time — and how often it has
been up — without trusting a single prober or a single caller.

Why consensus: a single probe proves nothing about a service. The
prober's own network could be down, a load balancer could answer one
request and drop the next, or a page could serve a cached copy while
the service behind it is failing. Here the leader and each validator
fetch the same URL independently and the verdict is only written when
they agree. A service that flaps between probes produces no
attestation at all, which is the honest outcome: disagreement is
reported as a failed round, not papered over with one side's answer.

What each side agrees on, and how: the verdict is derived from facts
every side can observe on its own fetch — the HTTP status and whether
the expected marker is present — plus one judgement call, the model's
reading of the page for incident language. A non-200 status, or a
missing marker, means DOWN without asking the model anything: a page
that answers 503, or that no longer contains the text the service is
supposed to serve, is not up. Only when the page loads and the marker
is there does the model classify it as operational, degraded, or down,
so a status page reporting an incident is caught even though it
answers 200. The leader and validator verdicts must match by plain
equality on the parsed verdict — the verdict is a judgement call, so
it is never put through strict_eq, and no strict_eq is used on any
model output anywhere in this contract.

Probes are point-in-time records, and an attestation is fresh for one
hour after verification — after that it stays readable as history but
a new probe is needed for the current status. The uptime summary is
derived by deterministic code from the stored probes: it counts how
many probes for a service came back UP, DEGRADED, and DOWN, and the
uptime percentage counts only UP probes against the total, so a
degraded probe never inflates the number.
"""

import json
from dataclasses import dataclass
from datetime import datetime, timezone

from genlayer import *


# Error prefixes. Failures are raised as gl.vm.UserError with one of
# these so the leader and the validators can tell deterministic failures
# apart from transient ones: [EXPECTED] and [EXTERNAL] must match
# exactly, [TRANSIENT] only has to be transient on both sides, and
# [LLM_ERROR] never agrees, so a bad model answer rotates the leader
# instead of locking bad state in.
ERR_EXPECTED = "[EXPECTED]"
ERR_EXTERNAL = "[EXTERNAL]"
ERR_TRANSIENT = "[TRANSIENT]"
ERR_LLM = "[LLM_ERROR]"


# Bounds. A contract that reads the open web needs limits that do not
# depend on the pages behaving: every transaction has a compute budget,
# and the leader's return value is written to the chain.
MAX_SERVICE_NAME_CHARS = 80
MAX_URL_CHARS = 500
MAX_MARKER_CHARS = 200
MAX_PAGE_CHARS = 6000

# An attestation is fresh for one hour after verification.
VALIDITY_SECONDS = u256(3600)

STATUS_REQUESTED = "REQUESTED"
STATUS_ATTESTED = "ATTESTED"

VERDICT_UP = "UP"
VERDICT_DEGRADED = "DEGRADED"
VERDICT_DOWN = "DOWN"

PAGE_OPERATIONAL = "operational"
PAGE_DEGRADED = "degraded"
PAGE_DOWN = "down"


@allow_storage
@dataclass
class UptimeRequest:
    request_id: u256
    service_name: str
    # Lookup key: the service name uppercased and stripped, so the
    # same service spelled two ways does not create two registries.
    service_key: str
    url: str
    # Expected content marker. Empty means no marker is required: any
    # page that loads with HTTP 200 is judged on its content alone.
    expected_marker: str
    requester: Address
    requested_at: u256
    status: str


@allow_storage
@dataclass
class UptimeAttestation:
    attestation_id: u256
    request_id: u256
    service_name: str
    service_key: str
    url: str
    # Agreed verdict: UP / DEGRADED / DOWN.
    verdict: str
    # HTTP status observed by the consensus round.
    http_status: u256
    # Whether the expected marker was present. False when no marker
    # was required.
    marker_found: bool
    verified_by: Address
    verified_at: u256
    valid_until: u256


def _fail(prefix: str, detail: str) -> None:
    raise gl.vm.UserError(prefix + " " + detail)


def _now_seconds() -> u256:
    """Current transaction time as unix seconds.

    The GenVM wires the standard library clock to the transaction
    datetime, so every validator observes the same value.
    """
    return u256(int(datetime.now(timezone.utc).timestamp()))


def _as_u256(value, label: str) -> u256:
    """Coerce one numeric argument to u256.

    Studio's write form serializes numbers as JSON strings, and the
    pinned py-genlayer's u256() is a pass-through (typing.NewType), so
    it does not convert them. int() normalizes both forms (a no-op on
    values that are already ints). A string that is not a whole number
    raises a clean [EXPECTED] revert here instead of letting a bare
    ValueError escape the contract.
    """
    try:
        return u256(int(value))
    except (TypeError, ValueError):
        raise gl.vm.UserError("[EXPECTED] " + label + " must be a whole number")


def _clean_service_name(service_name: str) -> str:
    """Validate and normalize a service name: stripped, internal
    whitespace collapsed, 1-80 characters."""
    text = " ".join(str(service_name).split())
    if len(text) == 0:
        _fail(ERR_EXPECTED, "service name must not be empty")
    if len(text) > MAX_SERVICE_NAME_CHARS:
        _fail(ERR_EXPECTED, "service name must be " + str(MAX_SERVICE_NAME_CHARS) + " characters or fewer")
    return text


def _clean_url(url: str) -> str:
    """Validate the probed URL. HTTPS only, no credentials, no
    explicit port, no whitespace, and a dotted DNS hostname (never an
    IP literal). URL syntax alone never makes a service trustworthy —
    that is the requester's choice, and the attestation records exactly
    which URL was probed."""
    text = str(url).strip()
    if len(text) == 0:
        _fail(ERR_EXPECTED, "service URL must not be empty")
    if len(text) > MAX_URL_CHARS:
        _fail(ERR_EXPECTED, "service URL must be " + str(MAX_URL_CHARS) + " characters or fewer")
    if not text.startswith("https://"):
        _fail(ERR_EXPECTED, "service URL must start with https://: " + text)
    if text != "".join(text.split()):
        _fail(ERR_EXPECTED, "service URL must not contain whitespace: " + text)
    rest = text[len("https://"):]
    authority_end = len(rest)
    for separator in ("/", "?", "#"):
        found = rest.find(separator)
        if found >= 0 and found < authority_end:
            authority_end = found
    authority = rest[:authority_end]
    if len(authority) == 0:
        _fail(ERR_EXPECTED, "service URL has no host: " + text)
    if "@" in authority:
        _fail(ERR_EXPECTED, "service URL must not contain credentials: " + text)
    if ":" in authority:
        _fail(ERR_EXPECTED, "service URL must not contain a port: " + text)
    host = authority.lower()
    if "." not in host:
        _fail(ERR_EXPECTED, "service URL host must be a dotted domain: " + text)
    return text


def _clean_marker(marker: str) -> str:
    """Validate the expected content marker. Empty means no marker is
    required. A marker must be plain text: no newlines, so a stored
    marker can never be confused with a field separator."""
    text = str(marker)
    if len(text) > MAX_MARKER_CHARS:
        _fail(ERR_EXPECTED, "expected marker must be " + str(MAX_MARKER_CHARS) + " characters or fewer")
    if "\n" in text or "\r" in text:
        _fail(ERR_EXPECTED, "expected marker must not contain newlines")
    return text


def _service_key(service_name: str) -> str:
    return service_name.upper()


def _error_text(exc) -> str:
    """Read the message out of a UserError / Result, across the runner
    versions that name the payload `.data` or `.message`."""
    for attr in ("data", "message"):
        value = getattr(exc, attr, None)
        if value is not None:
            return str(value)
    return str(exc)


def _leader_error_is_agreeable(leaders_res, judge) -> bool:
    """Decide whether to agree with a leader that failed. Deterministic
    failures (a bad argument, a 4xx) must match exactly. A transient
    failure only has to be transient on both sides. An LLM failure
    always disagrees, so consensus rotates rather than locking bad
    state."""
    leader_msg = _error_text(leaders_res)
    try:
        judge()
    except gl.vm.UserError as exc:
        validator_msg = _error_text(exc)
        if validator_msg.startswith(ERR_EXPECTED) or validator_msg.startswith(ERR_EXTERNAL):
            return validator_msg == leader_msg
        if validator_msg.startswith(ERR_TRANSIENT) and leader_msg.startswith(ERR_TRANSIENT):
            return True
        return False
    except Exception:
        return False
    # The leader failed where this validator succeeded: disagree.
    return False


def _classify_page(page_text: str, url: str) -> str:
    """Ask the model whether the fetched page reports an incident.

    Returns one of "operational", "degraded", "down". Anything else
    is an [LLM_ERROR]: a model that cannot answer in the required
    shape is not a model whose reading can be stored.
    """
    prompt = (
        "You are reading a web page fetched from a public service. "
        "Answer with JSON only, using exactly one key:\n"
        '- "status": one of "operational", "degraded", "down"\n\n'
        "Rules:\n"
        '- Answer "operational" unless the page clearly reports that '
        "the service is having an outage, degraded performance, "
        "elevated errors, or maintenance affecting the service. A "
        "normal homepage, documentation page, or status page showing "
        "all systems operational is \"operational\".\n"
        '- Answer "degraded" when the page reports partial problems: '
        "some features degraded, elevated errors, or an incident that "
        "affects only part of the service.\n"
        '- Answer "down" only when the page reports a major outage or '
        "that the service is unavailable.\n"
        "- Read the page itself. Do not use outside knowledge and do "
        "not guess."
    )
    prompt += "\n\nURL: " + url
    prompt += "\n\nPage:\n" + page_text[:MAX_PAGE_CHARS]
    try:
        answer = gl.nondet.exec_prompt(prompt, response_format="json")
    except Exception as exc:
        _fail(ERR_LLM, "model call failed: " + str(exc))
    if not isinstance(answer, dict):
        _fail(ERR_LLM, "model answer was not a JSON object")
    status = answer.get("status")
    if status not in (PAGE_OPERATIONAL, PAGE_DEGRADED, PAGE_DOWN):
        _fail(ERR_LLM, "model status must be operational, degraded or down")
    return status


# --------------------------------------------------------------------------
# Consensus worker (module level: it takes plain values, touches no
# storage, and is shared by leader and validators, so both sides run
# the same code).
# --------------------------------------------------------------------------
def _judge(service_name: str, url: str, expected_marker: str) -> dict:
    """Probe the service URL once, and return the judgement: the
    verdict, the HTTP status, and whether the marker was found.
    Raises a prefixed gl.vm.UserError on failure."""
    try:
        resp = gl.nondet.web.get(url)
    except Exception:
        _fail(ERR_TRANSIENT, "could not fetch service URL " + url)
    status = resp.status
    if 400 <= status < 500:
        # A 4xx means the probe URL itself is wrong — a missing page, a
        # bad path, a rejected request. Nothing about the service can
        # be attested from it, so the round fails instead of storing a
        # DOWN verdict for a typo.
        _fail(ERR_EXTERNAL, "service URL returned HTTP " + str(status))
    marker_required = expected_marker != ""
    if status != 200:
        # A 5xx or any other non-200 answer is the service failing:
        # that is a fact worth attesting, and both sides observing the
        # same failing status agree on DOWN.
        return {
            "verdict": VERDICT_DOWN,
            "http_status": u256(status),
            "marker_found": False,
            "marker_required": marker_required,
        }
    raw = resp.body
    if raw is None:
        text = ""
    elif isinstance(raw, bytes):
        text = raw.decode("utf-8", "ignore")
    else:
        text = str(raw)
    marker_found = True
    if marker_required and expected_marker not in text:
        # The page loads but no longer serves the content the service
        # is supposed to serve. That is DOWN, decided here in
        # deterministic code, without asking the model.
        return {
            "verdict": VERDICT_DOWN,
            "http_status": u256(status),
            "marker_found": False,
            "marker_required": marker_required,
        }
    page_status = _classify_page(text, url)
    if page_status == PAGE_OPERATIONAL:
        verdict = VERDICT_UP
    elif page_status == PAGE_DEGRADED:
        verdict = VERDICT_DEGRADED
    else:
        verdict = VERDICT_DOWN
    return {
        "verdict": verdict,
        "http_status": u256(status),
        "marker_found": marker_found,
        "marker_required": marker_required,
    }


def _request_to_dict(req: UptimeRequest) -> dict:
    return {
        "request_id": req.request_id,
        "service_name": req.service_name,
        "service_key": req.service_key,
        "url": req.url,
        "expected_marker": req.expected_marker,
        "requester": req.requester.as_hex,
        "requested_at": req.requested_at,
        "status": req.status,
        "found": True,
    }


def _attestation_to_dict(att: UptimeAttestation) -> dict:
    return {
        "attestation_id": att.attestation_id,
        "request_id": att.request_id,
        "service_name": att.service_name,
        "service_key": att.service_key,
        "url": att.url,
        "verdict": att.verdict,
        "http_status": att.http_status,
        "marker_found": att.marker_found,
        "verified_by": att.verified_by.as_hex,
        "verified_at": att.verified_at,
        "valid_until": att.valid_until,
        "found": True,
    }


class ServiceUptimeAttestation(gl.Contract):
    """Registry of consensus-attested service availability probes.

    Requests are made once per attestation; a service can be requested
    again at any time to add a newer probe. Attestations are never
    deleted: an old probe stays readable as history, and
    get_latest_attestation always returns the newest one for the
    service.
    """

    requests: TreeMap[u256, UptimeRequest]
    attestations: TreeMap[u256, UptimeAttestation]
    request_ids: DynArray[u256]
    attestation_ids: DynArray[u256]
    request_counter: u256
    attestation_counter: u256

    def __init__(self):
        self.request_counter = u256(0)
        self.attestation_counter = u256(0)

    # -- consensus ------------------------------------------------------
    # One round with gl.vm.run_nondet_unsafe. The leader and each
    # validator re-run _judge and their verdicts must match by plain
    # equality, together with the HTTP status and the marker result.
    # The verdict is a parsed enum compared in deterministic code,
    # never by strict_eq on model output: a byte-equality check on a
    # model classification would fail whenever two validators phrase
    # the same reading differently, and a service that flaps between
    # probes must produce no attestation rather than one side's
    # answer.
    def _attest_with_consensus(self, service_name: str, url: str, expected_marker: str) -> dict:
        def leader_fn() -> dict:
            return _judge(service_name, url, expected_marker)

        def validator_fn(leaders_res) -> bool:
            if not isinstance(leaders_res, gl.vm.Return):
                return _leader_error_is_agreeable(
                    leaders_res, lambda: _judge(service_name, url, expected_marker)
                )
            try:
                mine = _judge(service_name, url, expected_marker)
            except Exception:
                return False
            leader = leaders_res.calldata
            if not isinstance(leader, dict):
                return False
            leader_verdict = leader.get("verdict")
            if leader_verdict not in (VERDICT_UP, VERDICT_DEGRADED, VERDICT_DOWN):
                return False
            leader_status = leader.get("http_status")
            if (
                not isinstance(leader_status, int)
                or isinstance(leader_status, bool)
                or leader_status <= 0
            ):
                return False
            if mine["verdict"] != leader_verdict:
                return False
            if int(mine["http_status"]) != int(leader_status):
                return False
            if bool(mine["marker_found"]) != bool(leader.get("marker_found")):
                return False
            return True

        return gl.vm.run_nondet_unsafe(leader_fn, validator_fn)

    # -- public writes --------------------------------------------------
    @gl.public.write
    def request_check(self, service_name: str, url: str, expected_marker: str) -> dict:
        """Request an availability probe for a service. The expected
        marker is optional: pass an empty string when any page that
        loads with HTTP 200 should be judged on its content alone.
        Returns the new request id. Re-requesting a service is always
        allowed, even while a fresh attestation exists."""
        clean_name = _clean_service_name(service_name)
        clean_url = _clean_url(url)
        clean_marker = _clean_marker(expected_marker)
        self.request_counter = self.request_counter + u256(1)
        request_id = self.request_counter
        self.requests[request_id] = UptimeRequest(
            request_id=request_id,
            service_name=clean_name,
            service_key=_service_key(clean_name),
            url=clean_url,
            expected_marker=clean_marker,
            requester=gl.message.sender_address,
            requested_at=_now_seconds(),
            status=STATUS_REQUESTED,
        )
        self.request_ids.append(request_id)
        return {"request_id": request_id, "status": STATUS_REQUESTED}

    @gl.public.write
    def attest_uptime(self, request_id: u256) -> dict:
        """Run validator consensus on a requested service and store the
        attestation. Anyone may call this: the caller decides nothing,
        consensus does. One request is attested once; a fresh probe
        needs a new request."""
        request_id = _as_u256(request_id, "request id")
        if request_id not in self.requests:
            _fail(ERR_EXPECTED, "unknown request " + str(request_id))
        request = self.requests[request_id]
        if request.status != STATUS_REQUESTED:
            _fail(ERR_EXPECTED, "request " + str(request_id) + " is already " + request.status)
        # Storage cannot be read inside a nondet block: copy first,
        # then hand the consensus round plain in-memory values.
        request_mem = gl.storage.copy_to_memory(request)
        service_name = request_mem.service_name
        url = request_mem.url
        expected_marker = request_mem.expected_marker
        result = self._attest_with_consensus(service_name, url, expected_marker)
        verified_at = _now_seconds()
        self.attestation_counter = self.attestation_counter + u256(1)
        attestation_id = self.attestation_counter
        self.attestations[attestation_id] = UptimeAttestation(
            attestation_id=attestation_id,
            request_id=request_id,
            service_name=service_name,
            service_key=request_mem.service_key,
            url=url,
            verdict=result["verdict"],
            http_status=u256(int(result["http_status"])),
            marker_found=bool(result["marker_found"]),
            verified_by=gl.message.sender_address,
            verified_at=verified_at,
            valid_until=verified_at + VALIDITY_SECONDS,
        )
        self.attestation_ids.append(attestation_id)
        request.status = STATUS_ATTESTED
        self.requests[request_id] = request
        return {
            "attestation_id": attestation_id,
            "request_id": request_id,
            "service_name": service_name,
            "verdict": result["verdict"],
            "http_status": int(result["http_status"]),
            "valid_until": verified_at + VALIDITY_SECONDS,
            "status": STATUS_ATTESTED,
        }

    # -- public views ---------------------------------------------------
    @gl.public.view
    def get_request(self, request_id: u256) -> dict:
        request_id = _as_u256(request_id, "request id")
        if request_id not in self.requests:
            _fail(ERR_EXPECTED, "unknown request " + str(request_id))
        return _request_to_dict(self.requests[request_id])

    @gl.public.view
    def get_attestation(self, attestation_id: u256) -> dict:
        attestation_id = _as_u256(attestation_id, "attestation id")
        if attestation_id not in self.attestations:
            _fail(ERR_EXPECTED, "unknown attestation " + str(attestation_id))
        return _attestation_to_dict(self.attestations[attestation_id])

    @gl.public.view
    def get_latest_attestation(self, service_name: str) -> dict:
        """The attestation a reader should trust for a service: the
        newest probe. Raises when the service has none yet.
        Attestation ids grow with time, so scanning backwards finds
        the latest attestation for the service."""
        clean_name = _clean_service_name(service_name)
        key = _service_key(clean_name)
        index = len(self.attestation_ids) - 1
        while index >= 0:
            attestation = self.attestations[self.attestation_ids[index]]
            if attestation.service_key == key:
                return _attestation_to_dict(attestation)
            index = index - 1
        _fail(ERR_EXPECTED, "service " + clean_name + " has no attestation yet")

    @gl.public.view
    def service_status(self, service_name: str) -> dict:
        """Whether the latest probe for a service is still inside its
        validity window. Never reverts: a service with no attestation
        is simply not fresh."""
        clean_name = _clean_service_name(service_name)
        key = _service_key(clean_name)
        index = len(self.attestation_ids) - 1
        while index >= 0:
            latest = self.attestations[self.attestation_ids[index]]
            if latest.service_key == key:
                return {
                    "service_name": clean_name,
                    "verdict": latest.verdict,
                    "fresh": bool(_now_seconds() <= latest.valid_until),
                    "attestation_id": latest.attestation_id,
                    "checked_at": latest.verified_at,
                    "valid_until": latest.valid_until,
                }
            index = index - 1
        return {
            "service_name": clean_name,
            "verdict": "",
            "fresh": False,
            "attestation_id": u256(0),
            "checked_at": u256(0),
            "valid_until": u256(0),
        }

    @gl.public.view
    def uptime_summary(self, service_name: str) -> dict:
        """Uptime record for a service, derived by deterministic code
        from its stored probes: how many probes came back UP,
        DEGRADED, and DOWN, and the uptime percentage. Only UP probes
        count toward uptime — a DEGRADED probe is not an UP probe, so
        it never inflates the number. With no probes yet, the counts
        are zero and the percentage is zero."""
        clean_name = _clean_service_name(service_name)
        key = _service_key(clean_name)
        up = 0
        degraded = 0
        down = 0
        for attestation_id in self.attestation_ids:
            attestation = self.attestations[attestation_id]
            if attestation.service_key != key:
                continue
            if attestation.verdict == VERDICT_UP:
                up = up + 1
            elif attestation.verdict == VERDICT_DEGRADED:
                degraded = degraded + 1
            elif attestation.verdict == VERDICT_DOWN:
                down = down + 1
        total = up + degraded + down
        if total == 0:
            pct = 0
        else:
            pct = (up * 100) // total
        return {
            "service_name": clean_name,
            "checks": total,
            "up": up,
            "degraded": degraded,
            "down": down,
            "uptime_pct": pct,
        }

    @gl.public.view
    def list_requests(self) -> dict:
        return {"requests": [_request_to_dict(self.requests[rid]) for rid in self.request_ids]}

    @gl.public.view
    def list_attestations(self) -> dict:
        return {
            "attestations": [
                _attestation_to_dict(self.attestations[aid]) for aid in self.attestation_ids
            ]
        }

    @gl.public.view
    def stats(self) -> dict:
        fresh = u256(0)
        now = _now_seconds()
        for attestation_id in self.attestation_ids:
            if self.attestations[attestation_id].valid_until >= now:
                fresh = fresh + u256(1)
        return {
            "requests": u256(len(self.request_ids)),
            "attestations": u256(len(self.attestation_ids)),
            "fresh_attestations": fresh,
        }
