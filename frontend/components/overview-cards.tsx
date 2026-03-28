"use client";

import { useEffect, useState } from "react";

import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { fetchPanelState } from "@/lib/api";
import { PanelState } from "@/lib/types";

export function OverviewCards(): React.JSX.Element {
  const [state, setState] = useState<PanelState | null>(null);

  useEffect(() => {
    void fetchPanelState().then(setState).catch(() => setState(null));
  }, []);

  return (
    <div className="grid gap-5 md:grid-cols-2">
      <OverviewCard
        description={state?.profile.name || "Profile not configured yet."}
        title="Profile Snapshot"
        value={state?.profile.email || "Waiting for e-mail"}
      />
      <OverviewCard
        description={state?.preferences.location || "No location configured yet."}
        title="Search Snapshot"
        value={state?.preferences.keywords.join(", ") || "No keywords yet"}
      />
      <OverviewCard
        description={state?.ai.masked_api_key || "No API key stored yet."}
        title="AI Setup"
        value={state?.ai.model || "o3-mini"}
      />
      <OverviewCard
        description={
          state?.preferences.auto_connect_with_recruiter
            ? "Recruiter auto-connect is enabled."
            : "Disabled by default for the MVP."
        }
        title="Recruiter Connect"
        value={state?.preferences.auto_connect_with_recruiter ? "On" : "Off"}
      />
    </div>
  );
}

function OverviewCard({
  description,
  title,
  value,
}: {
  description: string;
  title: string;
  value: string;
}): React.JSX.Element {
  return (
    <Card className="bg-white/75">
      <CardHeader>
        <CardTitle>{title}</CardTitle>
        <CardDescription>{description}</CardDescription>
      </CardHeader>
      <CardContent>
        <Badge>{value}</Badge>
      </CardContent>
    </Card>
  );
}
