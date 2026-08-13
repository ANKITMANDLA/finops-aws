import type { Finding } from "./types";

/**
 * One recommendation, and every resource it applies to.
 *
 * An auto-scaling group's nodes share a Name tag, so a fleet of five identical workers
 * produces five findings whose titles and figures are indistinguishable. Listing them one
 * per row reads as the same advice repeated, and invites the reader to assume the total
 * counts it repeatedly too. They are one decision, so they belong on one row.
 */
export interface FindingGroup {
  key: string;
  /** The member whose evidence and commands represent the group, preferring a counted one. */
  lead: Finding;
  members: Finding[];
  /** Savings across the group, excluding members that are alternatives to another change. */
  savings: number;
  /** Savings per member, which is what each individual change delivers. */
  each: number;
  /** How many members count towards the total. */
  counted: number;
  /** The regions the members sit in, so near-identical rows stay distinguishable. */
  regions: string[];
}

/**
 * Identical advice with an identical figure, from the same rule.
 *
 * Whether a member counts towards the total is deliberately not part of this: two nodes
 * needing the same change belong together even when one of them has a larger change
 * claiming the money already.
 */
function signature(finding: Finding): string {
  return [finding.rule_id, finding.title, finding.estimated_monthly_savings.toFixed(2)].join("|");
}

export function groupFindings(findings: Finding[]): FindingGroup[] {
  const groups = new Map<string, FindingGroup>();

  for (const finding of findings) {
    const key = signature(finding);
    const counts = !finding.alternative_to;
    const held = groups.get(key);
    if (!held) {
      groups.set(key, {
        key,
        lead: finding,
        members: [finding],
        savings: counts ? finding.estimated_monthly_savings : 0,
        each: finding.estimated_monthly_savings,
        counted: counts ? 1 : 0,
        regions: finding.region ? [finding.region] : [],
      });
      continue;
    }

    held.members.push(finding);
    if (counts) {
      held.savings += finding.estimated_monthly_savings;
      held.counted += 1;
      // A row should read as something to do, so a counted member speaks for the group.
      if (held.lead.alternative_to) held.lead = finding;
    }
    if (finding.region && !held.regions.includes(finding.region)) {
      held.regions.push(finding.region);
    }
  }

  return [...groups.values()];
}

/** What a row covers: one named resource, or a count of them. */
export function groupResources(group: FindingGroup): string {
  if (group.members.length === 1) {
    return group.lead.resource_id ?? group.lead.region ?? "account";
  }
  return `${group.members.length} resources`;
}

/** Where a row's resources sit: one region, or how many. */
export function groupRegion(group: FindingGroup): string {
  if (group.regions.length === 1) return group.regions[0];
  if (group.regions.length === 0) return "account";
  return `${group.regions.length} regions`;
}

/** Both together, for places with room for only one column. */
export function groupScope(group: FindingGroup): string {
  if (group.members.length === 1) return groupResources(group);
  return `${groupResources(group)} · ${groupRegion(group)}`;
}
