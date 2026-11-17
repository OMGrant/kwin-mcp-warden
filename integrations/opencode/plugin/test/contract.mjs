import assert from "node:assert/strict";
import { copyFile, mkdir, mkdtemp, rm } from "node:fs/promises";
import { dirname, join } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const root = dirname(dirname(fileURLToPath(import.meta.url)));
const rootPlugin = (await import("kwin-mcp-warden-opencode")).default;
const serverPlugin = (await import("kwin-mcp-warden-opencode/server")).default;

for (const [name, plugin] of [
  ["root", rootPlugin],
  ["/server", serverPlugin],
]) {
  assert.equal(typeof plugin, "object", `${name} export must be an object`);
  assert.equal(plugin.id, "kwin-mcp-warden-opencode", `${name} export must have the plugin id`);
  assert.equal(typeof plugin.setup, "function", `${name} export must expose setup`);
}
assert.equal(rootPlugin.id, serverPlugin.id);

function createContext(mcp, skills, { onSkillTransform } = {}) {
  return {
    mcp: {
      transform: async (callback) =>
        callback({
          get: (id) => mcp.get(id),
          set: (id, config) => mcp.set(id, config),
        }),
    },
    skill: {
      transform: async (callback) => {
        onSkillTransform?.();
        return callback({
          get: (id) => skills.get(id),
          add: (skill) => skills.set(skill.id, skill),
        });
      },
    },
  };
}

async function assertRegistration(plugin) {
  const mcp = new Map([["user-server", { type: "local", command: ["user-tool"] }]]);
  const skills = new Map();

  await plugin.setup(createContext(mcp, skills));
  await plugin.setup(createContext(mcp, skills));

  assert.deepEqual(mcp.get("user-server"), { type: "local", command: ["user-tool"] });
  const config = mcp.get("kwin-mcp-warden");
  assert.deepEqual(config, { type: "local", command: ["kwin-mcp-warden"] });
  assert.equal(config.type, "local");
  assert.deepEqual(config.command, ["kwin-mcp-warden"]);
  assert.ok(!config.command.includes("uvx"), "must never launch upstream kwin-mcp through uvx");
  assert.equal("enabled" in config, false);
  assert.deepEqual(Object.keys(config).sort(), ["command", "type"]);
  assert.equal(mcp.size, 2);

  const skill = skills.get("kwin-desktop-automation");
  assert.ok(skill);
  assert.equal(skill.id, "kwin-desktop-automation");
  assert.equal(skill.name, "kwin-desktop-automation");
  assert.ok(skill.path.endsWith("skill/kwin-desktop-automation/SKILL.md"));
  assert.ok(skill.content.includes("Drive Linux KDE Plasma 6 Wayland desktops"));
  assert.equal(skills.size, 1);
}

async function assertSameIdPreservation(plugin) {
  const existingMcp = {
    type: "remote",
    url: "https://example.test/mcp",
    headers: { authorization: "Bearer user-configured" },
  };
  const existingSkill = {
    id: "kwin-desktop-automation",
    name: "user-configured-skill",
    path: "/tmp/user-configured/SKILL.md",
    content: "User-provided skill",
  };
  const mcp = new Map([["kwin-mcp-warden", existingMcp]]);
  const skills = new Map([["kwin-desktop-automation", existingSkill]]);

  await plugin.setup(createContext(mcp, skills));
  await plugin.setup(createContext(mcp, skills));

  assert.deepEqual(mcp.get("kwin-mcp-warden"), existingMcp);
  assert.deepEqual(skills.get("kwin-desktop-automation"), existingSkill);
  assert.equal(mcp.size, 1);
  assert.equal(skills.size, 1);
}

await assertRegistration(rootPlugin);
await assertRegistration(serverPlugin);
await assertSameIdPreservation(rootPlugin);
await assertSameIdPreservation(serverPlugin);

const missingSkillRoot = await mkdtemp(join(root, ".contract-missing-skill-"));
try {
  const missingSkillDist = join(missingSkillRoot, "dist");
  const missingSkillEntry = join(missingSkillDist, "index.js");
  await mkdir(missingSkillDist, { recursive: true });
  await copyFile(join(root, "dist", "index.js"), missingSkillEntry);

  const missingSkillPlugin = (
    await import(`${pathToFileURL(missingSkillEntry).href}?missing-skill`)
  ).default;
  const mcp = new Map();
  const skills = new Map();
  let skillTransformCalls = 0;

  await missingSkillPlugin.setup(
    createContext(mcp, skills, {
      onSkillTransform: () => {
        skillTransformCalls += 1;
        throw new Error("skill transform must not run without a bundled skill");
      },
    }),
  );

  assert.deepEqual(mcp.get("kwin-mcp-warden"), { type: "local", command: ["kwin-mcp-warden"] });
  assert.equal(skillTransformCalls, 0);
  assert.equal(skills.size, 0);
} finally {
  await rm(missingSkillRoot, { recursive: true, force: true });
}

console.log("OpenCode V2 plugin contract checks passed");
