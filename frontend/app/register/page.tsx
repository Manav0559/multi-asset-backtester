"use client";

import { useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { api } from "@/lib/api";
import { setTokens } from "@/lib/auth";
import { AuthShell } from "@/components/AuthShell";

export default function RegisterPage() {
  const router = useRouter();
  const [email, setEmail] = useState("");
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [err, setErr] = useState("");
  const [loading, setLoading] = useState(false);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setErr("");
    setLoading(true);
    try {
      await api("/auth/register", {
        method: "POST", auth: false,
        body: JSON.stringify({ email, username, password }),
      });
      // Auto-login right after registration.
      const t = await api<{ access_token: string; refresh_token: string }>(
        "/auth/login",
        { method: "POST", auth: false, body: JSON.stringify({ email, password }) }
      );
      setTokens(t.access_token, t.refresh_token);
      router.push("/dashboard");
    } catch (e: any) {
      setErr(e.message || "Registration failed");
    } finally {
      setLoading(false);
    }
  }

  return (
    <AuthShell title="Create account" subtitle="Start backtesting in seconds">
      <form onSubmit={submit} className="space-y-4">
        <div>
          <label className="label">Email</label>
          <input className="input" type="email" value={email} required
            onChange={(e) => setEmail(e.target.value)} placeholder="you@example.com" />
        </div>
        <div>
          <label className="label">Username</label>
          <input className="input" value={username} required minLength={3}
            onChange={(e) => setUsername(e.target.value)} placeholder="quant_alice" />
        </div>
        <div>
          <label className="label">Password</label>
          <input className="input" type="password" value={password} required minLength={8}
            onChange={(e) => setPassword(e.target.value)} placeholder="At least 8 characters" />
        </div>
        {err && <p className="text-down text-sm">{err}</p>}
        <button className="btn-primary w-full" disabled={loading}>
          {loading ? "Creating…" : "Create account"}
        </button>
      </form>
      <p className="text-sm text-muted mt-6 text-center">
        Already have an account?{" "}
        <Link href="/login" className="text-accent hover:underline">Sign in</Link>
      </p>
    </AuthShell>
  );
}
