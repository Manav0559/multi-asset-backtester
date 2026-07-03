"use client";

import React from "react";
import { motion } from "framer-motion";

// Shared centered glass frame for the login/register pages. Lives here (not in
// a page file) because Next.js App Router forbids non-default exports from
// `page.tsx`.
export function AuthShell({ title, subtitle, children }: {
  title: string; subtitle: string; children: React.ReactNode;
}) {
  return (
    <div className="min-h-screen flex items-center justify-center px-6">
      <motion.div
        initial={{ opacity: 0, y: 18, scale: 0.98 }}
        animate={{ opacity: 1, y: 0, scale: 1 }}
        transition={{ duration: 0.45, ease: [0.22, 1, 0.36, 1] }}
        className="w-full max-w-sm"
      >
        <div className="flex items-center gap-2 justify-center mb-8">
          <span className="text-accent text-2xl drop-shadow-[0_0_12px_rgba(34,211,238,0.7)]">◈</span>
          <span className="text-xl font-semibold">Backtester</span>
        </div>
        <div className="card p-8">
          <h1 className="text-lg font-semibold mb-1">{title}</h1>
          <p className="text-sm text-muted mb-6">{subtitle}</p>
          {children}
        </div>
      </motion.div>
    </div>
  );
}
