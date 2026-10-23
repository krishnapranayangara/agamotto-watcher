"""Require a Firebase sign-in token before changing the watcher."""

from fastapi import Header, HTTPException
from google.auth import exceptions
from google.auth.transport import requests
from google.oauth2 import id_token

PROJECT_ID = "jobs-9de59"
ISSUER = f"https://securetoken.google.com/{PROJECT_ID}"
_request = requests.Request()


def require_signed_in(authorization: str | None = Header(default=None)) -> dict:
    scheme, _, token = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise HTTPException(status_code=401, detail="Sign in required")
    try:
        claims = id_token.verify_firebase_token(
            token.strip(),
            _request,
            audience=PROJECT_ID,
        )
    except (ValueError, exceptions.GoogleAuthError):
        raise HTTPException(status_code=401, detail="Sign in required")
    except exceptions.TransportError:
        raise HTTPException(status_code=503, detail="Sign-in could not be checked")
    if claims.get("iss") != ISSUER or not claims.get("sub"):
        raise HTTPException(status_code=401, detail="Sign in required")
    return claims
