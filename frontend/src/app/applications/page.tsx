"use client";

import { useEffect, useState } from "react";
import { api, ApiError, ApplicationWithJob } from "@/lib/api";
import {
  Card,
  ErrorNote,
  Loading,
  PageTitle,
  StatusBadge,
  timeAgo,
} from "@/components/ui";

const TABS = ["all", "pending_review", "approved", "submitted", "failed", "rejected"] as const;

export default function ApplicationsPage() {
  const [apps, setApps] = useState<ApplicationWithJob[] | null>(null);
  const [tab, setTab] = useState<(typeof TABS)[number]>("all");
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    (async () => {
      try {
        setApps(await api.applications(tab === "all" ? undefined : tab));
        setError(null);
      } catch (e) {
        setError(e instanceof ApiError ? e.message : "Backend unreachable");
      }
    })();
  }, [tab]);

  return (
    <div className="mx-auto max-w-5xl">
      <PageTitle title="Applications" subtitle="Every application ever prepared or sent." />
      {error ? <ErrorNote error={error} /> : null}

      <div className="mb-4 flex gap-1">
        {TABS.map((t) => (
          <button
            key={t}
            onClick={() => setTab(t)}
            className={`rounded-lg px-3 py-1.5 text-sm transition ${
              tab === t ? "bg-zinc-100 text-zinc-900" : "text-zinc-400 hover:bg-zinc-900"
            }`}
          >
            {t.replaceAll("_", " ")}
          </button>
        ))}
      </div>

      {!apps && !error ? <Loading /> : null}
      {apps ? (
        <Card className="p-0">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-zinc-800 text-left text-xs uppercase tracking-wide text-zinc-500">
                <th className="px-4 py-2">Position</th>
                <th className="px-4 py-2">Company</th>
                <th className="px-4 py-2">Status</th>
                <th className="px-4 py-2">Created</th>
                <th className="px-4 py-2">Submitted</th>
              </tr>
            </thead>
            <tbody>
              {apps.map(({ application, job }) => (
                <tr key={application.id} className="border-b border-zinc-800/50 last:border-0">
                  <td className="px-4 py-2 font-medium">{job?.title ?? `job #${application.job_id}`}</td>
                  <td className="px-4 py-2 text-zinc-400">{job?.company ?? "—"}</td>
                  <td className="px-4 py-2">
                    <StatusBadge status={application.status} />
                  </td>
                  <td className="px-4 py-2 text-zinc-500">{timeAgo(application.created_at)}</td>
                  <td className="px-4 py-2 text-zinc-500">{timeAgo(application.submitted_at)}</td>
                </tr>
              ))}
              {apps.length === 0 ? (
                <tr>
                  <td colSpan={5}>
                    <Loading label="No applications yet." />
                  </td>
                </tr>
              ) : null}
            </tbody>
          </table>
        </Card>
      ) : null}
    </div>
  );
}
