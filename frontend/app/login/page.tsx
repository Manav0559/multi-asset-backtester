"use client";

import { useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { api } from "@/lib/api";
import { setTokens } from "@/lib/auth";
import { AuthShell } from "@/components/AuthShell";

export default function LoginPage() {
  const router = useRouter();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [err, setErr] = useState("");
  const [loading, setLoading] = useState(false);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setErr("");
    setLoading(true);
    try {
      const t = await api<{ access_token: string; refresh_token: string }>(
        "/auth/login",
        { method: "POST", auth: false, body: JSON.stringify({ email, password }) }
      );
      setTokens(t.access_token, t.refresh_token);
      router.push("/dashboard");
    } catch (e: any) {
      setErr(e.message || "Login failed");
    } finally {
      setLoading(false);
    }
  }

  return (
    <AuthShell title="Sign in" subtitle="Access your quant workspace">
      <form onSubmit={submit} className="space-y-4">
        <div>
          <label className="label">Email</label>
          <input className="input" type="email" value={email} required
            onChange={(e) => setEmail(e.target.value)} placeholder="you@example.com" />
        </div>
        <div>
          <label className="label">Password</label>
          <input className="input" type="password" value={password} required
            onChange={(e) => setPassword(e.target.value)} placeholder="••••••••" />
        </div>
        {err && <p className="text-down text-sm">{err}</p>}
        <button className="btn-primary w-full" disabled={loading}>
          {loading ? "Signing in…" : "Sign in"}
        </button>
      </form>
      <p className="text-sm text-muted mt-6 text-center">
        No account?{" "}
        <Link href="/register" className="text-accent hover:underline">Create one</Link>
      </p>
    </AuthShell>
  );
}
