import { AudioLines, CircleHelp, Clock, GraduationCap, Newspaper, ScrollText, Wand2, type LucideIcon } from "lucide-react";
import type { ArtifactKind, AudioFormat } from "@/lib/api";

/** Display label and icon for every artifact kind. */
export const KIND_META: Record<ArtifactKind, { label: string; icon: LucideIcon }> = {
  audio_overview: { label: "Audio overview", icon: AudioLines },
  summary: { label: "Summary", icon: ScrollText },
  briefing: { label: "Briefing", icon: Newspaper },
  study_guide: { label: "Study guide", icon: GraduationCap },
  faq: { label: "FAQ", icon: CircleHelp },
  timeline: { label: "Timeline", icon: Clock },
  custom: { label: "Custom document", icon: Wand2 },
};

export const FORMAT_LABEL: Record<AudioFormat, string> = {
  deep_dive: "Deep dive",
  brief: "Brief",
  debate: "Debate",
  critique: "Critique",
  solo: "Solo",
};
