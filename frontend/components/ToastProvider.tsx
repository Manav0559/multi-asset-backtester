"use client";

/**
 * Glass toast notifications. `useToast()` anywhere under the provider:
 *   const toast = useToast();
 *   toast.success("Filled buy 5 @ $120.00");
 *   toast.error("Insufficient cash");
 * Auto-dismisses after 4s; AnimatePresence handles enter/exit springs.
 */
import { createContext, useCallback, useContext, useRef, useState } from "react";
import { AnimatePresence, motion } from "framer-motion";

type Toast = { id: number; kind: "success" | "error" | "info"; text: string };
type ToastApi = {
  success: (text: string) => void;
  error: (text: string) => void;
  info: (text: string) => void;
};

const ToastContext = createContext<ToastApi | null>(null);

export function useToast(): ToastApi {
  const ctx = useContext(ToastContext);
  if (!ctx) throw new Error("useToast must be used inside <ToastProvider>");
  return ctx;
}

const KIND_STYLE: Record<Toast["kind"], string> = {
  success: "border-up/40 text-up",
  error: "border-down/40 text-down",
  info: "border-accent/40 text-accent",
};
const KIND_ICON: Record<Toast["kind"], string> = { success: "✓", error: "✕", info: "◈" };

export default function ToastProvider({ children }: { children: React.ReactNode }) {
  const [toasts, setToasts] = useState<Toast[]>([]);
  const nextId = useRef(1);

  const push = useCallback((kind: Toast["kind"], text: string) => {
    const id = nextId.current++;
    setToasts((t) => [...t, { id, kind, text }]);
    setTimeout(() => setToasts((t) => t.filter((x) => x.id !== id)), 4000);
  }, []);

  const api: ToastApi = {
    success: (text) => push("success", text),
    error: (text) => push("error", text),
    info: (text) => push("info", text),
  };

  return (
    <ToastContext.Provider value={api}>
      {children}
      <div className="fixed bottom-6 right-6 z-50 flex flex-col gap-2 pointer-events-none">
        <AnimatePresence>
          {toasts.map((t) => (
            <motion.div
              key={t.id}
              initial={{ opacity: 0, x: 40, scale: 0.95 }}
              animate={{ opacity: 1, x: 0, scale: 1 }}
              exit={{ opacity: 0, x: 40, scale: 0.95 }}
              transition={{ type: "spring", stiffness: 400, damping: 30 }}
              className={`card pointer-events-auto flex items-center gap-3 px-4 py-3
                          text-sm max-w-sm border ${KIND_STYLE[t.kind]}`}
            >
              <span className="font-mono">{KIND_ICON[t.kind]}</span>
              <span className="text-slate-200">{t.text}</span>
            </motion.div>
          ))}
        </AnimatePresence>
      </div>
    </ToastContext.Provider>
  );
}
