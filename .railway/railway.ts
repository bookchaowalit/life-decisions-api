import { defineRailway, project, service } from "railway/iac";

// life-decisions-api: the decisions cell. Variables stay unmanaged (secrets live in Railway only).
// decisions-api sleeps when idle (Railway Serverless) and wakes on the next request;
// decisions-publisher is a cron sweep of the outbox that exits after each run.
// Every service deploys from GitHub main after CI passes (Railway Wait for CI, 2026-10-18).
// Declared here so `railway config apply` keeps the connection instead of removing it.
const github = {
  type: "github" as const,
  repo: "bookchaowalit/life-decisions-api",
  branch: "main",
  checkSuites: true,
};

export default defineRailway(() => {
  const api = service("decisions-api", {
    source: github,
    healthcheck: "/health",
    healthcheckTimeout: 60,
    deploy: { sleepApplication: true },
  });
  const publisher = service("decisions-publisher", {
    source: github,
    startCommand: "decisions-publish",
    deploy: { cronSchedule: "*/15 * * * *", restartPolicyType: "NEVER" },
  });

  return project("life-decisions-api", {
    variables: { managed: false },
    resources: [api, publisher],
  });
});
