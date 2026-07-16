// Typed client for the JobPilot FastAPI backend.

export const API_BASE = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

export interface Job {
  id: number;
  title: string;
  company: string;
  location: string | null;
  salary_raw: string | null;
  salary_min: number | null;
  salary_max: number | null;
  employment_type: string;
  experience_level: string;
  remote: string;
  description: string;
  technologies: string[];
  application_url: string;
  source: string;
  date_posted: string | null;
  scraped_at: string;
}

export interface MatchResult {
  id: number;
  job_id: number;
  resume_id: number;
  score: number;
  matched_skills: string[];
  missing_skills: string[];
  recommendation: string;
  reasoning: string;
  llm_model: string;
}

export interface RankedMatch {
  job: Job;
  match: MatchResult;
  composite_score: number;
}

export interface Application {
  id: number;
  job_id: number;
  resume_id: number;
  status: string;
  cover_letter: string;
  answers: Record<string, string>;
  screenshot_path: string | null;
  notes: string;
  submitted_at: string | null;
  created_at: string;
}

export interface ApplicationWithJob {
  application: Application;
  job: Job | null;
}

export interface ScrapeRun {
  id: number;
  source: string;
  status: string;
  jobs_found: number;
  jobs_new: number;
  error: string | null;
  started_at: string;
  finished_at: string | null;
}

export interface Stats {
  total_jobs: number;
  total_matched: number;
  strong_matches: number;
  applications_by_status: Record<string, number>;
  resumes: number;
  last_scrape: string | null;
  scrape_failures_recent: number;
}

export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
  ) {
    super(message);
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, {
    ...init,
    headers: { "Content-Type": "application/json", ...init?.headers },
  });
  if (!response.ok) {
    let detail = response.statusText;
    try {
      detail = (await response.json()).detail ?? detail;
    } catch {
      // non-JSON error body; keep statusText
    }
    throw new ApiError(response.status, detail);
  }
  return response.json() as Promise<T>;
}

export interface ReviewItem {
  application_id: number;
  job_id: number;
  title: string;
  company: string;
  application_url: string;
  status: string;
  cover_letter: string;
  answers: Record<string, string>;
  notes: string;
  created_at: string;
}

export const api = {
  stats: () => request<Stats>("/api/stats"),
  jobs: (params: { search?: string; source?: string; limit?: number } = {}) => {
    const query = new URLSearchParams();
    if (params.search) query.set("search", params.search);
    if (params.source) query.set("source", params.source);
    query.set("limit", String(params.limit ?? 50));
    return request<Job[]>(`/api/jobs?${query}`);
  },
  job: (id: number) => request<Job>(`/api/jobs/${id}`),
  matches: (minScore = 0, top = 50) =>
    request<RankedMatch[]>(`/api/matches?min_score=${minScore}&top=${top}`),
  applications: (status?: string) =>
    request<ApplicationWithJob[]>(
      status ? `/api/applications?status=${status}` : "/api/applications",
    ),
  scrapeRuns: () => request<ScrapeRun[]>("/api/scrape-runs"),
  triggerScrape: () => request<{ status: string; detail: string }>("/api/actions/scrape", { method: "POST" }),
  triggerMatch: () => request<{ status: string; detail: string }>("/api/actions/match", { method: "POST" }),
  reviewQueue: (status = "pending_review") =>
    request<ReviewItem[]>(`/api/review?status=${status}`),
  approveApplication: (id: number) =>
    request<{ status: string; detail: string }>(`/api/review/${id}/approve`, { method: "POST" }),
  rejectApplication: (id: number) =>
    request<{ status: string; detail: string }>(`/api/review/${id}/reject`, { method: "POST" }),
  editMaterials: (id: number, data: { cover_letter?: string; answers?: Record<string, string> }) =>
    request<{ status: string; detail: string }>(`/api/review/${id}`, {
      method: "PATCH",
      body: JSON.stringify(data),
    }),
};
