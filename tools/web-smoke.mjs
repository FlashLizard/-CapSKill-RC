// 启动一个临时 web 子进程并访问 health/tasks 接口，用于迁移后的快速自检。
// 这个脚本不会修改 jobs、skills 或数据目录，结束时会主动回收子进程。
import { spawn } from "node:child_process";
import { fileURLToPath } from "node:url";

const port = String(Number(process.env.SKILLSBENCH_SMOKE_PORT || 5201));
const child = spawn(process.execPath, ["runner-app/server.mjs"], {
  cwd: fileURLToPath(new URL("..", import.meta.url)),
  env: { ...process.env, SKILLSBENCH_RUNNER_PORT: port },
  stdio: ["ignore", "pipe", "pipe"],
});

let stderr = "";
child.stderr.on("data", (chunk) => { stderr += chunk.toString(); });

async function getJson(url) {
  const response = await fetch(url);
  if (!response.ok) throw new Error(`${url}: HTTP ${response.status}`);
  return response.json();
}

try {
  let health;
  for (let attempt = 0; attempt < 30; attempt += 1) {
    try {
      health = await getJson(`http://127.0.0.1:${port}/api/health`);
      break;
    } catch {
      await new Promise((resolve) => setTimeout(resolve, 100));
    }
  }
  if (!health) throw new Error(`web server did not become ready${stderr ? `: ${stderr}` : ""}`);
  const tasks = await getJson(`http://127.0.0.1:${port}/api/tasks`);
  console.log(JSON.stringify({ health, taskCount: tasks.tasks?.length ?? 0, skillLibraryCount: tasks.skillLibraries?.length ?? 0 }, null, 2));
} finally {
  child.kill();
}
