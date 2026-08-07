import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";

const securityHeaders = {
  "Content-Security-Policy": "default-src 'self'; connect-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; frame-src 'self'; form-action 'self'",
  "Cross-Origin-Opener-Policy": "same-origin",
  "Referrer-Policy": "no-referrer",
  "X-Content-Type-Options": "nosniff",
};

export default defineConfig({
  plugins: [react()],
  server: {
    headers: securityHeaders,
    proxy: {
      "/v1": "http://127.0.0.1:8080",
      "/objects": "http://127.0.0.1:8080",
    },
  },
  preview: { headers: securityHeaders },
  test: {
    environment: "jsdom",
    include: ["src/**/*.test.ts", "src/**/*.test.tsx"],
    restoreMocks: true,
  },
});
