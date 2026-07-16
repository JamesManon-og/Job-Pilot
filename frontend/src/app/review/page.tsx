"use client";

import { Loading, PageTitle } from "@/components/ui";

export default function ReviewPage() {
  return (
    <div className="mx-auto max-w-5xl">
      <PageTitle
        title="Review"
        subtitle="Approve or reject prepared applications before they're sent."
      />
      <Loading label="Review queue arrives with the approval workflow (Milestone 11)." />
    </div>
  );
}
