"use client";

import { useCallback, useState } from "react";
import { api } from "@/lib/api";
import { useApi } from "@/lib/useApi";
import {
  Card,
  ErrorNote,
  Loading,
  PageTitle,
  ScoreBadge,
  StatusBadge,
} from "@/components/ui";

export default function MatchesPage() {
  const [minScore, setMinScore] = useState(0);
  const fetchMatches = useCallback(() => api.matches(minScore, 100), [minScore]);
  const { data: matches, error: rawError, loading } = useApi(fetchMatches);
  const error =
    rawError && rawError.includes("No active resume")
      ? "No active resume yet — run: jobpilot resume import <your.pdf>"
      : rawError;

  return (
    <div className="mx-auto max-w-5xl">
      <PageTitle title="Matches" subtitle="Ranked by LLM score + your preferences." />
      {error ? <ErrorNote error={error} /> : null}

      <div className="mb-4 flex items-center gap-3 text-sm text-zinc-400">
        <label htmlFor="min-score">Min LLM score</label>
        <input
          id="min-score"
          type="range"
          min={0}
          max={100}
          step={5}
          value={minScore}
          onChange={(e) => setMinScore(Number(e.target.value))}
        />
        <span className="tabular-nums">{minScore}</span>
      </div>

      {loading ? <Loading /> : null}
      <div className="space-y-3">
        {matches?.map(({ job, match, composite_score }) => (
          <Card key={match.id}>
            <div className="flex items-start justify-between gap-4">
              <div className="min-w-0">
                <div className="flex items-center gap-2">
                  <a
                    href={job.application_url}
                    target="_blank"
                    rel="noreferrer"
                    className="truncate font-medium hover:underline"
                  >
                    {job.title}
                  </a>
                  <StatusBadge status={match.recommendation} />
                </div>
                <p className="mt-0.5 text-sm text-zinc-500">
                  {job.company}
                  {job.location ? ` · ${job.location}` : ""}
                </p>
              </div>
              <div className="flex shrink-0 items-center gap-3">
                <div className="text-right">
                  <p className="text-xs text-zinc-500">LLM</p>
                  <ScoreBadge score={match.score} />
                </div>
                <div className="text-right">
                  <p className="text-xs text-zinc-500">Composite</p>
                  <p className="font-semibold tabular-nums">
                    {(composite_score * 100).toFixed(0)}
                  </p>
                </div>
              </div>
            </div>

            {match.reasoning ? (
              <p className="mt-2 text-sm text-zinc-400">{match.reasoning}</p>
            ) : null}

            <div className="mt-3 flex flex-wrap gap-x-6 gap-y-1 text-xs">
              {match.matched_skills.length ? (
                <p className="text-emerald-400">
                  ✓ {match.matched_skills.slice(0, 8).join(", ")}
                </p>
              ) : null}
              {match.missing_skills.length ? (
                <p className="text-red-400">
                  ✗ missing: {match.missing_skills.slice(0, 5).join(", ")}
                </p>
              ) : null}
            </div>
          </Card>
        ))}
        {matches?.length === 0 ? (
          <Loading label="No matches at this score — scrape and run matching first." />
        ) : null}
      </div>
    </div>
  );
}
