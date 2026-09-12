"use client";

import { useCallback } from "react";
import { api } from "@/lib/api";
import { useApi } from "@/lib/useApi";
import { Card, ErrorNote, Loading, PageTitle, StatusBadge, timeAgo } from "@/components/ui";

export default function PlatformsPage() {
  const fetchPlatforms = useCallback(() => api.platforms(), []);
  const { data: platforms, error, loading } = useApi(fetchPlatforms);

  return (
    <div className="mx-auto max-w-4xl">
      <PageTitle
        title="Platforms"
        subtitle="Job boards JobPilot can search and apply on. You log in yourself, once per board; JobPilot reuses that browser session and never sees your password."
      />
      {error ? <ErrorNote error={error} /> : null}
      {loading ? <Loading /> : null}

      <div className="space-y-3">
        {platforms?.map((platform) => (
          <Card
            key={platform.platform}
            className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between"
          >
            <div className="min-w-0">
              <div className="flex flex-wrap items-center gap-2">
                <h3 className="font-medium text-zinc-100">{platform.display_name}</h3>
                <StatusBadge status={platform.session_status} />
                {!platform.enabled ? (
                  <span className="text-xs text-zinc-500">disabled in config.yaml</span>
                ) : null}
              </div>
              {platform.session_detail ? (
                <p className="mt-1 text-sm text-amber-400">{platform.session_detail}</p>
              ) : null}
              <p className="mt-1 text-xs text-zinc-500">
                {platform.needs_account
                  ? platform.has_saved_login
                    ? `Saved login · checked ${timeAgo(platform.last_checked_at)}`
                    : "No saved login yet"
                  : "No account needed"}
              </p>
            </div>
            {platform.login_command && platform.session_status !== "logged_in" ? (
              <code className="w-fit whitespace-nowrap rounded bg-zinc-800 px-2 py-1 text-xs text-zinc-300">
                {platform.login_command}
              </code>
            ) : null}
          </Card>
        ))}
        {platforms?.length === 0 ? <Loading label="No platforms registered." /> : null}
      </div>

      <p className="mt-6 text-xs text-zinc-500">
        Enable a platform by setting <code>platforms.&lt;name&gt;.enabled: true</code> in
        config.yaml. JobPilot never bypasses CAPTCHAs, MFA, or bot checks — if one appears it
        pauses that platform and asks you.
      </p>
    </div>
  );
}
