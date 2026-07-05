"use client";

/**
 * Presence UI for a shared portfolio room. `PresenceAvatars` renders a stacked
 * row of colored-initial avatars for everyone currently online (fed by the
 * portfolio:{id} presence broadcast + the REST roster). `TypingLine` renders a
 * transient "X is typing…" hint. Both are purely presentational — the live
 * state lives in the page/ChatPanel that own the WS subscription.
 */

export type OnlineUser = { user_id: string; username: string };

// Deterministic hue per username so an avatar keeps its color across renders.
function hueFor(name: string): number {
  let h = 0;
  for (let i = 0; i < name.length; i++) h = (h * 31 + name.charCodeAt(i)) % 360;
  return h;
}

function initials(name: string): string {
  const parts = name.replace(/[^a-zA-Z0-9 ]/g, " ").trim().split(/\s+/);
  if (parts.length >= 2) return (parts[0][0] + parts[1][0]).toUpperCase();
  return name.slice(0, 2).toUpperCase();
}

export function Avatar({ username, size = 26, ring = true }: {
  username: string; size?: number; ring?: boolean;
}) {
  const hue = hueFor(username);
  return (
    <span
      title={username}
      className={`inline-flex items-center justify-center rounded-full font-semibold
                  text-white select-none ${ring ? "ring-2 ring-bg" : ""}`}
      style={{
        width: size, height: size, fontSize: size * 0.4,
        background: `linear-gradient(135deg, hsl(${hue} 65% 45%), hsl(${(hue + 40) % 360} 65% 38%))`,
      }}
    >
      {initials(username)}
    </span>
  );
}

export function PresenceAvatars({ online, max = 5 }: { online: OnlineUser[]; max?: number }) {
  if (!online.length) {
    return <span className="text-xs text-muted">No one else online</span>;
  }
  const shown = online.slice(0, max);
  const extra = online.length - shown.length;
  const names = online.map((u) => u.username).join(", ");
  return (
    <div className="flex items-center gap-2" title={`Online: ${names}`}>
      <div className="flex -space-x-2">
        {shown.map((u) => <Avatar key={u.user_id} username={u.username} />)}
        {extra > 0 && (
          <span className="inline-flex items-center justify-center rounded-full ring-2 ring-bg
                           bg-slate-700 text-slate-200 text-[10px] font-semibold h-[26px] w-[26px]">
            +{extra}
          </span>
        )}
      </div>
      <span className="flex items-center gap-1.5 text-xs text-muted">
        <span className="h-1.5 w-1.5 rounded-full bg-up animate-pulse" />
        {online.length} online
      </span>
    </div>
  );
}

export function TypingLine({ names }: { names: string[] }) {
  if (!names.length) return null;
  const label =
    names.length === 1 ? `${names[0]} is typing`
    : names.length === 2 ? `${names[0]} and ${names[1]} are typing`
    : `${names.length} people are typing`;
  return (
    <p className="text-xs text-muted italic flex items-center gap-1.5 h-4">
      <span className="typing-dots"><span /><span /><span /></span>
      {label}…
    </p>
  );
}
