"""FastAPI dependencies: current-user resolution and portfolio-scoped
role authorization (the AuthZ backbone for shared portfolios)."""
import uuid

from fastapi import Depends, HTTPException, Path, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from app.core.security import ACCESS, TokenError, decode_token
from app.db.session import get_db
from app.models import PortfolioMember, User
from app.models.enums import PortfolioRole

_bearer = HTTPBearer(auto_error=False)

# Role hierarchy: any role >= the required level is allowed.
_ROLE_RANK = {
    PortfolioRole.VIEWER: 0,
    PortfolioRole.TRADER: 1,
    PortfolioRole.OWNER: 2,
}


def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    db: Session = Depends(get_db),
) -> User:
    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
            headers={"WWW-Authenticate": "Bearer"},
        )
    try:
        payload = decode_token(credentials.credentials, expected_type=ACCESS)
    except TokenError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(exc),
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc

    user = db.get(User, uuid.UUID(payload["sub"]))
    if user is None or not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User inactive or not found",
        )
    return user


def require_portfolio_role(min_role: PortfolioRole):
    """Dependency factory: asserts the current user is a member of the
    portfolio in the path with at least `min_role`. Returns the
    membership row (so handlers know the caller's actual role).

    Usage:
        @router.post("/portfolios/{portfolio_id}/orders")
        def place_order(member = Depends(require_portfolio_role(PortfolioRole.TRADER))):
            ...
    """

    def _check(
        portfolio_id: uuid.UUID = Path(...),
        user: User = Depends(get_current_user),
        db: Session = Depends(get_db),
    ) -> PortfolioMember:
        member = db.get(PortfolioMember, (portfolio_id, user.id))
        if member is None:
            # 404 (not 403) so non-members can't probe which portfolio IDs exist.
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                                detail="Portfolio not found")
        if _ROLE_RANK[member.role] < _ROLE_RANK[min_role]:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Requires {min_role.value} access",
            )
        return member

    return _check
