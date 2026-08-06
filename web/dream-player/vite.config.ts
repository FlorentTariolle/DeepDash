import { defineConfig } from "vite";

export default defineConfig({
  base: "./",
  build: {
    outDir: "../../docs/player",
    emptyOutDir: true,
    assetsDir: "assets",
    assetsInlineLimit: 0,
    target: "es2022",
  },
  worker: {
    format: "es",
  },
});
