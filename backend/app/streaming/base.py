"""Adapter base class + a small subscription spec.

Every source adapter implements `run()`, an async loop that pulls from the
vendor and pushes normalized Ticks/Bars onto the TickBus. `run()` must be
cancellation-safe (clean up sockets/sessions on CancelledError) so the
supervisor can stop it gracefully.
"""
from __future__ import annotations

import abc
import asyncio
import logging
from dataclasses import dataclass

from app.models.enums import AssetClass, Timeframe
from app.streaming.bus import TickBus

logger = logging.getLogger("streaming")


@dataclass(frozen=True)
class Subscription:
    """One instrument an adapter should stream."""
    symbol: str
    exchange: str
    asset_class: AssetClass
    timeframe: Timeframe = Timeframe.M1


class StreamAdapter(abc.ABC):
    """Base for all market-data source adapters."""

    name: str = "base"

    def __init__(self, subscriptions: list[Subscription], bus: TickBus):
        self.subscriptions = subscriptions
        self.bus = bus
        self._stopped = asyncio.Event()

    @abc.abstractmethod
    async def run(self) -> None:
        """Stream until cancelled. Publishes onto self.bus."""
        raise NotImplementedError

    def stop(self) -> None:
        self._stopped.set()

    @property
    def stopped(self) -> bool:
        return self._stopped.is_set()
