"""Direct tests for ServiceUptimeAttestation — mocked web + LLM, no network.

Every consensus round runs against mocks: the service page is stubbed
with mock_web, the model classification with mock_llm. run_validator()
replays the captured validator with the current mocks, so swapping the
mocks between the leader run and the replay simulates a validator that
sees different external data.
"""

import json
import re

from tests.direct.conftest import to_hex

URL_EXAMPLE = "https://example.com/"
URL_STATUS = "https://www.githubstatus.com/"
SERVICE = "Example"
MARKER = "Example Domain"

PAGE_EXAMPLE = "<html><head><title>Example Domain</title></head><body><h1>Example Domain</h1></body></html>"
PAGE_STATUS = "All Systems Operational - GitHub Status"


def _classify_answer(status):
    """Model answer: one classification for the fetched page."""
    return {"status": status}


def _mock_page(vm, url, body, status=200):
    vm.mock_web(re.escape(url), {"status": status, "body": body})


def _mock_llm(vm, answer):
    vm.mock_llm("operational", json.dumps(answer))


def _request(vm, contract, service=SERVICE, url=URL_EXAMPLE, marker=MARKER):
    return contract.request_check(service, url, marker)


def _attest_up(vm, contract, request_id=1, page=PAGE_EXAMPLE, status="operational"):
    _mock_page(vm, URL_EXAMPLE, page)
    _mock_llm(vm, _classify_answer(status))
    return contract.attest_uptime(request_id)


# -- requests ------------------------------------------------------------


def test_request_stores_request(direct_vm, direct_deploy, direct_alice):
    contract = direct_deploy("contracts/ServiceUptimeAttestation.py")
    direct_vm.sender = direct_alice

    result = _request(direct_vm, contract)

    assert result["request_id"] == 1
    assert result["status"] == "REQUESTED"

    req = contract.get_request(1)
    assert req["service_name"] == SERVICE
    assert req["service_key"] == "EXAMPLE"
    assert req["url"] == URL_EXAMPLE
    assert req["expected_marker"] == MARKER
    assert req["requester"] == to_hex(direct_alice)
    assert req["requested_at"] > 0
    assert req["status"] == "REQUESTED"


def test_request_allows_empty_marker(direct_vm, direct_deploy):
    contract = direct_deploy("contracts/ServiceUptimeAttestation.py")

    result = _request(direct_vm, contract, marker="")

    assert result["request_id"] == 1
    assert contract.get_request(1)["expected_marker"] == ""


def test_request_normalizes_service_name(direct_vm, direct_deploy):
    contract = direct_deploy("contracts/ServiceUptimeAttestation.py")

    _request(direct_vm, contract, service="  GitHub   Status ")

    req = contract.get_request(1)
    assert req["service_name"] == "GitHub Status"
    assert req["service_key"] == "GITHUB STATUS"


def test_request_rejects_bad_inputs(direct_vm, direct_deploy):
    contract = direct_deploy("contracts/ServiceUptimeAttestation.py")

    with direct_vm.expect_revert("[EXPECTED]"):
        _request(direct_vm, contract, service="   ")
    with direct_vm.expect_revert("[EXPECTED]"):
        _request(direct_vm, contract, service="S" * 81)
    with direct_vm.expect_revert("[EXPECTED]"):
        _request(direct_vm, contract, url="http://example.com/")
    with direct_vm.expect_revert("[EXPECTED]"):
        _request(direct_vm, contract, url="https://example.com:8080/")
    with direct_vm.expect_revert("[EXPECTED]"):
        _request(direct_vm, contract, url="https://user@example.com/")
    with direct_vm.expect_revert("[EXPECTED]"):
        _request(direct_vm, contract, marker="M" * 201)
    with direct_vm.expect_revert("[EXPECTED]"):
        _request(direct_vm, contract, marker="line one\nline two")


def test_re_requesting_a_service_is_allowed(direct_vm, direct_deploy):
    contract = direct_deploy("contracts/ServiceUptimeAttestation.py")

    first = _request(direct_vm, contract)
    second = _request(direct_vm, contract)

    assert first["request_id"] == 1
    assert second["request_id"] == 2
    assert len(contract.list_requests()["requests"]) == 2


# -- attestation ---------------------------------------------------------


def test_attest_up_stores_verdict_and_validity(direct_vm, direct_deploy, direct_alice):
    contract = direct_deploy("contracts/ServiceUptimeAttestation.py")
    direct_vm.sender = direct_alice
    direct_vm.warp("2026-11-23T00:00:00Z")
    _request(direct_vm, contract)

    result = _attest_up(direct_vm, contract)

    assert result["attestation_id"] == 1
    assert result["request_id"] == 1
    assert result["service_name"] == SERVICE
    assert result["verdict"] == "UP"
    assert result["http_status"] == 200
    assert result["status"] == "ATTESTED"

    att = contract.get_attestation(1)
    assert att["verdict"] == "UP"
    assert att["http_status"] == 200
    assert att["marker_found"] is True
    assert att["verified_by"] == to_hex(direct_alice)
    assert att["verified_at"] > 0
    # One hour of validity, in seconds.
    assert att["valid_until"] == att["verified_at"] + 3600

    assert contract.get_request(1)["status"] == "ATTESTED"
    assert contract.get_latest_attestation(SERVICE)["attestation_id"] == 1
    status = contract.service_status(SERVICE)
    assert status["fresh"] is True
    assert status["verdict"] == "UP"


def test_attest_degraded_when_page_reports_problems(direct_vm, direct_deploy):
    contract = direct_deploy("contracts/ServiceUptimeAttestation.py")
    _request(direct_vm, contract, service="GitHub Status", url=URL_STATUS, marker="")

    _mock_page(direct_vm, URL_STATUS, "Partial Outage - Git Operations are degraded")
    _mock_llm(direct_vm, _classify_answer("degraded"))
    result = contract.attest_uptime(1)

    assert result["verdict"] == "DEGRADED"
    assert contract.get_attestation(1)["verdict"] == "DEGRADED"


def test_attest_down_when_marker_missing(direct_vm, direct_deploy):
    contract = direct_deploy("contracts/ServiceUptimeAttestation.py")
    _request(direct_vm, contract)

    # The page loads but no longer contains the expected marker: DOWN,
    # decided without asking the model, so no LLM mock is needed.
    _mock_page(direct_vm, URL_EXAMPLE, "<html><body>Something else entirely</body></html>")
    result = contract.attest_uptime(1)

    assert result["verdict"] == "DOWN"
    assert result["http_status"] == 200
    att = contract.get_attestation(1)
    assert att["marker_found"] is False


def test_attest_down_when_service_returns_5xx(direct_vm, direct_deploy):
    contract = direct_deploy("contracts/ServiceUptimeAttestation.py")
    _request(direct_vm, contract)

    # A 5xx is the service failing: a DOWN attestation is stored.
    _mock_page(direct_vm, URL_EXAMPLE, "", status=503)
    result = contract.attest_uptime(1)

    assert result["verdict"] == "DOWN"
    assert result["http_status"] == 503
    assert contract.get_attestation(1)["marker_found"] is False


def test_attest_4xx_is_external(direct_vm, direct_deploy):
    contract = direct_deploy("contracts/ServiceUptimeAttestation.py")
    _request(direct_vm, contract)

    # A 404 means the probe URL itself is wrong: the round fails.
    _mock_page(direct_vm, URL_EXAMPLE, "", status=404)
    with direct_vm.expect_revert("[EXTERNAL]"):
        contract.attest_uptime(1)
    assert contract.get_request(1)["status"] == "REQUESTED"


def test_fetch_failure_is_transient(direct_vm, direct_deploy):
    contract = direct_deploy("contracts/ServiceUptimeAttestation.py")
    _request(direct_vm, contract)

    # No mock for the URL at all: the fetch fails.
    with direct_vm.expect_revert("[TRANSIENT]"):
        contract.attest_uptime(1)


def test_bad_model_classification_is_llm_error(direct_vm, direct_deploy):
    contract = direct_deploy("contracts/ServiceUptimeAttestation.py")
    _request(direct_vm, contract)

    _mock_page(direct_vm, URL_EXAMPLE, PAGE_EXAMPLE)
    _mock_llm(direct_vm, {"status": "fine"})
    with direct_vm.expect_revert("[LLM_ERROR]"):
        contract.attest_uptime(1)

    direct_vm.clear_mocks()
    _mock_page(direct_vm, URL_EXAMPLE, PAGE_EXAMPLE)
    _mock_llm(direct_vm, {"health": "operational"})
    with direct_vm.expect_revert("[LLM_ERROR]"):
        contract.attest_uptime(1)

    assert contract.get_request(1)["status"] == "REQUESTED"


def test_validator_agrees_with_leader(direct_vm, direct_deploy):
    contract = direct_deploy("contracts/ServiceUptimeAttestation.py")
    _request(direct_vm, contract)
    _attest_up(direct_vm, contract)

    # Replays the captured validator with the same mocks.
    assert direct_vm.run_validator() is True


def test_validator_disagrees_on_verdict(direct_vm, direct_deploy):
    contract = direct_deploy("contracts/ServiceUptimeAttestation.py")
    _request(direct_vm, contract)
    _attest_up(direct_vm, contract)

    # The validator reads the same page as degraded: no agreement.
    direct_vm.clear_mocks()
    _mock_page(direct_vm, URL_EXAMPLE, PAGE_EXAMPLE)
    _mock_llm(direct_vm, _classify_answer("degraded"))

    assert direct_vm.run_validator() is False


def test_validator_disagrees_when_service_flaps(direct_vm, direct_deploy):
    contract = direct_deploy("contracts/ServiceUptimeAttestation.py")
    _request(direct_vm, contract)
    _attest_up(direct_vm, contract)

    # The validator sees a 503 where the leader saw a 200: the service
    # flapped between probes, so no attestation is agreed.
    direct_vm.clear_mocks()
    _mock_page(direct_vm, URL_EXAMPLE, "", status=503)

    assert direct_vm.run_validator() is False


def test_attest_twice_and_unknown_ids_rejected(direct_vm, direct_deploy):
    contract = direct_deploy("contracts/ServiceUptimeAttestation.py")
    _request(direct_vm, contract)
    _attest_up(direct_vm, contract)

    with direct_vm.expect_revert("[EXPECTED]"):
        contract.attest_uptime(1)
    with direct_vm.expect_revert("[EXPECTED]"):
        contract.attest_uptime(999)
    with direct_vm.expect_revert("[EXPECTED]"):
        contract.get_request(999)
    with direct_vm.expect_revert("[EXPECTED]"):
        contract.get_attestation(999)
    with direct_vm.expect_revert("[EXPECTED]"):
        contract.get_latest_attestation("Nonexistent Service")


# -- views ---------------------------------------------------------------


def test_service_status_for_unknown_service(direct_vm, direct_deploy):
    contract = direct_deploy("contracts/ServiceUptimeAttestation.py")

    status = contract.service_status("Nobody")
    assert status["fresh"] is False
    assert status["attestation_id"] == 0
    assert status["verdict"] == ""


def test_service_status_goes_stale(direct_vm, direct_deploy):
    contract = direct_deploy("contracts/ServiceUptimeAttestation.py")
    direct_vm.warp("2026-11-23T00:00:00Z")
    _request(direct_vm, contract)
    _attest_up(direct_vm, contract)

    assert contract.service_status(SERVICE)["fresh"] is True

    direct_vm.warp("2026-11-23T02:00:00Z")
    assert contract.service_status(SERVICE)["fresh"] is False
    # The attestation itself is still readable as history.
    assert contract.get_latest_attestation(SERVICE)["verdict"] == "UP"


def test_uptime_summary_counts_verdicts(direct_vm, direct_deploy):
    contract = direct_deploy("contracts/ServiceUptimeAttestation.py")

    # Probe 1: UP.
    _request(direct_vm, contract)
    _attest_up(direct_vm, contract)

    # Probe 2: DEGRADED.
    _request(direct_vm, contract)
    direct_vm.clear_mocks()
    _mock_page(direct_vm, URL_EXAMPLE, PAGE_EXAMPLE)
    _mock_llm(direct_vm, _classify_answer("degraded"))
    contract.attest_uptime(2)

    # Probe 3: DOWN (marker missing).
    _request(direct_vm, contract)
    direct_vm.clear_mocks()
    _mock_page(direct_vm, URL_EXAMPLE, "<html><body>Something else</body></html>")
    contract.attest_uptime(3)

    # Probe 4: UP again.
    _request(direct_vm, contract)
    direct_vm.clear_mocks()
    _attest_up(direct_vm, contract, request_id=4)

    summary = contract.uptime_summary(SERVICE)
    assert summary["checks"] == 4
    assert summary["up"] == 2
    assert summary["degraded"] == 1
    assert summary["down"] == 1
    # Only UP probes count toward uptime: 2 of 4 is 50%.
    assert summary["uptime_pct"] == 50


def test_uptime_summary_empty_service(direct_vm, direct_deploy):
    contract = direct_deploy("contracts/ServiceUptimeAttestation.py")

    summary = contract.uptime_summary("Nobody")
    assert summary["checks"] == 0
    assert summary["uptime_pct"] == 0


def test_latest_attestation_is_newest_for_service(direct_vm, direct_deploy):
    contract = direct_deploy("contracts/ServiceUptimeAttestation.py")
    _request(direct_vm, contract)
    _attest_up(direct_vm, contract)
    _request(direct_vm, contract)
    direct_vm.clear_mocks()
    _attest_up(direct_vm, contract, request_id=2)

    latest = contract.get_latest_attestation(SERVICE)
    assert latest["attestation_id"] == 2
    assert latest["request_id"] == 2

    # The first attestation is still readable as history.
    assert contract.get_attestation(1)["attestation_id"] == 1


def test_services_are_independent(direct_vm, direct_deploy):
    contract = direct_deploy("contracts/ServiceUptimeAttestation.py")
    _request(direct_vm, contract)
    _attest_up(direct_vm, contract)
    _request(direct_vm, contract, service="GitHub Status", url=URL_STATUS, marker="")
    direct_vm.clear_mocks()
    _mock_page(direct_vm, URL_STATUS, PAGE_STATUS)
    _mock_llm(direct_vm, _classify_answer("operational"))
    contract.attest_uptime(2)

    assert contract.get_latest_attestation(SERVICE)["attestation_id"] == 1
    assert contract.get_latest_attestation("GitHub Status")["attestation_id"] == 2
    assert contract.uptime_summary(SERVICE)["checks"] == 1
    assert contract.uptime_summary("GitHub Status")["checks"] == 1


def test_list_and_stats(direct_vm, direct_deploy):
    contract = direct_deploy("contracts/ServiceUptimeAttestation.py")
    direct_vm.warp("2026-11-23T00:00:00Z")
    _request(direct_vm, contract)
    _attest_up(direct_vm, contract)
    _request(direct_vm, contract)

    assert len(contract.list_requests()["requests"]) == 2
    assert len(contract.list_attestations()["attestations"]) == 1
    stats = contract.stats()
    assert stats["requests"] == 2
    assert stats["attestations"] == 1
    assert stats["fresh_attestations"] == 1

    direct_vm.warp("2026-11-23T02:00:00Z")
    assert contract.stats()["fresh_attestations"] == 0
