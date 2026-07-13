"use client";

// Live market data: fetches the snapshot (initial price/book + channel names +
// provenance), then streams tick:/depth: updates over the WS Hub. Returns the
// latest price, the order book, a rolling trade tape, and a provenance badge so
// the UI can label every surface honestly (LIVE / DELAYED / LAST SESSION).
import { useEffect, useState } from "react";
import { api } from "./api";
import { Hub } from "./ws";

export type Provenance = "live" | "delayed" | "last_session" | "unknown";
export type BookLevel = [string, string]; // [price, size]
export type Book = { bids: BookLevel[]; asks: BookLevel[]; is_live: boolean; ts: string };
export type Trade = { price: string; qty: string; ts: string };

export type LiveState = {
  price: string | null;
  provenance: Provenance;
  book: Book | null;
  tape: Trade[];
  status: { is_open: boolean | null; label: string; next_open: string | null } | null;
  connected: boolean;
};

type Snapshot = {
  tick: { price: string; ts: string } | null;
  depth: Book | null;
  provenance: Provenance;
  channels: { tick: string; depth: string };
  status: LiveState["status"];
};

export function useLive(assetId: number | null): LiveState {
  const [state, setState] = useState<LiveState>({
    price: null, provenance: "unknown", book: null, tape: [], status: null, connected: false,
  });

  useEffect(() => {
    if (assetId == null) return;
    let cancelled = false;
    let hub: Hub | null = null;

    api<Snapshot>(`/market/${assetId}/snapshot`).then((snap) => {
      if (cancelled) return;
      setState((s) => ({
        ...s, price: snap.tick?.price ?? null, provenance: snap.provenance,
        book: snap.depth, status: snap.status, connected: true,
      }));
      hub = new Hub();
      hub.connect();
      let lastTradeMs = snap.tick ? Date.parse(snap.tick.ts) : 0;
      hub.subscribe(snap.channels.tick, (d: any) => {
        lastTradeMs = Date.now();
        setState((s) => ({
          ...s, price: d.price ?? s.price,
          tape: [{ price: d.price, qty: d.volume ?? "0", ts: d.ts }, ...s.tape].slice(0, 30),
        }));
      });
      hub.subscribe(snap.channels.depth, (d: any) =>
        setState((s) => {
          // Sparse symbols can go minutes between TRADES while the book streams
          // every 100ms — between trades, tick the price from the live mid so
          // the card moves with the market instead of freezing. Still LIVE
          // data, just a different (and clearly fresher) part of it.
          let price = s.price;
          if (d.is_live && Date.now() - lastTradeMs > 10_000 && d.bids?.length && d.asks?.length) {
            price = ((Number(d.bids[0][0]) + Number(d.asks[0][0])) / 2).toString();
          }
          return { ...s, price, book: { bids: d.bids, asks: d.asks, is_live: d.is_live, ts: d.ts } };
        }));
    }).catch(() => {});

    return () => {
      cancelled = true;
      hub?.close();
      setState((s) => ({ ...s, connected: false }));
    };
  }, [assetId]);

  return state;
}
