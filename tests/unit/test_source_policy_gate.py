from __future__ import annotations

import json

from pydantic import HttpUrl
from typer.testing import CliRunner

from astock.cli import app
from astock.core.source_policy_gate import (
    AuthorityDomainRegistry,
    AuthoritySource,
    SourcePolicyGate,
)
from astock.schemas import AgentSourceProposal, SourceClass
from astock.schemas.source_access import SourceAdmissionStatus


def _proposal(
    *,
    capability: str,
    candidate_url: str | None,
    source_class: SourceClass = SourceClass.PRIMARY_OFFICIAL_WEB,
    formal_use: bool = True,
    require_complete: bool = False,
) -> AgentSourceProposal:
    return AgentSourceProposal(
        requested_capability=capability,
        query="bounded source verification",
        candidate_url=HttpUrl(candidate_url) if candidate_url is not None else None,
        expected_fact="one decision-relevant fact",
        preferred_source_class=source_class,
        formal_use=formal_use,
        require_complete=require_complete,
        reason="test deterministic source admission",
    )


def test_registered_official_exact_url_can_enter_snapshot_admission() -> None:
    decision = SourcePolicyGate().validate(
        _proposal(
            capability="web.authoritative_fact",
            candidate_url="https://www.sse.com.cn/disclosure/example.html",
        )
    )

    assert decision.allowed
    assert decision.formal_eligible
    assert decision.source_id == "sse-official-web"
    assert decision.admission_status is SourceAdmissionStatus.ADMIT_AFTER_SNAPSHOT
    assert not decision.exhaustive_proof_allowed


def test_formal_official_web_requires_https_and_rejects_url_credentials() -> None:
    gate = SourcePolicyGate()

    insecure = gate.validate(
        _proposal(
            capability="web.authoritative_fact",
            candidate_url="http://www.sse.com.cn/disclosure/example.html",
        )
    )
    credentialed = gate.validate(
        _proposal(
            capability="web.authoritative_fact",
            candidate_url="https://user:secret@www.sse.com.cn/disclosure/example.html",
        )
    )

    assert not insecure.allowed
    assert insecure.reason_codes == ["FORMAL_WEB_SOURCE_REQUIRES_HTTPS"]
    assert not credentialed.allowed
    assert credentialed.reason_codes == ["FORMAL_WEB_SOURCE_CANNOT_USE_URL_CREDENTIALS"]


def test_search_discovery_can_never_claim_exhaustive_completeness() -> None:
    decision = SourcePolicyGate().validate(
        _proposal(
            capability="web.authoritative_fact",
            candidate_url=None,
            source_class=SourceClass.REPUTABLE_WEB_SEARCH,
            formal_use=False,
            require_complete=True,
        )
    )

    assert not decision.allowed
    assert decision.reason_codes == ["SEARCH_WEB_CANNOT_PROVE_COMPLETENESS"]


def test_cninfo_kill_can_recover_known_disclosure_from_registered_exchange_web() -> None:
    decision = SourcePolicyGate().validate(
        _proposal(
            capability="disclosure.document",
            candidate_url="https://www.sse.com.cn/disclosure/listedinfo/announcement/example.pdf",
            formal_use=True,
            require_complete=False,
        )
    )

    assert decision.allowed
    assert decision.formal_eligible
    assert decision.source_id == "sse-official-web"
    assert decision.admission_status is SourceAdmissionStatus.ADMIT_AFTER_SNAPSHOT
    assert not decision.exhaustive_proof_allowed


def test_official_web_requires_explicit_exhaustive_contract_for_negative_proof() -> None:
    decision = SourcePolicyGate().validate(
        _proposal(
            capability="web.authoritative_fact",
            candidate_url="https://www.sse.com.cn/disclosure/example.html",
            require_complete=True,
        )
    )

    assert not decision.allowed
    assert decision.reason_codes == ["AUTHORITY_SOURCE_LACKS_EXHAUSTIVE_PROOF_CONTRACT"]


def test_audited_official_exhaustive_contract_can_pass_complete_gate() -> None:
    registry = AuthorityDomainRegistry(
        schema_version="authority-domains-v1",
        sources=(
            AuthoritySource(
                source_id="audited-exchange-enumerator",
                domains=("exchange.example",),
                source_class=SourceClass.PRIMARY_OFFICIAL_WEB,
                capabilities=frozenset({"disclosure.enumerate"}),
                formal_capabilities=frozenset({"disclosure.enumerate"}),
                exhaustive_capabilities=frozenset({"disclosure.enumerate"}),
                independence_group="EXCHANGE_OFFICIAL",
            ),
        ),
    )

    decision = SourcePolicyGate(registry).validate(
        _proposal(
            capability="disclosure.enumerate",
            candidate_url="https://exchange.example/announcements?page=1",
            require_complete=True,
        )
    )

    assert decision.allowed
    assert decision.formal_eligible
    assert decision.exhaustive_proof_allowed
    assert decision.admission_status is SourceAdmissionStatus.ADMIT_AFTER_SNAPSHOT


def test_unregistered_web_domain_cannot_be_formal_evidence() -> None:
    decision = SourcePolicyGate().validate(
        _proposal(
            capability="web.authoritative_fact",
            candidate_url="https://unregistered.example/fact",
        )
    )

    assert not decision.allowed
    assert decision.reason_codes == ["UNREGISTERED_FORMAL_SOURCE"]


def test_source_proposal_cli_exposes_discovery_only_admission() -> None:
    result = CliRunner().invoke(
        app,
        [
            "source-proposal-check",
            "--capability",
            "news.discover",
            "--query",
            "latest issuer news",
            "--expected-fact",
            "decision-relevant issuer news",
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["allowed"] is True
    assert payload["admission_status"] == "DISCOVERY_ONLY"
    assert payload["formal_eligible"] is False


def test_source_proposal_cli_rejects_search_as_complete_formal_proof() -> None:
    result = CliRunner().invoke(
        app,
        [
            "source-proposal-check",
            "--capability",
            "instrument.master",
            "--query",
            "A-share universe",
            "--expected-fact",
            "complete listed-stock universe",
            "--formal-use",
            "--require-complete",
        ],
    )

    assert result.exit_code == 2
    payload = json.loads(result.stdout)
    assert payload["allowed"] is False
    assert payload["reason_codes"] == ["SEARCH_WEB_CANNOT_PROVE_COMPLETENESS"]
