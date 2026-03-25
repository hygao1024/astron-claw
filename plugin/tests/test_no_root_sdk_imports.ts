import { strict as assert } from "node:assert";
import { readFileSync } from "node:fs";
import { join } from "node:path";

const files = [
  "plugin/src/messaging/inbound.ts",
  "plugin/src/messaging/outbound.ts",
  "plugin/src/messaging/handlers.ts",
];

for (const relativePath of files) {
  const source = readFileSync(join(process.cwd(), relativePath), "utf8");
  assert.equal(
    source.includes('"openclaw/plugin-sdk"'),
    false,
    `${relativePath} should not import the root openclaw/plugin-sdk surface`,
  );
}

console.log("PASS test_no_root_sdk_imports");
