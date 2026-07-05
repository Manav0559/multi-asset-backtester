"use client";

import { getAccess } from "./auth";

// Thin wrapper over the FastAPI /ws hub: connects with the JWT, exposes
// subscribe/unsubscribe, and dispatches relayed messages by channel.
export type HubMessage = { type: string; channel?: string; data?: any };

export class Hub {
  private ws: WebSocket | null = null;
  private handlers = new Map<string, Set<(data: any) => void>>();
  private desired = new Set<string>();
  private reconnectTimer: any = null;

  connect() {
    const token = getAccess();
    if (!token) return;
    const proto = window.location.protocol === "https:" ? "wss" : "ws";
    // WS bypasses the Next rewrite; talk to the backend directly in dev.
    const host = process.env.NEXT_PUBLIC_WS_HOST || "localhost:8000";
    this.ws = new WebSocket(`${proto}://${host}/ws?token=${token}`);

    this.ws.onopen = () => {
      if (this.desired.size) this.send("subscribe", [...this.desired]);
    };
    this.ws.onmessage = (ev) => {
      const msg: HubMessage = JSON.parse(ev.data);
      if (msg.type === "message" && msg.channel) {
        this.handlers.get(msg.channel)?.forEach((h) => h(msg.data));
      }
    };
    this.ws.onclose = () => this.scheduleReconnect();
  }

  private scheduleReconnect() {
    if (this.reconnectTimer) return;
    this.reconnectTimer = setTimeout(() => {
      this.reconnectTimer = null;
      this.connect();
    }, 2000);
  }

  private send(action: string, channels: string[]) {
    if (this.ws?.readyState === WebSocket.OPEN) {
      this.ws.send(JSON.stringify({ action, channels }));
    }
  }

  // Fire an arbitrary action frame (e.g. {action:"typing", portfolio}). Dropped
  // silently if the socket isn't open — callers are best-effort (typing pings).
  emit(action: string, payload: Record<string, any> = {}) {
    if (this.ws?.readyState === WebSocket.OPEN) {
      this.ws.send(JSON.stringify({ action, ...payload }));
    }
  }

  subscribe(channel: string, handler: (data: any) => void) {
    if (!this.handlers.has(channel)) this.handlers.set(channel, new Set());
    this.handlers.get(channel)!.add(handler);
    this.desired.add(channel);
    this.send("subscribe", [channel]);
    return () => this.unsubscribe(channel, handler);
  }

  unsubscribe(channel: string, handler: (data: any) => void) {
    this.handlers.get(channel)?.delete(handler);
    if (!this.handlers.get(channel)?.size) {
      this.desired.delete(channel);
      this.send("unsubscribe", [channel]);
    }
  }

  close() {
    this.ws?.close();
    this.ws = null;
  }
}
