import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { api } from "@/api/client";
import { can } from "@/api/permissions";
import { DecisionRow } from "@/app/pages/today/rows";
import { useRole } from "@/app/useRole";
import { useWorkspace } from "@/app/workspace";
import { useAddons } from "./slots";

/**
 * A `decision` node: one open decision of this addon, drawn and signed by core in place (the same row as on Today).
 * The addon only names the decision's id; the question, options and facts come from core's list, which is also what
 * the host checks. A decision that is no longer open renders nothing.
 */
export default function DecisionNode({
  addon,
  id,
}: {
  addon: string;
  id: string;
}) {
  const { workspace } = useWorkspace();
  const role = useRole();
  const { data: packages } = useAddons();
  const q = useQuery({
    queryKey: ["addon-decisions", workspace?.id],
    queryFn: () => api.getAddonDecisions(workspace!.id),
    enabled: !!workspace,
  });
  const [open, setOpen] = useState(true);
  const d = q.data?.find((x) => x.addon === addon && x.id === id);
  if (!d) return null;
  const canDecide = can(role, "addon.decide");
  const owners =
    workspace?.members.filter((m) => m.role === "owner").map((m) => m.name) ??
    [];
  return (
    <ul className="overflow-hidden rounded-md border border-border">
      <DecisionRow
        d={d}
        readOnly={!canDecide}
        addonTitle={packages?.find((p) => p.name === addon)?.title ?? addon}
        expanded={open}
        onToggle={() => setOpen((o) => !o)}
        decider={!canDecide ? owners.join(", ") || "The owner" : undefined}
      />
    </ul>
  );
}
