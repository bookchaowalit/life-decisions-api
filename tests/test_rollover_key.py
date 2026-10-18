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


def test_a_stranger_key_and_a_wrong_issuer_still_fail():
    _, old_public = _pair()
    _, new_public = _pair()
    stranger_private, _ = _pair()
    verifier = TokenVerifier(
        old_public, "empire-prod", extra_public_key_pems=(new_public,)
    )
    with pytest.raises(Unauthenticated):
        verifier.verify(f"Bearer {_token(stranger_private)}", None)


def test_without_a_rollover_key_only_the_current_key_verifies():
    old_private, old_public = _pair()
    new_private, _ = _pair()
    verifier = TokenVerifier(old_public, "empire-prod")
    assert verifier.verify(f"Bearer {_token(old_private)}", None)
    with pytest.raises(Unauthenticated):
        verifier.verify(f"Bearer {_token(new_private)}", None)
