import { useScanContext } from "@/state/ScanContext";

type Link = { label: string; href: string };

const REPO = "https://github.com/ANKITMANDLA/finops-aws";

const SUPPORT: Link[] = [
  { label: "Troubleshooting", href: `${REPO}/blob/main/TROUBLESHOOTING.md` },
  { label: "Documentation", href: `${REPO}#readme` },
  { label: "Report an issue", href: `${REPO}/issues/new` },
];

// Where support requests should go. An empty value is left off the footer rather than
// rendered as a link that goes nowhere.
const SUPPORT_EMAIL: string = "ankit.mandla@gmail.com";
const TEAM_CHAT: Link | null = null;

const CONTACT: Link[] = [
  ...(SUPPORT_EMAIL ? [{ label: SUPPORT_EMAIL, href: `mailto:${SUPPORT_EMAIL}` }] : []),
  ...(TEAM_CHAT ? [TEAM_CHAT] : []),
  { label: "github.com/ANKITMANDLA", href: "https://github.com/ANKITMANDLA" },
];

function LinkGroup({ title, links }: { title: string; links: Link[] }) {
  return (
    <div>
      <p className="mb-1.5 text-[11px] font-semibold tracking-wider text-ink-muted uppercase">
        {title}
      </p>
      <ul className="space-y-1">
        {links.map((link) => (
          <li key={link.href}>
            <a
              href={link.href}
              target="_blank"
              rel="noreferrer"
              className="text-ink-muted transition-colors hover:text-accent"
            >
              {link.label}
            </a>
          </li>
        ))}
      </ul>
    </div>
  );
}

export default function Footer() {
  const { health } = useScanContext();

  return (
    // Right padding clears the chat widget, which is fixed 24px from the viewport edge.
    <footer className="border-t border-line/70 bg-surface/30 px-6 pr-20 pt-4 pb-3 text-xs text-ink-faint">
      <div className="flex flex-wrap items-start gap-x-10 gap-y-4">
        <div>
          <p className="text-[13px] font-semibold text-ink">
            FinOps Agent{health && ` v${health.version}`}
          </p>
          <p className="mt-1 max-w-[46ch] leading-relaxed">
            Read-only analysis of one AWS account. Figures are list-price estimates unless
            labelled as billed cost, and a resource whose rate could not be resolved is reported
            as unpriced rather than as $0.
          </p>
        </div>

        <div className="ml-auto flex gap-9 text-right">
          <LinkGroup title="Support" links={SUPPORT} />
          <LinkGroup title="Contact" links={CONTACT} />
        </div>
      </div>

      <div className="mt-3 border-t border-line/50 pt-2.5">
        <p>© {new Date().getFullYear()} FinOps Agent</p>
      </div>
    </footer>
  );
}
