"use client";

/**
 * Route transition shell. App Router remounts template.tsx on every
 * navigation, so this one motion.div gives every page a consistent
 * fade-and-rise entrance without touching page files.
 */
import { motion } from "framer-motion";

export default function Template({ children }: { children: React.ReactNode }) {
  return (
    <motion.div
      initial={{ opacity: 0, y: 10 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.35, ease: [0.22, 1, 0.36, 1] }}
    >
      {children}
    </motion.div>
  );
}
