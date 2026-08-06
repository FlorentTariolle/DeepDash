import { cp, mkdir, readFile, rm, writeFile } from "node:fs/promises";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const projectDir = dirname(fileURLToPath(import.meta.url));
const docsDir = resolve(projectDir, "..", "..", "docs");
const distDir = resolve(projectDir, "dist");
const clientDir = resolve(distDir, "client");
const serverDir = resolve(distDir, "server");

await rm(distDir, { recursive: true, force: true });
await mkdir(serverDir, { recursive: true });
await cp(docsDir, clientDir, { recursive: true });
await cp(resolve(projectDir, "worker.js"), resolve(serverDir, "index.js"));

// Sites serves the compact preview video used by the first <source>. The
// legacy full-length fallback exceeds the platform's per-file asset limit.
await rm(resolve(clientDir, "demo.mp4"), { force: true });
const indexPath = resolve(clientDir, "index.html");
const indexHtml = await readFile(indexPath, "utf8");
await writeFile(
  indexPath,
  indexHtml.replace(
    '          <source src="demo.mp4" type="video/mp4" />\n',
    "",
  ),
  "utf8",
);

console.log(`Prepared ${clientDir}`);
