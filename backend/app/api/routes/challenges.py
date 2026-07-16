"""Consent-based head-to-head competitions.

No global leaderboard. A challenge exists only when BOTH users opt in, on
portfolios they each choose. Each participant sees only whitelisted aggregates
about the other (services/challenges.windowed_metrics) — the opponent's
positions/trades/strategy stay behind the normal portfolio-membership authz,
which the challenger is not a member of.
"""
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.api.deps import get_current_user
from app.db.session import get_db
from app.models import Challenge, Portfolio, PortfolioMember, User
from app.models.enums import ChallengeStatus
from app.models.trading import Position
from app.services.live_pricing import sync_prices
from app.schemas.challenge import (
    ChallengeAccept,
    ChallengeCreate,
    ChallengeOut,
    HeadToHeadOut,
    OpponentMetricsOut,
)
from app.services.challenges import current_equity, windowed_metrics

router = APIRouter(prefix="/challenges", tags=["challenges"])


def _require_member(db: Session, portfolio_id: uuid.UUID, user_id: uuid.UUID):
    if db.get(PortfolioMember, (portfolio_id, user_id)) is None:
        raise HTTPException(status.HTTP_403_FORBIDDEN,
                            "you must be a member of the portfolio you enter")


def _username(db: Session, user_id: uuid.UUID) -> str:
    return db.scalar(select(User.username).where(User.id == user_id)) or "unknown"


def _to_out(db: Session, ch: Challenge, viewer_id: uuid.UUID) -> ChallengeOut:
    return ChallengeOut(
        id=ch.id, status=ch.status.value,
        challenger_id=ch.challenger_id, challenger_username=_username(db, ch.challenger_id),
        opponent_id=ch.opponent_id, opponent_username=_username(db, ch.opponent_id),
        duration_days=ch.duration_days, start_at=ch.start_at, end_at=ch.end_at,
        winner_id=ch.winner_id, created_at=ch.created_at,
        viewer_is_challenger=(viewer_id == ch.challenger_id),
    )


@router.post("", response_model=ChallengeOut, status_code=status.HTTP_201_CREATED)
def create_challenge(body: ChallengeCreate, user: User = Depends(get_current_user),
                     db: Session = Depends(get_db)) -> ChallengeOut:
    opponent = db.scalar(select(User).where(User.username == body.opponent_username))
    if opponent is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "opponent not found")
    if opponent.id == user.id:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "cannot challenge yourself")
    _require_member(db, body.challenger_portfolio_id, user.id)

    ch = Challenge(
        challenger_id=user.id, challenger_portfolio_id=body.challenger_portfolio_id,
        opponent_id=opponent.id, duration_days=body.duration_days,
        status=ChallengeStatus.PENDING,
    )
    db.add(ch); db.commit(); db.refresh(ch)
    return _to_out(db, ch, user.id)


@router.get("", response_model=list[ChallengeOut])
def list_challenges(user: User = Depends(get_current_user),
                    db: Session = Depends(get_db)) -> list[ChallengeOut]:
    rows = db.execute(
        select(Challenge).where(
            or_(Challenge.challenger_id == user.id, Challenge.opponent_id == user.id))
        .order_by(Challenge.created_at.desc())
    ).scalars().all()
    return [_to_out(db, ch, user.id) for ch in rows]


def _get_participant_challenge(db: Session, challenge_id: uuid.UUID,
                               user_id: uuid.UUID) -> Challenge:
    ch = db.get(Challenge, challenge_id)
    if ch is None or user_id not in (ch.challenger_id, ch.opponent_id):
        # 404 (not 403) so non-participants can't probe challenge IDs.
        raise HTTPException(status.HTTP_404_NOT_FOUND, "challenge not found")
    return ch


@router.post("/{challenge_id}/accept", response_model=ChallengeOut)
def accept_challenge(challenge_id: uuid.UUID, body: ChallengeAccept,
                     user: User = Depends(get_current_user),
                     db: Session = Depends(get_db)) -> ChallengeOut:
    ch = _get_participant_challenge(db, challenge_id, user.id)
    if user.id != ch.opponent_id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "only the opponent can accept")
    if ch.status != ChallengeStatus.PENDING:
        raise HTTPException(status.HTTP_409_CONFLICT, f"challenge is {ch.status.value}")
    _require_member(db, body.opponent_portfolio_id, user.id)

    now = datetime.now(timezone.utc)
    ch.opponent_portfolio_id = body.opponent_portfolio_id
    ch.start_at = now
    ch.end_at = now + timedelta(days=ch.duration_days)
    ch.challenger_baseline = current_equity(db, ch.challenger_portfolio_id)
    ch.opponent_baseline = current_equity(db, ch.opponent_portfolio_id)
    ch.status = ChallengeStatus.ACTIVE
    db.commit(); db.refresh(ch)
    return _to_out(db, ch, user.id)


@router.post("/{challenge_id}/decline", response_model=ChallengeOut)
def decline_challenge(challenge_id: uuid.UUID, user: User = Depends(get_current_user),
                      db: Session = Depends(get_db)) -> ChallengeOut:
    ch = _get_participant_challenge(db, challenge_id, user.id)
    if user.id != ch.opponent_id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "only the opponent can decline")
    if ch.status != ChallengeStatus.PENDING:
        raise HTTPException(status.HTTP_409_CONFLICT, f"challenge is {ch.status.value}")
    ch.status = ChallengeStatus.DECLINED
    db.commit(); db.refresh(ch)
    return _to_out(db, ch, user.id)


@router.post("/{challenge_id}/cancel", response_model=ChallengeOut)
def cancel_challenge(challenge_id: uuid.UUID, user: User = Depends(get_current_user),
                     db: Session = Depends(get_db)) -> ChallengeOut:
    ch = _get_participant_challenge(db, challenge_id, user.id)
    if user.id != ch.challenger_id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "only the challenger can cancel")
    if ch.status != ChallengeStatus.PENDING:
        # An active challenge runs to end_at; neither side can bail.
        raise HTTPException(status.HTTP_409_CONFLICT,
                            f"cannot cancel a {ch.status.value} challenge")
    ch.status = ChallengeStatus.CANCELLED
    db.commit(); db.refresh(ch)
    return _to_out(db, ch, user.id)


@router.get("/{challenge_id}", response_model=HeadToHeadOut)
def head_to_head(challenge_id: uuid.UUID, user: User = Depends(get_current_user),
                 db: Session = Depends(get_db)) -> HeadToHeadOut:
    ch = _get_participant_challenge(db, challenge_id, user.id)
    if ch.status not in (ChallengeStatus.ACTIVE, ChallengeStatus.FINISHED):
        raise HTTPException(status.HTTP_409_CONFLICT,
                            f"no comparison for a {ch.status.value} challenge")

    if ch.status == ChallengeStatus.FINISHED:
        frozen = True
        ch_m = ch.final_metrics["challenger"]
        op_m = ch.final_metrics["opponent"]
    else:
        frozen = False
        # On-demand "live-ish" marks: refresh prices for both books' open
        # positions (cache-bounded) so an active competition's standings tick.
        held = set(db.scalars(select(Position.asset_id).where(
            Position.portfolio_id.in_(
                [ch.challenger_portfolio_id, ch.opponent_portfolio_id]),
            Position.qty != 0)).all())
        sync_prices(db, held)
        ch_m = windowed_metrics(db, ch.challenger_portfolio_id,
                                ch.challenger_baseline, ch.start_at)
        op_m = windowed_metrics(db, ch.opponent_portfolio_id,
                                ch.opponent_baseline, ch.start_at)

    mine, theirs = ((ch_m, op_m) if user.id == ch.challenger_id else (op_m, ch_m))
    return HeadToHeadOut(
        challenge=_to_out(db, ch, user.id),
        you=OpponentMetricsOut(**mine), them=OpponentMetricsOut(**theirs),
        frozen=frozen,
    )
