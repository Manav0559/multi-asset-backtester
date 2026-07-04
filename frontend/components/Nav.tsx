"use client";

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { motion } from "framer-motion";
import { clearTokens } from "@/lib/auth";

const LINKS = [
  { href: "/dashboard", label: "Dashboard" },
  { href: "/portfolios", label: "Portfolios" },
  { href: "/backtests", label: "Backtests" },
  { href: "/leaderboard", label: "Leaderboard" },
  { href: "/compete", label: "Compete" },
];

export default function Nav() {
  const pathname = usePathname();
  const router = useRouter();

  return (
    <header className="sticky top-0 z-20 border-b border-white/[0.07]
                       bg-bg/55 backdrop-blur-xl backdrop-saturate-150">
      <div className="max-w-7xl mx-auto px-6 h-14 flex items-center justify-between">
        <div className="flex items-center gap-8">
          <Link href="/dashboard" className="flex items-center gap-2 font-semibold group">
            <span className="text-accent text-lg drop-shadow-[0_0_8px_rgba(34,211,238,0.6)]
                             transition-transform duration-300 group-hover:rotate-45">◈</span>
            <span>Backtester</span>
          </Link>
          <nav className="flex items-center gap-1">
            {LINKS.map((l) => {
              const active = pathname.startsWith(l.href);
              return (
                <Link
                  key={l.href}
                  href={l.href}
                  className={`relative px-3 py-1.5 rounded-xl text-sm transition-colors ${
                    active ? "text-accent" : "text-slate-400 hover:text-slate-200"
                  }`}
                >
                  {active && (
                    // Shared layoutId: the glass pill glides between links.
                    <motion.span
                      layoutId="nav-pill"
                      transition={{ type: "spring", stiffness: 450, damping: 35 }}
                      className="absolute inset-0 rounded-xl bg-white/[0.06]
                                 border border-white/[0.09] shadow-glow-sm"
                    />
                  )}
                  <span className="relative">{l.label}</span>
                </Link>
              );
            })}
          </nav>
        </div>
        <button
          onClick={() => {
            clearTokens();
            router.push("/login");
          }}
          className="text-sm text-muted hover:text-slate-200 transition-colors"
        >
          Sign out
        </button>
      </div>
    </header>
  );
}
