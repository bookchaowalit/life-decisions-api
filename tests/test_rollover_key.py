"""A rollover key for the same issuer verifies next to the current key; strangers still fail."""

from __future__ import annotations

import time
import uuid

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from solo_decisions.auth import AUDIENCE, TokenVerifier, Unauthenticated


def _pair() -> tuple[str, str]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    public = (
        key.public_key()
        .public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
        )
        .decode()
    )
    return private, public


def _token(private: str, issuer: str = "empire-prod") -> str:
    now = int(time.time())
    claims = {
        "iss": issuer,
        "aud": AUDIENCE,
        "sub": str(uuid.uuid4()),
        "ws": str(uuid.uuid4()),
        "iat": now,
        "exp": now + 600,
        "jti": str(uuid.uuid4()),
    }
    return jwt.encode(claims, private, algorithm="RS256")


def test_tokens_from_either_key_verify():
    old_private, old_public = _pair()
    new_private, new_public = _pair()
    verifier = TokenVerifier(
        old_public, "empire-prod", extra_public_key_pems=(new_public,)
    )
    for private in (old_private, new_private):
        assert verifier.verify(f"Bearer {_token(private)}", None).channel == "api"


def test_a_stranger_key_still_fails():
    _, old_public = _pair()
    _, new_public = _pair()
    stranger_private, _ = _pair()
    verifier = TokenVerifier(
        old_public, "empire-prod", extra_public_key_pems=(new_public,)
    )
    with pytest.raises(Unauthenticated):
        verifier.verify(f"Bearer {_token(stranger_private)}", None)


@pytest.mark.parametrize("which", ["current", "rollover"])
def test_issuer_audience_and_claims_are_enforced_for_both_keys(which):
    old_private, old_public = _pair()
    new_private, new_public = _pair()
    private = old_private if which == "current" else new_private
    verifier = TokenVerifier(
        old_public, "empire-prod", extra_public_key_pems=(new_public,)
    )
    now = int(time.time())
    good = {
        "iss": "empire-prod",
        "aud": AUDIENCE,
        "sub": str(uuid.uuid4()),
        "ws": str(uuid.uuid4()),
        "iat": now,
        "exp": now + 600,
        "jti": str(uuid.uuid4()),
    }
    assert verifier.verify(
        f"Bearer {jwt.encode(good, private, algorithm='RS256')}", None
    )
    bad_claims = [
        {**good, "iss": "someone-else"},
        {**good, "aud": "another-api"},
        {k: v for k, v in good.items() if k != "jti"},
        {**good, "exp": now - 10},
    ]
    for claims in bad_claims:
        token = jwt.encode(claims, private, algorithm="RS256")
        with pytest.raises(Unauthenticated):
            verifier.verify(f"Bearer {token}", None)


def test_without_a_rollover_key_only_the_current_key_verifies():
    old_private, old_public = _pair()
    new_private, _ = _pair()
    verifier = TokenVerifier(old_public, "empire-prod")
    assert verifier.verify(f"Bearer {_token(old_private)}", None)
    with pytest.raises(Unauthenticated):
        verifier.verify(f"Bearer {_token(new_private)}", None)
