"""Authentication middleware for internal API key validation."""

from fastapi import HTTPException, Header, Depends

from app.config import settings


async def verify_api_key(x_api_key: str = Header(..., alias="X-API-Key")) -> str:
    """
    Verify the internal API key from the Orchestration Service.

    Args:
        x_api_key: API key from request header

    Returns:
        str: Validated API key

    Raises:
        HTTPException: If API key is invalid
    """
    if x_api_key != settings.API_KEY:
        raise HTTPException(
            status_code=401,
            detail="Invalid API key"
        )
    return x_api_key


# Dependency for use in routes
api_key_dependency = Depends(verify_api_key)
