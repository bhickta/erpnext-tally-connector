import { cp, mkdir, rm } from "node:fs/promises";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const packageRoot = resolve(here, "..");
const source = resolve(packageRoot, "src");
const output = resolve(packageRoot, "..", "express_tally", "bridge", "web");

if (process.argv.includes("--check")) {
  process.stdout.write(`Control Centre source: ${source}\n`);
  process.exit(0);
}

await rm(output, { recursive: true, force: true });
await mkdir(output, { recursive: true });
await cp(source, output, { recursive: true });
process.stdout.write(`Built Control Centre into ${output}\n`);
