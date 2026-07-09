"use client";

/**
 * Global "live link lost" strip. Watches the Hub registry (lib/ws): the moment
 * any wanted WebSocket is down it slides in; each Hub already retries every 2s
 * and the banner disappears on the first successful reopen. Honesty rule: while
 * this shows, on-screen "live" data may be stale — say so plainly.
 */
import { useSyncExternalStore } from "react";
import { anyHubDown, subscribeConnectionStatus } from "@/lib/ws";

export default function ConnectionBanner() {
  const down = useSyncExternalStore(subscribeConnectionStatus, anyHubDown, () => false);
  if (!down) return null;
  return (
    <div data-testid="ws-banner"
      className="fixed top-0 inset-x-0 z-[100] flex items-center justify-center gap-2
                 bg-amber-500/15 border-b border-amber-500/30 backdrop-blur-md
                 px-4 py-1.5 text-xs text-amber-200">
      <span className="h-2 w-2 rounded-full bg-amber-400 animate-pulse" />
      Live connection lost — reconnecting… prices and team activity may be stale.
    </div>
  );
}
