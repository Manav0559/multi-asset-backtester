"""Portfolio + membership + invite routes (Module C, non-trading parts).

Authorization uses require_portfolio_role() from deps.py:
  * viewer  — read portfolio/positions/ledger
  * trader  — place orders (see orders.py)
  * owner   — invite/remove members, manage the portfolio
"""
import secrets
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, require_portfolio_role
from app.db.session import get_db
from app.models import (
    Portfolio,
    PortfolioInvite,
    PortfolioMember,
    User,
)
from app.models.enums import InviteStatus, LedgerEntryType, PortfolioRole
from app.models.trading import LedgerEntry, Position
from app.schemas.portfolio import (
    AcceptInvite,
    EquityPointOut,
    InviteCreate,
    InviteOut,
    LedgerEntryOut,
    MemberOut,
    PendingInviteOut,
    PortfolioCreate,
    PortfolioOut,
    PortfolioRename,
    PositionOut,
)
from app.services.equity import EquityPoint, equity_histories
from app.services.live_pricing import sync_prices

router = APIRouter(prefix="/portfolios", tags=["portfolios"])

_INVITE_TTL = timedelta(days=7)


@router.post("", response_model=PortfolioOut, status_code=status.HTTP_201_CREATED)
def create_portfolio(body: PortfolioCreate, user: User = Depends(get_current_user),
                     db: Session = Depends(get_db)) -> Portfolio:
    portfolio = Portfolio(
        name=body.name, owner_id=user.id, base_currency=body.base_currency,
        initial_cash=body.initial_cash, cash_balance=body.initial_cash,
        is_public=body.is_public,
    )
    db.add(portfolio)
    db.flush()
    # Owner is a member too, so all AuthZ is a single membership lookup.
    db.add(PortfolioMember(portfolio_id=portfolio.id, user_id=user.id,
                           role=PortfolioRole.OWNER))
    # Seed the ledger with the opening deposit (audit trail starts here).
    db.add(LedgerEntry(
        portfolio_id=portfolio.id, entry_type=LedgerEntryType.DEPOSIT,
        amount=body.initial_cash, balance_after=body.initial_cash,
        note="initial funding",
    ))
    db.commit()
    db.refresh(portfolio)
    return portfolio


@router.get("", response_model=list[PortfolioOut])
def list_my_portfolios(user: User = Depends(get_current_user),
                       db: Session = Depends(get_db)) -> list[Portfolio]:
    return db.scalars(
        select(Portfolio)
        .join(PortfolioMember, PortfolioMember.portfolio_id == Portfolio.id)
        .where(PortfolioMember.user_id == user.id)
        .order_by(Portfolio.created_at.desc())
    ).all()


@router.get("/{portfolio_id}", response_model=PortfolioOut)
def get_portfolio(portfolio_id: uuid.UUID,
                  member: PortfolioMember = Depends(require_portfolio_role(PortfolioRole.VIEWER)),
                  db: Session = Depends(get_db)) -> Portfolio:
    return db.get(Portfolio, portfolio_id)


@router.patch("/{portfolio_id}", response_model=PortfolioOut)
def rename_portfolio(portfolio_id: uuid.UUID, body: PortfolioRename,
                     member: PortfolioMember = Depends(require_portfolio_role(PortfolioRole.OWNER)),
                     db: Session = Depends(get_db)) -> Portfolio:
    pf = db.get(Portfolio, portfolio_id)
    pf.name = body.name
    db.commit()
    db.refresh(pf)
    return pf


@router.delete("/{portfolio_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_portfolio(portfolio_id: uuid.UUID,
                     member: PortfolioMember = Depends(require_portfolio_role(PortfolioRole.OWNER)),
                     db: Session = Depends(get_db)) -> None:
    # FK cascades remove members/orders/trades/ledger/positions/snapshots.
    db.delete(db.get(Portfolio, portfolio_id))
    db.commit()


@router.post("/{portfolio_id}/leave", status_code=status.HTTP_204_NO_CONTENT)
def leave_portfolio(portfolio_id: uuid.UUID,
                    member: PortfolioMember = Depends(require_portfolio_role(PortfolioRole.VIEWER)),
                    db: Session = Depends(get_db)) -> None:
    """Walk away from a shared portfolio. The OWNER can't leave (the book's
    cash has to belong to someone) — they delete the portfolio instead."""
    if member.role == PortfolioRole.OWNER:
        raise HTTPException(status.HTTP_409_CONFLICT,
                            "the owner cannot leave — delete the portfolio instead")
    db.delete(member)
    db.commit()


@router.get("/{portfolio_id}/members", response_model=list[MemberOut])
def list_members(portfolio_id: uuid.UUID,
                 member: PortfolioMember = Depends(require_portfolio_role(PortfolioRole.VIEWER)),
                 db: Session = Depends(get_db)) -> list[PortfolioMember]:
    return db.scalars(
        select(PortfolioMember).where(PortfolioMember.portfolio_id == portfolio_id)
    ).all()


@router.get("/{portfolio_id}/presence")
def list_presence(portfolio_id: uuid.UUID,
                  member: PortfolioMember = Depends(require_portfolio_role(PortfolioRole.VIEWER)),
                  db: Session = Depends(get_db)) -> list[dict]:
    """Who's online in this room right now (TTL-pruned in-memory presence),
    resolved to usernames for avatars."""
    from app.services.presence import online_members
    ids = online_members(portfolio_id)
    if not ids:
        return []
    rows = db.execute(
        select(User.id, User.username).where(User.id.in_([uuid.UUID(i) for i in ids]))
    ).all()
    return [{"user_id": str(uid), "username": uname} for uid, uname in rows]


@router.get("/{portfolio_id}/positions", response_model=list[PositionOut])
def list_positions(portfolio_id: uuid.UUID,
                   member: PortfolioMember = Depends(require_portfolio_role(PortfolioRole.VIEWER)),
                   db: Session = Depends(get_db)) -> list[Position]:
    return db.scalars(
        select(Position).where(Position.portfolio_id == portfolio_id,
                               Position.qty != 0)
    ).all()


@router.get("/{portfolio_id}/ledger", response_model=list[LedgerEntryOut])
def list_ledger(portfolio_id: uuid.UUID,
                member: PortfolioMember = Depends(require_portfolio_role(PortfolioRole.VIEWER)),
                db: Session = Depends(get_db)) -> list[LedgerEntry]:
    return db.scalars(
        select(LedgerEntry).where(LedgerEntry.portfolio_id == portfolio_id)
        .order_by(LedgerEntry.created_at.desc()).limit(200)
    ).all()


@router.get("/{portfolio_id}/equity-history", response_model=list[EquityPointOut])
def equity_history(portfolio_id: uuid.UUID,
                   member: PortfolioMember = Depends(require_portfolio_role(PortfolioRole.VIEWER)),
                   db: Session = Depends(get_db)) -> list[EquityPoint]:
    """Equity over time, reconstructed from the ledger (see services/equity.py
    for the marking model). Feeds the portfolio page chart + total."""
    # On-demand "live-ish" marks: refresh prices for this book's open positions
    # (cache-bounded) before marking, so the terminal equity point ticks between
    # trades as the frontend polls.
    held = set(db.scalars(select(Position.asset_id).where(
        Position.portfolio_id == portfolio_id, Position.qty != 0)).all())
    sync_prices(db, held)
    return equity_histories(db, [portfolio_id])[portfolio_id]


# --------------------------------------------------------------- invites --
@router.post("/{portfolio_id}/invites", response_model=InviteOut,
             status_code=status.HTTP_201_CREATED)
def create_invite(portfolio_id: uuid.UUID, body: InviteCreate,
                  member: PortfolioMember = Depends(require_portfolio_role(PortfolioRole.OWNER)),
                  db: Session = Depends(get_db)) -> PortfolioInvite:
    invite = PortfolioInvite(
        portfolio_id=portfolio_id, inviter_id=member.user_id,
        invitee_email=body.invitee_email.lower(), role=body.role,
        status=InviteStatus.PENDING, token=secrets.token_urlsafe(32),
        expires_at=datetime.now(timezone.utc) + _INVITE_TTL,
    )
    db.add(invite)
    db.commit()
    db.refresh(invite)
    return invite


@router.get("/invites/pending", response_model=list[PendingInviteOut])
def list_pending_invites(user: User = Depends(get_current_user),
                         db: Session = Depends(get_db)) -> list[PendingInviteOut]:
    """Invites the current user can act on (their email, still pending & valid)."""
    rows = db.execute(
        select(PortfolioInvite, Portfolio.name, User.username)
        .join(Portfolio, Portfolio.id == PortfolioInvite.portfolio_id)
        .join(User, User.id == PortfolioInvite.inviter_id)
        .where(PortfolioInvite.invitee_email == user.email.lower(),
               PortfolioInvite.status == InviteStatus.PENDING,
               PortfolioInvite.expires_at > datetime.now(timezone.utc))
        .order_by(PortfolioInvite.created_at.desc())
    ).all()
    return [PendingInviteOut(
        id=inv.id, portfolio_id=inv.portfolio_id, portfolio_name=pname,
        inviter_username=uname, role=inv.role, token=inv.token,
        expires_at=inv.expires_at,
    ) for inv, pname, uname in rows]


@router.post("/invites/decline", status_code=status.HTTP_204_NO_CONTENT)
def decline_invite(body: AcceptInvite, user: User = Depends(get_current_user),
                   db: Session = Depends(get_db)) -> None:
    invite = db.scalar(select(PortfolioInvite).where(PortfolioInvite.token == body.token))
    if invite is None or invite.status != InviteStatus.PENDING:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail="Invite not found or already used")
    if invite.invitee_email != user.email.lower():
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                            detail="Invite was issued to a different email")
    invite.status = InviteStatus.DECLINED
    invite.responded_at = datetime.now(timezone.utc)
    db.commit()


@router.post("/invites/accept", response_model=MemberOut)
def accept_invite(body: AcceptInvite, user: User = Depends(get_current_user),
                  db: Session = Depends(get_db)) -> PortfolioMember:
    invite = db.scalar(select(PortfolioInvite).where(PortfolioInvite.token == body.token))
    if invite is None or invite.status != InviteStatus.PENDING:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail="Invite not found or already used")
    if invite.expires_at < datetime.now(timezone.utc):
        invite.status = InviteStatus.EXPIRED
        db.commit()
        raise HTTPException(status_code=status.HTTP_410_GONE, detail="Invite expired")
    # Bind invite to the authenticated user's email (can't accept someone else's).
    if invite.invitee_email != user.email.lower():
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                            detail="Invite was issued to a different email")

    existing = db.get(PortfolioMember, (invite.portfolio_id, user.id))
    if existing is None:
        existing = PortfolioMember(portfolio_id=invite.portfolio_id, user_id=user.id,
                                   role=invite.role, invited_by=invite.inviter_id)
        db.add(existing)
    invite.status = InviteStatus.ACCEPTED
    invite.responded_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(existing)
    return existing
