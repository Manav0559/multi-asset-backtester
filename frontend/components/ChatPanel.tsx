"use client";

/**
 * Portfolio chat. History over REST, live messages over the SAME portfolio:{id}
 * WS room the rest of the page already uses (events with type="chat"). Member-
 * only is enforced server-side; a 429 surfaces as a toast.
 */
import { useEffect, useRef, useState } from "react";
import { api } from "@/lib/api";
import { Hub } from "@/lib/ws";
import { useToast } from "@/components/ToastProvider";

type Msg = { id: string; user_id: string; username: string; body: string;
  deleted: boolean; created_at: string };

export default function ChatPanel({ portfolioId }: { portfolioId: string }) {
  const [msgs, setMsgs] = useState<Msg[]>([]);
  const [text, setText] = useState("");
  const [loaded, setLoaded] = useState(false);
  const bottomRef = useRef<HTMLDivElement>(null);
  const toast = useToast();

  useEffect(() => {
    api<{ messages: Msg[] }>(`/portfolios/${portfolioId}/chat`)
      .then((p) => setMsgs([...p.messages].reverse())) // oldest-first for display
      .finally(() => setLoaded(true));

    const hub = new Hub();
    hub.connect();
    const off = hub.subscribe(`portfolio:${portfolioId}`, (evt: any) => {
      if (evt?.type === "chat") {
        setMsgs((m) => m.some((x) => x.id === evt.id) ? m : [...m, {
          id: evt.id, user_id: evt.user_id, username: evt.username,
          body: evt.body, deleted: false, created_at: evt.created_at }]);
      } else if (evt?.type === "chat_deleted") {
        setMsgs((m) => m.map((x) => x.id === evt.id ? { ...x, deleted: true, body: "" } : x));
      }
    });
    return () => { off(); hub.close(); };
  }, [portfolioId]);

  useEffect(() => { bottomRef.current?.scrollIntoView({ behavior: "smooth" }); }, [msgs]);

  async function send(e: React.FormEvent) {
    e.preventDefault();
    const body = text.trim();
    if (!body) return;
    setText("");
    try {
      await api(`/portfolios/${portfolioId}/chat`, { method: "POST", body: JSON.stringify({ body }) });
      // The WS echo appends it; nothing else to do.
    } catch (err: any) {
      toast.error(err.status === 429 ? "Slow down — 10 messages / 10s" : err.message);
      setText(body);
    }
  }

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
      <form onSubmit={send} className="mt-3 flex gap-2">
        <input className="input flex-1" value={text} maxLength={2000}
          placeholder="Message the team…" onChange={(e) => setText(e.target.value)} />
        <button className="btn-primary" disabled={!text.trim()}>Send</button>
      </form>
    </div>
  );
}
