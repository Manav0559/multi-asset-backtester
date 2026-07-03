"use client";

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { isAuthed } from "@/lib/auth";
import Nav from "./Nav";

// Client-side auth guard: redirects to /login when there's no token.
// Wraps every authenticated page so the nav + gating live in one place.
export default function Guard({ children }: { children: React.ReactNode }) {
  const router = useRouter();
  const [ok, setOk] = useState(false);

  useEffect(() => {
    if (!isAuthed()) router.replace("/login");
    else setOk(true);
  }, [router]);

  if (!ok) return null;
  return (
    <div className="min-h-screen">
      <Nav />
      {/* .stagger: every page's top-level sections rise in sequence. */}
      <main className="stagger max-w-7xl mx-auto px-6 max-sm:px-4 py-8">{children}</main>
    </div>
  );
}
