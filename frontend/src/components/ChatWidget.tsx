import { useEffect, useRef, useState } from "react";

import { api, ApiError } from "@/lib/api";
import { useApi } from "@/lib/hooks";
import type { ChatMessage, ToolInvocation } from "@/lib/types";
import { useScanContext } from "@/state/ScanContext";

import Markdown from "./Markdown";
import { Badge, Spinner, cx } from "./ui";

/** A turn as the panel needs it: what was said, plus what the model did to say it. */
interface Turn {
  role: "user" | "assistant";
  content: string;
  tools?: ToolInvocation[];
  error?: string | null;
  truncated?: boolean;
}

const SUGGESTIONS = [
  "What are my three biggest cost problems?",
  "Which EC2 instances are underutilized, and what should I resize them to?",
  "Is gp3 cheaper than gp2 for my volumes? Show the arithmetic.",
];

const SOURCE_LABEL: Record<string, string> = {
  finops: "scan",
  aws: "AWS docs",
  pricing: "AWS pricing",
};

interface Size {
  width: number;
  height: number;
}

// The size the panel had before it could be resized, and the smallest it stays usable at:
// narrower than this and a table in an answer is unreadable, shorter and the composer eats
// the conversation.
const DEFAULT_SIZE: Size = { width: 448, height: 608 };
const MIN_SIZE: Size = { width: 320, height: 260 };
const EDGE = 24; // Matches the panel's offset from the corner of the window.
const SIZE_KEY = "finops.chat.size";

/** Hold a size inside what the window can show, and inside what stays usable. */
function fit({ width, height }: Size): Size {
  const room = {
    width: Math.max(MIN_SIZE.width, window.innerWidth - EDGE * 2),
    height: Math.max(MIN_SIZE.height, window.innerHeight - EDGE * 2),
  };
  return {
    width: Math.round(Math.min(Math.max(width, MIN_SIZE.width), room.width)),
    height: Math.round(Math.min(Math.max(height, MIN_SIZE.height), room.height)),
  };
}

function storedSize(): Size {
  try {
    const raw = window.localStorage.getItem(SIZE_KEY);
    const saved = raw ? (JSON.parse(raw) as Partial<Size>) : null;
    if (typeof saved?.width === "number" && typeof saved?.height === "number") {
      return fit({ width: saved.width, height: saved.height });
    }
  } catch {
    // A corrupt entry, or a browser refusing storage, is not worth failing to open over.
  }
  return fit(DEFAULT_SIZE);
}

/**
 * The assistant, docked in the corner of every page.
 *
 * It lives in the layout rather than on a route so a conversation survives moving
 * between Overview, Savings, and Inventory: you can ask about a finding, go look at it,
 * and carry on asking.
 */
export default function ChatWidget() {
  const { scan, scanId, health } = useScanContext();
  const [open, setOpen] = useState(false);
  const [turns, setTurns] = useState<Turn[]>([]);
  const [draft, setDraft] = useState("");
  const [busy, setBusy] = useState(false);
  const [size, setSize] = useState<Size>(storedSize);
  const [resizing, setResizing] = useState(false);
  const bottom = useRef<HTMLDivElement>(null);
  const composer = useRef<HTMLTextAreaElement>(null);
  const from = useRef<{ x: number; y: number; size: Size; axis: "both" | "x" | "y" } | null>(null);

  const capabilities = useApi(() => api.chatCapabilities(), []);
  const noProvider = health?.llm_provider === "none";
  const ready = Boolean(scan?.meta) && !noProvider;

  // A new scan is a different account state, so the conversation no longer applies.
  useEffect(() => {
    setTurns([]);
  }, [scanId]);

  // Braces matter: React 19 calls whatever an effect returns as its cleanup, and a
  // concise arrow body would hand it the return value of the call below.
  useEffect(() => {
    bottom.current?.scrollIntoView({ behavior: "smooth", block: "nearest" });
  }, [turns, busy, open]);

  useEffect(() => {
    if (open) composer.current?.focus();
  }, [open]);

  useEffect(() => {
    if (!open) return;
    function onKey(event: KeyboardEvent) {
      if (event.key === "Escape") setOpen(false);
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open]);

  // A window that shrinks below the chosen size must not push the panel off screen.
  useEffect(() => {
    function onResize() {
      setSize((current) => fit(current));
    }
    window.addEventListener("resize", onResize);
    return () => window.removeEventListener("resize", onResize);
  }, []);

  useEffect(() => {
    try {
      window.localStorage.setItem(SIZE_KEY, JSON.stringify(size));
    } catch {
      // Storage being unavailable costs the preference, nothing more.
    }
  }, [size]);

  function startResize(axis: "both" | "x" | "y") {
    return (event: React.PointerEvent<HTMLDivElement>) => {
      event.preventDefault();
      event.currentTarget.setPointerCapture(event.pointerId);
      from.current = { x: event.clientX, y: event.clientY, size, axis };
      setResizing(true);
    };
  }

  function onResizeMove(event: React.PointerEvent<HTMLDivElement>) {
    const start = from.current;
    if (!start) return;
    // The panel is pinned to the bottom right corner, so dragging up and to the left is what
    // makes it bigger.
    const wider = start.size.width + (start.x - event.clientX);
    const taller = start.size.height + (start.y - event.clientY);
    setSize(
      fit({
        width: start.axis === "y" ? start.size.width : wider,
        height: start.axis === "x" ? start.size.height : taller,
      }),
    );
  }

  function endResize(event: React.PointerEvent<HTMLDivElement>) {
    from.current = null;
    setResizing(false);
    event.currentTarget.releasePointerCapture?.(event.pointerId);
  }

  async function send(question: string) {
    const text = question.trim();
    if (!text || busy || !ready) return;

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
          error:
            cause instanceof ApiError
              ? cause.message
              : "The assistant did not respond.",
        },
      ]);
    } finally {
      setBusy(false);
    }
  }

  if (!open) {
    return (
      <button
        onClick={() => setOpen(true)}
        title="Ask the FinOps assistant"
        aria-label="Ask the FinOps assistant"
        className={cx(
          "fixed right-6 bottom-6 z-40 flex size-12 items-center justify-center rounded-full",
          "bg-accent text-canvas shadow-lg transition-transform hover:scale-105",
        )}
      >
        <ChatIcon />
        {turns.length > 0 && (
          <span className="absolute -top-0.5 -right-0.5 size-3 rounded-full bg-good ring-2 ring-canvas" />
        )}
      </button>
    );
  }

  const sources = [
    "scan",
    ...(capabilities.data?.mcp_enabled
      ? (capabilities.data.servers
          .filter((server) => server.enabled)
          .map((server) => SOURCE_LABEL[server.key] ?? server.key) ?? [])
      : []),
  ];

  return (
    <section
      role="dialog"
      aria-label="FinOps assistant"
      style={{ width: size.width, height: size.height }}
      className={cx(
        "fixed right-6 bottom-6 z-40 flex flex-col overflow-hidden rounded-xl",
        "border border-line/70 bg-surface shadow-2xl",
        resizing && "select-none",
      )}
    >
      {/* Grab the top or left edge, or the corner for both at once. */}
      <div
        onPointerDown={startResize("y")}
        onPointerMove={onResizeMove}
        onPointerUp={endResize}
        className="absolute top-0 right-0 left-4 z-10 h-1.5 cursor-ns-resize touch-none"
        aria-hidden="true"
      />
      <div
        onPointerDown={startResize("x")}
        onPointerMove={onResizeMove}
        onPointerUp={endResize}
        className="absolute top-4 bottom-0 left-0 z-10 w-1.5 cursor-ew-resize touch-none"
        aria-hidden="true"
      />
      <div
        onPointerDown={startResize("both")}
        onPointerMove={onResizeMove}
        onPointerUp={endResize}
        onDoubleClick={() => setSize(fit(DEFAULT_SIZE))}
        title="Drag to resize · double-click to reset"
        className={cx(
          "group absolute top-0 left-0 z-20 flex size-5 cursor-nwse-resize touch-none",
          "items-start justify-start p-1",
        )}
        aria-hidden="true"
      >
        <GripIcon />
      </div>

      <header className="flex items-center gap-2 border-b border-line/60 px-4 py-3">
        <div className="min-w-0">
          <p className="text-sm font-semibold text-ink">Assistant</p>
          <p className="truncate text-xs text-ink-faint">
            {ready ? `Reads your ${sources.join(", ")}` : "Unavailable"}
          </p>
        </div>
        <div className="ml-auto flex items-center gap-1">
          {turns.length > 0 && (
            <button
              onClick={() => setTurns([])}
              disabled={busy}
              className="rounded-md px-2 py-1 text-xs text-ink-faint hover:bg-surface-2 hover:text-ink disabled:opacity-50"
            >
              Clear
            </button>
          )}
          <button
            onClick={() => setOpen(false)}
            aria-label="Close the assistant"
            className="rounded-md px-2 py-1 text-ink-faint hover:bg-surface-2 hover:text-ink"
          >
            ✕
          </button>
        </div>
      </header>

      <div className="min-h-0 flex-1 space-y-3 overflow-y-auto px-4 py-3">
        {!scan?.meta ? (
          <p className="text-sm text-ink-faint">
            Run a scan first — the assistant answers from what the scan found.
          </p>
        ) : noProvider ? (
          <p className="text-sm text-ink-faint">
            No LLM provider is configured. Set{" "}
            <code className="text-ink">FINOPS_LLM_PROVIDER</code> to bedrock,
            anthropic, openai, or gemini in{" "}
            <code className="text-ink">.env</code>.
          </p>
        ) : (
          turns.length === 0 && (
            <div className="space-y-3">
              <p className="text-sm text-ink-muted">
                Ask about this account. I can read the scan's findings,
                inventory, and costs, and check AWS documentation and list
                prices. I never change anything.
              </p>
              <div className="space-y-1.5">
                {SUGGESTIONS.map((suggestion) => (
                  <button
                    key={suggestion}
                    onClick={() => send(suggestion)}
                    className={cx(
                      "block w-full rounded-lg border border-line/70 bg-surface-2/60 px-3 py-2",
                      "text-left text-xs text-ink-muted transition-colors",
                      "hover:border-accent/40 hover:text-ink",
                    )}
                  >
                    {suggestion}
                  </button>
                ))}
              </div>
            </div>
          )
        )}

        {turns.map((turn, index) =>
          turn.role === "user" ? (
            <p
              key={index}
              className="ml-auto max-w-[85%] rounded-xl rounded-br-sm bg-accent/10 px-3 py-2 text-sm text-ink"
            >
              {turn.content}
            </p>
          ) : (
            <div key={index} className="space-y-1.5">
              {turn.tools && turn.tools.length > 0 && (
                <ToolTrace calls={turn.tools} />
              )}
              {turn.error ? (
                <p className="rounded-xl border border-bad/40 bg-bad/5 px-3 py-2 text-sm text-bad">
                  {turn.error}
                </p>
              ) : (
                <div className="rounded-xl border border-line/70 bg-surface-2/40 px-3 py-2">
                  <Markdown>{turn.content}</Markdown>
                  {turn.truncated && (
                    <p className="mt-2 text-xs text-warn">
                      Stopped at the tool call limit, so this may be incomplete.
                    </p>
                  )}
                </div>
              )}
            </div>
          ),
        )}

        {busy && (
          <div className="rounded-xl border border-line/70 bg-surface-2/40 px-3 py-2">
            <Spinner label="Reading the scan and checking AWS…" />
          </div>
        )}
        <div ref={bottom} />
      </div>

      <form
        className="flex items-end gap-2 border-t border-line/60 px-3 py-3"
        onSubmit={(event) => {
          event.preventDefault();
          send(draft);
        }}
      >
        <textarea
          ref={composer}
          value={draft}
          rows={1}
          disabled={!ready}
          placeholder={
            ready
              ? "Ask about cost, a resource, or an AWS service…"
              : "Unavailable"
          }
          onChange={(event) => setDraft(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === "Enter" && !event.shiftKey) {
              event.preventDefault();
              send(draft);
            }
          }}
          className={cx(
            "max-h-32 min-h-[2.25rem] flex-1 resize-none rounded-lg border border-line bg-surface-2",
            "px-3 py-2 text-sm text-ink placeholder:text-ink-faint",
            "focus:border-accent focus:ring-1 focus:ring-accent focus:outline-none",
            "disabled:cursor-not-allowed disabled:opacity-60",
          )}
        />
        <button
          type="submit"
          disabled={busy || !ready || !draft.trim()}
          className={cx(
            "flex size-9 shrink-0 items-center justify-center rounded-lg bg-accent text-canvas",
            "transition-colors hover:bg-accent/85 disabled:cursor-not-allowed disabled:opacity-40",
          )}
          aria-label="Send"
        >
          {busy ? "…" : <SendIcon />}
        </button>
      </form>
    </section>
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
            <Badge
              key={source}
              tone={source === "finops" ? "neutral" : "accent"}
            >
              {SOURCE_LABEL[source] ?? source}
            </Badge>
          ))}
        </span>
      </button>

      {open && (
        <ul className="mt-1.5 space-y-1 border-l border-line/70 pl-3">
          {calls.map((call, index) => (
            <li key={`${call.id}-${index}`}>
              <p className="flex items-center gap-2">
                <code className="font-mono text-[11px] text-ink">
                  {call.name}
                </code>
                <span className="text-ink-faint">{call.duration_ms}ms</span>
                {call.is_error && <Badge tone="bad">failed</Badge>}
              </p>
              {Object.keys(call.arguments).length > 0 && (
                <p
                  className="truncate text-ink-faint"
                  title={JSON.stringify(call.arguments)}
                >
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

function GripIcon() {
  return (
    <svg
      viewBox="0 0 12 12"
      className="size-3 text-ink-faint/70 transition-colors group-hover:text-accent"
      aria-hidden="true"
    >
      <path
        d="M11 1 1 11M6 1 1 6"
        stroke="currentColor"
        strokeWidth="1.5"
        strokeLinecap="round"
      />
    </svg>
  );
}

function ChatIcon() {
  return (
    <svg viewBox="0 0 24 24" fill="none" className="size-5" aria-hidden="true">
      <path
        d="M21 11.5a8.38 8.38 0 0 1-.9 3.8 8.5 8.5 0 0 1-7.6 4.7 8.38 8.38 0 0 1-3.8-.9L3 21l1.9-5.7a8.38 8.38 0 0 1-.9-3.8 8.5 8.5 0 0 1 4.7-7.6 8.38 8.38 0 0 1 3.8-.9h.5a8.48 8.48 0 0 1 8 8v.5Z"
        stroke="currentColor"
        strokeWidth="2"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}

function SendIcon() {
  return (
    <svg viewBox="0 0 24 24" fill="none" className="size-4" aria-hidden="true">
      <path
        d="M4 12h16m0 0-6-6m6 6-6 6"
        stroke="currentColor"
        strokeWidth="2"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}
