import { readdir } from "node:fs/promises";
import { join, extname } from "node:path";
import { spawnSync } from "node:child_process";

const roots = ["app", "components", "e2e", "lib", "scripts"];
const files = [];
async function collect(dir) {
  for (const entry of await readdir(dir, { withFileTypes: true })) {
    const path = join(dir, entry.name);
    if (entry.isDirectory()) await collect(path);
    else if ([".js", ".mjs"].includes(extname(entry.name)) && !path.endsWith(".test.mjs")) files.push(path);
  }
}
for (const root of roots) await collect(root);
for (const file of files) {
  const result = spawnSync(process.execPath, ["--check", file], { stdio: "inherit" });
  if (result.status !== 0) process.exit(result.status ?? 1);
}
console.log(`frontend lint: checked ${files.length} JavaScript files`);
