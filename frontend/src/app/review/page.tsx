"use client";

import { useCallback, useState } from "react";
import { api, ApiError, describeError, ReviewItem } from "@/lib/api";
import { useApi } from "@/lib/useApi";
import { Button, Card, ErrorNote, Loading, PageTitle, StatusBadge, timeAgo } from "@/components/ui";

export default function ReviewPage() {
  const fetchQueue = useCallback(() => api.reviewQueue(), []);
  const { data: items, error: loadError, loading, reload } = useApi(fetchQueue);
  const [selected, setSelected] = useState<ReviewItem | null>(null);
  const [editedLetter, setEditedLetter] = useState("");
  const [acting, setActing] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const dirty = selected !== null && editedLetter !== selected.cover_letter;

  function selectItem(item: ReviewItem) {
    if (dirty && !window.confirm("Discard your unsaved cover-letter edits?")) return;
    setSelected(item);
    setEditedLetter(item.cover_letter);
    setActionError(null);
  }

  async function act(fn: () => Promise<{ detail: string }>, clearSelection: boolean) {
    setActing(true);
    setActionError(null);
    try {
      const result = await fn();
      setNotice(result.detail);
      if (clearSelection) setSelected(null);
    } catch (e: unknown) {
      if (e instanceof ApiError && e.status === 409) {
        // Changed elsewhere (another tab, the CLI): show the truth, not our copy.
        setActionError(`${e.message} — refreshed the queue.`);
        setSelected(null);
      } else {
        setActionError(describeError(e));
      }
    } finally {
      setActing(false);
      reload();
    }
  }

  function handleSave() {
    if (!selected) return;
    const id = selected.application_id;
    void act(async () => {
      const result = await api.editMaterials(id, { cover_letter: editedLetter });
      setSelected({ ...selected, cover_letter: editedLetter });
      return result;
    }, false);
  }

  function handleApprove() {
    if (!selected) return;
    if (dirty && !window.confirm("Approve without saving your cover-letter edits?")) return;
    void act(() => api.approveApplication(selected.application_id), true);
  }

  function handleReject() {
    if (!selected) return;
    void act(() => api.rejectApplication(selected.application_id), true);
  }

  if (loading) return <Loading />;
  const queue = items ?? [];

  return (
    <div className="mx-auto max-w-6xl">
      <PageTitle
        title="Review"
        subtitle="Approve or reject prepared applications. Approved ones are opened and autofilled by `jobpilot apply`; you submit them yourself."
      />
      {loadError && <ErrorNote error={loadError} />}
      {actionError && <ErrorNote error={actionError} />}
      {notice && !actionError && <p className="mb-4 text-sm text-emerald-400">✓ {notice}</p>}

      <div className="mt-4 grid gap-6 lg:grid-cols-[1fr_1.5fr]">
        <div className="space-y-2">
          <h2 className="text-sm font-medium text-zinc-400">Pending ({queue.length})</h2>
          {queue.length === 0 && !loadError && (
            <p className="py-6 text-center text-sm text-zinc-500">
              No applications pending review.
            </p>
          )}
          {queue.map((item) => (
            <button
              key={item.application_id}
              onClick={() => selectItem(item)}
              className={`w-full rounded-lg border p-3 text-left transition ${
                selected?.application_id === item.application_id
                  ? "border-zinc-500 bg-zinc-800/80"
                  : "border-zinc-800 bg-zinc-900/50 hover:border-zinc-700"
              }`}
            >
              <p className="truncate font-medium text-zinc-200">{item.title}</p>
              <p className="truncate text-sm text-zinc-400">{item.company}</p>
              <div className="mt-1 flex items-center gap-2 text-xs text-zinc-500">
                <StatusBadge status={item.status} />
                <span>{item.source}</span>
                <span>{timeAgo(item.created_at)}</span>
              </div>
            </button>
          ))}
        </div>

        <div>
          {selected ? (
            <Card className="space-y-4">
              <div>
                <h3 className="text-lg font-semibold text-zinc-100">{selected.title}</h3>
                <p className="text-sm text-zinc-400">{selected.company}</p>
                <a
                  href={selected.application_url}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="mt-1 inline-block max-w-full truncate text-xs text-sky-400 hover:underline"
                >
                  {selected.application_url}
                </a>
              </div>

              {selected.notes ? (
                <p className="rounded-lg border border-amber-500/30 bg-amber-500/10 p-2 text-sm text-amber-300">
                  {selected.notes}
                </p>
              ) : null}

              <div>
                <label
                  htmlFor="cover-letter"
                  className="mb-1 block text-xs font-medium uppercase tracking-wide text-zinc-500"
                >
                  Cover letter {dirty ? <span className="text-amber-400">(unsaved)</span> : null}
                </label>
                <textarea
                  id="cover-letter"
                  value={editedLetter}
                  onChange={(e) => setEditedLetter(e.target.value)}
                  rows={10}
                  className="w-full rounded-lg border border-zinc-700 bg-zinc-800 p-3 text-sm text-zinc-200 focus:border-zinc-500 focus:outline-none"
                />
              </div>

              {Object.keys(selected.answers).length > 0 && (
                <div>
                  <h4 className="mb-1 text-xs font-medium uppercase tracking-wide text-zinc-500">
                    Prepared answers
                  </h4>
                  <div className="space-y-2">
                    {Object.entries(selected.answers).map(([q, a]) => (
                      <div key={q} className="rounded-lg bg-zinc-800/60 p-2 text-sm">
                        <p className="font-medium text-zinc-300">{q}</p>
                        <p className="mt-0.5 text-zinc-400">{a}</p>
                      </div>
                    ))}
                  </div>
                </div>
              )}

              <div className="flex items-center gap-2 pt-2">
                <Button onClick={handleApprove} disabled={acting}>
                  Approve
                </Button>
                <Button onClick={handleReject} variant="danger" disabled={acting}>
                  Reject
                </Button>
                {dirty && (
                  <Button onClick={handleSave} variant="ghost" disabled={acting}>
                    Save edits
                  </Button>
                )}
              </div>
            </Card>
          ) : (
            <Card>
              <p className="py-12 text-center text-sm text-zinc-500">
                Select an application from the queue to review.
              </p>
            </Card>
          )}
        </div>
      </div>
    </div>
  );
}
