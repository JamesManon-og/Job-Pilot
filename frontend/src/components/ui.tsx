"use client";

// Small shared UI primitives for the dashboard.

export function PageTitle({ title, subtitle }: { title: string; subtitle?: string }) {
  return (
    <header className="mb-6">
      <h1 className="text-2xl font-semibold tracking-tight">{title}</h1>
      {subtitle ? <p className="mt-1 text-sm text-zinc-500">{subtitle}</p> : null}
    </header>
  );
}

export function Card({ children, className = "" }: { children: React.ReactNode; className?: string }) {
  return (
    <div className={`rounded-xl border border-zinc-800 bg-zinc-900/50 p-4 ${className}`}>
      {children}
    </div>
  );
}

export function StatCard({ label, value, hint }: { label: string; value: string | number; hint?: string }) {
  return (
    <Card>
      <p className="text-xs uppercase tracking-wide text-zinc-500">{label}</p>
      <p className="mt-1 text-3xl font-semibold tabular-nums">{value}</p>
      {hint ? <p className="mt-1 text-xs text-zinc-500">{hint}</p> : null}
    </Card>
  );
}

const SCORE_COLORS = (score: number) =>
  score >= 80
    ? "bg-emerald-500/15 text-emerald-400 border-emerald-500/30"
    : score >= 60
      ? "bg-amber-500/15 text-amber-400 border-amber-500/30"
      : "bg-zinc-500/15 text-zinc-400 border-zinc-500/30";

export function ScoreBadge({ score }: { score: number }) {
  return (
    <span
      className={`inline-flex items-center rounded-full border px-2 py-0.5 text-xs font-medium tabular-nums ${SCORE_COLORS(score)}`}
    >
      {score}
    </span>
  );
}

const STATUS_COLORS: Record<string, string> = {
  pending_review: "bg-amber-500/15 text-amber-400 border-amber-500/30",
  approved: "bg-sky-500/15 text-sky-400 border-sky-500/30",
  awaiting_confirmation: "bg-violet-500/15 text-violet-300 border-violet-500/30",
  submitted: "bg-emerald-500/15 text-emerald-400 border-emerald-500/30",
  failed: "bg-red-500/15 text-red-400 border-red-500/30",
  rejected: "bg-red-500/15 text-red-400 border-red-500/30",
  skipped: "bg-zinc-500/15 text-zinc-400 border-zinc-500/30",
  discovered: "bg-zinc-500/15 text-zinc-400 border-zinc-500/30",
  matched: "bg-sky-500/15 text-sky-400 border-sky-500/30",
  prepared: "bg-amber-500/15 text-amber-400 border-amber-500/30",
  applied: "bg-emerald-500/15 text-emerald-400 border-emerald-500/30",
  logged_in: "bg-emerald-500/15 text-emerald-400 border-emerald-500/30",
  needs_login: "bg-amber-500/15 text-amber-400 border-amber-500/30",
  blocked: "bg-red-500/15 text-red-400 border-red-500/30",
  not_required: "bg-zinc-500/15 text-zinc-400 border-zinc-500/30",
  unknown: "bg-zinc-500/15 text-zinc-400 border-zinc-500/30",
  completed: "bg-emerald-500/15 text-emerald-400 border-emerald-500/30",
  running: "bg-sky-500/15 text-sky-400 border-sky-500/30",
  strong_apply: "bg-emerald-500/15 text-emerald-400 border-emerald-500/30",
  apply: "bg-sky-500/15 text-sky-400 border-sky-500/30",
  maybe: "bg-amber-500/15 text-amber-400 border-amber-500/30",
  skip: "bg-zinc-500/15 text-zinc-400 border-zinc-500/30",
};

export function StatusBadge({ status }: { status: string }) {
  const color = STATUS_COLORS[status] ?? STATUS_COLORS.skipped;
  return (
    <span className={`inline-flex items-center rounded-full border px-2 py-0.5 text-xs font-medium ${color}`}>
      {status.replaceAll("_", " ")}
    </span>
  );
}

export function Button({
  children,
  onClick,
  disabled,
  variant = "primary",
}: {
  children: React.ReactNode;
  onClick?: () => void;
  disabled?: boolean;
  variant?: "primary" | "ghost" | "danger";
}) {
  const styles = {
    primary: "bg-zinc-100 text-zinc-900 hover:bg-white disabled:bg-zinc-700 disabled:text-zinc-400",
    ghost: "border border-zinc-700 text-zinc-300 hover:bg-zinc-900",
    danger: "bg-red-600/90 text-white hover:bg-red-600 disabled:bg-zinc-700",
  }[variant];
  return (
    <button
      onClick={onClick}
      disabled={disabled}
      className={`rounded-lg px-3 py-1.5 text-sm font-medium transition disabled:cursor-not-allowed ${styles}`}
    >
      {children}
    </button>
  );
}

export function Loading({ label = "Loading…" }: { label?: string }) {
  return <p className="py-8 text-center text-sm text-zinc-500">{label}</p>;
}

export function ErrorNote({ error }: { error: string }) {
  return (
    <div className="rounded-lg border border-red-500/30 bg-red-500/10 p-3 text-sm text-red-300">
      {error}
    </div>
  );
}

export function timeAgo(iso: string | null): string {
  if (!iso) return "—";
  // Backend stores UTC; SQLite returns naive timestamps. Treat offset-less as UTC.
  const normalized = /Z$|[+-]\d{2}:\d{2}$/.test(iso) ? iso : `${iso}Z`;
  const seconds = (Date.now() - new Date(normalized).getTime()) / 1000;
  if (seconds < 60) return "just now";
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`;
  return `${Math.floor(seconds / 86400)}d ago`;
}
