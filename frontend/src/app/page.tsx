"use client";

import { useCallback, useEffect, useState } from "react";
import { api, describeError, ScrapeRun, Stats } from "@/lib/api";
import {
  Button,
  Card,
  ErrorNote,
  Loading,
  PageTitle,
  StatCard,
  StatusBadge,
  timeAgo,
} from "@/components/ui";

const PIPELINE = ["discovered", "matched", "prepared", "applied", "rejected", "skipped"];

export default function OverviewPage() {
  const [stats, setStats] = useState<Stats | null>(null);
  const [runs, setRuns] = useState<ScrapeRun[]>([]);
  const [error, setError] = useState<string | null>(null);
  // Kept separate from `error`: the 5s poll clears polling errors, and used to
  // wipe a failed action's message before you could read it.
  const [actionError, setActionError] = useState<string | null>(null);
  const [actionNote, setActionNote] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const refresh = useCallback(async () => {
    try {
      const [statsData, runsData] = await Promise.all([api.stats(), api.scrapeRuns()]);
      setStats(statsData);
      setRuns(runsData);
      setError(null);
    } catch (e) {
      setError(describeError(e));
    }
  }, []);

  useEffect(() => {
    const tick = () => void refresh();
    const initial = setTimeout(tick, 0);
    const interval = setInterval(tick, 5000);
    return () => {
      clearTimeout(initial);
      clearInterval(interval);
    };
  }, [refresh]);

  const act = async (fn: () => Promise<{ detail: string }>) => {
    setBusy(true);
    setActionError(null);
    setActionNote(null);
    try {
      const result = await fn();
      setActionNote(result.detail);
      setTimeout(refresh, 1500);
    } catch (e) {
      setActionError(describeError(e));
    } finally {
      setBusy(false);
    }
  };

  const apps = stats?.applications_by_status ?? {};

  return (
    <div className="mx-auto max-w-5xl">
      <div className="flex items-start justify-between">
        <PageTitle title="Overview" subtitle="Your local job pipeline at a glance." />
        <div className="flex gap-2">
          <Button onClick={() => act(api.triggerScrape)} disabled={busy}>
            Search now
          </Button>
          <Button variant="ghost" onClick={() => act(api.triggerMatch)} disabled={busy}>
            Match now
          </Button>
        </div>
      </div>

      {stats?.paused ? (
        <div className="mb-4 rounded-lg border border-amber-500/30 bg-amber-500/10 p-3 text-sm text-amber-300">
          JobPilot is paused — searches stop and nothing new starts. Run{" "}
          <code>jobpilot unpause</code> to continue.
        </div>
      ) : null}
      {error ? <ErrorNote error={error} /> : null}
      {actionError ? <ErrorNote error={actionError} /> : null}
      {actionNote ? <p className="mb-4 text-sm text-emerald-400">✓ {actionNote}</p> : null}

      {!stats && !error ? <Loading /> : null}

      {stats ? (
        <>
          <div className="grid grid-cols-2 gap-4 md:grid-cols-4">
            <StatCard label="Jobs scraped" value={stats.total_jobs} />
            <StatCard label="Jobs matched" value={stats.total_matched} hint="for the active resume" />
            <StatCard label="Strong matches" value={stats.strong_matches} hint="LLM score ≥ 80" />
            <StatCard
              label="Applications"
              value={Object.values(apps).reduce((a, b) => a + b, 0)}
              hint={`${apps.pending_review ?? 0} to review · ${apps.approved ?? 0} approved · ${apps.submitted ?? 0} submitted`}
            />
          </div>

          <h2 className="mb-3 mt-10 text-sm font-semibold uppercase tracking-wide text-zinc-500">
            Pipeline
          </h2>
          <div className="grid grid-cols-3 gap-4 md:grid-cols-6">
            {PIPELINE.map((status) => (
              <StatCard
                key={status}
                label={status}
                value={stats.jobs_by_status?.[status] ?? 0}
              />
            ))}
          </div>

          <p className="mt-4 text-sm text-zinc-500">
            Today: {stats.applications_today}/{stats.daily_cap} applications submitted or open
            for submission.
          </p>

          <h2 className="mb-3 mt-10 text-sm font-semibold uppercase tracking-wide text-zinc-500">
            Recent searches
          </h2>
          <Card className="p-0">
            {runs.length === 0 ? (
              <Loading label="No searches yet — hit “Search now”." />
            ) : (
              <table className="w-full text-sm">
                <thead>
                  <tr className="border-b border-zinc-800 text-left text-xs uppercase tracking-wide text-zinc-500">
                    <th className="px-4 py-2">Platform</th>
                    <th className="px-4 py-2">Status</th>
                    <th className="px-4 py-2 text-right">Found</th>
                    <th className="px-4 py-2 text-right">New</th>
                    <th className="px-4 py-2">When</th>
                    <th className="px-4 py-2">Error</th>
                  </tr>
                </thead>
                <tbody>
                  {runs.map((run) => (
                    <tr key={run.id} className="border-b border-zinc-800/50 last:border-0">
                      <td className="px-4 py-2 font-medium">{run.source}</td>
                      <td className="px-4 py-2">
                        <StatusBadge status={run.status} />
                      </td>
                      <td className="px-4 py-2 text-right tabular-nums">{run.jobs_found}</td>
                      <td className="px-4 py-2 text-right tabular-nums">{run.jobs_new}</td>
                      <td className="px-4 py-2 text-zinc-500">{timeAgo(run.started_at)}</td>
                      <td className="max-w-48 truncate px-4 py-2 text-red-400">
                        {run.error ?? ""}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </Card>
        </>
      ) : null}
    </div>
  );
}
