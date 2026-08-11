import { useEffect, useRef, useState } from "react";

import Markdown from "@/components/Markdown";
import { Badge, Button, Card, EmptyState, Spinner, cx } from "@/components/ui";
import { api, ApiError } from "@/lib/api";
import { useApi } from "@/lib/hooks";
import type { ChatMessage, ToolInvocation } from "@/lib/types";
import { useScanContext } from "@/state/ScanContext";

/** A turn as the view needs it: what was said, plus what the model did to say it. */
interface Turn {
  role: "user" | "assistant";
  content: string;
  tools?: ToolInvocation[];
  error?: string | null;
  truncated?: boolean;
}

const SUGGESTIONS = [
  "What are my three biggest cost problems, and what would fixing them save?",
  "Which EC2 instances are underutilized, and what should I resize them to?",
  "Is gp3 cheaper than gp2 for my volumes? Show the arithmetic.",
  "What would it cost to run my idle EKS clusters on a single shared cluster instead?",
];

const SOURCE_LABEL: Record<string, string> = {
  finops: "scan",
  aws: "AWS docs",
  pricing: "AWS pricing",
};

export default function Chat() {
  const { scan, scanId, health, loading } = useScanContext();
  const [turns, setTurns] = useState<Turn[]>([]);
  const [draft, setDraft] = useState("");
  const [busy, setBusy] = useState(false);
  const bottom = useRef<HTMLDivElement>(null);

  const capabilities = useApi(() => api.chatCapabilities(), []);

  // A new scan is a different account state, so the conversation no longer applies.
  useEffect(() => {
    setTurns([]);
  }, [scanId]);

  // Braces matter: React 19 calls whatever an effect returns as its cleanup, and a
  // concise arrow body would hand it the return value of the call below.
  useEffect(() => {
    // "nearest" keeps the scroll inside the message list instead of nudging the page.
    bottom.current?.scrollIntoView({ behavior: "smooth", block: "nearest" });
  }, [turns, busy]);

  async function send(question: string) {
    const text = question.trim();
    if (!text || busy) return;

    const history: ChatMessage[] = [
      ...turns.map((turn) => ({ role: turn.role, content: turn.content })),
      { role: "user" as const, content: text },
    ];
    setTurns((current) => [...current, { role: "user", content: text }]);
    setDraft("");
    setBusy(true);
    try {
      const reply = await api.chat(scanId, history);
      setTurns((current) => [
        ...current,
        {
          role: "assistant",
          content: reply.message,
          tools: reply.tool_calls,
          error: reply.error,
          truncated: reply.truncated,
        },
      ]);
    } catch (cause) {
      setTurns((current) => [
        ...current,
        {
          role: "assistant",
          content: "",
          error: cause instanceof ApiError ? cause.message : "The assistant did not respond.",
        },
      ]);
    } finally {
      setBusy(false);
    }
  }

  if (loading && !scan) return <Spinner label="Loading…" />;
  if (!scan?.meta) {
    return <EmptyState title="No scan data" description="Run a scan before asking about it." />;
  }

  const noProvider = health?.llm_provider === "none";

  return (
    <div className="flex h-[calc(100vh-9rem)] gap-5">
      <div className="flex min-w-0 flex-1 flex-col">
        <div className="min-h-0 flex-1 space-y-4 overflow-y-auto pr-1">
          {turns.length === 0 && (
            <Card
              title="Ask about this account"
              subtitle="Grounded in your scan, with AWS documentation and pricing on tap"
            >
              <p className="text-sm text-ink-muted">
                The assistant can read this scan's findings, inventory, and cost breakdown, and
                look up AWS documentation, Well-Architected guidance, and list prices to check
                its answers. It never changes anything in your account.
              </p>
              <div className="mt-4 grid gap-2 sm:grid-cols-2">
                {SUGGESTIONS.map((suggestion) => (
                  <button
                    key={suggestion}
                    onClick={() => send(suggestion)}
                    disabled={noProvider}
                    className={cx(
                      "rounded-lg border border-line/70 bg-surface-2/60 px-3 py-2 text-left text-sm",
                      "text-ink-muted transition-colors hover:border-accent/40 hover:text-ink",
                      "disabled:cursor-not-allowed disabled:opacity-50",
                    )}
                  >
                    {suggestion}
                  </button>
                ))}
              </div>
            </Card>
          )}

          {turns.map((turn, index) =>
            turn.role === "user" ? (
              <div key={index} className="flex justify-end">
                <p className="max-w-2xl rounded-xl rounded-br-sm bg-accent/10 px-4 py-2.5 text-sm text-ink">
                  {turn.content}
                </p>
              </div>
            ) : (
              <div key={index} className="space-y-2">
                {turn.tools && turn.tools.length > 0 && <ToolTrace calls={turn.tools} />}
                {turn.error ? (
                  <div className="rounded-xl border border-bad/40 bg-bad/5 px-4 py-3 text-sm text-bad">
                    {turn.error}
                  </div>
                ) : (
                  <div className="rounded-xl border border-line/70 bg-surface/60 px-4 py-3">
                    <Markdown>{turn.content}</Markdown>
                    {turn.truncated && (
                      <p className="mt-2 text-xs text-warn">
                        Stopped after the tool call limit, so this answer may be incomplete.
                      </p>
                    )}
                  </div>
                )}
              </div>
            ),
          )}

          {busy && (
            <div className="rounded-xl border border-line/70 bg-surface/60 px-4 py-3">
              <Spinner label="Reading the scan and checking AWS…" />
            </div>
          )}
          <div ref={bottom} />
        </div>

        <form
          className="mt-4 flex items-end gap-2"
          onSubmit={(event) => {
            event.preventDefault();
            send(draft);
          }}
        >
          <textarea
            value={draft}
            rows={2}
            disabled={noProvider}
            placeholder={
              noProvider
                ? "Set FINOPS_LLM_PROVIDER in .env to enable the assistant"
                : "Ask about cost, a resource, or an AWS service…"
            }
            onChange={(event) => setDraft(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === "Enter" && !event.shiftKey) {
                event.preventDefault();
                send(draft);
              }
            }}
            className={cx(
              "min-h-[3rem] flex-1 resize-y rounded-lg border border-line bg-surface-2 px-3 py-2",
              "text-sm text-ink placeholder:text-ink-faint",
              "focus:border-accent focus:ring-1 focus:ring-accent focus:outline-none",
              "disabled:cursor-not-allowed disabled:opacity-60",
            )}
          />
          <Button type="submit" variant="primary" disabled={busy || noProvider || !draft.trim()}>
            {busy ? "Thinking…" : "Send"}
          </Button>
          {turns.length > 0 && (
            <Button onClick={() => setTurns([])} disabled={busy}>
              Clear
            </Button>
          )}
        </form>
      </div>

      <aside className="hidden w-64 shrink-0 space-y-3 overflow-y-auto xl:block">
        <Card title="Where answers come from" bodyClassName="space-y-3 p-4">
          <Source
            label="This scan"
            detail={`${scan.meta.resource_count} resources, ${scan.meta.finding_count} findings`}
            connected
          />
          {capabilities.data?.mcp_enabled ? (
            capabilities.data.servers.map((server) => (
              <Source
                key={server.key}
                label={SOURCE_LABEL[server.key] ?? server.key}
                detail={server.description}
                connected={server.enabled}
              />
            ))
          ) : (
            <p className="text-xs text-ink-faint">
              MCP is disabled, so the assistant can only see this scan.
            </p>
          )}
          <p className="border-t border-line/60 pt-3 text-xs text-ink-faint">
            Model: {capabilities.data?.provider ?? health?.llm_provider ?? "unknown"}
          </p>
        </Card>
      </aside>
    </div>
  );
}

function Source({
  label,
  detail,
  connected,
}: {
  label: string;
  detail: string;
  connected: boolean;
}) {
  return (
    <div>
      <p className="flex items-center gap-2 text-xs font-medium text-ink">
        <span className={cx("size-1.5 rounded-full", connected ? "bg-good" : "bg-line")} />
        {label}
      </p>
      <p className="mt-0.5 text-xs text-ink-faint">{detail}</p>
    </div>
  );
}

function ToolTrace({ calls }: { calls: ToolInvocation[] }) {
  const [open, setOpen] = useState(false);

  return (
    <div className="text-xs">
      <button
        onClick={() => setOpen(!open)}
        className="inline-flex items-center gap-1.5 text-ink-faint hover:text-ink"
      >
        <span className="text-[10px]">{open ? "▾" : "▸"}</span>
        Checked {calls.length} source{calls.length === 1 ? "" : "s"}
        <span className="flex gap-1">
          {[...new Set(calls.map((call) => call.source))].map((source) => (
            <Badge key={source} tone={source === "finops" ? "neutral" : "accent"}>
              {SOURCE_LABEL[source] ?? source}
            </Badge>
          ))}
        </span>
      </button>

      {open && (
        <ul className="mt-2 space-y-1.5 border-l border-line/70 pl-3">
          {calls.map((call, index) => (
            <li key={`${call.id}-${index}`}>
              <p className="flex items-center gap-2">
                <code className="font-mono text-[11px] text-ink">{call.name}</code>
                <span className="text-ink-faint">{call.duration_ms}ms</span>
                {call.is_error && <Badge tone="bad">failed</Badge>}
              </p>
              {Object.keys(call.arguments).length > 0 && (
                <p className="truncate text-ink-faint" title={JSON.stringify(call.arguments)}>
                  {JSON.stringify(call.arguments)}
                </p>
              )}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
