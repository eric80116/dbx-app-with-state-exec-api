import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Build the SPA straight into the FastAPI static dir; use relative asset base
// so it works under the Databricks Apps URL prefix.
export default defineConfig({
  plugins: [react()],
  base: "./",
  build: {
    outDir: "../backend/static",
    emptyOutDir: true,
  },
  server: {
    // local dev: proxy API calls to the FastAPI backend on :8077
    proxy: {
      "/api": "http://127.0.0.1:8077",
    },
  },
});
