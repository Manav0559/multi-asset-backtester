import type { Metadata } from "next";
import "./globals.css";
import ToastProvider from "@/components/ToastProvider";

export const metadata: Metadata = {
  title: "Backtester — Quant Workspace",
  description: "Multi-asset backtesting & multiplayer paper trading",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>
        {/* Ambient light sources the glass surfaces refract. Fixed + blurred,
            purely decorative. */}
        <div aria-hidden className="fixed inset-0 -z-10 overflow-hidden pointer-events-none">
          <div className="absolute -top-40 -left-32 h-[34rem] w-[34rem] rounded-full
                          bg-accent/[0.07] blur-3xl animate-drift" />
          <div className="absolute -top-24 right-[-10rem] h-[28rem] w-[28rem] rounded-full
                          bg-accent2/[0.07] blur-3xl animate-drift [animation-delay:-9s]" />
        </div>
        <ToastProvider>{children}</ToastProvider>
      </body>
    </html>
  );
}
