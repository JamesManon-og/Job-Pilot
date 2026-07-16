"use client";

import { useCallback, useEffect, useState } from "react";
import { api, ReviewItem } from "@/lib/api";
import { Button, Card, ErrorNote, Loading, PageTitle, StatusBadge, timeAgo } from "@/components/ui";

export default function ReviewPage() {
  const [items, setItems] = useState<ReviewItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [selected, setSelected] = useState<ReviewItem | null>(null);
  const [editedLetter, setEditedLetter] = useState("");
  const [acting, setActing] = useState(false);

  const load = useCallback(async () => {
    try {
      setError("");
      const data = await api.reviewQueue();
      setItems(data);
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : "Failed to load review queue");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { load(); }, [load]);

  function selectItem(item: ReviewItem) {
    setSelected(item);
    setEditedLetter(item.cover_letter);
  }

  async function handleSave() {
    if (!selected) return;
    setActing(true);
    try {
      await api.editMaterials(selected.application_id, { cover_letter: editedLetter });
      setSelected({ ...selected, cover_letter: editedLetter });
      setItems((prev) =>
        prev.map((i) =>
          i.application_id === selected.application_id ? { ...i, cover_letter: editedLetter } : i,
        ),
      );
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : "Save failed");
    } finally {
      setActing(false);
    }
  }

  async function handleApprove() {
    if (!selected) return;
    setActing(true);
    try {
      await api.approveApplication(selected.application_id);
      setItems((prev) => prev.filter((i) => i.application_id !== selected.application_id));
      setSelected(null);
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : "Approve failed");
    } finally {
      setActing(false);
    }
  }

  async function handleReject() {
    if (!selected) return;
    setActing(true);
    try {
      await api.rejectApplication(selected.application_id);
      setItems((prev) => prev.filter((i) => i.application_id !== selected.application_id));
      setSelected(null);
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : "Reject failed");
    } finally {
      setActing(false);
    }
  }

  if (loading) return <Loading />;

  return (
    <div className="mx-auto max-w-6xl">
      <PageTitle
        title="Review"
        subtitle="Approve or reject prepared applications before they're sent."
      />
      {error && <ErrorNote error={error} />}

      <div className="mt-4 grid gap-6 lg:grid-cols-[1fr_1.5fr]">
        {/* Queue list */}
        <div className="space-y-2">
          <h2 className="text-sm font-medium text-zinc-400">
            Pending ({items.length})
          </h2>
          {items.length === 0 && (
            <p className="py-6 text-center text-sm text-zinc-500">
              No applications pending review.
            </p>
          )}
          {items.map((item) => (
            <button
              key={item.application_id}
              onClick={() => selectItem(item)}
              className={`w-full rounded-lg border p-3 text-left transition ${
                selected?.application_id === item.application_id
                  ? "border-zinc-500 bg-zinc-800/80"
                  : "border-zinc-800 bg-zinc-900/50 hover:border-zinc-700"
              }`}
            >
              <p className="font-medium text-zinc-200 truncate">{item.title}</p>
              <p className="text-sm text-zinc-400 truncate">{item.company}</p>
              <div className="mt-1 flex items-center gap-2 text-xs text-zinc-500">
                <StatusBadge status={item.status} />
                <span>{timeAgo(item.created_at)}</span>
              </div>
            </button>
          ))}
        </div>

        {/* Detail panel */}
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
                  className="mt-1 inline-block text-xs text-sky-400 hover:underline truncate max-w-full"
                >
                  {selected.application_url}
                </a>
              </div>

              <div>
                <label className="mb-1 block text-xs font-medium uppercase tracking-wide text-zinc-500">
                  Cover Letter
                </label>
                <textarea
                  value={editedLetter}
                  onChange={(e) => setEditedLetter(e.target.value)}
                  rows={10}
                  className="w-full rounded-lg border border-zinc-700 bg-zinc-800 p-3 text-sm text-zinc-200 focus:border-zinc-500 focus:outline-none"
                />
              </div>

              {Object.keys(selected.answers).length > 0 && (
                <div>
                  <h4 className="mb-1 text-xs font-medium uppercase tracking-wide text-zinc-500">
                    Pre-filled Answers
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
                  Approve & Submit
                </Button>
                <Button onClick={handleReject} variant="danger" disabled={acting}>
                  Reject
                </Button>
                {editedLetter !== selected.cover_letter && (
                  <Button onClick={handleSave} variant="ghost" disabled={acting}>
                    Save Edits
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
