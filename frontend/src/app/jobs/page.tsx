"use client";

import { useCallback, useEffect, useState } from "react";
import { api, Job } from "@/lib/api";
import { useApi } from "@/lib/useApi";
import { Card, ErrorNote, Loading, PageTitle, StatusBadge, timeAgo } from "@/components/ui";

const STATUSES = ["all", "discovered", "matched", "prepared", "applied", "skipped"] as const;

export default function JobsPage() {
  const [search, setSearch] = useState("");
  const [query, setQuery] = useState("");
  useEffect(() => {
    const timer = setTimeout(() => setQuery(search.trim()), 250);
    return () => clearTimeout(timer);
  }, [search]);
  const [status, setStatus] = useState<(typeof STATUSES)[number]>("all");
  const fetchJobs = useCallback(
    () =>
      api.jobs({
        search: query || undefined,
        status: status === "all" ? undefined : status,
        limit: 100,
      }),
    [query, status],
  );
  const { data: jobs, error, loading } = useApi(fetchJobs);

  return (
    <div className="mx-auto max-w-6xl">
      <PageTitle title="Jobs" subtitle="Everything scraped so far." />
      {error ? <ErrorNote error={error} /> : null}

      <input
        value={search}
        onChange={(e) => setSearch(e.target.value)}
        placeholder="Search title, company, description…"
        className="mb-4 w-full max-w-md rounded-lg border border-zinc-800 bg-zinc-900 px-3 py-2 text-sm outline-none placeholder:text-zinc-600 focus:border-zinc-600"
      />

      <div className="mb-4 flex flex-wrap gap-1">
        {STATUSES.map((option) => (
          <button
            key={option}
            onClick={() => setStatus(option)}
            className={`rounded-lg px-3 py-1.5 text-sm transition ${
              status === option ? "bg-zinc-100 text-zinc-900" : "text-zinc-400 hover:bg-zinc-900"
            }`}
          >
            {option}
          </button>
        ))}
      </div>

      {loading ? <Loading /> : null}
      {jobs ? (
        <Card className="overflow-x-auto p-0">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-zinc-800 text-left text-xs uppercase tracking-wide text-zinc-500">
                <th className="px-4 py-2">Title</th>
                <th className="px-4 py-2">Company</th>
                <th className="px-4 py-2">Platform</th>
                <th className="px-4 py-2">Status</th>
                <th className="px-4 py-2">Tech</th>
                <th className="px-4 py-2">Salary</th>
                <th className="px-4 py-2">Posted</th>
              </tr>
            </thead>
            <tbody>
              {jobs.map((job) => (
                <tr key={job.id} className="border-b border-zinc-800/50 last:border-0">
                  <td className="max-w-72 px-4 py-2">
                    <a
                      href={job.application_url}
                      target="_blank"
                      rel="noreferrer"
                      className="font-medium text-zinc-100 hover:underline"
                    >
                      {job.title}
                    </a>
                  </td>
                  <td className="px-4 py-2 text-zinc-400">{job.company}</td>
                  <td className="px-4 py-2 text-zinc-500">{job.source}</td>
                  <td className="px-4 py-2">
                    <StatusBadge status={job.status} />
                    {job.status_reason ? (
                      <p className="mt-0.5 max-w-48 truncate text-xs text-zinc-600">
                        {job.status_reason}
                      </p>
                    ) : null}
                  </td>
                  <td className="max-w-56 px-4 py-2">
                    <div className="flex flex-wrap gap-1">
                      {job.technologies.slice(0, 4).map((tech) => (
                        <span
                          key={tech}
                          className="rounded bg-zinc-800 px-1.5 py-0.5 text-xs text-zinc-400"
                        >
                          {tech}
                        </span>
                      ))}
                      {job.technologies.length > 4 ? (
                        <span className="text-xs text-zinc-600">
                          +{job.technologies.length - 4}
                        </span>
                      ) : null}
                    </div>
                  </td>
                  <td className="px-4 py-2 text-zinc-400">{formatSalary(job)}</td>
                  <td className="px-4 py-2 text-zinc-500">{timeAgo(job.date_posted)}</td>
                </tr>
              ))}
              {jobs.length === 0 ? (
                <tr>
                  <td colSpan={7}>
                    <Loading label="No jobs found." />
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

function formatSalary(job: Job): string {
  const k = (n: number) => `$${Math.round(n / 1000)}k`;
  if (job.salary_min && job.salary_max) return `${k(job.salary_min)}–${k(job.salary_max)}`;
  if (job.salary_min) return `from ${k(job.salary_min)}`;
  if (job.salary_max) return `up to ${k(job.salary_max)}`;
  return job.salary_raw ?? "—";
}
