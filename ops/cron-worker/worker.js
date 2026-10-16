// GitHub 对公开仓库的 schedule 触发会大幅降频（实测 4–5 小时一次），
// 这里用 Cloudflare Cron Trigger 每 5 分钟调一次 workflow_dispatch，保证监控频率。
// 需要的 secret：GH_TOKEN —— fine-grained PAT，仅限本仓库，权限 Actions: Read and write。
// 部署：cd ops/cron-worker && npx wrangler login && npx wrangler deploy && npx wrangler secret put GH_TOKEN
export default {
  async scheduled(event, env, ctx) {
    const res = await fetch(
      `https://api.github.com/repos/${env.REPO}/actions/workflows/${env.WORKFLOW}/dispatches`,
      {
        method: "POST",
        headers: {
          authorization: `Bearer ${env.GH_TOKEN}`,
          accept: "application/vnd.github+json",
          "x-github-api-version": "2022-11-28",
          "user-agent": "mantle-liquidity-monitor-cron",
        },
        body: JSON.stringify({ ref: env.REF }),
      },
    );
    if (res.status !== 204) {
      throw new Error(`dispatch failed: ${res.status} ${await res.text()}`);   // 在 Worker 日志里可见
    }
  },
};
