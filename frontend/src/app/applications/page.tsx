"use client";

import { useCallback, useState } from "react";
import { api } from "@/lib/api";
import { useApi } from "@/lib/useApi";
import { Card, ErrorNote, Loading, PageTitle, StatusBadge, timeAgo } from "@/components/ui";

const TABS = [
  "all",
  "pending_review",
  "approved",
  "awaiting_confirmation",
  "submitted",
  "failed",
  "rejected",
] as const;

export default function ApplicationsPage() {
  const [tab, setTab] = useState<(typeof TABS)[number]>("all");
  const fetchApps = useCallback(() => api.applications(tab === "all" ? undefined : tab), [tab]);
  const { data: apps, error, loading } = useApi(fetchApps);

  return (
    <div className="mx-auto max-w-5xl">
      <PageTitle title="Applications" subtitle="Every application ever prepared or sent." />
      {error ? <ErrorNote error={error} /> : null}

      <div className="mb-4 flex flex-wrap gap-1">
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

      {loading ? <Loading /> : null}
      {apps ? (
        <Card className="overflow-x-auto p-0">
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
                  <td className="px-4 py-2 font-medium">
                    {job?.title ?? `job #${application.job_id}`}
                    {application.notes ? (
                      <p className="mt-0.5 max-w-md truncate text-xs font-normal text-zinc-500">
                        {application.notes}
                      </p>
                    ) : null}
                  </td>
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
                    <Loading label="No applications here." />
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
