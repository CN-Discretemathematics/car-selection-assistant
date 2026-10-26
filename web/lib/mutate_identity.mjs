// 变异验证：孤儿 token 的修复必须真能抓住缺陷。
// 每个变异点都让对应测试**变红**；绿灯说明那条测试是恒真的。
import { readFileSync, writeFileSync } from "node:fs";
import { execFileSync } from "node:child_process";

const SRC = "lib/agent.ts";
const orig = readFileSync(SRC, "utf8");

function run() {
  try {
    const out = execFileSync(
      "node",
      ["--experimental-strip-types", "--test", "lib/agent.test.mts"],
      { encoding: "utf8", stdio: ["ignore", "pipe", "pipe"] },
    );
    return { pass: true, out };
  } catch (e) {
    return { pass: false, out: String(e.stdout ?? "") + String(e.stderr ?? "") };
  }
}

const mutations = [
  {
    name: "ensureIdentityToken 不再验证有效性（回到「本地有就用」）",
    apply: (t) =>
      t.replace(
        /const known = await identityIsKnown\(existing\);\s*\n\s*if \(known\) return existing;/,
        "return existing; // 变异：不再验证",
      ),
    expectFail: ["ensureIdentityToken：本地 token 已失效时**换新的**"],
  },
  {
    name: "identityIsKnown 永远返回 true（失效也当有效）",
    apply: (t) =>
      t.replace(
        /async function identityIsKnown\(token: string\): Promise<boolean> \{/,
        "async function identityIsKnown(token: string): Promise<boolean> {\n  return true; // 变异",
      ),
    expectFail: [
      "ensureIdentityToken：本地 token 已失效时**换新的**",
      "listPreferences：token 已失效时 identityValid=false",
    ],
  },
  {
    name: "identityIsKnown 永远返回 false（有效也当失效）",
    apply: (t) =>
      t.replace(
        /async function identityIsKnown\(token: string\): Promise<boolean> \{/,
        "async function identityIsKnown(token: string): Promise<boolean> {\n  return false; // 变异",
      ),
    expectFail: [
      "ensureIdentityToken：本地 token 有效时直接复用",
      "listPreferences：token 有效时 identityValid=true",
    ],
  },
  {
    name: "探活时 fetch 抛异常 ⇒ 误判 token 失效（网络故障丢掉好 token）",
    apply: (t) =>
      t.replace(
        /return true; \/\/ 网络故障：不能据此判定失效（否则会把好 token 丢掉）/,
        "return false; // 变异",
      ),
    expectFail: ["ensureIdentityToken：探活请求**抛异常**时保留原 token"],
  },
  {
    name: "listPreferences 不再探身份（identityValid 恒为 true）",
    apply: (t) =>
      t.replace(
        /let identityValid = true;\s*\n\s*if \(identityToken && items\.length === 0\) \{\s*\n\s*identityValid = await identityIsKnown\(identityToken\);\s*\n\s*\}/,
        "let identityValid = true; // 变异：不探",
      ),
    expectFail: ["listPreferences：token 已失效时 identityValid=false"],
  },
  {
    name: "listPreferences 有内容时也探身份（多发请求）",
    apply: (t) =>
      t.replace(
        /if \(identityToken && items\.length === 0\) \{/,
        "if (identityToken) { // 变异",
      ),
    expectFail: ["listPreferences：有内容时不探身份"],
  },
];

let missed = 0;
for (const m of mutations) {
  const mutated = m.apply(orig);
  if (mutated === orig) {
    console.log(`SKIP 变异点 [${m.name}] —— 替换没匹配上（变异脚本本身的问题）`);
    missed++;
    continue;
  }
  writeFileSync(SRC, mutated);
  const r = run();
  // node --test 的失败摘要行形如：
  //   ✖ ensureIdentityToken：本地 token 已失效时**换新的**（不再复用死 token） (1.2ms)
  // 名字里含全角括号 ⇒ 用「行首 ✖ + 去掉尾部 (数字ms)」提取
  const failedNames = [...r.out.matchAll(/^✖ (.+?) \(\d[\d.]*ms\)\s*$/gm)].map((x) => x[1]);
  const hit = m.expectFail.some((n) => failedNames.some((f) => f.includes(n)));
  if (!r.pass && hit) {
    console.log(`OK   变异点 [${m.name}] 被抓到 ✔`);
  } else {
    console.log(`MISS 变异点 [${m.name}] **没被抓到** ✘`);
    console.log("      失败的测试:", [...new Set(failedNames)].join(" | ") || "(无)");
    missed++;
  }
}
writeFileSync(SRC, orig);
const final = run();
console.log(final.pass ? "=== 还原后全绿 ✔" : "=== 还原后仍红 ✘");
console.log(missed === 0 ? "全部变异点均被抓到" : `有 ${missed} 个变异点未被抓到`);
