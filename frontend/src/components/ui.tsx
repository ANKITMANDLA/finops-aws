import type { ReactNode } from "react";

import type { Confidence, Effort, Risk } from "@/lib/types";

export function cx(...values: (string | false | null | undefined)[]): string {
  return values.filter(Boolean).join(" ");
}

export function Card({
  title,
  subtitle,
  actions,
  children,
  className,
  bodyClassName,
}: {
  title?: ReactNode;
  subtitle?: ReactNode;
  actions?: ReactNode;
  children: ReactNode;
  className?: string;
  bodyClassName?: string;
}) {
  return (
    <section
      className={cx(
        "rounded-xl border border-line/70 bg-surface/60 shadow-sm backdrop-blur-sm",
        className,
      )}
    >
      {(title || actions) && (
        <header className="flex items-start justify-between gap-4 border-b border-line/60 px-5 py-3.5">
          <div>
            {title && <h2 className="text-sm font-semibold text-ink">{title}</h2>}
            {subtitle && <p className="mt-0.5 text-xs text-ink-faint">{subtitle}</p>}
          </div>
          {actions}
        </header>
      )}
      <div className={cx("p-5", bodyClassName)}>{children}</div>
    </section>
  );
}

const TONES = {
  neutral: "bg-surface-2 text-ink-muted ring-line",
  good: "bg-good/10 text-good ring-good/30",
  warn: "bg-warn/10 text-warn ring-warn/30",
  bad: "bg-bad/10 text-bad ring-bad/30",
  accent: "bg-accent/10 text-accent ring-accent/30",
} as const;

export type Tone = keyof typeof TONES;

export function Badge({
  children,
  tone = "neutral",
  title,
}: {
  children: ReactNode;
  tone?: Tone;
  title?: string;
}) {
  return (
    <span
      title={title}
      className={cx(
        "inline-flex items-center rounded-md px-1.5 py-0.5 text-[11px] font-medium ring-1 ring-inset whitespace-nowrap",
        TONES[tone],
      )}
    >
      {children}
    </span>
  );
}

const EFFORT_TONE: Record<Effort, Tone> = { low: "good", medium: "warn", high: "bad" };
const RISK_TONE: Record<Risk, Tone> = { low: "good", medium: "warn", high: "bad" };
const CONFIDENCE_TONE: Record<Confidence, Tone> = { high: "good", medium: "warn", low: "neutral" };

export function EffortBadge({ effort }: { effort: Effort }) {
  return (
    <Badge tone={EFFORT_TONE[effort]} title="How much work this change takes">
      {effort} effort
    </Badge>
  );
}

export function RiskBadge({ risk }: { risk: Risk }) {
  return (
    <Badge tone={RISK_TONE[risk]} title="Operational risk of making the change">
      {risk} risk
    </Badge>
  );
}

export function ConfidenceBadge({ confidence }: { confidence: Confidence }) {
  return (
    <Badge tone={CONFIDENCE_TONE[confidence]} title="How sure the agent is about this finding">
      {confidence} confidence
    </Badge>
  );
}

export function Button({
  children,
  onClick,
  variant = "secondary",
  disabled,
  type = "button",
  className,
  title,
}: {
  children: ReactNode;
  onClick?: () => void;
  variant?: "primary" | "secondary" | "ghost";
  disabled?: boolean;
  type?: "button" | "submit";
  className?: string;
  title?: string;
}) {
  const styles = {
    primary: "bg-accent text-canvas hover:bg-accent/85 font-semibold",
    secondary: "bg-surface-2 text-ink hover:bg-line border border-line",
    ghost: "text-ink-muted hover:text-ink hover:bg-surface-2",
  }[variant];

  return (
    <button
      type={type}
      title={title}
      onClick={onClick}
      disabled={disabled}
      className={cx(
        "inline-flex items-center gap-2 rounded-lg px-3 py-1.5 text-sm transition-colors",
        "disabled:cursor-not-allowed disabled:opacity-50",
        styles,
        className,
      )}
    >
      {children}
    </button>
  );
}

export function Select({
  value,
  onChange,
  options,
  placeholder,
  className,
}: {
  value: string;
  onChange: (value: string) => void;
  options: { value: string; label: string }[];
  placeholder?: string;
  className?: string;
}) {
  return (
    <select
      value={value}
      onChange={(event) => onChange(event.target.value)}
      className={cx(
        "rounded-lg border border-line bg-surface-2 px-2.5 py-1.5 text-sm text-ink",
        "focus:border-accent focus:ring-1 focus:ring-accent focus:outline-none",
        className,
      )}
    >
      {placeholder && <option value="">{placeholder}</option>}
      {options.map((option) => (
        <option key={option.value} value={option.value}>
          {option.label}
        </option>
      ))}
    </select>
  );
}

export function SearchInput({
  value,
  onChange,
  placeholder = "Search",
  className,
}: {
  value: string;
  onChange: (value: string) => void;
  placeholder?: string;
  className?: string;
}) {
  return (
    <input
      type="search"
      value={value}
      placeholder={placeholder}
      onChange={(event) => onChange(event.target.value)}
      className={cx(
        "rounded-lg border border-line bg-surface-2 px-3 py-1.5 text-sm text-ink placeholder:text-ink-faint",
        "focus:border-accent focus:ring-1 focus:ring-accent focus:outline-none",
        className,
      )}
    />
  );
}

export function Spinner({ label }: { label?: string }) {
  return (
    <div className="flex items-center gap-2 text-sm text-ink-faint">
      <span className="size-3.5 animate-spin rounded-full border-2 border-line border-t-accent" />
      {label}
    </div>
  );
}

export function EmptyState({
  title,
  description,
  action,
}: {
  title: string;
  description?: ReactNode;
  action?: ReactNode;
}) {
  return (
    <div className="flex flex-col items-center justify-center gap-2 rounded-xl border border-dashed border-line px-6 py-14 text-center">
      <p className="text-sm font-medium text-ink">{title}</p>
      {description && <p className="max-w-md text-sm text-ink-faint">{description}</p>}
      {action && <div className="mt-2">{action}</div>}
    </div>
  );
}

export function ErrorState({ message, onRetry }: { message: string; onRetry?: () => void }) {
  return (
    <div className="rounded-xl border border-bad/40 bg-bad/5 px-5 py-4">
      <p className="text-sm font-medium text-bad">Something went wrong</p>
      <p className="mt-1 text-sm text-ink-muted">{message}</p>
      {onRetry && (
        <div className="mt-3">
          <Button onClick={onRetry}>Try again</Button>
        </div>
      )}
    </div>
  );
}

export function Stat({
  label,
  value,
  hint,
  tone = "neutral",
}: {
  label: string;
  value: ReactNode;
  hint?: ReactNode;
  tone?: Tone;
}) {
  const valueTone = {
    neutral: "text-ink",
    good: "text-good",
    warn: "text-warn",
    bad: "text-bad",
    accent: "text-accent",
  }[tone];

  return (
    <div className="rounded-xl border border-line/70 bg-surface/60 px-5 py-4">
      <p className="text-xs font-medium tracking-wide text-ink-faint uppercase">{label}</p>
      <p className={cx("tabular mt-1.5 text-2xl font-semibold", valueTone)}>{value}</p>
      {hint && <p className="mt-1 text-xs text-ink-faint">{hint}</p>}
    </div>
  );
}

export function Table({ children, className }: { children: ReactNode; className?: string }) {
  return (
    <div className="overflow-x-auto">
      <table className={cx("w-full border-collapse text-sm", className)}>{children}</table>
    </div>
  );
}

export function Th({
  children,
  align = "left",
  className,
  onClick,
  sorted,
}: {
  children: ReactNode;
  align?: "left" | "right" | "center";
  className?: string;
  onClick?: () => void;
  sorted?: "asc" | "desc" | null;
}) {
  return (
    <th
      onClick={onClick}
      className={cx(
        "border-b border-line/70 px-3 py-2 text-xs font-medium tracking-wide text-ink-faint uppercase",
        align === "right" && "text-right",
        align === "center" && "text-center",
        align === "left" && "text-left",
        onClick && "cursor-pointer select-none hover:text-ink",
        className,
      )}
    >
      {children}
      {sorted && <span className="ml-1 text-accent">{sorted === "desc" ? "↓" : "↑"}</span>}
    </th>
  );
}

export function Td({
  children,
  align = "left",
  className,
}: {
  children: ReactNode;
  align?: "left" | "right" | "center";
  className?: string;
}) {
  return (
    <td
      className={cx(
        "border-b border-line/40 px-3 py-2 text-ink-muted",
        align === "right" && "tabular text-right",
        align === "center" && "text-center",
        className,
      )}
    >
      {children}
    </td>
  );
}
