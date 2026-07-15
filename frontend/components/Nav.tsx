"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { motion, AnimatePresence } from "framer-motion";
import { clearTokens } from "@/lib/auth";

const LINKS = [
  { href: "/dashboard", label: "Dashboard" },
  { href: "/portfolios", label: "Portfolios" },
  { href: "/backtests", label: "Backtests" },
  { href: "/compete", label: "Compete" },
];

export default function Nav() {
  const pathname = usePathname();
  const router = useRouter();
  const [open, setOpen] = useState(false);

  // Collapse the mobile menu whenever the route changes.
  useEffect(() => { setOpen(false); }, [pathname]);

  function signOut() {
    clearTokens();
    router.push("/login");
  }

  return (
    <header className="sticky top-0 z-20 border-b border-white/[0.07]
                       bg-bg/55 backdrop-blur-xl backdrop-saturate-150">
      <div className="max-w-7xl mx-auto px-4 sm:px-6 h-14 flex items-center justify-between gap-3">
        <div className="flex items-center gap-4 sm:gap-8 min-w-0">
          <Link href="/dashboard" className="flex items-center gap-2 font-semibold group shrink-0">
            <span className="text-accent text-lg drop-shadow-[0_0_8px_rgba(34,211,238,0.6)]
                             transition-transform duration-300 group-hover:rotate-45">◈</span>
            <span>Backtester</span>
          </Link>
          {/* Desktop nav — the glass pill glides between links via a shared layoutId. */}
          <nav className="hidden sm:flex items-center gap-1">
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
          onClick={signOut}
          className="hidden sm:block text-sm text-muted hover:text-slate-200
                     transition-colors whitespace-nowrap"
        >
          Sign out
        </button>

        {/* Mobile: hamburger toggles the dropdown below. */}
        <button
          onClick={() => setOpen((o) => !o)}
          aria-label={open ? "Close menu" : "Open menu"}
          aria-expanded={open}
          className="sm:hidden inline-flex items-center justify-center h-9 w-9 rounded-lg
                     text-slate-300 hover:text-slate-100 hover:bg-white/[0.06] transition-colors"
        >
          <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor"
               strokeWidth="2" strokeLinecap="round">
            {open ? (
              <><line x1="6" y1="6" x2="18" y2="18" /><line x1="6" y1="18" x2="18" y2="6" /></>
            ) : (
              <><line x1="3" y1="6" x2="21" y2="6" /><line x1="3" y1="12" x2="21" y2="12" /><line x1="3" y1="18" x2="21" y2="18" /></>
            )}
          </svg>
        </button>
      </div>

      <AnimatePresence>
        {open && (
          <motion.nav
            initial={{ opacity: 0, height: 0 }}
            animate={{ opacity: 1, height: "auto" }}
            exit={{ opacity: 0, height: 0 }}
            transition={{ duration: 0.2, ease: [0.22, 1, 0.36, 1] }}
            className="sm:hidden overflow-hidden border-t border-white/[0.06]
                       bg-bg/80 backdrop-blur-xl"
          >
            <div className="px-4 py-2 flex flex-col">
              {LINKS.map((l) => {
                const active = pathname.startsWith(l.href);
                return (
                  <Link
                    key={l.href}
                    href={l.href}
                    className={`px-3 py-2.5 rounded-lg text-sm transition-colors ${
                      active ? "text-accent bg-white/[0.06]" : "text-slate-300 hover:bg-white/[0.04]"
                    }`}
                  >
                    {l.label}
                  </Link>
                );
              })}
              <button
                onClick={signOut}
                className="text-left px-3 py-2.5 rounded-lg text-sm text-muted
                           hover:text-slate-200 hover:bg-white/[0.04] transition-colors"
              >
                Sign out
              </button>
            </div>
          </motion.nav>
        )}
      </AnimatePresence>
    </header>
  );
}
