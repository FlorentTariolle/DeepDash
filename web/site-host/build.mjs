import { cp, mkdir, rm } from "node:fs/promises";
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

console.log(`Prepared ${clientDir}`);
