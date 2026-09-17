from devflow.mock_provider import DeterministicMockProvider


def test_mock_provider_is_deterministic() -> None:
    provider = DeterministicMockProvider()
    assert provider.proposal("  add   health endpoint ") == provider.proposal(
        "add health endpoint"
    )

