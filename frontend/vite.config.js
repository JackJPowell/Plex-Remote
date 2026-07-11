import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      "/status": "http://127.0.0.1:8000",
      "/tv": "http://127.0.0.1:8000",
      "/sunshine": "http://127.0.0.1:8000",
      "/plex": "http://127.0.0.1:8000",
      "/playback": "http://127.0.0.1:8000",
      "/messages": "http://127.0.0.1:8000",
      "/debug": "http://127.0.0.1:8000"
    }
  }
});
