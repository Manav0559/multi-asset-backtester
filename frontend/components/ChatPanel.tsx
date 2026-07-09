"use client";

/**
 * Portfolio chat. History over REST, live messages over the SAME portfolio:{id}
 * WS room the rest of the page already uses (events with type="chat"). Member-
 * only is enforced server-side; a 429 surfaces as a toast.
 */
import { useEffect, useRef, useState } from "react";
import { api } from "@/lib/api";
import { currentUserId } from "@/lib/auth";
import { Hub } from "@/lib/ws";
import { TypingLine } from "@/components/Presence";
import { useToast } from "@/components/ToastProvider";

type Msg = { id: string; user_id: string; username: string; body: string;
  deleted: boolean; created_at: string };

const TYPING_TTL_MS = 3000;   // a typer is "active" for this long after their last ping

export default function ChatPanel({ portfolioId }: { portfolioId: string }) {
  const [msgs, setMsgs] = useState<Msg[]>([]);
  const [text, setText] = useState("");
  const [loaded, setLoaded] = useState(false);
  const [typers, setTypers] = useState<Record<string, { username: string; at: number }>>({});
  const bottomRef = useRef<HTMLDivElement>(null);
  const hubRef = useRef<Hub | null>(null);
  const lastTypingSent = useRef(0);
  const me = useRef<string | null>(null);
  const toast = useToast();

  useEffect(() => {
    me.current = currentUserId();
    api<{ messages: Msg[] }>(`/portfolios/${portfolioId}/chat`)
      .then((p) => setMsgs([...p.messages].reverse())) // oldest-first for display
      .catch(() => {})
      .finally(() => setLoaded(true));

    const hub = new Hub();
    hubRef.current = hub;
    hub.connect();
    const off = hub.subscribe(`portfolio:${portfolioId}`, (evt: any) => {
      if (evt?.type === "chat") {
        setMsgs((m) => m.some((x) => x.id === evt.id) ? m : [...m, {
          id: evt.id, user_id: evt.user_id, username: evt.username,
          body: evt.body, deleted: false, created_at: evt.created_at }]);
        // a delivered message ends that author's typing state
        setTypers((t) => {
          if (!(evt.user_id in t)) return t;
          const rest = { ...t }; delete rest[evt.user_id]; return rest;
        });
      } else if (evt?.type === "chat_deleted") {
        setMsgs((m) => m.map((x) => x.id === evt.id ? { ...x, deleted: true, body: "" } : x));
      } else if (evt?.type === "typing" && evt.user_id !== me.current) {
        setTypers((t) => ({ ...t, [evt.user_id]: { username: evt.username, at: Date.now() } }));
      }
    });
    return () => { off(); hub.close(); hubRef.current = null; };
  }, [portfolioId]);

  // Age out stale typers so the hint disappears when they stop.
  useEffect(() => {
    const iv = setInterval(() => {
      setTypers((t) => {
        const now = Date.now();
        const kept = Object.fromEntries(Object.entries(t).filter(([, v]) => now - v.at < TYPING_TTL_MS));
        return Object.keys(kept).length === Object.keys(t).length ? t : kept;
      });
    }, 700);
    return () => clearInterval(iv);
  }, []);

  useEffect(() => { bottomRef.current?.scrollIntoView({ behavior: "smooth" }); }, [msgs]);

  function onType(v: string) {
    setText(v);
    // Throttle typing pings to one every 1.5s — enough to keep the hint alive
    // (TTL 3s) without a frame per keystroke.
    const now = Date.now();
    if (v && now - lastTypingSent.current > 1500) {
      lastTypingSent.current = now;
      hubRef.current?.emit("typing", { portfolio: portfolioId });
    }
  }

  async function send(e: React.FormEvent) {
    e.preventDefault();
    const body = text.trim();
    if (!body) return;
    setText("");
    lastTypingSent.current = 0; // allow an immediate ping for the next message
    try {
      await api(`/portfolios/${portfolioId}/chat`, { method: "POST", body: JSON.stringify({ body }) });
      // The WS echo appends it; nothing else to do.
    } catch (err: any) {
      toast.error(err.status === 429 ? "Slow down — 10 messages / 10s" : err.message);
      setText(body);
    }
  }

  const typingNames = Object.values(typers)
    .filter((v) => Date.now() - v.at < TYPING_TTL_MS)
    .map((v) => v.username);

  return (
    <div className="card p-5 flex flex-col h-[26rem]">
      <h2 className="text-sm font-medium mb-3">Team chat</h2>
      <div className="flex-1 overflow-y-auto space-y-2 pr-1">
        {loaded && msgs.length === 0 && (
          <p className="text-muted text-xs py-4 text-center">No messages yet — say hi 👋</p>
        )}
        {msgs.map((m) => (
          <div key={m.id} className="text-sm">
            <span className="font-mono text-xs text-accent">{m.username}</span>{" "}
            {m.deleted ? <span className="text-muted italic text-xs">message deleted</span>
                       : <span className="text-slate-200">{m.body}</span>}
          </div>
        ))}
        <div ref={bottomRef} />
      </div>
      <div className="min-h-4 mt-2"><TypingLine names={typingNames} /></div>
      <form onSubmit={send} className="mt-1 flex gap-2">
        <input className="input flex-1" value={text} maxLength={2000}
          placeholder="Message the team…" onChange={(e) => onType(e.target.value)} />
        <button className="btn-primary" disabled={!text.trim()}>Send</button>
      </form>
    </div>
  );
}
