import {
  Bot,
  BookOpen,
  CircleHelp,
  Gauge,
  GitBranch,
  GitPullRequest,
  LayoutDashboard,
  LayoutPanelTop,
  ListChecks,
  Puzzle,
  Ruler,
  Settings,
  Share2,
  SquareKanban,
  SquareTerminal,
  type LucideIcon,
} from 'lucide-react'

// Addons name icons as strings in their manifests; only a fixed set is renderable.
const ICONS: Record<string, LucideIcon> = {
  Bot,
  BookOpen,
  Gauge,
  Github: GitBranch,
  GitBranch,
  GitPullRequest,
  LayoutDashboard,
  LayoutPanelTop,
  ListChecks,
  Puzzle,
  Ruler,
  Settings,
  Share2,
  SquareKanban,
  SquareTerminal,
}

export function iconByName(name?: string): LucideIcon {
  return (name && ICONS[name]) || CircleHelp
}
