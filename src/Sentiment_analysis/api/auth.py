"""Trust only the HTTP API's verified authorizer context, never decoded headers."""

from uuid import UUID

from fastapi import Request, Security
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer, SecurityScopes

from src.Sentiment_analysis.entity.api import Principal
from src.Sentiment_analysis.repositories.jobs import JobError

bearer = HTTPBearer(scheme_name="CognitoAccessToken", bearerFormat="JWT", auto_error=False)


async def authenticated_principal(request: Request, security_scopes: SecurityScopes,
                                  credentials: HTTPAuthorizationCredentials | None = Security(bearer)) -> Principal:
    if credentials is None or len(request.headers.getlist("authorization")) != 1:
        raise JobError("UNAUTHORIZED")
    event = request.scope.get("aws.event", {})
    if not isinstance(event, dict):
        raise JobError("UNAUTHORIZED")
    context = event.get("requestContext", {})
    if not isinstance(context, dict) or not isinstance(context.get("authorizer", {}), dict):
        raise JobError("UNAUTHORIZED")
    jwt = context.get("authorizer", {}).get("jwt", {})
    if not isinstance(jwt, dict) or not isinstance(jwt.get("claims", {}), dict):
        raise JobError("UNAUTHORIZED")
    claims = jwt.get("claims", {})
    settings = request.app.state.settings
    if event.get("version") != "2.0" or not claims or not settings.auth.cognito_issuer or not settings.auth.public_client_id:
        raise JobError("UNAUTHORIZED")
    try:
        owner = str(UUID(claims["sub"]))
        exp = int(claims["exp"])
        if isinstance(claims["exp"], bool) or str(exp) != str(claims["exp"]):
            raise ValueError("Invalid expiry")
        scopes = tuple(claims.get("scope", "").split())
        verified_scopes = jwt.get("scopes", [])
        if not isinstance(verified_scopes, list) or not all(isinstance(s, str) for s in verified_scopes):
            raise ValueError("Invalid verified scopes")
    except (KeyError, TypeError, ValueError, AttributeError):
        raise JobError("UNAUTHORIZED") from None
    if (claims.get("token_use") != "access" or claims.get("iss") != settings.auth.cognito_issuer or
            claims.get("client_id") != settings.auth.public_client_id or
            exp <= request.app.state.clock().timestamp()):
        raise JobError("UNAUTHORIZED")
    if any(scope not in scopes or scope not in verified_scopes for scope in security_scopes.scopes):
        raise JobError("FORBIDDEN")
    return Principal(owner=owner, scopes=scopes)
