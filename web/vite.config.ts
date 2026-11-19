import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Dev: `npm run dev` → http://localhost:5173, API-Aufrufe gehen an den Harness auf :8100
export default defineConfig({
  plugins: [react()],
  build: { outDir: "dist", emptyOutDir: true, chunkSizeWarningLimit: 1500 },
  server: {
    proxy: {
      "/api": "http://localhost:8100",
      "/memory": "http://localhost:8100",
      "/mcp": "http://localhost:8100",
    },
  },
});
