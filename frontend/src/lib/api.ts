import type {
  Advice,
  ChatCapabilities,
  ChatMessage,
  ChatReply,
  Comparison,
  FilterOptions,
  Finding,
  Health,
  Page,
  Resource,
  ScanDetail,
  ScanJob,
  ScanMeta,
  TcoReport,
} from "./types";

export class ApiError extends Error {
  constructor(
    readonly status: number,
    message: string,
  ) {
    super(message);
    this.name = "ApiError";
  }

  /** A 404 usually means "no scan yet", which is a normal first-run state. */
  get isMissing() {
    return this.status === 404;
  }
}

type Params = Record<string, string | number | boolean | null | undefined>;

function query(params?: Params): string {
  if (!params) return "";
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value !== undefined && value !== null && value !== "") {
      search.set(key, String(value));
    }
  }
  const encoded = search.toString();
  return encoded ? `?${encoded}` : "";
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let response: Response;
  try {
    response = await fetch(`/api${path}`, {
      headers: { "content-type": "application/json" },
      ...init,
    });
  } catch {
    throw new ApiError(0, "Cannot reach the FinOps API. Is `finops serve` running?");
  }

  if (!response.ok) {
    let detail = response.statusText;
    try {
      const body = await response.json();
      detail = typeof body.detail === "string" ? body.detail : detail;
    } catch {
      // Keep the status text.
    }
    throw new ApiError(response.status, detail);
  }
  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

export const api = {
  health: () => request<Health>("/health"),

  listScans: (limit = 50) => request<ScanMeta[]>(`/scans${query({ limit })}`),
  scan: (scanId: string) => request<ScanDetail>(`/scans/${scanId}`),
  deleteScan: (scanId: string) => request<void>(`/scans/${scanId}`, { method: "DELETE" }),

  startScan: (body?: Record<string, unknown>) =>
    request<ScanJob>("/scans", { method: "POST", body: JSON.stringify(body ?? {}) }),
  scanStatus: () => request<ScanJob>("/scans/status"),

  tco: (scanId: string) => request<TcoReport>(`/scans/${scanId}/tco`),
  notes: (scanId: string) => request(`/scans/${scanId}/notes`),
  filters: (scanId: string) => request<FilterOptions>(`/scans/${scanId}/filters`),

  resources: (scanId: string, params?: Params) =>
    request<Page<Resource>>(`/scans/${scanId}/resources${query(params)}`),
  resource: (scanId: string, arn: string) =>
    request<Resource>(`/scans/${scanId}/resource${query({ arn })}`),

  findings: (scanId: string, params?: Params) =>
    request<Page<Finding>>(`/scans/${scanId}/findings${query(params)}`),

  advice: (scanId: string) => request<Advice>(`/scans/${scanId}/advice`),
  generateAdvice: (scanId: string) =>
    request<Advice>(`/scans/${scanId}/advice`, { method: "POST" }),

  chatCapabilities: () => request<ChatCapabilities>("/chat/capabilities"),
  chat: (scanId: string, messages: ChatMessage[]) =>
    request<ChatReply>(`/scans/${scanId}/chat`, {
      method: "POST",
      body: JSON.stringify({ messages }),
    }),

  trends: (limit = 30) => request<ScanMeta[]>(`/trends${query({ limit })}`),
  compare: (scanId: string, against = "previous") =>
    request<Comparison>(`/scans/${scanId}/compare${query({ against })}`),
};
